"""任务聚合、结果与覆盖率模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
)

from app.models.review_plan import ReviewPlanOptions, PlanSnapshot
from app.models.review.common import (
    ReviewFileStatus, ReviewMode, ReviewPhase, ReviewUnitStatus, ReviewUnitTerminalReason, TaskStatus,
    UnitPlanStatus,
)
from app.models.review.diff import ChangedFile, ExcludedReviewFile, PullRequestInfo
from app.models.review.evidence import ContextSnippet, UnitActiveEvidenceSet, UnitBudgetRejection, UnitContextSelection, UnitCoverageLedger, UnitDiffBatch, UnitDiffManifest, UnitEvidenceStore
from app.models.review.issues import AgentEvent, CrossUnitCoordinationPlan, CrossUnitRiskAssessment, CrossUnitRuntimeMetrics, IssueMetrics, ProjectProfile, ReviewIssue, ReviewUnitToolEvent, UnitInputCoverage, UnitReviewPlan, UnitReviewSummary
from app.models.review.plan import ReviewUnit
from app.models.review.requests import ExecutionBudget, HumanReviewRequest, TaskStep
from app.models.review.usage import ModelUsage, ModelUsageSummary

class ReviewUnitResult(BaseModel):
    review_unit_id: str
    input_fingerprint: str | None = None
    status: ReviewUnitStatus = ReviewUnitStatus.pending
    unit_completion_status: Literal[
        "complete", "complete_with_unresolved_contracts", "incomplete", "unknown"
    ] = "unknown"
    terminal_reason: ReviewUnitTerminalReason | None = None
    plan_skipped: bool = False
    plan: UnitReviewPlan | None = None
    plan_status: UnitPlanStatus | None = None
    plan_skip_reason: str | None = None
    plan_error: str | None = None
    input_coverage: UnitInputCoverage | None = None
    diff_batches: list[UnitDiffBatch] = Field(default_factory=list)
    diff_manifest: UnitDiffManifest | None = None
    coverage_ledger: UnitCoverageLedger | None = None
    active_evidence_set: UnitActiveEvidenceSet | None = None
    evidence_store: UnitEvidenceStore | None = None
    context_selection: UnitContextSelection | None = None
    review_summary: UnitReviewSummary = Field(default_factory=UnitReviewSummary)
    issues: list[ReviewIssue] = Field(default_factory=list)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    messages: list[AgentEvent] = Field(default_factory=list)
    tool_events: list[ReviewUnitToolEvent] = Field(default_factory=list)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    model_usages: list[ModelUsage] = Field(default_factory=list)
    output_repair_attempted: bool = False
    error: str | None = None
    budget_rejection: UnitBudgetRejection | None = None
    human_request: HumanReviewRequest | None = None


class CrossUnitFollowupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    outcome: Literal["candidate_found", "refuted", "unresolved", "failed"]
    unit_result: ReviewUnitResult | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=1_000)
    fingerprint: str | None = None
    validation_status: Literal["pending", "completed"] = "pending"


class ReviewFileCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    eligible: bool
    status: ReviewFileStatus
    review_unit_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    omitted_targets: list[str] = Field(default_factory=list)


class ReviewUnitCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_unit_id: str
    files: list[str] = Field(default_factory=list)
    status: ReviewUnitStatus
    terminal_reason: ReviewUnitTerminalReason | None = None
    failure_reason: str | None = None
    input_coverage: UnitInputCoverage | None = None
    model_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class ReviewCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage_version: Literal["legacy-units-v1", "task-goals-v1"] = "legacy-units-v1"

    changed_files: int = Field(default=0, ge=0)
    eligible_files: int = Field(default=0, ge=0)
    reviewed_files: int = Field(default=0, ge=0)
    partial_files: int = Field(default=0, ge=0)
    skipped_files: int = Field(default=0, ge=0)
    failed_files: int = Field(default=0, ge=0)
    coverage_rate: float = Field(default=0.0, ge=0, le=1)
    completed_units: int = Field(default=0, ge=0)
    total_units: int = Field(default=0, ge=0)
    unit_coverage_rate: float = Field(default=0.0, ge=0, le=1)
    coordination_status: Literal[
        "unknown", "not_required", "not_run", "completed", "unresolved", "failed", "cancelled"
    ] = "unknown"
    coordination_reason: str | None = None
    # 旧记录缺少整体覆盖证据时保留未知，不能由文件覆盖率推断完成。
    review_complete: bool | None = None
    files: list[ReviewFileCoverage] = Field(default_factory=list)
    units: list[ReviewUnitCoverage] = Field(default_factory=list)


class ReviewRunManifest(BaseModel):
    """一次静态审查的可审计、稳定运行摘要。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review-run-manifest-v1"] = "review-run-manifest-v1"
    engine: Literal["legacy_units", "task_review_v1"] = "legacy_units"
    coverage_version: Literal["legacy-units-v1", "task-goals-v1"] = "legacy-units-v1"
    usage_integrity: Literal["complete", "unknown", "inconsistent"] = "unknown"
    review_id: str
    repository: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    base_sha: str | None = None
    effective_base_sha: str | None = None  # 内容基准（merge base）；base_sha 是 PR 身份
    diff_policy: str | None = None
    head_sha: str | None = None
    planner_version: str | None = None
    provider: str
    model: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=0, ge=0)
    confirmed_issues: int = Field(default=0, ge=0)
    coverage: ReviewCoverage
    coordination_metrics: CrossUnitRuntimeMetrics | None = None
    coordination_fingerprint: str | None = None
    # 增量审查绑定（若有）；缺失表示本轮按完整 PR 审查。
    incremental: dict | None = None
    warnings: list[str] = Field(default_factory=list)


