"""补丁与外部验证模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.core.paths import validate_repo_relative_path
from app.models.review.common import FailureKind, PatchApplyCheckStatus, PatchStatus, ValidationStage, ValidationStatus
from app.models.review.issues import FailureFingerprint, TestRunResult

class ValidationSnapshot(BaseModel):
    """Base、Head 或 Patched 阶段的一组受控验证结果。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    stage: ValidationStage
    sha: str = Field(min_length=1)
    patch_id: str | None = None
    command_results: list[TestRunResult] = Field(default_factory=list)
    collected_test_count: int | None = Field(default=None, ge=0)
    failure_fingerprints: list[FailureFingerprint] = Field(default_factory=list)
    passed: bool
    failure_kind: FailureKind | None = None
    failure_detail: str | None = None


class ValidationDelta(BaseModel):
    """两个验证快照的语义差异，用于区分既有失败与新增回归。"""

    from_stage: ValidationStage
    to_stage: ValidationStage
    patch_id: str | None = None
    previous_passed: bool
    current_passed: bool
    failure_kind: FailureKind | None = None
    introduced_failure: bool = False
    resolved_failure: bool = False
    introduced_failures: list[FailureFingerprint] = Field(default_factory=list)
    resolved_failures: list[FailureFingerprint] = Field(default_factory=list)


class ValidationCheck(BaseModel):
    """验证后端报告的单项结构化检查。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: ValidationStatus
    detail: str | None = None


class ValidationCapabilities(BaseModel):
    """由服务端声明的验证后端安全能力。"""

    model_config = ConfigDict(extra="forbid")

    available: bool
    supported_languages: list[str] = Field(default_factory=list)
    supported_profiles: list[str] = Field(default_factory=list)
    executes_untrusted_code: bool
    requires_user_configuration: bool
    unavailable_reason: str | None = None


class PatchValidationRequest(BaseModel):
    """传给服务端所选验证后端的不可变标识。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    patch_id: str
    repository_id: str
    base_sha: str
    head_sha: str
    patch_sha: str
    validation_profile: str | None = None
    repository_clone_url: str | None = None
    repository_fetch_ref: str | None = None
    patch_content: str | None = None
    is_fork: bool = False


class PatchValidationResult(BaseModel):
    """后端无关的补丁验证结果，也是 API 的公开结构。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    backend: str = Field(min_length=1)
    status: ValidationStatus
    head_sha: str
    patch_sha: str
    checks: list[ValidationCheck] = Field(default_factory=list)
    resolved_failures: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    environment_fingerprint: str | None = None
    trusted: bool
    trust_source: str | None = None
    runner_id: str | None = None
    validation_request_id: str | None = None
    profile: str | None = None
    exit_status: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    log_summary: str | None = None
    artifact_references: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ValidationResult(PatchValidationResult):
    """保留给 ReviewTask.validation 的兼容名称。"""

    patch_id: str | None = None


class PatchEligibilityDecision(BaseModel):
    """服务端对单个 confirmed Issue 作出的补丁生成准入结论。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    max_files: int = Field(ge=1)
    max_changed_lines: int = Field(ge=1)

    @field_validator("allowed_files")
    @classmethod
    def validate_allowed_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))


class PatchApplyCheck(BaseModel):
    """只描述 git apply 与隔离恢复结果，绝不表达功能正确性。"""

    model_config = ConfigDict(extra="forbid")

    status: PatchApplyCheckStatus = PatchApplyCheckStatus.not_checked
    detail: str = "尚未执行可应用性检查。"
    checked_head_sha: str | None = None
    worktree_clean: bool | None = None


class PatchPresentation(BaseModel):
    """面向 GitHub 或报告的候选补丁展示格式。"""

    model_config = ConfigDict(extra="forbid")

    inline_suggestion: str | None = None
    full_diff: str | None = None
    warning: str


class PatchProposal(BaseModel):
    """基于确定 Head、尚未执行项目测试的候选补丁。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=2_000)
    unified_diff: str = Field(min_length=1)
    touched_files: list[str] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    status: PatchStatus = PatchStatus.unverified
    head_sha: str = Field(min_length=1)
    patch_sha: str | None = None
    revision_of: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    validation_backend: str | None = None
    validation_result_id: str | None = None
    apply_check: PatchApplyCheck = Field(default_factory=PatchApplyCheck)
    presentation: PatchPresentation | None = None
    stale: bool = False

    # 兼容阶段 3 的本地验证结果读取；新候选流程不会写入该字段。
    validation_snapshot_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fields(cls, value: Any) -> Any:
        """只在领域边界迁移旧字段；新模型输出不经过此兼容入口。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_diff = data.pop("diff_content", None)
        legacy_issue = data.pop("issue_id", None)
        if "unified_diff" not in data and legacy_diff is not None:
            data["unified_diff"] = legacy_diff
        if "issue_ids" not in data and legacy_issue:
            data["issue_ids"] = [legacy_issue]
        if legacy_diff is not None:
            data.setdefault("issue_ids", ["legacy-unassigned"])
            data.setdefault("title", "候选修复")
            data.setdefault("rationale", "由阶段 3 兼容数据迁移。")
            data.setdefault("touched_files", ["legacy-unknown"])
            data.setdefault("risk", "low")
            data.setdefault("head_sha", "legacy-head")
        return data

    @field_validator("status", mode="before")
    @classmethod
    def migrate_legacy_status(cls, value: PatchStatus | str) -> PatchStatus | str:
        legacy_statuses = {
            "generated": PatchStatus.suggested,
            "applied": PatchStatus.validation_pending,
            "apply_failed": PatchStatus.abandoned,
            "validation_passed": PatchStatus.verified,
        }
        return legacy_statuses.get(value, value)

    @field_validator("issue_ids")
    @classmethod
    def validate_issue_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("issue_ids cannot be empty")
        return cleaned

    @field_validator("touched_files")
    @classmethod
    def validate_touched_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))

    @computed_field(return_type=str | None)
    @property
    def issue_id(self) -> str | None:
        """阶段 3 单 Issue 调用方的只读兼容属性。"""
        return self.issue_ids[0] if self.issue_ids else None

    @computed_field(return_type=str)
    @property
    def diff_content(self) -> str:
        """阶段 3 调用方的只读兼容属性。"""
        return self.unified_diff


class PatchResult(PatchProposal):
    """阶段 3 构造方式的兼容适配器；新代码应使用 PatchProposal。"""

    issue_ids: list[str] = Field(default_factory=lambda: ["legacy-unassigned"])
    title: str = "候选修复"
    rationale: str = "由兼容补丁生成接口创建。"
    unified_diff: str = ""
    touched_files: list[str] = Field(default_factory=lambda: ["legacy-unknown"])
    risk: Literal["low", "medium", "high"] = "low"
    head_sha: str = "legacy-head"
