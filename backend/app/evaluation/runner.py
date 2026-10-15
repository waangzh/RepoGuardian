"""固定快照 + 当前 task_review_v1 生产图；不复用生产持久化状态。"""

import asyncio
import inspect
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.evaluation.data import DatasetLock, FrozenSample, sha256
from app.evaluation.diff_inputs import DiffInputLock, PreparedDiff
from app.graph.review_graph import build_review_graph
from app.models.review import PullRequestInfo, PullRequestRef, ReviewIssue
from app.review.issue_audit import capture_issue_audit
from app.review.run_audit import capture_run_audit
from app.services.model_usage import unpack_model_call
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
from app.tools.git_tool import DIFF_CONTEXT_LINES, DIFF_POLICY_VERSION, GitTool, GitToolError


EVALUATION_ENGINE = "task_review_v1"


class SnapshotGitTool(GitTool):
    """复用 Git 安全封装，但只 fetch SHA，拒绝分支回退和工作树偏离。"""

    def __init__(self, workdir=None, *, prepared_diff: PreparedDiff | None = None, **kwargs):
        super().__init__(workdir, **kwargs)
        self.prepared_diff = prepared_diff

    def _fetch_ref(self, repo_dir, remote, sha, ref, **kwargs):
        self._run([self._git, "-C", str(repo_dir), "fetch", "--progress", remote, sha], **kwargs)
        actual = self._run([self._git, "-C", str(repo_dir), "rev-parse", "FETCH_HEAD"],
                           cancel_event=kwargs.get("cancel_event")).strip()
        if actual != sha:
            raise GitToolError("snapshot_fetch_sha_mismatch")

    def review_diff_context(self, repo_path, base_sha=None, head_sha=None):
        self._validate_worktree(repo_path)
        if self.prepared_diff is None:
            return {}
        return {"effective_base_sha": self.prepared_diff.effective_base_sha,
                "diff_policy": "merge_base", "diff_policy_version": DIFF_POLICY_VERSION}

    def clone_and_diff(self, pr, **kwargs):
        # 输入锁未就绪或身份不符时在 clone 前失败，不产生任何工作区副作用。
        fact = self.prepared_diff
        if fact and (fact.alignment_status != "ready" or fact.declared_base_sha != pr.base.sha
                     or fact.head_sha != pr.head.sha):
            raise GitToolError("snapshot_diff_input_not_ready_or_identity_mismatch")
        return super().clone_and_diff(pr, **kwargs)

    def unified_diff(self, repo_path, base_sha, head_sha, *, cancel_event=None):
        """带输入锁时强制 merge base 与 diff 哈希；v1 协议保留声明 base 两点比较。"""
        fact = self.prepared_diff
        if fact is None:
            diff = self._run([self._git, "-C", str(repo_path), "diff", "--no-ext-diff",
                              f"--unified={DIFF_CONTEXT_LINES}", base_sha, head_sha],
                             cancel_event=cancel_event)
            self._record_snapshot(repo_path, base_sha, head_sha, base_sha, "declared_base", diff)
            return base_sha, diff
        if (fact.alignment_status != "ready" or fact.declared_base_sha != base_sha
                or fact.head_sha != head_sha):
            raise GitToolError("snapshot_diff_input_not_ready_or_identity_mismatch")
        effective, diff = super().unified_diff(repo_path, base_sha, head_sha, cancel_event=cancel_event)
        if effective != fact.effective_base_sha:
            raise GitToolError("snapshot_merge_base_mismatch")
        if sha256(diff.encode()) != fact.diff_sha256:
            raise GitToolError("snapshot_diff_hash_mismatch")
        self._record_snapshot(repo_path, base_sha, head_sha, effective, "merge_base", diff)
        return effective, diff

    def _record_snapshot(self, repo_path, base_sha, head_sha, effective, policy, diff):
        self.snapshot = {"repo_path": str(Path(repo_path).resolve()), "verified_head": head_sha,
                         "verified_base": effective, "declared_base_sha": base_sha,
                         "diff_policy": policy, "diff_sha256": sha256(diff.encode()),
                         "diff_text": diff}


class FrozenGitHubTool:
    def __init__(self, sample: FrozenSample):
        self.sample = sample

    async def fetch_pr(self, pr_url):
        if pr_url != self.sample.pr_url:
            raise ValueError("snapshot_pr_url_mismatch")
        owner, repo = self.sample.repository.split("/")
        url = f"https://github.com/{owner}/{repo}.git"
        return PullRequestInfo(owner=owner, repo=repo, number=int(pr_url.rsplit("/", 1)[1]),
            title=f"AACR-Bench {self.sample.repository} PR #{pr_url.rsplit('/', 1)[1]}",
            body=None, html_url=pr_url, clone_url=url,
            base=PullRequestRef(ref=self.sample.base_sha, sha=self.sample.base_sha, repo_clone_url=url),
            head=PullRequestRef(ref=self.sample.head_sha, sha=self.sample.head_sha, repo_clone_url=url))