class RepoSnapshot(BaseModel):
    """仓库概览快照（RepoIndexer 产出）。"""
    language: str
    languages: list[str] = Field(default_factory=list)
    language_counts: dict[str, int] = Field(default_factory=dict)
    is_mixed_language: bool = False
    framework: str | None = None
    test_framework: str | None = None
    total_files: int


class ReviewSummary(BaseModel):
    """API 中与 issues/patches/validation 并列的只读审查结果摘要。"""

    mode: ReviewMode = ReviewMode.review
    status: TaskStatus = TaskStatus.queued
    completed: bool = False


class ReviewTask(BaseModel):
    """审查任务聚合根，聚合所有阶段的产出，供前端完整展示。"""
    id: str
    status: TaskStatus = TaskStatus.queued
    phase: ReviewPhase = ReviewPhase.prepare
    pr_url: str
    model: str | None = None
    # 持久化快照中的值优先；缺少字段的旧任务按 legacy_units 恢复。
    engine: Literal["legacy_units", "task_review_v1"] = Field(default="legacy_units", frozen=True)
    task_runtime_configuration: dict | None = None
    task_runtime_report: dict | None = None
    # side_effects 独立冻结工件；不复制进主审查快照或公开聚合响应。
    review_synthesis: dict | None = Field(default=None, exclude=True)
    pr_discussion: dict | None = None
    task_runtime_retry_unit_id: str | None = None
    mode: ReviewMode = ReviewMode.review
    review: ReviewSummary = Field(default_factory=ReviewSummary)
    # 增量审查冻结绑定；None 表示本轮按完整 PR 审查。
    incremental: dict | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    pr: PullRequestInfo | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    review_unit_results: list[ReviewUnitResult] = Field(default_factory=list)
    cross_unit_risk: CrossUnitRiskAssessment | None = None
    coordination_plan: CrossUnitCoordinationPlan | None = None
    followup_results: list[CrossUnitFollowupResult] = Field(default_factory=list)
    model_usages: list[ModelUsage] = Field(default_factory=list)
    model_usage_summary: ModelUsageSummary = Field(default_factory=ModelUsageSummary)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    coverage: ReviewCoverage = Field(default_factory=ReviewCoverage)
    run_manifest: ReviewRunManifest | None = None
    issues: list[ReviewIssue] = Field(default_factory=list)
    issue_metrics: IssueMetrics = Field(default_factory=IssueMetrics)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    repo_snapshot: RepoSnapshot | None = None
    project_profile: ProjectProfile | None = None
    agent_events: list[AgentEvent] = Field(default_factory=list)
    human_request: HumanReviewRequest | None = None
    report_markdown: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def confirmed_plan(self) -> ReviewPlanOptions | None:
        value = (self.task_runtime_configuration or {}).get("effective_plan")
        return ReviewPlanOptions.model_validate(value) if value else None

    @computed_field
    @property
    def snapshot(self) -> PlanSnapshot | None:
        value = (self.task_runtime_configuration or {}).get("snapshot")
        return PlanSnapshot.model_validate(value) if value else None

    @computed_field
    @property
    def plan_hash(self) -> str | None:
        return (self.task_runtime_configuration or {}).get("plan_hash")
