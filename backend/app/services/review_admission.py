"""审查请求的结构化准入判定。

RequestFit 只回答请求能否被 Provider 表达，BudgetFit 只回答当前运行
资源是否允许发送。两者不能通过拆分请求来互相替代。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AdmissionKind(StrEnum):
    REQUEST_FIT = "request_fit"
    BUDGET = "budget"
    DEADLINE = "deadline"
    CONCURRENCY = "concurrency"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RequestFit:
    admitted: bool
    reason: str | None = None
    required_tokens: int = 0
    estimated_input_tokens: int = 0
    output_tokens: int = 0
    profile_source: str | None = None
    kind: AdmissionKind = AdmissionKind.REQUEST_FIT
    can_defer: bool = False
    can_split: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "kind": self.kind.value,
            "required_tokens": self.required_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "output_tokens": self.output_tokens,
            "profile_source": self.profile_source,
            "can_defer": self.can_defer,
            "can_split": self.can_split,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class BudgetFit:
    admitted: bool
    reason: str | None = None
    required_tokens: int = 0
    available_tokens: int | None = None
    required_calls: int = 0
    available_calls: int | None = None
    kind: AdmissionKind = AdmissionKind.BUDGET
    can_defer: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "kind": self.kind.value,
            "required_tokens": self.required_tokens,
            "available_tokens": self.available_tokens,
            "required_calls": self.required_calls,
            "available_calls": self.available_calls,
            "can_defer": self.can_defer,
            "details": dict(self.details),
        }


def budget_fit(budget: Any, *, token_usage: int = 0, model_calls: int = 0,
               diagnosis_attempts: int = 0, protected: dict[str, int] | None = None) -> BudgetFit:
    """检查累计资源；余额不足永远不触发内容拆分。"""

    protected = protected or {}
    available_tokens = max(0, int(budget.max_token_usage) - int(budget.token_usage)
                           - int(protected.get("token_usage", 0)))
    available_calls = max(0, int(budget.max_model_calls) - int(budget.model_calls)
                          - int(protected.get("model_calls", 0)))
    available_attempts = max(0, int(budget.max_diagnosis_attempts)
                             - int(budget.diagnosis_attempts)
                             - int(protected.get("diagnosis_attempts", 0)))
    if token_usage > available_tokens:
        return BudgetFit(False, "budget_tokens_insufficient", token_usage, available_tokens,
                         model_calls, available_calls, details={"available_diagnosis_attempts": available_attempts})
    if model_calls > available_calls:
        return BudgetFit(False, "budget_calls_insufficient", token_usage, available_tokens,
                         model_calls, available_calls, details={"available_diagnosis_attempts": available_attempts})
    if diagnosis_attempts > available_attempts:
        return BudgetFit(False, "budget_diagnosis_attempts_insufficient", token_usage, available_tokens,
                         model_calls, available_calls, details={"available_diagnosis_attempts": available_attempts})
    return BudgetFit(True, required_tokens=token_usage, available_tokens=available_tokens,
                     required_calls=model_calls, available_calls=available_calls,
                     details={"available_diagnosis_attempts": available_attempts})


def annotate_admission(metadata: dict[str, Any], *, admitted: bool,
                       reason: str | None = None, kind: AdmissionKind | str | None = None,
                       can_defer: bool | None = None, can_split: bool | None = None,
                       budget: BudgetFit | None = None, request_fit: RequestFit | None = None) -> dict[str, Any]:
    """保留旧 dict 字段，同时附加稳定的结构化准入投影。"""

    result = dict(metadata)
    resolved_kind = kind.value if isinstance(kind, AdmissionKind) else (kind or (
        budget.kind.value if budget else request_fit.kind.value if request_fit else AdmissionKind.UNKNOWN.value))
    result.update({
        "admitted": admitted,
        "reason": reason,
        "kind": resolved_kind,
        "can_defer": bool(can_defer if can_defer is not None else budget is not None),
        "can_split": bool(can_split if can_split is not None else request_fit is not None),
    })
    if budget is not None:
        result["budget_fit"] = budget.as_dict()
    if request_fit is not None:
        result["request_fit"] = request_fit.as_dict()
    return result