class ObservedProvider:
    """保留原 Provider 行为，旁路记录失败 Unit 在状态丢弃前的调用与实测用量。"""

    def __init__(self, provider):
        self.provider = provider
        self.calls = []

    def __getattr__(self, name):
        provider = self.__dict__.get("provider")
        if provider is None:
            raise AttributeError(name)
        method = getattr(provider, name)
        if not inspect.iscoroutinefunction(method):
            return method

        async def call(*args, **kwargs):
            record = {"operation": name, "status": "running", "usage": None}
            self.calls.append(record)
            started = time.monotonic()
            try:
                result = await method(*args, **kwargs)
                _, usage = unpack_model_call(result)
                record.update(status="completed", usage=usage.model_dump(mode="json") if usage else None)
                if name == "task_review_step":
                    record["raw_response"] = result.value
                return result
            except BaseException as exc:
                usage = getattr(exc, "usage", None)
                record.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                              error_type=type(exc).__name__,
                              usage=usage.model_dump(mode="json") if usage else None)
                raise
            finally:
                record["duration_ms"] = round((time.monotonic() - started) * 1000)
        return call


def prediction(issue: dict) -> dict:
    item = ReviewIssue.model_validate(issue)
    anchor = item.primary_evidence
    return {"issue_id": item.id, "path": anchor.file_path,
            "start_line": anchor.resolved_start_line, "end_line": anchor.resolved_end_line,
            "side": {"head": "right", "base": "left"}.get(anchor.resolved_side),
            "category": item.category.value, "severity": item.severity.value,
            "note": "\n".join([item.title, item.affected_behavior, item.failure_scenario,
                                 item.recommendation]), "evidence": anchor.model_dump(mode="json")}


