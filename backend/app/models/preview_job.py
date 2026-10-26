"""预览作业协议；运行状态不代表正式审查或模型质量。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.review.contracts import ReviewPreviewRequest, ReviewPreviewResponse


class PreviewJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request: ReviewPreviewRequest


class PreviewStage(BaseModel):
    phase: str
    message: str
    started_at: datetime
    finished_at: datetime | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    operation: str | None = None


class PreviewJobResponse(BaseModel):
    id: str
    request: ReviewPreviewRequest
    status: Literal["queued", "running", "completed", "failed", "cancelled", "expired", "consumed"]
    stages: list[PreviewStage]
    cache_hit: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    error: str | None
    result: ReviewPreviewResponse | None = None
    consumed_task_id: str | None = None
