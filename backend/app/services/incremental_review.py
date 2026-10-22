"""增量审查：确定可复用范围、重新校验前序结论、合并增量计划。

设计边界：
- 是否采用增量由服务端在创建任务时冻结；运行时只复核身份未变。
- 只有前序 Head 是当前 Head 的祖先、内容基准一致且审查策略一致时才考虑复用。
- 复用粒度是文件：自前序 Head 以来该文件没有任何变化时，其前序 Unit 才可沿用。
- 变化文件仍按完整 PR Diff（base..新 Head）规划 Unit：证据锚点与行号必须对
  当前 Head 成立，且后续提交可能改变先前变更的语义。
- 前序已确认问题只做“当前快照证据重新定位”，状态记为 carried_forward，
  不计入本轮可发布结论；语义未重新核验。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import settings
from app.models.incremental import (
    IncrementalPreview,
    IncrementalReusedUnit,
    IncrementalReviewBinding,
    IncrementalSupersededFinding,
)
from app.models.review import (
    ChangedFile,
    CommentPlacement,
    EvidenceAnchor,
    EvidenceResolutionStatus,
    IssueStatus,
    ReviewIssue,
    ReviewPlan,
    ReviewTask,
    ReviewUnit,
    ReviewUnitResult,
)
from app.models.review_runtime import Candidate
from app.services.fingerprints import stable_hash

INCREMENTAL_CONTRACT = "incremental-review-v1"
# 参与“同一审查策略”比较的配置键；模型/端点/预算单独比较。
_COMPARABLE_CONFIGURATION = (
    "prompt_version", "discussion_contract", "publication_contract", "pr_intent_contract",
    "risk_planner_version", "call_resolution_version", "readable_scope_version",
    "policy_version", "max_lines_per_read", "max_search_results",
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
    return IncrementalDecision(False, reason, preview=IncrementalPreview(
        status="not_applicable", reason=reason, full_changed_file_count=len(full_changed),
        message="本次按完整 PR 审查；未复用前序结论。"))


def delta_paths_from(full_changed: list[ChangedFile], changed_since_previous: list[str]) -> set[str]:
    """自前序 Head 以来变化的路径；只保留当前完整 Diff 中仍存在的路径。"""
    current = {item.file_path for item in full_changed}
    current.update(item.old_file_path for item in full_changed if item.old_file_path)
    return {path for path in changed_since_previous if path in current}


def reusable_units(
    previous: ReviewTask,
    full_changed: list[ChangedFile],
    delta_paths: set[str],
    full_plan_units: list[ReviewUnit],
) -> list[ReviewUnit]:
    """前序 Unit 的每个主审文件都未在增量中变化，且当前计划完整覆盖同一组文件。"""
    results = {item.review_unit_id: item for item in previous.review_unit_results}
    reusable: list[ReviewUnit] = []
    for unit in previous.review_units:
        if unit.grouping_reason == "cross_unit_followup":
            continue
        files = set(unit.primary_files)
        if files & delta_paths:
            continue
        result = results.get(unit.id)
        if result is None or not _reusable_result_complete(result):
            continue
        overlapping = [current for current in full_plan_units if files & set(current.primary_files)]
        # 当前计划必须把这些文件完整归入同一 Unit；部分重叠说明分组已变化。
        if any(not set(current.primary_files) <= files for current in overlapping):
            continue
        if not overlapping:
            continue
        reusable.append(unit)
    return reusable


def _reusable_result_complete(result: ReviewUnitResult) -> bool:
    from app.review.unit_completion import is_reusable_review_unit_result

    return is_reusable_review_unit_result(result)


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
    for key in ("depth", "focus", "include_pr_discussions"):
        if previous_plan.get(key) != current_plan.get(key):
            return "review_plan_changed"
    return None


def _all_previous_issues(task: ReviewTask) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for record in (task.task_runtime_report or {}).get("findings") or []:
        payload = record.get("issue") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        try:
            issues.append(ReviewIssue.model_validate(payload))
        except ValidationError:
            continue
    return issues


def _dedupe_superseded(items: list[IncrementalSupersededFinding]) -> list[IncrementalSupersededFinding]:
    order = {"file_changed_since_previous_review": 0, "base_side_evidence_not_reused": 1,
             "review_unit_not_reused": 2, "source_finding_missing": 3}
    seen: dict[str, IncrementalSupersededFinding] = {}
    for item in items:
        current = seen.get(item.candidate_id)
        if current is None or order[item.reason] < order[current.reason]:
            seen[item.candidate_id] = item
    return [seen[key] for key in sorted(seen, key=lambda value: (seen[value].file_path or "", value))]


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
    if not previous.review_units or not previous.review_unit_results:
        return _not_applicable("previous_review_units_missing", full_changed)
    if manifest.head_sha == new_head_sha:
        return _not_applicable("same_head", full_changed)
    if not ancestor:
        return _not_applicable("history_rewritten", full_changed)
    if not same_merge_base or manifest.effective_base_sha != effective_base_sha:
        return _not_applicable("content_base_changed", full_changed)
    if changed_since_previous is None:
        return _not_applicable("delta_diff_unavailable", full_changed)
    reason = _same_strategy(previous, configuration, model)
    if reason:
        return _not_applicable(reason, full_changed)
    from app.services.review_input_context import build_pr_intent

    if build_pr_intent(previous.pr.model_dump(mode="json") if previous.pr else {})["intent_hash"] != \
            build_pr_intent(pr_info)["intent_hash"]:
        return _not_applicable("pr_description_changed", full_changed)

    delta_paths = delta_paths_from(full_changed, changed_since_previous)
    if not delta_paths:
        return _not_applicable("no_incremental_changes", full_changed)
    reusable = reusable_units(previous, full_changed, delta_paths, full_plan_units)
    if not reusable:
        return _not_applicable("no_reusable_units", full_changed)

    reused_ids = {unit.id for unit in reusable}
    superseded: list[IncrementalSupersededFinding] = []
    for issue in _all_previous_issues(previous):
        evidence = issue.primary_evidence
        if evidence.file_path in delta_paths:
            superseded.append(IncrementalSupersededFinding(
                candidate_id=issue.id, file_path=evidence.file_path,
                reason="file_changed_since_previous_review"))
        elif evidence.resolved_side == "base" or evidence.expected_side == "base":
            superseded.append(IncrementalSupersededFinding(
                candidate_id=issue.id, file_path=evidence.file_path,
                reason="base_side_evidence_not_reused"))
        elif issue.review_unit_id not in reused_ids:
            superseded.append(IncrementalSupersededFinding(
                candidate_id=issue.id, file_path=evidence.file_path,
                reason="review_unit_not_reused"))
    superseded = _dedupe_superseded(superseded)

    delta_files = sorted(delta_paths)
    warnings = [
        f"增量审查：复用前序任务 {previous.id[:8]} 中 {len(reusable)} 个未变更 Unit；"
        f"仅对 {len(delta_files)} 个变化文件重新检查。",
    ]
    if superseded:
        warnings.append(
            f"增量审查：{len(superseded)} 项前序问题涉及变化文件或 Base 证据，"
            "已标记为待复核，不计入本轮已核验结论。")
    binding = IncrementalReviewBinding(
        previous_task_id=previous.id, previous_head_sha=manifest.head_sha,
        review_base_sha=manifest.effective_base_sha, effective_base_sha=effective_base_sha,
        new_head_sha=new_head_sha,
        diff_hash=stable_hash({"full": [item.model_dump(mode="json") for item in full_changed],
                               "delta": delta_files}),
        delta_paths=delta_files, delta_file_count=len(delta_files), pr_file_count=len(full_changed),
        reused_units=[IncrementalReusedUnit(unit_id=unit.id, source_task_id=previous.id)
                      for unit in reusable],
        superseded_findings=superseded, warnings=warnings)
    preview = IncrementalPreview(
        status="applied", previous_task_id=previous.id, previous_head_sha=manifest.head_sha,
        new_head_sha=new_head_sha, review_base_sha=effective_base_sha,
        delta_file_count=len(delta_files), full_changed_file_count=len(full_changed),
        reused_unit_count=len(reusable),
        reused_file_count=len({path for unit in reusable for path in unit.primary_files}),
        superseded_finding_count=len(superseded), warnings=warnings,
        message=(f"检测到 {len(delta_files)} 个文件在 {manifest.head_sha[:8]}..{new_head_sha[:8]} 间变化；"
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
    return binding


def reused_unit_snapshots(configuration: dict | None) -> list[ReviewUnit]:
    values = (configuration or {}).get("incremental_units") or []
    return [ReviewUnit.model_validate(value) for value in values]


def reuse_results(binding: IncrementalReviewBinding, previous: ReviewTask | None) -> list[ReviewUnitResult]:
    """从前序任务加载复用 Unit 结果；缺失或不完整时显式失败。"""
    if previous is None or previous.id != binding.previous_task_id:
        raise ValueError("incremental_previous_task_missing")
    manifest = previous.run_manifest
    if manifest is None or manifest.head_sha != binding.previous_head_sha:
        raise ValueError("incremental_previous_head_mismatch")
    results = {item.review_unit_id: item for item in previous.review_unit_results}
    output = []
    for item in binding.reused_units:
        result = results.get(item.unit_id)
        if result is None or not _reusable_result_complete(result):
            raise ValueError("incremental_reused_result_missing")
        output.append(result.model_copy(update={
            "reused_from_task_id": previous.id,
            "reused_from_head_sha": manifest.head_sha,
        }))
    return output


def _relocate_anchor(anchor: EvidenceAnchor, *, file_loader):
    """在当前固定快照中重新定位锚点；找不到或存在歧义即返回 None。"""
    side = anchor.resolved_side or ("base" if anchor.expected_side == "base" else "head")
    content = file_loader(anchor.file_path, side)
    if not content:
        return None
    lines = content.splitlines()
    snippet = anchor.existing_code.strip("\n").splitlines()
    if not snippet:
        return None
    normalized = [line.rstrip() for line in snippet]
    matches = []
    width = len(normalized)
    for start in range(0, max(0, len(lines) - width) + 1):
        if [line.rstrip() for line in lines[start:start + width]] == normalized:
            matches.append(start + 1)
    if len(matches) != 1:
        return None
    start = matches[0]
    return anchor.model_copy(update={
        "resolved_start_line": start, "resolved_end_line": start + width - 1,
        "resolved_side": side, "resolution_status": EvidenceResolutionStatus.exact,
        "match_count": 1,
        "anchor_hash": stable_hash({"file": anchor.file_path, "start": start,
                                    "content": "\n".join(snippet)}),
        "unresolved_reason": None,
    })


def carry_forward_findings(
    *,
    previous: ReviewTask,
    binding: IncrementalReviewBinding,
    plan_units: list[ReviewUnit],
    file_loader,
) -> list[dict]:
    """重新定位前序已确认问题，返回带 carried_forward 来源标记的记录。

    只读取调用方提供的固定快照正文；不加载目标仓库、不调用模型。
    未变更文件的内容与前序 Head 完全一致，问题仍保持 confirmed 并参与合并
    判断；但原 verification/publication 绑定旧快照，发布准入会扣留，必须
    重新核验后才能发布。
    """
    superseded_ids = {item.candidate_id for item in binding.superseded_findings}
    units = {unit.id: unit for unit in plan_units}
    carried: list[dict] = []
    for record in (previous.task_runtime_report or {}).get("findings") or []:
        if not isinstance(record, dict):
            continue
        issue_payload = record.get("issue")
        candidate_payload = record.get("candidate")
        if not isinstance(issue_payload, dict) or not isinstance(candidate_payload, dict):
            continue
        try:
            issue = ReviewIssue.model_validate(issue_payload)
            Candidate.model_validate(candidate_payload)
        except ValidationError:
            continue
        if record.get("status") != "confirmed" or issue.id in superseded_ids:
            continue
        unit = units.get(issue.review_unit_id)
        if unit is None:
            continue
        relocated = _relocate_anchor(issue.primary_evidence, file_loader=file_loader)
        if relocated is None:
            continue
        updated = issue.model_copy(update={
            "review_unit_id": unit.id, "primary_evidence": relocated,
            "resolved_location": _resolved_location(relocated),
            # 未变更文件在当前 Diff 中没有 hunk，无法行内定位，只能作为摘要呈现。
            "placement": CommentPlacement.summary, "status": IssueStatus.confirmed,
            "source_review_unit_ids": [unit.id], "source_issue_ids": [issue.id],
        })
        from app.services.issue_policy import IssuePolicyService

        check = IssuePolicyService().check(updated, [unit])
        if not check.passed:
            continue
        carried.append({
            **record, "status": IssueStatus.confirmed.value, "reason": None,
            "issue": updated.model_copy(update={"severity": check.normalized_severity}).model_dump(mode="json"),
            # 保留原 publication/verification：其快照绑定旧 Head，发布准入会因
            # snapshot_mismatch 扣留，必须先重新核验才能发布。
            "carried_forward": {
                "from_task_id": previous.id,
                "from_head_sha": binding.previous_head_sha,
                "original_candidate_id": issue.id,
                "requires_current_reverification": True,
            },
        })
    return carried


def _resolved_location(anchor: EvidenceAnchor):
    from app.models.review import ResolvedIssueLocation

    if (anchor.resolved_start_line is None or anchor.resolved_end_line is None
            or anchor.resolved_side is None):
        return None
    return ResolvedIssueLocation(file_path=anchor.file_path, start_line=anchor.resolved_start_line,
                                 end_line=anchor.resolved_end_line, side=anchor.resolved_side)