def final_audit(events: list[dict], state: dict, run_status: str) -> list[dict]:
    # 映射只来自本次运行中的真实父子账本；保留原始事件的 Unit 和候选 ID。
    parents, ambiguous = {}, set()
    for result in state.get("review_unit_results") or []:
        parent = result["review_unit_id"]
        for batch in result.get("diff_batches") or []:
            if batch.get("parent_unit_id") != parent or batch["id"] == parent:
                continue
            child = (batch.get("result") or {}).get("review_unit_id") or batch["id"]
            if child in parents and parents[child] != parent:
                ambiguous.add(child)
            parents[child] = parent
    def owner(unit):
        current, seen = unit, set()
        while current in parents:
            if current in seen or current in ambiguous:
                return unit
            seen.add(current)
            current = parents[current]
        return current
    final = {(owner(x["review_unit_id"]), x["id"]): x for x in state.get("review_issues") or []}
    aliases = {}
    for event in events:
        if not event.get("canonical_id"):
            continue
        source = (owner(event["review_unit_id"]), event["issue_id"])
        target = (owner(event.get("canonical_review_unit_id") or event["review_unit_id"]), event["canonical_id"])
        aliases.setdefault(source, set()).add(target)

    def resolve_alias(source):
        current, seen = source, set()
        while current in aliases:
            if current in seen:
                return source, "alias_cycle"
            seen.add(current)
            targets = aliases[current]
            if len(targets) != 1:
                return source, "alias_ambiguous"
            current = next(iter(targets))
        return current, None
    unit_reasons = {x["review_unit_id"]: x.get("terminal_reason") or x.get("status")
                    for x in state.get("review_unit_results") or []}
    latest, terminal = {}, {}
    for event in events:
        latest[(event["review_unit_id"], event["issue_id"])] = event
        terminal[(owner(event["review_unit_id"]), event["issue_id"])] = event
    output = list(events)
    for issue in final.values():
        key = (issue["review_unit_id"], issue["id"])
        if (owner(key[0]), key[1]) not in terminal:
            event = {"stage": "audit_gap", "issue_id": issue["id"],
                     "review_unit_id": issue["review_unit_id"], "status": issue["status"],
                     "reason": "candidate_entry_not_observed", "issue": issue}
            latest[key] = event
            terminal[(owner(key[0]), key[1])] = event
            output.append(event)
    for (unit_id, issue_id), event in latest.items():
        root = owner(unit_id)
        canonical_key, alias_error = resolve_alias((root, issue_id))
        canonical_root, canonical = canonical_key
        issue = final.get(canonical_key) if not alias_error else None
        event = terminal[(root, issue_id)]
        if alias_error:
            disposition, reason = "not_published", alias_error
        elif canonical_key != (root, issue_id):
            deduplicated = any(x["stage"] == "deduplication" and x["issue_id"] == issue_id
                               and owner(x["review_unit_id"]) == root for x in events)
            disposition = "merged" if deduplicated else "reidentified"
            schema_entry = any(x["stage"] == "schema_validation" and x["issue_id"] == issue_id
                               and owner(x["review_unit_id"]) == root for x in events)
            reason = "deduplication" if deduplicated else "schema_accepted" if schema_entry else "followup_server_identity"
            if canonical_key not in terminal or terminal[canonical_key]["stage"] == "audit_gap":
                disposition, reason = "not_published", "canonical_lifecycle_not_observed"
                issue = None
        elif issue and issue["status"] in {"confirmed", "unresolved", "candidate", "needs_human"}:
            disposition, reason = "unresolved" if issue["status"] == "needs_human" else issue["status"], None
        elif issue and issue["status"] == "dismissed":
            disposition, reason = "dismissed", issue.get("unresolved_reason") or "not_publishable"
        elif event["stage"] == "unit_filter" and event["reason"] != "accepted":
            disposition, reason = "dismissed", event["reason"]
        elif event["status"] == "dismissed":
            disposition, reason = "dismissed", event["reason"]
        else:
            disposition = "not_published"
            reason = f"{run_status}:{unit_reasons.get(root) or 'pipeline_interrupted'}"
        output.append({"stage": "final", "issue_id": issue_id, "review_unit_id": unit_id,
                       "status": disposition, "reason": reason,
                       "canonical_status": ("unresolved" if issue["status"] == "needs_human" else issue["status"]) if issue else None,
                        "canonical_review_unit_id": canonical_root,
                        "canonical_id": canonical if canonical_key != (root, issue_id) and not alias_error else None})
    # schema 与服务端改名的原身份均继承真实目标终态，身份变化本身不是终态。
    dispositions = {(owner(item["review_unit_id"]), item["issue_id"]): item
                    for item in output if item["stage"] == "final"}
    for event in events:
        if event["stage"] not in {"schema_validation", "identity"} or not event.get("canonical_id"):
            continue
        source_key, error = resolve_alias((owner(event["review_unit_id"]), event["issue_id"]))
        source = dispositions.get(source_key) if not error else None
        if source and terminal.get(source_key, {}).get("stage") != "audit_gap":
            for target in output:
                if (target["stage"] == "final" and target["issue_id"] == event["issue_id"]
                        and owner(target["review_unit_id"]) == owner(event["review_unit_id"])):
                    target.update({key: source.get(key) for key in ("status", "reason", "canonical_status")})
    return [{"sequence": i + 1, **event} for i, event in enumerate(output)]


