"""只读审查结论协议；内容合格不代表已向远端发布。"""

from enum import StrEnum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from app.models.review_plan import FindingView


class ReviewOutcomeKind(StrEnum):
    CLEAN = "clean"
    FINDINGS = "findings"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CoverageKind(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class OutcomeReason(StrEnum):
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TERMINATION_INCOMPLETE = "termination_incomplete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SCOPE_INCOMPLETE = "scope_incomplete"
    SCOPE_EMPTY = "scope_empty"
    VERIFICATION_UNRESOLVED = "verification_unresolved"
    FINDING_STATUS_UNKNOWN = "finding_status_unknown"
    INVESTIGATION_INCOMPLETE = "investigation_incomplete"
    UNSCHEDULED_INVESTIGATION = "unscheduled_investigation"
    USAGE_INTEGRITY_UNKNOWN = "usage_integrity_unknown"
    SNAPSHOT_MISSING = "snapshot_missing"
    RUNTIME_REPORT_MISSING = "runtime_report_missing"
    RUNTIME_REPORT_INVALID = "runtime_report_invalid"
    UNSUPPORTED_LEGACY_PROJECTION = "unsupported_legacy_projection"
    HISTORICAL_DISCUSSION_UNRESOLVED = "historical_discussion_unresolved"


class HistoricalFindingView(BaseModel):
    discussion_id: str
    path: str | None = None
    status: Literal["still_present"] = "still_present"
    severity: Literal["critical", "high", "medium", "low", "unknown"] = "unknown"
    priority: Literal["fix_before_merge", "assess_improvement", "assess_impact"] = "assess_impact"
    affected_behavior: str | None = None
    recommendation: str | None = None
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    discussion_url: str | None = None


class HistoricalDiscussionOutcome(BaseModel):
    """历史 PR 讨论复核的独立结论；与当前 Diff 主审查分别判定。"""

    status: Literal["not_recorded", "completed", "partial", "timed_out", "unavailable"] = "not_recorded"
    collected: int = Field(default=0, ge=0)
    checked: int = Field(default=0, ge=0)
    unchecked: int = Field(default=0, ge=0)
    verified_defects: list[dict] = Field(default_factory=list)
    termination_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    affects_risk: bool = False
    findings: list[HistoricalFindingView] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_findings(self, handler):
        value = handler(self)
        if "findings" not in self.model_fields_set:
            value.pop("findings", None)
        return value


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 外层字段描述当前 Diff 主审查；历史讨论复核作为独立结果并列返回。
    kind: ReviewOutcomeKind
    coverage: CoverageKind
    confirmed_count: int = Field(default=0, ge=0)
    publishable_count: int = Field(default=0, ge=0)
    unresolved_count: int = Field(default=0, ge=0)
    examined_goals: int = Field(default=0, ge=0)
    total_goals: int = Field(default=0, ge=0)
    incomplete_investigations: int = Field(default=0, ge=0)
    reason_codes: list[OutcomeReason] = Field(default_factory=list)
    finding_conclusion: Literal["confirmed", "unresolved", "no_confirmed_findings", "unknown"] = "unknown"
    review_completeness: CoverageKind = CoverageKind.UNKNOWN
    runtime_reliability: Literal["reliable", "unreliable", "unknown"] = "unknown"
    dynamic_validation: Literal["not_executed", "passed", "failed", "independent_results", "unknown"] = "unknown"
    # 历史讨论复核结果明确与主审查并列；其超时或计量缺失不写入上方主审查字段。
    historical_discussion: HistoricalDiscussionOutcome = Field(default_factory=HistoricalDiscussionOutcome)

    @model_serializer(mode="wrap")
    def serialize_historical_discussion(self, handler):
        value = handler(self)
        # 没有讨论记录的任务保持旧计划字节一致；有记录时才并列输出。
        if self.historical_discussion.status == "not_recorded":
            value.pop("historical_discussion", None)
        return value


class DigestGoal(BaseModel):
    goal_id: str | None = None
    path: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    side: Literal["head", "base"] | None = None
    status: str = "unknown"
    reason: str | None = None


class DigestExcludedFile(BaseModel):
    path: str
    reason: str


class DigestInvestigation(BaseModel):
    work_item_id: str | None = None
    proposition: str
    answer: Literal["supported", "refuted", "unresolved"] = "unresolved"
    reason: str | None = None
    limitations: list[str] = Field(default_factory=list)
    requested_ranges: list[DigestGoal] = Field(default_factory=list)


class DigestIntent(BaseModel):
    author_target: str
    status: Literal["observed", "contradicted", "insufficient", "not_applicable", "not_checked"] = "not_checked"
    observations: list[dict] = Field(default_factory=list)


class DigestValidation(BaseModel):
    id: str
    source: str
    stage: str
    head_sha: str
    patch_sha: str | None = None
    status: str
    trusted: bool = False
    snapshot_matches: bool = False
    reason: str
    checks: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None


class MergeDecision(BaseModel):
    # 对外只生成三态；review_suggestions 仅为旧冻结计划的反序列化兼容。
    status: Literal["unavailable", "fix_blockers", "resolve_unknowns", "review_suggestions", "conditional_merge"] = "unavailable"
    label: str = "暂不能给出合并建议"
    advice: str = "本轮缺少有效审查事实，请重新审查后再判断是否合并。"
    blocking_finding_keys: list[str] = Field(default_factory=list)
    nonblocking_finding_keys: list[str] = Field(default_factory=list)
    unresolved_reasons: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    blocking_historical_keys: list[str] = Field(default_factory=list)
    nonblocking_historical_keys: list[str] = Field(default_factory=list)
    # 独立缺陷数按“当前问题—历史讨论”的严格重复关联去重；来源数量仍由上方键列表分别保留。
    blocking_independent_count: int | None = Field(default=None, ge=0)
    nonblocking_independent_count: int | None = Field(default=None, ge=0)
    deduplicated_historical_keys: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_history_keys(self, handler):
        value = handler(self)
        for key in ("blocking_historical_keys", "nonblocking_historical_keys", "blocking_independent_count",
                    "nonblocking_independent_count", "deduplicated_historical_keys"):
            if key not in self.model_fields_set:
                value.pop(key, None)
        return value


class ChangeUnderstanding(BaseModel):
    """由已绑定证据的检查记录归纳；不新增模型调用，也不宣称未核验行为。"""

    author_goal: str = "未记录明确的作者目标。"
    actual_changes: str = "未保存可归纳的实际变更。"
    alignment: str = "未保存逐目标的符合性核验记录，不能由覆盖率推断作者目标已经实现。"
    author_goal_source: Literal["summary", "description", "title", "unknown"] | None = None
    implementation_changes: str | None = None
    test_observations: str | None = None

    @model_serializer(mode="wrap")
    def serialize_editorial_fields(self, handler):
        value = handler(self)
        for key in ("author_goal_source", "implementation_changes", "test_observations"):
            if getattr(self, key) is None:
                value.pop(key, None)
        return value


class ReviewDigest(BaseModel):
    discussion_review_enabled: bool | None = None
    discussion_stats: dict | None = None
    discussion_rechecks: list[dict] = Field(default_factory=list)
    discussion_links: dict[str, str] = Field(default_factory=dict)
    pr_url: str | None = None
    author_goal: str | None = None
    author_context_status: Literal["complete", "truncated", "unknown", "not_recorded"] = "not_recorded"
    author_context_limitation: str | None = None
    actual_changes: list[str] = Field(default_factory=list)
    change_understanding: ChangeUnderstanding | None = None
    intent_alignment: str = "未保存独立的目标符合性核验记录，不能由覆盖率推断作者目标已经实现。"
    intent_assessments: list[DigestIntent] = Field(default_factory=list)
    findings: list[FindingView] = Field(default_factory=list)
    investigations_known: bool = False
    investigations: list[DigestInvestigation] = Field(default_factory=list)
    unresolved_leads: list[str] = Field(default_factory=list)
    execution_status: str | None = None
    examined_goals: list[DigestGoal] = Field(default_factory=list)
    incomplete_goals: list[DigestGoal] = Field(default_factory=list)
    reviewed_files: list[str] = Field(default_factory=list)
    excluded_files: list[DigestExcludedFile] = Field(default_factory=list)
    validation_state: Literal["not_executed", "passed", "failed", "independent_results", "unknown"] = "unknown"
    validations: list[DigestValidation] = Field(default_factory=list)
    merge_decision: MergeDecision = Field(default_factory=MergeDecision)
    checked_areas: list[Literal["head_source_ranges", "base_source_ranges", "file_changes"]] = Field(default_factory=list)
    termination_reason: str | None = None

    @model_serializer(mode="wrap")
    def serialize_discussion_choice(self, handler):
        value = handler(self)
        if self.discussion_review_enabled is None:
            value.pop("discussion_review_enabled", None)
        return value


class PublicationSnapshot(BaseModel):
    head_sha: str | None = None
    effective_base_sha: str | None = None


class PublicationAnchor(BaseModel):
    file_path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    side: Literal["head", "base"] | None = None
    hunk_id: str | None = None


class PublicationFinding(BaseModel):
    finding_key: str
    candidate_id: str
    severity: Literal["critical", "high", "medium", "low"]
    placement: Literal["inline", "summary"]
    anchor: PublicationAnchor
    title: str
    body_markdown: str
    # 新计划保留纯文本；缺失表示旧计划的 title 已经过 Markdown 转义。
    raw_title: str | None = None
    affected_behavior: str = ""
    failure_scenario: str = ""
    recommendation: str = ""
    category: str | None = None
    evidence_summary: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    default_selected: bool = True
    default_selection_reason: str | None = None
    discussion_urls: list[str] = Field(default_factory=list)
    discussion_matches: list[dict] = Field(default_factory=list)


class WithheldFinding(BaseModel):
    candidate_id: str
    reason_code: str


class ReviewSummaryDigest(BaseModel):
    """结果概览所需的轻量投影；不含逐条问题正文、目标明细或执行追踪。"""

    discussion_review_enabled: bool | None = None
    author_goal: str | None = None
    discussion_stats: dict | None = None
    author_context_status: Literal["complete", "truncated", "unknown", "not_recorded"] = "not_recorded"
    author_context_limitation: str | None = None
    change_understanding: ChangeUnderstanding | None = None
    actual_changes: list[str] = Field(default_factory=list)
    intent_alignment: str = ""
    merge_decision: MergeDecision = Field(default_factory=MergeDecision)
    excluded_files: list[DigestExcludedFile] = Field(default_factory=list)
    validation_state: Literal["not_executed", "passed", "failed", "independent_results", "unknown"] = "unknown"
    validations: list[DigestValidation] = Field(default_factory=list)
    checked_areas: list[Literal["head_source_ranges", "base_source_ranges", "file_changes"]] = Field(default_factory=list)
    termination_reason: str | None = None

    @model_serializer(mode="wrap")
    def serialize_discussion_choice(self, handler):
        value = handler(self)
        if self.discussion_review_enabled is None:
            value.pop("discussion_review_enabled", None)
        return value


class ReviewSummary(BaseModel):
    schema_version: Literal["review-summary-v1"] = "review-summary-v1"
    review_id: str
    snapshot: PublicationSnapshot
    outcome: ReviewOutcome
    digest: ReviewSummaryDigest = Field(default_factory=ReviewSummaryDigest)
    review_comment_markdown: str = ""
    withheld_findings: list[WithheldFinding] = Field(default_factory=list)
    content_eligible: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review-publication-v1"] = "review-publication-v1"
    review_id: str
    snapshot: PublicationSnapshot
    outcome: ReviewOutcome
    digest: ReviewDigest = Field(default_factory=ReviewDigest)
    summary_markdown: str = ""
    user_report_markdown: str = ""
    review_comment_markdown: str = ""
    inline_drafts: list[PublicationFinding] = Field(default_factory=list)
    summary_findings: list[PublicationFinding] = Field(default_factory=list)
    withheld_findings: list[WithheldFinding] = Field(default_factory=list)
    content_eligible: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class ReviewCommentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: Literal["compact", "full"] = "compact"
    exclude_resolved: bool = False
    include_discussed: bool = False
    finding_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def single_finding_scope(self):
        if self.finding_key is not None and self.exclude_resolved:
            raise ValueError("single finding selection cannot exclude resolved findings")
        return self


class ReviewCommentCheck(BaseModel):
    review_id: str
    status: Literal["current", "stale", "unknown"]
    reviewed_head_sha: str | None
    latest_head_sha: str | None = None
    checked_at: datetime
    review_comment_markdown: str
    # 稳定的正文，不含随检查时点变化的过期/未知快照附注。
    preview_markdown: str | None = None
    included_finding_keys: list[str] = Field(default_factory=list)
    omitted_finding_keys: list[str] = Field(default_factory=list)
    retained_blocking_keys: list[str] = Field(default_factory=list)
    detail: Literal["compact", "full"] = "compact"


class PublicationState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    STALE = "stale"


class PublicationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_head_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    publish_summary: Literal[True] = True
    finding_keys: list[str] = Field(default_factory=list, max_length=100)
    acknowledge_partial_review: bool = False


class PublicationItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    publication_id: str
    kind: Literal["summary", "inline"]
    finding_key: str
    fingerprint: str
    payload_hash: str
    state: Literal["pending", "publishing", "published", "failed", "uncertain", "summary_only"]
    github_comment_id: int | None = None
    github_review_id: int | None = None
    github_url: str | None = None
    last_error_code: str | None = None


class PublicationAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    review_id: str
    repository_id: int
    pr_number: int
    head_sha: str
    plan_hash: str
    selection_hash: str
    publisher_identity: str
    state: PublicationState
    requested_at: datetime
    completed_at: datetime | None = None
    last_error_code: str | None = None
    items: list[PublicationItem] = Field(default_factory=list)


class GitHubSummarySlot(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: int
    pr_number: int
    publisher_identity: str
    github_comment_id: int | None = None
    github_url: str | None = None
    latest_review_id: str | None = None
    latest_head_sha: str | None = None
    latest_order: str = ""
    last_payload_hash: str | None = None
    pending_publication_id: str | None = None
    revision: int = 0


class PublicationPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_keys: list[str] | None = Field(default=None, max_length=100)


class GitHubInlineLocation(BaseModel):
    path: str
    line: int = Field(ge=1)
    side: Literal["LEFT", "RIGHT"]


class PublicationInlineCounts(BaseModel):
    selected: int = Field(ge=0)
    ready: int = Field(ge=0)
    already_published: int = Field(ge=0)
    summary_fallback: int = Field(ge=0)
    blocked: int = Field(ge=0)


class PublicationInlinePreview(BaseModel):
    finding_key: str
    action: Literal["create", "already_published", "summary_fallback"]
    body_markdown: str
    location: GitHubInlineLocation | None = None


class PublicationPreflightResult(BaseModel):
    ready: bool
    review_id: str
    plan_hash: str
    head_sha: str
    head_sha_matches: bool
    pr_open: bool
    permissions_ok: bool
    summary_action: Literal["create", "update"]
    summary_comment_id: int | None = None
    finding_keys: list[str]
    inline: PublicationInlineCounts
    inline_previews: list[PublicationInlinePreview]
    summary_markdown: str
    requires_partial_acknowledgement: bool
    warnings: list[str]


class PublicationAccepted(BaseModel):
    publication_id: str
    state: PublicationState
    status_url: str
