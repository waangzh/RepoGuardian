"""审查通用枚举与小模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal, TypeVar






class TaskStatus(str, Enum):
    """审查任务生命周期状态。"""
    # pending/running are retained only so in-memory tasks created by older
    # releases can still be read while clients migrate to the granular states.
    pending = "pending"
    running = "running"
    queued = "queued"
    planning = "planning"
    reviewing = "reviewing"
    resolving_evidence = "resolving_evidence"
    verifying_issues = "verifying_issues"
    generating_patches = "generating_patches"
    validating = "validating"
    waiting_for_human = "waiting_for_human"
    completed = "completed"
    completed_with_warnings = "completed_with_warnings"
    failed = "failed"
    cancelled = "cancelled"


class ReviewMode(str, Enum):
    """产品级审查模式；默认路径永远不执行目标仓库代码。"""

    review = "review"


class ReviewPhase(str, Enum):
    """审查图的受控阶段。"""

    prepare = "prepare"
    project_detection = "project_detection"
    baseline = "baseline"
    discovery = "discovery"
    verification = "verification"
    repair = "repair"
    validation = "validation"
    publishing = "publishing"
    completed = "completed"
    failed = "failed"


class ReviewUnitComplexity(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"


class ReviewUnitStatus(str, Enum):
    pending = "pending"
    planning = "planning"
    reviewing = "reviewing"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    needs_human = "needs_human"


class ReviewUnitTerminalReason(str, Enum):
    """Unit 结束原因；状态描述结果，原因描述为何结束。"""

    completed = "completed"
    no_issue = "no_issue"
    no_new_context = "no_new_context"
    model_budget_exhausted = "model_budget_exhausted"
    retrieval_budget_exhausted = "retrieval_budget_exhausted"
    diagnosis_budget_exhausted = "diagnosis_budget_exhausted"
    timed_out = "timed_out"
    provider_error = "provider_error"
    execution_error = "execution_error"
    human_required = "human_required"
    unsupported_change = "unsupported_change"


class ReviewFileStatus(str, Enum):
    unknown = "unknown"
    pending = "pending"
    reviewed = "reviewed"
    partial = "partial"
    excluded_binary = "excluded_binary"
    excluded_generated = "excluded_generated"
    excluded_sensitive = "excluded_sensitive"
    unsupported = "unsupported"
    timed_out = "timed_out"
    model_failed = "model_failed"
    budget_exhausted = "budget_exhausted"


class UnitPlanStatus(str, Enum):
    planned = "planned"
    skipped = "skipped"
    failed = "failed"


class StepStatus(str, Enum):
    """单个图节点的执行状态。"""
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class Severity(str, Enum):
    """问题严重性等级。"""
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IssueCategory(str, Enum):
    """问题分类。"""
    correctness = "correctness"
    maintainability = "maintainability"
    performance = "performance"
    security = "security"
    test = "test"


class IssueStatus(str, Enum):
    """问题从模型候选到发布前的服务端状态。"""

    candidate = "candidate"
    evidence_resolved = "evidence_resolved"
    confirmed = "confirmed"
    dismissed = "dismissed"
    unresolved = "unresolved"
    published = "published"

    @classmethod
    def _missing_(cls, value):
        return cls.unresolved if value == "needs_human" else None


class IssueVerificationDecision(str, Enum):
    """独立 verifier 只能缩小候选集合，不能创建新的问题。"""

    keep = "keep"
    drop = "drop"
    unresolved = "unresolved"

    @classmethod
    def _missing_(cls, value):
        return cls.unresolved if value == "needs_human" else None


class EvidenceResolutionMethod(str, Enum):
    diff_exact = "diff_exact"
    diff_normalized = "diff_normalized"
    file_exact = "file_exact"
    symbol_assisted = "symbol_assisted"
    unresolved = "unresolved"


class EvidenceResolutionStatus(str, Enum):
    exact = "exact"
    relocated = "relocated"
    symbol_resolved = "symbol_resolved"
    context_resolved = "context_resolved"
    unresolved = "unresolved"


class EvidenceProvenance(str, Enum):
    diff = "diff"
    repository_file = "repository_file"
    symbol_index = "symbol_index"


class CommentPlacement(str, Enum):
    inline = "inline"
    summary = "summary"
    suppressed = "suppressed"
    unresolved = "unresolved"

    @classmethod
    def _missing_(cls, value):
        return cls.unresolved if value == "needs_human" else None


class AgentActionName(str, Enum):
    """Agent 的结构化只读与流程控制动作；不包含代码执行能力。"""
    retrieve_context = "retrieve_context"
    file_read = "file_read"
    file_find = "file_find"
    code_search = "code_search"
    file_read_diff = "file_read_diff"
    review_code = "review_code"
    finish_report = "finish_report"
    request_human = "request_human"
    revise_patch = "revise_patch"
    accept_patch = "accept_patch"
    abandon_patch = "abandon_patch"
    report_issue = "report_issue"
    task_done = "task_done"


class RetrievalRelevanceType(str, Enum):
    """服务端支持的、可审计的上下文关联类型。"""

    direct = "direct"
    caller = "caller"
    callee = "callee"
    test = "test"
    module_config = "module_config"
    text = "text"
    adjacent = "adjacent"
    type_definition = "type_definition"
    import_source = "import_source"
    failure_location = "failure_location"


class FixRisk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


ModelCallValue = TypeVar("ModelCallValue")


def _default_review_mode() -> ReviewMode:
    """延迟读取配置，避免领域模型与 Settings 在模块导入时互相依赖。"""
    from app.core.config import settings

    return ReviewMode(settings.repoguardian_default_review_mode)


def _default_review_engine() -> Literal["task_review_v1"]:
    """只在创建请求构造时读取默认引擎，任务随后保存冻结值。"""
    from app.core.config import settings

    return settings.repoguardian_review_engine