async def run_sample(sample: FrozenSample, provider, workdir: Path, *, timeout_seconds: float = 1800,
                     git_tool=None, prepared_diff: PreparedDiff | None = None, experiment=None,
                     runtime_configuration: dict | None = None) -> dict:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    configuration = deepcopy(runtime_configuration) if runtime_configuration is not None else (
        task_runtime_configuration(settings.repoguardian_model, provider=provider))
    observed = ObservedProvider(provider)
    events = []
    model_requests, tool_reads = [], []
    selected_git = git_tool or SnapshotGitTool(workdir, prepared_diff=prepared_diff)
    state = {"task_id": uuid4().hex, "mode": "review", "pr_url": sample.pr_url,
             "engine": EVALUATION_ENGINE,
             "model": configuration["model"], "created_at": datetime.now(timezone.utc).isoformat(),
             "generate_patches": False, "validation_backend": "none",
             "_persistence_enabled": False,
             "_github_tool": FrozenGitHubTool(sample), "_git_tool": selected_git,
             "_provider": observed}
    state["task_runtime_configuration"] = configuration
    if experiment is not None:
        if (not experiment.get("runtime_versions") or any(
                configuration.get(key) != value for key, value in experiment["runtime_versions"].items())):
            raise ValueError("experiment runtime versions changed before dispatch")
        configuration.update(experiment=experiment["variables"],
            experiment_expected_scope_hash=experiment["samples"][sample.instance_id]["readable_scope_hash"])
    if prepared_diff:
        state.update(effective_base_sha=prepared_diff.effective_base_sha, diff_policy="merge_base")
    started = time.monotonic()
    last_node = None
    run_status = "completed"
    error_type = None
    run_budget = await ReviewRunBudget.load_or_create(state["task_id"], **configuration["limits"])
    resolver = getattr(provider, "resolve_request_profile", None)
    profile = resolver(state["model"]) if callable(resolver) else None
    try:
        with activate_run_budget(run_budget), capture_issue_audit(events.append), capture_run_audit(
            model_sink=model_requests.append, tool_sink=tool_reads.append,
        ):
            async with asyncio.timeout(timeout_seconds):
                graph = build_review_graph(phase=3).compile()
                async for delta in graph.astream(state, stream_mode="updates"):
                    for node, update in delta.items():
                        last_node = node
                        if update:
                            state.update(update)
    except TimeoutError:
        run_status, error_type = "timed_out", "TimeoutError"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        run_status = "prepare_failed" if not state.get("repo_path") else "failed"
        error_type = type(exc).__name__
    if run_status == "completed":
        report = state.get("task_runtime_report") or {}
        run_status = ("failed" if state.get("status") == "failed" or not report
                      or report.get("execution_status") == "failed" else
                      "cancelled" if report.get("execution_status") == "cancelled" else
                      "partial" if not report.get("scope_complete") else state.get("status", "completed"))
    issues = state.get("review_issues") or []
    confirmed = [prediction(x) for x in issues if x["status"] == "confirmed"]
    human = [prediction(x) for x in issues if x["status"] == "needs_human"]
    unresolved = [prediction(x) for x in issues if x["status"] in {"unresolved", "candidate"}]
    usages = [c["usage"] for c in observed.calls]
    actual = [u.get("actual_total_tokens") if u else None for u in usages]
    output = {"schema_version": "evaluation-result-v1", "sample": sample.model_dump(),
              "engine": state["engine"], "task_runtime_report": state.get("task_runtime_report"),
              "task_runtime_configuration": state.get("task_runtime_configuration"),
              "status": run_status, "error_type": error_type, "last_completed_node": last_node,
              "duration_ms": round((time.monotonic() - started) * 1000),
              "confirmed": confirmed, "unresolved": unresolved, "needs_human": human,
              "scoring_profiles": {"official_all": confirmed, "product_core": [x for x in confirmed
                  if x["category"] in {"correctness", "security", "performance"}]},
              "issues": issues, "issue_audit": final_audit(events, state, run_status),
              "coverage": state.get("review_coverage"), "manifest": state.get("run_manifest"),
              "unit_results": state.get("review_unit_results") or [],
              "followup_results": state.get("followup_results") or [],
              "coordination": state.get("coordination_plan"), "warnings": state.get("warnings") or [],
              "snapshot": getattr(selected_git, "snapshot", None),
              "diff_input": prepared_diff.model_dump(mode="json") if prepared_diff else None,
              "model_requests": model_requests, "tool_reads": tool_reads,
               "run_budget": run_budget.snapshot(),
               "effective_model_profile": ({"source": profile.source,
                   "profile": profile.profile.model_dump(mode="json")} if profile else None),
              "model_calls": observed.calls, "usage_summary": {"calls": len(usages),
                  "missing_usage_calls": sum(x is None for x in actual),
                  "observed_total_tokens": sum(x for x in actual if x is not None),
                  "actual_total_tokens": sum(actual) if all(x is not None for x in actual) else None}}
    return output


def write_json(path: Path, value: dict):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def implementation_digest() -> str:
    code_root = Path(__file__).parents[1]
    content = b"".join(path.relative_to(code_root).as_posix().encode() + b"\0" +
                       path.read_bytes() + b"\0" for path in sorted(code_root.rglob("*.py")))
    return sha256(content)


