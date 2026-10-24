"""API 请求、步骤与检索请求模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.core.paths import validate_repo_relative_path
from app.models.review.common import (
    RetrievalRelevanceType, ReviewMode, StepStatus, TaskStatus, ValidationBackend,
    _default_review_engine, _default_review_mode, _default_validation_backend, _default_validation_profile,
)
from app.models.review_plan import ReviewPlanOptions, PlanSnapshot

class ContextRetrievalPlan(BaseModel):
    """模型提出、服务端归一化并按索引执行的上下文检索计划。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    target_files: list[str] = Field(default_factory=list, max_length=12)
    target_symbols: list[str] = Field(default_factory=list, max_length=12)
    search_terms: list[str] = Field(default_factory=list, max_length=8)
    relevance_types: list[RetrievalRelevanceType] = Field(min_length=1, max_length=10)
    include_callers: bool = False
    include_callees: bool = False
    include_tests: bool = False
    max_results: int = Field(default=12, ge=1, le=20)
    depth: int = Field(default=1, ge=0, le=2)

    @field_validator("target_files")
    @classmethod
    def validate_target_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))

    @field_validator("target_symbols")
    @classmethod
    def validate_target_symbols(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 500
               or not all(part.isidentifier() or part.replace("$", "_").isidentifier()
                          for part in value.split(".")) for value in values):
            raise ValueError("target symbols must be indexed symbol names")
        return list(dict.fromkeys(values))

    @field_validator("search_terms")
    @classmethod
    def validate_search_terms(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 120:
                raise ValueError("search terms must be short non-empty literals")
            if any(ord(char) < 32 for char in value):
                raise ValueError("search terms cannot contain control characters")
            cleaned.append(value.strip())
        return list(dict.fromkeys(cleaned))

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("max_results must be an integer")
        value = int(value)
        if value < 1:
            raise ValueError("max_results must be positive")
        return min(value, 20)

    @field_validator("depth", mode="before")
    @classmethod
    def clamp_depth(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("depth must be an integer")
        value = int(value)
        if value < 0:
            raise ValueError("depth must not be negative")
        return min(value, 2)

    @model_validator(mode="after")
    def require_a_safe_target(self) -> "ContextRetrievalPlan":
        if not (self.target_files or self.target_symbols or self.search_terms):
            raise ValueError("retrieval plan requires a file, symbol, or literal search term")
        return self


class FileReadRequest(BaseModel):
    """读取 Unit 已授权文件的有界行范围。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> "FileReadRequest":
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class FileFindRequest(BaseModel):
    """仅在 Unit readable_files 清单中执行路径字面量查找。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=120)
    max_results: int = Field(default=12, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("file_find query must not be blank")
        if any(ord(char) < 32 for char in cleaned):
            raise ValueError("file_find query cannot contain control characters")
        if "\\" in cleaned or cleaned.startswith(("/", "~")) or ".." in cleaned.split("/"):
            raise ValueError("file_find query must not contain path traversal")
        return cleaned


class CodeSearchRequest(BaseModel):
    """面向模型的最小仓库级字面量搜索请求。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=120)
    relation: Literal["text", "caller", "callee", "test", "type_definition"] = "text"
    max_results: int = Field(default=12, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(char) < 32 for char in cleaned):
            raise ValueError("code_search query must be a short literal")
        return cleaned


class FileReadDiffRequest(BaseModel):
    """读取 Unit 内已解析 diff，可选限制到已知 hunk_id。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    hunk_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @field_validator("hunk_ids")
    @classmethod
    def validate_hunk_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 100 or any(ord(char) < 32 for char in value) for value in values):
            raise ValueError("hunk_ids must contain short non-empty identifiers")
        return list(dict.fromkeys(values))


class HumanReviewRequest(BaseModel):
    """必须人工确认时向调用方暴露的最小结构化信息。"""

    model_config = ConfigDict(extra="forbid")

    missing_information: list[str] = Field(min_length=1, max_length=8)
    known_evidence: list[str] = Field(min_length=1, max_length=12)
    questions: list[str] = Field(min_length=1, max_length=8)
    prohibited_operations: list[str] = Field(min_length=1, max_length=8)

    @field_validator("missing_information", "known_evidence", "questions", "prohibited_operations")
    @classmethod
    def validate_items(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise ValueError("human review fields must contain short non-empty text")
        return list(dict.fromkeys(value.strip() for value in values))


class ExecutionBudget(BaseModel):
    """限制一次审查中可消耗的外部与模型资源。"""

    context_retrievals: int = Field(default=0, ge=0)
    max_context_retrievals: int = Field(default=2, ge=0)
    diagnosis_attempts: int = Field(default=0, ge=0)
    max_diagnosis_attempts: int = Field(default=1, ge=0)
    patch_attempts: int = Field(default=0, ge=0)
    max_patch_attempts: int = Field(default=3, ge=0)
    model_calls: int = Field(default=0, ge=0)
    max_model_calls: int = Field(default=6, ge=0)
    token_usage: int = Field(default=0, ge=0)
    max_token_usage: int = Field(default=24_000, ge=0)

    def can_consume(self, **amounts: int) -> bool:
        """检查一组预算消耗是否仍在上限内。"""
        for name, amount in amounts.items():
            if amount < 0:
                raise ValueError("budget consumption must not be negative")
            limit_name = f"max_{name}"
            if not hasattr(self, name) or not hasattr(self, limit_name):
                raise ValueError(f"unsupported budget metric: {name}")
            if getattr(self, name) + amount > getattr(self, limit_name):
                return False
        return True

    def consume(self, **amounts: int) -> "ExecutionBudget":
        """返回已消耗预算的新实例；超限时拒绝执行。"""
        if not self.can_consume(**amounts):
            raise ValueError("execution budget exhausted")
        return self.model_copy(
            update={name: getattr(self, name) + amount for name, amount in amounts.items()}
        )


class ReviewCreateRequest(BaseModel):
    """POST /api/reviews 请求体。"""
    pr_url: HttpUrl
    model: str | None = None
    force_full_review: bool = False
    preview_id: str | None = Field(default=None, min_length=1, max_length=64)
    plan_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_effective_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    engine: Literal["task_review_v1"] = Field(default_factory=_default_review_engine)
    mode: ReviewMode = Field(default_factory=_default_review_mode)
    validation_backend: ValidationBackend = Field(default_factory=_default_validation_backend)
    validation_profile: str = Field(default_factory=_default_validation_profile)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ReviewCreateRequest":
        confirmation = [self.preview_id, self.plan_hash, self.expected_head_sha, self.expected_effective_base_sha]
        if any(confirmation) and not all(confirmation):
            raise ValueError("preview confirmation requires preview_id, plan_hash and both expected SHAs")
        if self.validation_backend != ValidationBackend.none:
            raise ValueError("task_review_v1 only supports read-only inspect tasks")
        if self.mode == ReviewMode.review:
            self.validation_backend = ValidationBackend.none
        elif self.mode == ReviewMode.review_and_suggest:
            self.validation_backend = ValidationBackend.none
        elif self.validation_backend == ValidationBackend.user_runner:
            from app.core.config import registered_runner_profiles

            if self.validation_profile not in registered_runner_profiles():
                raise ValueError("validation_profile is not registered by server policy")
        return self


class ReviewCreateResponse(BaseModel):
    """POST /api/reviews 响应体。"""
    task_id: str
    status: TaskStatus
    confirmed_plan: ReviewPlanOptions | None = None
    snapshot: PlanSnapshot | None = None
    plan_hash: str | None = None


class ReviewPreviewRequest(BaseModel):
    """仅执行确定性 PR 分析，不创建任务或调用模型。"""

    pr_url: HttpUrl
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reuse_review_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    force_full_review: bool = False
    plan: ReviewPlanOptions = Field(default_factory=ReviewPlanOptions)
    engine: Literal["task_review_v1"] = Field(default_factory=_default_review_engine)
    mode: ReviewMode = Field(default_factory=_default_review_mode)
    validation_backend: ValidationBackend = Field(default_factory=_default_validation_backend)
    validation_profile: str = Field(default_factory=_default_validation_profile)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ReviewPreviewRequest":
        if self.force_full_review and self.reuse_review_id:
            raise ValueError("full review cannot specify a reuse source")
        normalized = ReviewCreateRequest(
            pr_url=self.pr_url,
            engine=self.engine,
            mode=self.mode,
            validation_backend=self.validation_backend,
            validation_profile=self.validation_profile,
        )
        self.validation_backend = normalized.validation_backend
        self.validation_profile = normalized.validation_profile
        return self


class TaskStep(BaseModel):
    """图节点执行步骤记录。"""
    name: str
    status: StepStatus = StepStatus.pending
    message: str | None = None
    progress: "TaskStepProgress | None" = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class TaskStepProgress(BaseModel):
    """长时间运行步骤的结构化局部进度；percent 为空时表示不可确定进度。"""

    phase: str
    operation: str | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=200)
