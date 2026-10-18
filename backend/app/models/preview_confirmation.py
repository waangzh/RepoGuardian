"""服务端私有确认凭据；不把展示字段当作可执行方案。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PreviewConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["review-confirmation-v1"] = "review-confirmation-v1"
    pr_fingerprint: Fingerprint
    configuration_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint
    response_fingerprint: Fingerprint
    request_fingerprint: Fingerprint
