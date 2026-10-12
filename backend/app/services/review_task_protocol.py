"""task_review_v1 的 prepare/send/parse 适配边界。

send 由现有 Provider 的 PreparedModelRequest 发送链负责；本模块只生成稳定
协议请求和解析服务端投影，避免新路径复用旧 review_step 提示词。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.review_runtime import (
    ReviewGoal, ReviewRunSpec, ReviewWorkItem, RuntimeReadResponse, RuntimeSubmitResponse,
    InvestigationSubmitResponse, VerificationSubmitResponse,
)
from app.services.model_request_budgeter import PreparedModelRequest
from app.services.review_task_executor import ParsedRuntimeResponse, ReviewTaskExecutor
from app.services.fingerprints import stable_hash


class PreparedSender(Protocol):
    async def __call__(self, request: PreparedModelRequest) -> Any: ...


@dataclass
class TaskReviewV1Protocol:
    spec: ReviewRunSpec

    protocol_name = "task-review-protocol-v1"

    def prepare(self, work_item: ReviewWorkItem, goals: list[ReviewGoal],
                *, active_evidence_ids: set[str], evidence_catalog: dict[str, dict],
                related_goals: list[ReviewGoal] | None = None) -> PreparedModelRequest:
        goal_map = {goal.goal_id: goal for goal in goals}
        selected = [goal_map[identity].model_dump(mode="json") for identity in work_item.goal_ids
                    if identity in goal_map]
        if len(selected) != len(work_item.goal_ids):
            raise ValueError("cannot prepare work item with unknown goal")
        checker = ReviewTaskExecutor(self.spec, work_item, [*goals, *(related_goals or [])],
            active_evidence_ids=active_evidence_ids, evidence_catalog=evidence_catalog, request_id="prepare")
        invalid = checker.evidence_error(list(active_evidence_ids))
        if invalid:
            raise ValueError(invalid)
        payload = {
            "protocol": self.protocol_name,
            "run_spec_hash": stable_hash(self.spec.model_dump(mode="json")),
            "engine": self.spec.engine_version,
            "policy_version": self.spec.policy_version,
            "diff_manifest_hash": self.spec.diff_manifest_hash,
            "task": {"run_id": self.spec.run_id, "work_item_id": work_item.work_item_id,
                      "kind": work_item.kind.value, "revision": work_item.revision},
            "snapshot": {"head_sha": self.spec.snapshot_head_sha,
                          "base_sha": self.spec.snapshot_base_sha},
            "goals": selected,
            "related_goals": [goal.model_dump(mode="json") for goal in related_goals or []],
            "proposition": work_item.proposition,
            "candidate": work_item.verification_candidate.model_dump(mode="json",
                exclude={"source_result_ids"}) if work_item.verification_candidate else None,
            "required_evidence_ids": work_item.required_evidence_ids,
            "counter_evidence_ids": work_item.counter_evidence_ids,
            "scope_files": work_item.scope_files,
            "active_evidence_ids": sorted(active_evidence_ids),
            "active_evidence": [{"id": identity, **evidence_catalog[identity]}
                                for identity in sorted(active_evidence_ids)],
            "response": {"one_of": [RuntimeReadResponse.model_json_schema(),
                                      {"inspect": RuntimeSubmitResponse, "investigate": InvestigationSubmitResponse,
                                       "verify": VerificationSubmitResponse}[work_item.kind.value].model_json_schema()]},
        }
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return PreparedModelRequest(
            model=self.spec.model_name, operation="task_review_v1",
            system=("You review one server-selected task. Return exactly one JSON object: "
                     "kind=read for one controlled readonly tool, or kind=submit for results. "
                    "Do not change task identity, snapshot, goals, or completion state."),
            prompt=prompt, output_tokens=4096,
        )

    def parse(self, executor: ReviewTaskExecutor, raw: Any) -> ParsedRuntimeResponse:
        return executor.parse_and_apply(raw)

    async def send_and_parse(
        self, executor: ReviewTaskExecutor, work_item: ReviewWorkItem,
        goals: list[ReviewGoal], *, active_evidence_ids: set[str], sender: PreparedSender,
    ) -> ParsedRuntimeResponse:
        """调用现有发送边界，再把供应商返回交给新协议解析器。

        sender 可以是真实 Provider 的 PreparedModelRequest 适配器或 SDK stub；
        本方法不自行重试、不绕过预算，也不把异常转换成合法提交。
        """
        request = self.prepare(work_item, goals, active_evidence_ids=active_evidence_ids,
                               evidence_catalog=executor.evidence_catalog)
        result = await sender(request)
        raw = getattr(result, "value", result)
        return self.parse(executor, raw)
