"""增量审查的冻结绑定、预览投影与 webhook 回执。

绑定只保存可复算的身份与来源，不保存模型结论正文；前序结论在运行时
从持久化任务重新加载并按当前快照重新校验。
"""

from typing import Literal

from pydantic import Field

from app.models.review_plan import PlanModel


class IncrementalReusedUnit(PlanModel):
    unit_id: str = Field(min_length=1, max_length=160)
    source_task_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class IncrementalSupersededFinding(PlanModel):
    candidate_id: str = Field(min_length=1, max_length=160)
    file_path: str | None = Field(default=None, max_length=1_000)
    reason: Literal[
        "file_changed_since_previous_review",
        "base_side_evidence_not_reused",
        "review_unit_not_reused",
        "source_finding_missing",
    ]


class IncrementalReviewBinding(PlanModel):
    """随任务冻结；运行时不重新判断是否采用增量，只复核身份未变。"""

    version: Literal["incremental-review-v1"] = "incremental-review-v1"
    previous_task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    previous_head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    review_base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    effective_base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    new_head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    diff_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    delta_paths: list[str] = Field(default_factory=list, max_length=2_000)
    delta_file_count: int = Field(ge=0)
    pr_file_count: int = Field(ge=0)
    reused_units: list[IncrementalReusedUnit] = Field(default_factory=list, max_length=500)
    superseded_findings: list[IncrementalSupersededFinding] = Field(default_factory=list, max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class IncrementalPreview(PlanModel):
    """预览与创建确认使用的轻量投影；不含可执行权限或模型结论。"""

    status: Literal["applied", "not_applicable"] = "not_applicable"
    previous_task_id: str | None = Field(default=None, max_length=32)
    previous_head_sha: str | None = Field(default=None, max_length=64)
    new_head_sha: str | None = Field(default=None, max_length=64)
    review_base_sha: str | None = Field(default=None, max_length=64)
    delta_file_count: int = Field(default=0, ge=0)
    full_changed_file_count: int = Field(default=0, ge=0)
    reused_unit_count: int = Field(default=0, ge=0)
    reused_file_count: int = Field(default=0, ge=0)
    superseded_finding_count: int = Field(default=0, ge=0)
    reason: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class IncrementalWebhookReceipt(PlanModel):
    delivery_id: str = Field(min_length=1, max_length=128)
    status: Literal["accepted", "skipped", "completed", "failed"]
    reason: str | None = Field(default=None, max_length=120)
    task_id: str | None = Field(default=None, max_length=32)
