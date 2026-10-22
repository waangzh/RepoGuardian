"""模型仅输出编辑文本和来源引用；快照、判定、用量由服务端写入。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SynthesisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SynthesisSource(SynthesisModel):
    id: str = Field(min_length=1, max_length=256)
    kind: Literal["author_claim", "diff_observation", "verified_observation",
                  "verified_finding", "test_source_observation", "review_boundary"]
    text: str = Field(min_length=1, max_length=6000)


class ReviewSynthesisInput(SynthesisModel):
    schema_version: Literal["review-synthesis-input-v1"] = "review-synthesis-input-v1"
    review_id: str
    head_sha: str
    effective_base_sha: str
    language: Literal["zh-CN"] = "zh-CN"
    sources: list[SynthesisSource] = Field(default_factory=list, max_length=200)
    finding_keys: list[str] = Field(default_factory=list, max_length=100)
    input_truncated: bool = False
    omitted_files: int = Field(default=0, ge=0)
    omitted_hunks: int = Field(default=0, ge=0)


class CitedText(SynthesisModel):
    text: str = Field(default="", max_length=1000)
    source_refs: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def require_sources(self):
        if self.text.strip() and not self.source_refs:
            raise ValueError("synthesis_reference_required")
        if not self.text.strip() and self.source_refs:
            raise ValueError("synthesis_empty_text_reference")
        return self


class SynthesisPurpose(CitedText):
    source_kind: Literal["author_claim", "code_inferred", "unknown"] = "unknown"


class FindingSummary(SynthesisModel):
    finding_key: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=240)


class ReviewSynthesisDraft(SynthesisModel):
    schema_version: Literal["review-synthesis-v1"] = "review-synthesis-v1"
    language: Literal["zh-CN"] = "zh-CN"
    pr_purpose: SynthesisPurpose = Field(default_factory=SynthesisPurpose)
    change_summary: CitedText
    key_changes: list[CitedText] = Field(default_factory=list, max_length=3)
    test_changes: list[CitedText] = Field(default_factory=list, max_length=3)
    finding_summaries: list[FindingSummary] = Field(default_factory=list, max_length=100)
    review_summary: CitedText = Field(default_factory=CitedText)


class ReviewSynthesisArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["review-synthesis-artifact-v1"] = "review-synthesis-artifact-v1"
    prompt_version: Literal["review-synthesis-prompt-v1"] = "review-synthesis-prompt-v1"
    source_policy_version: Literal["review-synthesis-sources-v1"] = "review-synthesis-sources-v1"
    review_id: str
    head_sha: str
    effective_base_sha: str
    fact_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    language: Literal["zh-CN"] = "zh-CN"
    model: str | None = None
    status: Literal["ready", "fallback", "unavailable"] = "fallback"
    synthesis: ReviewSynthesisDraft | None = None
    input_truncated: bool = False
    failure_code: str | None = None
    usage: dict | None = None
    budget: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def ready_requires_draft(self):
        if (self.status == "ready") != (self.synthesis is not None):
            raise ValueError("synthesis_status_mismatch")
        return self
