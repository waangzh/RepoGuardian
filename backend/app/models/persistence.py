"""阶段 6A 的持久化、分页和人工恢复 API 模型。"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.review import ReviewIssue, ReviewTask, ReviewUnit, ReviewUnitResult


class HumanRequestStatus(str, Enum):
    pending = "pending"
    answered = "answered"
    expired = "expired"
    cancelled = "cancelled"


class HumanRequestOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=1000)


class HumanRequestAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str | None = Field(default=None, min_length=1, max_length=128)
    text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_answer(self) -> "HumanRequestAnswer":
        if not self.option_id and not (self.text and self.text.strip()):
            raise ValueError("option_id or text is required")
        return self


class HumanRequestDetail(BaseModel):
    request_id: str
    task_id: str
    reason: str
    question: str
    options: list[HumanRequestOption] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)
    status: HumanRequestStatus
    answer: HumanRequestAnswer | None = None
    answered_by: str | None = None
    deadline: datetime
    created_at: datetime
    answered_at: datetime | None = None


class HumanRequestAnswerResponse(BaseModel):
    request: HumanRequestDetail
    idempotent_replay: bool = False


class ReviewTaskListResponse(BaseModel):
    items: list[ReviewTask]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class ReviewUnitDetail(BaseModel):
    unit: ReviewUnit
    result: ReviewUnitResult | None = None
    attempts: int = Field(ge=0)
    reused_from_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str | None = None


class ReviewIssueDetail(BaseModel):
    issue: ReviewIssue
    evidence_resolution: dict | None = None
    verifier_decision: dict | None = None
    dedup_source: dict | None = None
    publication_status: str


class PatchDetail(BaseModel):
    patch: dict
    diff_artifact_uri: str | None = None
    fingerprint: str
    revision: int
    policy_result: dict | None = None


class ValidationDetail(BaseModel):
    validation_id: str
    task_id: str
    patch_id: str
    backend: str
    request: dict
    result: dict | None = None
    environment_fingerprint: str
    trust_source: str | None = None
    patch_hash: str
    fingerprint: str
    status: str
    requested_at: datetime
    completed_at: datetime | None = None
