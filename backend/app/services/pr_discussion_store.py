"""Task-local discussion journal; durable responses are replayed without resending."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from app.models.pr_discussion import CONTRACT
from app.services.artifact_store import LocalArtifactStore
from app.services.fingerprints import stable_hash
from app.services.review_run_budget import (
    RunBudgetError, activate_run_budget, run_budget_attempt_observer, run_budget_dispatch_guard,
    run_budget_response_observer, run_budget_task_id,
)


class DiscussionStore:
    def __init__(self, task_id, identity, *, repository=None, lease=None, artifacts=None):
        self.task_id, self.repository, self.lease = task_id, repository, lease
        self.artifacts = artifacts or (repository._artifacts if repository else LocalArtifactStore())
        self.data = {"task_id": task_id, "version": CONTRACT, "revision": -1, "identity": identity,
                     "artifacts": [], "calls": {}, "used": {}, "rechecks": {}, "sessions": {}}
        self.lock = asyncio.Lock()
        self.invalidated = False

    async def load(self):
        if self.repository:
            existing = await asyncio.to_thread(self.repository.load_discussion_runtime, self.task_id)
            if existing:
                if existing.get("identity") != self.data["identity"] or existing.get("version") != CONTRACT:
                    raise ValueError("discussion frozen identity mismatch")
                self.data = existing
                for ref in self.data.get("artifacts", []):
                    await self.read(ref)
        return self

    async def put(self, kind, value):
        ref = asdict(await asyncio.to_thread(self.artifacts.put_text, task_id=self.task_id,
            kind="pr-discussion-" + kind, content=json.dumps(value, ensure_ascii=False, sort_keys=True)))
        if ref not in self.data["artifacts"]:
            self.data["artifacts"].append(ref)
        return ref

    async def read(self, ref):
        if not ref["uri"].startswith((self.artifacts.root / self.task_id).as_uri() + "/"):
            raise ValueError("discussion artifact outside task")
        content = await asyncio.to_thread(self.artifacts.read_text, ref["uri"])
        if (hashlib.sha256(content.encode("utf-8")).hexdigest() != ref["sha256"]
                or len(content.encode("utf-8")) != ref["size_bytes"]):
            raise ValueError("discussion artifact invalid")
        return json.loads(content)

    async def save(self):
        if self.invalidated:
            raise ValueError("discussion runtime requires reload")
        before = self.data["revision"]
        value = deepcopy(self.data)
        value["revision"] = before + 1
        try:
            if self.repository:
                await asyncio.to_thread(self.repository.save_discussion_runtime,
                                         self.task_id, value, before, self.lease)
        except BaseException:
            self.invalidated = True
            raise
        self.data = value

    async def used(self, discussion_id, comment_ids):
        async with self.lock:
            existing = set(self.data["used"].get(discussion_id, []))
            self.data["used"][discussion_id] = sorted(existing | set(comment_ids))
            await self.save()

    async def call(self, key, request, provider, budget, *, token_cap=None, call_cap=None):
        """One durable logical operation. An unknown sent attempt is never retried here."""
        known = self.data["calls"].get(key)
        request_hash = stable_hash(request.envelope())
        if known:
            for attempt in budget.attempts.values():
                if attempt.task_id == "discussion:" + key and attempt.attempt_id not in known["attempts"]:
                    known["attempts"].append(attempt.attempt_id)
            if known["request_hash"] != request_hash:
                raise ValueError("discussion request changed")
            if known.get("response"):
                response = await self.read(known["response"])
                from app.models.review import ModelUsage
                usage = ModelUsage.model_validate(response["usage"])
                for identity in known.get("attempts", [])[-1:]:
                    attempt = budget.attempts.get(identity)
                    if attempt and not attempt.settled:
                        await budget.settle(identity, actual_tokens=usage.actual_total_tokens,
                                            cost_microusd=usage.cost_microusd)
                return response
            for attempt_id in known.get("attempts", []):
                attempt = budget.attempts.get(attempt_id)
                if attempt is None:
                    return None
                if attempt.sent:
                    if not attempt.settled:
                        await budget.settle(attempt_id, actual_tokens=None)
                    return None
                if not attempt.settled:
                    await budget.release_unsent(attempt_id, reason="discussion_recovered_before_dispatch")
        record = {"request_hash": request_hash, "attempts": [], "response": None}
        self.data["calls"][key] = record
        await self.save()

        async def bind(attempt, admission):
            if call_cap is not None:
                prior = sum(len(v["attempts"]) for k, v in self.data["calls"].items() if k.startswith("summary:"))
                if prior >= call_cap:
                    await budget.release_unsent(attempt.attempt_id, reason="discussion_summary_call_cap")
                    raise RunBudgetError("discussion_summary_call_cap")
            if token_cap is not None:
                ids = [a for k, v in self.data["calls"].items() if k.startswith("summary:") for a in v["attempts"]]
                spent = sum(budget.attempts[a].reserved_tokens for a in ids if a in budget.attempts)
                if spent + admission["reserved_tokens"] > token_cap:
                    await budget.release_unsent(attempt.attempt_id, reason="discussion_summary_token_cap")
                    raise RunBudgetError("discussion_summary_token_cap")
            self.data["calls"][key]["attempts"].append(attempt.attempt_id)
            await self.save()

        async def guard(attempt):
            # This is also a lease/cancellation check immediately before network dispatch.
            await self.save()

        async def response(attempt, raw, usage):
            self.data["calls"][key]["response"] = await self.put("response", {
                "value": raw, "usage": usage.model_dump(mode="json")})
            await self.save()

        tokens = [(run_budget_attempt_observer, run_budget_attempt_observer.set(bind)),
                  (run_budget_dispatch_guard, run_budget_dispatch_guard.set(guard)),
                  (run_budget_response_observer, run_budget_response_observer.set(response)),
                  (run_budget_task_id, run_budget_task_id.set("discussion:" + key))]
        try:
            with activate_run_budget(budget):
                result = await provider.task_review_step(request)
            # Test senders may not implement observers; real providers always do.
            if not self.data["calls"][key].get("response"):
                await response(None, result.value, result.usage)
            return {"value": result.value, "usage": result.usage.model_dump(mode="json")}
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)
