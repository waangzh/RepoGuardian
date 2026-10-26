from typing import Literal
from pydantic import BaseModel, Field


class IncrementalWebhookReceipt(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=128)
    status: Literal["accepted", "skipped", "completed", "failed"]
    reason: str | None = Field(default=None, max_length=120)
    task_id: str | None = Field(default=None, max_length=32)
