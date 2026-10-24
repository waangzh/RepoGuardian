"""增量审查：确定可复用范围、重新校验前序结论、合并增量计划。

设计边界：
- 是否采用增量由服务端在创建任务时冻结；运行时只复核身份未变。
- 只有前序 Head 是当前 Head 的祖先、内容基准一致且审查策略一致时才考虑复用。
- 复用粒度是文件：自前序 Head 以来该文件没有任何变化时，其前序 Unit 才可沿用。
- 变化文件仍按完整 PR Diff（base..新 Head）规划 Unit：证据锚点与行号必须对
  当前 Head 成立，且后续提交可能改变先前变更的语义。
- 只复用当前运行时完整且无待复核问题的覆盖记录；依赖不完整时退回完整审查。
- 前序问题必须在当前 Head 重新审查，不自动沿用已确认或发布状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import settings
from app.models.incremental import (
    IncrementalPreview,
    IncrementalReusedUnit,
    IncrementalReviewBinding,
)
from app.models.review import (
    ChangedFile,
    ReviewPlan,
    ReviewTask,
    ReviewUnit,
)
from app.models.review_runtime import TaskRuntimeReport, UnitReuseDependencies
from app.services.fingerprints import stable_hash

INCREMENTAL_CONTRACT = "incremental-review-v1"
# 参与“同一审查策略”比较的配置键；模型/端点/预算单独比较。
_COMPARABLE_CONFIGURATION = (
    "prompt_version", "discussion_contract", "publication_contract", "pr_intent_contract",
    "risk_planner_version", "call_resolution_version", "readable_scope_version",
    "policy_version", "max_lines_per_read", "max_search_results", "request_profile",
    "limits",
    "stability_contract",
)
_CONFIGURATION_KEYS = ("incremental", "incremental_units")


@dataclass(frozen=True)
class IncrementalDecision:
    """创建期的一次性判定；预览与正式创建共用同一份计算结果。"""

    applicable: bool
    reason: str | None = None
    binding: IncrementalReviewBinding | None = None
    preview: IncrementalPreview | None = None
    previous_task: ReviewTask | None = None
    reused_units: tuple[ReviewUnit, ...] = ()


def _not_applicable(reason: str, full_changed: list[ChangedFile]) -> IncrementalDecision:
    explanation = {
        "incremental_review_disabled": "增量复用已关闭",
        "previous_review_missing": "没有可复用的前序审查",
        "previous_review_incomplete": "前序审查或用量记录不完整",
        "previous_findings_require_reverification": "前序问题需要在当前快照重新核验",
        "previous_investigations_require_reverification": "前序关联调查需要重新核验",
        "discussion_context_requires_reverification": "本次包含历史讨论，需要重新核验讨论背景",
        "custom_scope_not_incremental": "本次使用自定义审查范围",
        "history_rewritten": "提交历史已变化，无法证明前序快照可复用",
        "content_base_changed": "审查比较基准已变化",
        "no_reusable_units": "没有依赖与覆盖均满足条件的前序变更组",
    }.get(reason, "前序审查与当前快照不满足复用条件")
    return IncrementalDecision(False, reason, preview=IncrementalPreview(
        status="not_applicable", reason=reason, full_changed_file_count=len(full_changed),
        message=f"{explanation}；本次按完整 PR 审查。"))


def _report(task: ReviewTask) -> TaskRuntimeReport | None:
    try:
        return TaskRuntimeReport.model_validate(task.task_runtime_report)
    except (ValidationError, TypeError):
        return None


def _report_reuse_reason(report: TaskRuntimeReport | None, *, same_snapshot=False) -> str | None:
    if (report is None or report.execution_status != "completed"
            or report.termination_reason != "plan_exhausted" or not report.scope_complete
            or report.usage_integrity != "complete" or report.unknown_attempts):
        return "previous_review_incomplete"
    if any(item.get("status") != "dismissed" for item in report.findings):
        return "previous_findings_require_reverification"
    if same_snapshot:
        if (any(item.get("status") != "answered" or item.get("answer") not in {"supported", "refuted"}
                for item in report.admitted_investigations)
                or any(item.get("status") != "duplicate" for item in report.unscheduled_leads)):
            return "previous_investigations_require_reverification"
    elif report.admitted_investigations or report.unscheduled_leads or report.relationship_hints:
        return "previous_investigations_require_reverification"
    return None


def dependency_closure(paths: set[str], graph: dict | None) -> set[str]:
    """双向保守闭包；变化的调用方和被调用方都会使相关 Unit 失效。"""
    neighbours: dict[str, set[str]] = {}
    for edge in (graph or {}).get("edges", []):
        source = edge.get("source_file") or str(edge.get("source") or "").split("::", 1)[0]
        target = edge.get("target_file") or str(edge.get("target") or "").split("::", 1)[0]
        if source and target:
            neighbours.setdefault(source, set()).add(target)
            neighbours.setdefault(target, set()).add(source)
    result, pending = set(paths), list(paths)
    while pending:
        for path in neighbours.get(pending.pop(), set()) - result:
            result.add(path)
            pending.append(path)
    return result


def runtime_dependencies(plan: ReviewPlan, steps: dict, graph: dict | None):
    output = {}
    for unit in plan.review_units:
        selected = [step for step in steps.values() if step.executor.work_item.unit_id == unit.id]
        if not selected:
            continue
        paths = set(unit.primary_files) | set(unit.related_files)
        for step in selected:
            paths.update(step.dependency_paths)
            paths.update(value["file_path"] for value in step.executor.evidence_catalog.values()
                         if isinstance(value.get("file_path"), str))
        output[unit.id] = UnitReuseDependencies(
            paths=sorted(dependency_closure(paths, graph)),
            repository_discovery=any(step.repository_discovery for step in selected))
    return output


def reusable_units(
    previous: ReviewTask,
    full_changed: list[ChangedFile],
    delta_paths: set[str],
    full_plan_units: list[ReviewUnit],
    repository_graph: dict | None = None,
    *,
    same_snapshot=False,
) -> list[ReviewUnit]:
    """复用当前运行时的完整覆盖；旧结果或不完整依赖一律不作成功缓存。"""
    report = _report(previous)
    if _report_reuse_reason(report, same_snapshot=same_snapshot):
        return []
    current_paths = {item.file_path for item in full_changed}
    reusable: list[ReviewUnit] = []
    for unit in previous.review_units:
        if unit.grouping_reason == "cross_unit_followup":
            continue
        files = set(unit.primary_files)
        dependencies = report.reuse_dependencies.get(unit.id)
        if (dependencies is None or (dependencies.repository_discovery and not same_snapshot)
                or not files <= current_paths):
            continue
        paths = set(dependencies.paths) | files | set(unit.related_files)
        if dependency_closure(paths, repository_graph) & delta_paths:
            continue
        covered = [item for item in report.initial_scope_coverage if item.get("unit_id") == unit.id]
        if not covered or any(item.get("status") != "examined"
                              or not isinstance(item.get("goal_id"), str)
                              or not isinstance(item.get("result_id"), str) or not item["result_id"]
                              for item in covered):
            continue
        from app.graph.nodes.task_review_runtime import plan_runtime_goals

        expected = [goal for goal in plan_runtime_goals(
            previous.changed_files, ReviewPlan(planner_version="reuse", review_units=[unit]))
            if goal.unit_id == unit.id]
        if (not expected or {goal.goal_id for goal in expected} != {item["goal_id"] for item in covered}
                or len(covered) != len(expected) or any(goal.unsupported_reason for goal in expected)
                or any(any(item.get(key) != value for key, value in goal.model_dump(mode="json").items())
                       for goal in expected for item in covered if item["goal_id"] == goal.goal_id)):
            continue
        if expected != [goal for goal in plan_runtime_goals(
                full_changed, ReviewPlan(planner_version="reuse", review_units=[unit]))
                if goal.unit_id == unit.id]:
            continue
        overlapping = [current for current in full_plan_units if files & set(current.primary_files)]
        # 当前计划必须把这些文件完整归入同一 Unit；部分重叠说明分组已变化。
        if any(not set(current.primary_files) <= files for current in overlapping):
            continue
        if {path for current in overlapping for path in current.primary_files} != files:
            continue
        if (len(overlapping) != 1 or any(
                getattr(unit, key) != getattr(overlapping[0], key)
                for key in ("rule_ids", "risk_tags", "grouping_reason", "changed_symbols", "related_files"))):
            continue
        reusable.append(unit)
    return reusable


def _same_strategy(previous: ReviewTask, configuration: dict, model: str) -> str | None:
    saved = previous.task_runtime_configuration or {}
    if (saved.get("model") or previous.model) != model:
        return "model_changed"
    if saved.get("provider") != configuration.get("provider"):
        return "provider_changed"
    if saved.get("endpoint") != configuration.get("endpoint"):
        return "endpoint_changed"
    for key in _COMPARABLE_CONFIGURATION:
        if saved.get(key) != configuration.get(key):
            return "review_configuration_changed"
    previous_plan = saved.get("effective_plan") or {}
    current_plan = configuration.get("effective_plan") or {}
    for key in ("depth", "focus", "include_pr_discussions", "scope"):
        if previous_plan.get(key) != current_plan.get(key):
            return "review_plan_changed"
    return None


def same_snapshot_configuration_matches(saved: dict | None, current: dict) -> bool:
    saved = saved or {}
    keys = (set(saved) | set(current)) - {"incremental", "incremental_units", "plan_hash"}
    return all(saved.get(key) == current.get(key) for key in keys)


def plan_incremental(
    *,
    previous: ReviewTask | None,
    pr_url: str,
    pr_info: dict,
    effective_base_sha: str,
    new_head_sha: str,
    full_changed: list[ChangedFile],
    full_plan_units: list[ReviewUnit],
    changed_since_previous: list[str] | None,
    configuration: dict,
    model: str,
    ancestor: bool,
    same_merge_base: bool,
    repository_graph: dict | None = None,
    allow_same_snapshot: bool = False,
) -> IncrementalDecision:
    """创建任务前完成全部可复用判定；任何不确定都退回完整审查。"""

    if not settings.repoguardian_incremental_review_enabled:
        return _not_applicable("incremental_review_disabled", full_changed)
    plan_scope = ((configuration.get("effective_plan") or {}).get("scope") or {})
    if plan_scope.get("mode", "all") != "all":
        # 自定义范围会改变“哪些文件算已审查”的口径，与文件级复用冲突。
        return _not_applicable("custom_scope_not_incremental", full_changed)
    if previous is None:
        return _not_applicable("previous_review_missing", full_changed)
    if previous.pr_url != pr_url:
        return _not_applicable("previous_review_other_pr", full_changed)
    if previous.engine != "task_review_v1" or not previous.task_runtime_report:
        return _not_applicable("previous_review_incomplete", full_changed)
    manifest = previous.run_manifest
    if manifest is None or manifest.review_id != previous.id or not manifest.head_sha:
        return _not_applicable("previous_review_manifest_missing", full_changed)
    if not manifest.effective_base_sha:
        return _not_applicable("previous_review_base_missing", full_changed)
    if previous.status.value not in {"completed", "completed_with_warnings"}:
        return _not_applicable("previous_review_not_terminal", full_changed)
    if not previous.review_units:
        return _not_applicable("previous_review_units_missing", full_changed)
    same_snapshot = manifest.head_sha == new_head_sha
    if same_snapshot and (
            not allow_same_snapshot or configuration.get("stability_contract") != "review-stability-v1"
            or (previous.task_runtime_configuration or {}).get("stability_contract") != "review-stability-v1"):
        return _not_applicable("same_head", full_changed)
    reason = _report_reuse_reason(_report(previous), same_snapshot=same_snapshot)
    if reason:
        return _not_applicable(reason, full_changed)
    if (configuration.get("effective_plan") or {}).get("include_pr_discussions"):
        return _not_applicable("discussion_context_requires_reverification", full_changed)
    if not ancestor:
        return _not_applicable("history_rewritten", full_changed)
    if not same_merge_base or manifest.effective_base_sha != effective_base_sha:
        return _not_applicable("content_base_changed", full_changed)
    if changed_since_previous is None:
        return _not_applicable("delta_diff_unavailable", full_changed)
    reason = _same_strategy(previous, configuration, model)
    if reason:
        return _not_applicable(reason, full_changed)
    if same_snapshot:
        if not same_snapshot_configuration_matches(previous.task_runtime_configuration, configuration):
            return _not_applicable("review_configuration_changed", full_changed)
    from app.services.review_input_context import build_pr_intent

    if build_pr_intent(previous.pr.model_dump(mode="json") if previous.pr else {})["intent_hash"] != \
            build_pr_intent(pr_info)["intent_hash"]:
        return _not_applicable("pr_description_changed", full_changed)

    delta_paths = set(changed_since_previous)
    if same_snapshot and (delta_paths or stable_hash([item.model_dump(mode="json")
            for item in previous.changed_files]) != stable_hash([item.model_dump(mode="json")
            for item in full_changed])):
        return _not_applicable("same_snapshot_input_changed", full_changed)
    if not delta_paths and not same_snapshot:
        return _not_applicable("no_incremental_changes", full_changed)
    reusable = reusable_units(previous, full_changed, delta_paths, full_plan_units, repository_graph,
                              same_snapshot=same_snapshot)
    if not reusable:
        return _not_applicable("no_reusable_units", full_changed)

    delta_files = sorted(delta_paths)
    warnings = [
        f"增量审查：复用前序任务 {previous.id[:8]} 中 {len(reusable)} 个未变更 Unit；"
        "其余 Unit 及依赖受影响的范围重新检查。",
    ]
    if same_snapshot:
        warnings = [f"同快照复用：沿用显式选择的任务 {previous.id[:8]} 中 {len(reusable)} 个已完整审查 Unit；"
                    "未重新执行这些范围的模型审查。"]
    binding = IncrementalReviewBinding(
        previous_task_id=previous.id, previous_head_sha=manifest.head_sha,
        review_base_sha=manifest.effective_base_sha, effective_base_sha=effective_base_sha,
        new_head_sha=new_head_sha,
        previous_report_hash=stable_hash(previous.task_runtime_report),
        reused_units_hash=stable_hash([unit.model_dump(mode="json") for unit in reusable]),
        diff_hash=stable_hash({"full": [item.model_dump(mode="json") for item in full_changed],
                               "delta": delta_files}),
        delta_paths=delta_files, delta_file_count=len(delta_files), pr_file_count=len(full_changed),
        reused_units=[IncrementalReusedUnit(unit_id=unit.id, source_task_id=previous.id)
                      for unit in reusable],
        warnings=warnings)
    preview = IncrementalPreview(
        status="applied", previous_task_id=previous.id, previous_head_sha=manifest.head_sha,
        binding_hash=stable_hash(binding.model_dump(mode="json")),
        new_head_sha=new_head_sha, review_base_sha=effective_base_sha,
        delta_file_count=len(delta_files), full_changed_file_count=len(full_changed),
        reused_unit_count=len(reusable),
        reused_file_count=len({path for unit in reusable for path in unit.primary_files}),
        warnings=warnings,
        message=(warnings[0] if same_snapshot else
                 f"检测到 {len(delta_files)} 个文件在 {manifest.head_sha[:8]}..{new_head_sha[:8]} 间变化；"
                 f"复用 {len(reusable)} 个未变更 Unit。"))
    return IncrementalDecision(True, None, binding, preview, previous, tuple(reusable))


def merge_incremental_plan(plan: ReviewPlan, reused_units: list[ReviewUnit],
                           warnings: list[str]) -> ReviewPlan:
    """把复用 Unit 并入当前计划；当前计划中已被完整覆盖的 Unit 不再重复主审。"""
    covered = {path for unit in reused_units for path in unit.primary_files}
    current = [unit for unit in plan.review_units
               if not set(unit.primary_files) <= covered]
    merged = plan.model_copy(update={
        "review_units": [*current, *reused_units],
        "warnings": [*plan.warnings, *warnings],
    })
    owners: dict[str, int] = {}
    for unit in merged.review_units:
        for path in unit.primary_files:
            owners[path] = owners.get(path, 0) + 1
    if any(count > 1 for count in owners.values()):
        raise ValueError("incremental_plan_primary_conflict")
    return merged


def validate_binding(configuration: dict | None, *, head_sha: str,
                     effective_base_sha: str) -> IncrementalReviewBinding | None:
    """运行时复核冻结绑定；身份变化必须显式失败，不能静默降级。"""
    value = (configuration or {}).get("incremental")
    if not value:
        return None
    try:
        binding = IncrementalReviewBinding.model_validate(value)
    except ValidationError as exc:
        raise ValueError("incremental_binding_invalid") from exc
    if binding.new_head_sha != head_sha:
        raise ValueError("incremental_snapshot_changed")
    if binding.effective_base_sha != effective_base_sha:
        raise ValueError("incremental_base_changed")
    if (binding.review_base_sha != effective_base_sha or not binding.reused_units
            or len({item.unit_id for item in binding.reused_units}) != len(binding.reused_units)
            or any(item.source_task_id != binding.previous_task_id for item in binding.reused_units)):
        raise ValueError("incremental_binding_invalid")
    return binding


def reused_unit_snapshots(configuration: dict | None) -> list[ReviewUnit]:
    values = (configuration or {}).get("incremental_units") or []
    units = [ReviewUnit.model_validate(value) for value in values]
    binding = (configuration or {}).get("incremental") or {}
    if stable_hash([unit.model_dump(mode="json") for unit in units]) != binding.get("reused_units_hash"):
        raise ValueError("incremental_reused_units_changed")
    return units


def reuse_coverage(binding: IncrementalReviewBinding, previous: ReviewTask | None,
                   full_changed, plan_units, *, delta_paths, repository_graph):
    """重新验证固定来源、实际 Git 增量和依赖；不能复用变化后的前序产物。"""
    if previous is None or previous.id != binding.previous_task_id:
        raise ValueError("incremental_previous_task_missing")
    if previous.status.value not in {"completed", "completed_with_warnings"}:
        raise ValueError("incremental_previous_review_incomplete")
    manifest = previous.run_manifest
    if (manifest is None or manifest.review_id != previous.id
            or manifest.head_sha != binding.previous_head_sha
            or manifest.effective_base_sha != binding.review_base_sha):
        raise ValueError("incremental_previous_head_mismatch")
    if stable_hash(previous.task_runtime_report) != binding.previous_report_hash:
        raise ValueError("incremental_previous_report_changed")
    if (set(binding.delta_paths) != set(delta_paths)
            or binding.delta_file_count != len(set(delta_paths))
            or binding.pr_file_count != len(full_changed)
            or binding.diff_hash != stable_hash({
            "full": [item.model_dump(mode="json") for item in full_changed],
            "delta": sorted(delta_paths)})):
        raise ValueError("incremental_delta_changed")
    same_snapshot = binding.previous_head_sha == binding.new_head_sha
    if same_snapshot and (
            (previous.task_runtime_configuration or {}).get("stability_contract") != "review-stability-v1"
            or delta_paths or stable_hash([item.model_dump(mode="json") for item in previous.changed_files])
            != stable_hash([item.model_dump(mode="json") for item in full_changed])):
        raise ValueError("same_snapshot_input_changed")
    eligible = reusable_units(previous, full_changed, set(delta_paths), plan_units, repository_graph,
                              same_snapshot=same_snapshot)
    expected = {item.unit_id for item in binding.reused_units}
    if not expected <= {unit.id for unit in eligible}:
        raise ValueError("incremental_reused_coverage_invalid")
    source = [unit.model_dump(mode="json") for unit in previous.review_units if unit.id in expected]
    if stable_hash(source) != binding.reused_units_hash:
        raise ValueError("incremental_reused_units_changed")
    return {item["goal_id"]: {**item, "reused": True,
            "reused_from_task_id": previous.id, "reused_from_head_sha": manifest.head_sha}
            for item in previous.task_runtime_report["initial_scope_coverage"]
            if item.get("unit_id") in expected}