async def run_dataset(lock_path: Path, output: Path, provider, *, sample_ids: list[str] | None = None,
                      limit: int | None = None, timeout_seconds: float = 1800,
                      diff_lock_path: Path | None = None, experiment_plan=None, experiment_group=None,
                      experiment_stage=None, first_gate=None, quality_gate=None,
                      experiment_registry: Path | None = None) -> dict:
    data = await asyncio.to_thread(lock_path.read_bytes)
    lock = DatasetLock.model_validate_json(data)
    experiment = None
    samples = lock.samples
    diff_lock, diff_inputs, diff_data = None, {}, None
    if diff_lock_path:
        diff_data = await asyncio.to_thread(diff_lock_path.read_bytes)
        diff_lock = DiffInputLock.model_validate_json(diff_data)
        diff_lock.validate_dataset(lock)
        diff_inputs = {x.instance_id: x for x in diff_lock.samples}
        samples = [s for s in samples if s.instance_id in diff_inputs]
    if experiment_plan is not None:
        from app.evaluation.runtime_experiments import validate_experiment_plan, GROUPS, FIXED_SETTINGS
        await asyncio.to_thread(validate_experiment_plan, experiment_plan, data, diff_data or b"", experiment_group,
                                experiment_stage, sample_ids, first_gate, quality_gate)
        if await asyncio.to_thread(implementation_digest) != experiment_plan["implementation_sha256"]:
            raise ValueError("experiment implementation changed; new frozen audit plan required")
        from app.graph.nodes.task_review_runtime import task_runtime_configuration
        configuration = task_runtime_configuration(settings.repoguardian_model, provider=provider)
        if (not experiment_plan.get("runtime_versions") or any(
                configuration.get(key) != value for key, value in experiment_plan["runtime_versions"].items())):
            raise ValueError("experiment runtime versions changed; new frozen audit plan required")
        if limit is not None or timeout_seconds != experiment_plan["timeout_seconds"]:
            raise ValueError("experiment sample limit or timeout cannot change")
        for name, value in FIXED_SETTINGS.items():
            if getattr(settings, name) != value:
                raise ValueError("experiment live configuration mismatch: " + name)
        if settings.repoguardian_model_request_profile.model_dump(mode="json") != experiment_plan["request_profile"]:
            raise ValueError("experiment request profile mismatch")
        resolved = provider.resolve_request_profile(settings.repoguardian_model)
        if {"source": resolved.source, "profile": resolved.profile.model_dump(mode="json")} != experiment_plan["effective_model_profile"]:
            raise ValueError("experiment effective model profile mismatch")
        experiment = {"variables": GROUPS[experiment_group], "samples": experiment_plan["samples"],
                      "runtime_versions": experiment_plan["runtime_versions"]}
        if experiment_registry is None:
            raise ValueError("experiment requires a shared plan dispatch registry")
    if sample_ids:
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("duplicate sample-id")
        if set(sample_ids) - {x.instance_id for x in samples}:
            raise ValueError("未知 sample-id")
        by_id = {x.instance_id: x for x in samples}
        samples = [by_id[identity] for identity in sample_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        samples = samples[:limit]
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    configuration = task_runtime_configuration(settings.repoguardian_model, provider=provider)
    if experiment is not None:
        from app.evaluation.runtime_experiments import claim_experiment_slot
        await asyncio.to_thread(claim_experiment_slot, experiment_plan, experiment_group, experiment_stage,
                                experiment_registry, output)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    config = {k: v for k, v in settings.model_dump(mode="json").items()
              if k.startswith("repoguardian_") and not any(x in k for x in
                  ("key", "secret", "token", "registration", "workdir", "db", "artifact"))}
    # 评测执行版本独立于产品默认引擎；清单必须记录实际执行值。
    config["repoguardian_review_engine"] = EVALUATION_ENGINE
    summary = {"protocol_version": diff_lock.protocol_version if diff_lock else lock.protocol_version,
               "engine": EVALUATION_ENGINE, "task_runtime_configuration": configuration,
               "lock_sha256": sha256(data), "diff_lock_sha256": sha256(diff_data) if diff_data else None,
               "diff_policy": "merge_base" if diff_lock else "declared_base",
               "source_revision": lock.source_revision, "config": config,
               "implementation_sha256": await asyncio.to_thread(implementation_digest),
               "timeout_seconds": timeout_seconds, "cache_reuse": False,
               "samples": [{"instance_id": s.instance_id, "status": "not_run"} for s in samples]}
    if experiment is not None:
        summary["experiment"] = {"plan_sha256": experiment_plan["plan_sha256"], "group": experiment_group,
                                  "stage": experiment_stage, "variables": experiment["variables"],
                                  "runtime_versions": experiment["runtime_versions"]}
    await asyncio.to_thread(write_json, output / "run.json", summary)
    for index, sample in enumerate(samples):
        summary["samples"][index]["status"] = "running"
        await asyncio.to_thread(write_json, output / "run.json", summary)
        result = await run_sample(sample, provider, output / "workspaces", timeout_seconds=timeout_seconds,
                                  prepared_diff=diff_inputs.get(sample.instance_id), experiment=experiment,
                                  runtime_configuration=configuration)
        filename = f"{index:04d}.json"
        await asyncio.to_thread(write_json, output / filename, result)
        summary["samples"][index].update(status=result["status"], result_file=filename)
        await asyncio.to_thread(write_json, output / "run.json", summary)
        if experiment is not None and experiment_stage == "first":
            from app.evaluation.runtime_experiments import first_sample_gate
            gate = first_sample_gate(result, experiment_plan, experiment_group)
            await asyncio.to_thread(write_json, output / "first-gate.json", gate)
    return summary
