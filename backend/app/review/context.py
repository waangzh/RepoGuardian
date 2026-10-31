"""Attempt-local references over shared immutable source bytes."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.review.domain import SourceRef
from app.review.evidence import source_lines
from app.review.planner import identity, members, tokens

MAX_MATERIAL_TOKENS = 12000


@dataclass
class AgentContext:
    provider: Any
    work: Any
    budget: Any
    visible: dict[str, SourceRef] = field(default_factory=dict)
    pending: dict[str, SourceRef] = field(default_factory=dict)
    pending_hunks: dict[str, str] = field(default_factory=dict)
    material_failures: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, dict] = field(default_factory=dict)
    tool_calls: int = 0
    model_calls: int = 0
    source_counter: int = 0

    @property
    def evidence(self):
        return self.provider.evidence

    @property
    def denied_paths(self):
        return self.provider.denied_paths

    @property
    def changed_files(self):
        return [item.model_dump() for item in self.provider.run.changed_files]

    async def check_active(self):
        await self.budget.active()

    def stage(self, ref: SourceRef):
        for handle, issued in {**self.visible, **self.pending}.items():
            if issued.id == ref.id:
                return handle
        self.source_counter += 1
        handle = f"S{self.source_counter}"
        self.pending[handle] = ref
        return handle

    def resolve(self, handles):
        if len(set(handles)) != len(handles) or any(handle not in self.visible for handle in handles):
            raise ValueError("source_not_visible_in_this_attempt")
        return [self.visible[handle] for handle in handles]

    async def acknowledge(self, pending, hunks):
        await asyncio.to_thread(self.provider.store.deliver, self.provider.run.id, self.work,
            list(hunks), self.material_failures, self.provider.lease,
            source_ids=[ref.id for ref in pending.values()], digests=hunks)
        self.visible.update(pending)
        for handle in pending:
            self.pending.pop(handle, None)
        for key in hunks:
            self.pending_hunks.pop(key, None)

    async def read(self, path, start, end, revision):
        result = await self.evidence.read(path, start, end, revision)
        ref = SourceRef.model_validate(result.pop("evidence"))
        handle = self.stage(ref)
        return {**result, "source_ref": handle, "source": ref.model_dump()}


@dataclass
class ContextProvider:
    run: Any
    evidence: Any
    store: Any
    lease: dict
    denied_paths: frozenset[str] = frozenset()
    discussions: list[dict] = field(default_factory=list)
    index: Any = None

    def _member(self, key):
        return members(self.run.changed_files)[key]

    async def material(self, context, key):
        member = self._member(key)
        references = []
        for side, number_key, path in (
            ("base", "old_line_no", member["old_path"]),
            ("head", "new_line_no", member["path"]),
        ):
            lines = [line for line in member["lines"] if line[number_key] is not None]
            if not lines:
                continue
            first, last = lines[0][number_key], lines[-1][number_key]
            result = await self.evidence.read(path, first, last, side)
            actual = source_lines(result["content"])
            for line in lines:
                if actual[line[number_key] - first].rstrip("\n") != line["content"]:
                    raise ValueError("diff_blob_mismatch")
            ref = SourceRef.model_validate(result["evidence"])
            references.append((ref, result["content"]))
        if not member["lines"]:
            git, repo, snap = self.evidence.git, self.evidence.repo_path, self.run.snapshot
            before = await asyncio.to_thread(git.get_tree_entries, repo, snap.base_sha, [member["old_path"]])
            after = await asyncio.to_thread(git.get_tree_entries, repo, snap.head_sha, [member["path"]])
            if not before and not after:
                raise ValueError("structural_member_unavailable")
            member = {**member, "base_entry": before, "head_entry": after}
        packet = {"member": member, "sources": [
            {"source": ref.model_dump(), "content": content} for ref, content in references]}
        if tokens(packet) > MAX_MATERIAL_TOKENS:
            raise ValueError("material_capacity")
        for item, (ref, _) in zip(packet["sources"], references):
            item["source_ref"] = context.stage(ref)
        context.pending_hunks[key] = identity(packet)
        return packet

    async def initial(self, context, candidate=None):
        unit = next(unit for unit in self.run.plan.units if unit.id == context.work.unit_id)
        material, spent = [], 0
        for key in unit.required_hunk_ids:
            before_pending = dict(context.pending)
            try:
                packet = await self.material(context, key)
            except (ValueError, OSError):
                context.material_failures[key] = "material_unavailable"
                continue
            cost = tokens(packet)
            if spent + cost > MAX_MATERIAL_TOKENS:
                context.pending_hunks.pop(key, None)
                context.pending = before_pending
                context.material_failures[key] = "material_capacity"
                continue
            spent += cost
            material.append(packet)
        candidate_materials = []
        if candidate:
            for ref in candidate.sources:
                try:
                    candidate_materials.append(await context.read(
                        ref.path, ref.start_line, ref.end_line, ref.side))
                except (ValueError, OSError):
                    continue
        saved = await asyncio.to_thread(self.store.candidates, self.run.id)
        return {"snapshot": self.run.snapshot.model_dump(), "focus": unit.focus,
            "change_map": [{"paths": item.paths, "risk": item.risk} for item in self.run.plan.units][:100],
            "history_untrusted": [{"claim": item["claim"], "head_sha": item["head_sha"],
                "status": "historical_not_current_evidence"} for item in self.run.plan.history[:20]],
            "author_statement_untrusted": {"title": self.run.pr.title, "body": (self.run.pr.body or "")[:4000]},
            "unit": unit.model_dump(), "materials": material,
            "missing_material": context.material_failures,
            "already_submitted": [{"id": item.id, "claim": item.proposal.claim}
                                  for item in saved if item.unit_id == unit.id][:100],
            "candidate": candidate.proposal.model_dump(exclude={"source_refs"}) if candidate else None,
            "candidate_materials": candidate_materials}
