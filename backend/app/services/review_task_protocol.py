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

    @staticmethod
    def change_context(goals, active, catalog):
        """固定规范 hunk 的双侧片段；分页只呈现当前 Head 范围对应行。"""
        goal_map = {goal.goal_id: goal for goal in goals}
        output = {}
        for identity in active:
            change = catalog[identity].get("canonical_change")
            if not change or change["goal_id"] not in goal_map:
                continue
            goal = goal_map[change["goal_id"]]
            cursor, lines = change["new_start"], []
            last_head = change["new_start"] + change["new_length"] - 1
            for line in change["lines"]:
                position = line["new_line_no"] if line["new_line_no"] is not None else min(cursor, last_head)
                if goal.start_line <= position <= goal.end_line:
                    lines.append(line)
                if line["new_line_no"] is not None:
                    cursor = line["new_line_no"] + 1
            output[change["goal_id"]] = {**change, "lines": lines,
                "visible_head_start": goal.start_line, "visible_head_end": goal.end_line,
                "source": "canonical_diff_context_not_source_anchor"}
        return [output[key] for key in sorted(output)]

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
            "readable_scope": {"hash": work_item.readable_scope_hash,
                "file_count": len(checker.readable_files),
                "discovery": "Use file_find or code_search to locate other files within the frozen scope."},
            "active_evidence_ids": sorted(active_evidence_ids),
            "active_evidence": [{"id": identity, **evidence_catalog[identity]}
                                for identity in sorted(active_evidence_ids)],
            "canonical_changes": self.change_context(goals, active_evidence_ids, evidence_catalog),
            "response": {"one_of": [RuntimeReadResponse.model_json_schema(),
                                      {"inspect": RuntimeSubmitResponse, "investigate": InvestigationSubmitResponse,
                                       "verify": VerificationSubmitResponse}[work_item.kind.value].model_json_schema()]},
            "read_example": {"kind": "read", "tool": "file_read",
                "arguments": {"file_path": selected[0]["target_ref"], "start_line": 1, "end_line": 40},
                "purpose": "Read the implementation needed for the current task"} if selected else None,
            "submit_example": ({"kind": "submit", "examinations": [{"target_id": selected[0]["goal_id"],
                "disposition": "examined", "evidence_ids": sorted(active_evidence_ids),
                "reason": "State the concrete behavior checked"}], "candidates": [], "leads": []}
                if selected and work_item.kind.value == "inspect" else None),
        }
        for evidence in payload["active_evidence"]:
            evidence.pop("canonical_change", None)
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return PreparedModelRequest(
            model=self.spec.model_name, operation="task_review_v1",
            system=("You review one server-selected task. Return exactly one JSON object: "
                     "kind=read for one controlled readonly tool, or kind=submit for results. "
                    "Do not change task identity, snapshot, goals, or completion state. "
                    "Use the read_example/submit_example as JSON shape examples, copying actual IDs from goals/evidence. "
                    "Never emit API transport fields such as type=json_object or response_format. "
                    "Return business fields directly at the top level without a wrapper. "
                    "Compare added/deleted lines in canonical_changes with Head source and necessary callers. "
                    "Look for concrete regressions in correctness, security and performance with a triggering scenario. "
                    "canonical_changes contains fixed diff context, not valid source evidence IDs. "
                    "Only mark examined after checking the entire requested range; otherwise report blocked."),
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
