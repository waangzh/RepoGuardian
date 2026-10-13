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
    PublicationVerificationSubmitResponse,
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
    def compact_schema(value):
        if isinstance(value, list):
            return [TaskReviewV1Protocol.compact_schema(item) for item in value]
        if isinstance(value, dict):
            return {key: TaskReviewV1Protocol.compact_schema(item) for key, item in value.items()
                    if key not in {"title", "description", "default"}}
        return value

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
            "run_spec_hash": stable_hash(self.spec.model_dump(mode="json", exclude={"prompt_version"})
                if self.spec.prompt_version == "task-review-prompt-v1" else self.spec.model_dump(mode="json")),
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
                                       "verify": (PublicationVerificationSubmitResponse
                                           if self.spec.publication_contract == "claim-publication-v1" else
                                           VerificationSubmitResponse)}[work_item.kind.value].model_json_schema()]},
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
        system = ("You review one server-selected task. Return exactly one JSON object: "
            "kind=read for one controlled readonly tool, or kind=submit for results. "
            "Do not change task identity, snapshot, goals, or completion state. "
            "Use the read_example/submit_example as JSON shape examples, copying actual IDs from goals/evidence. "
            "Never emit API transport fields such as type=json_object or response_format. "
            "Return business fields directly at the top level without a wrapper. "
            "Compare added/deleted lines in canonical_changes with Head source and necessary callers. "
            "Look for concrete regressions in correctness, security and performance with a triggering scenario. "
            "canonical_changes contains fixed diff context, not valid source evidence IDs. "
            "Only mark examined after checking the entire requested range; otherwise report blocked.")
        if self.spec.prompt_version in {"task-review-prompt-v2", "task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5"}:
            payload.pop("submit_example")
            if self.spec.prompt_version in {"task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5"} and selected and work_item.kind.value == "inspect":
                payload["submit_shape_example"] = {"kind": "submit", "examinations": [{
                    "target_id": selected[0]["goal_id"], "disposition": "blocked",
                    "evidence_ids": [], "reason": "<state the actual evidence gap; replace this example>"}],
                    "candidates": [{"claim": "<actual defect claim; omit this item if none>",
                        "primary_anchor": selected[0]["goal_id"], "evidence_ids": sorted(active_evidence_ids)[:1],
                        "affected_behavior": "<observable impact>", "failure_scenario": "<specific trigger and failure>",
                        "recommendation": "<specific repair>", "category": "correctness", "severity": "medium"}],
                    "leads": []}
            payload["prompt_version"] = self.spec.prompt_version
            payload["response"] = self.compact_schema(payload["response"])
            if self.spec.prompt_version in {"task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5"}:
                for schema in payload["response"]["one_of"]:
                    schema["required"] = list(dict.fromkeys([*schema.get("required", []), "kind"]))
                    definitions = schema.get("$defs", {})
                    for name, field in (("ExaminationSubmission", "target_id"), ("CandidateSubmission", "primary_anchor")):
                        if name in definitions:
                            definitions[name]["properties"][field]["enum"] = [goal.goal_id for goal in
                                [*goals, *(related_goals or [])]]
                    if self.spec.prompt_version in {"task-review-prompt-v4", "task-review-prompt-v5"}:
                        for name in ("ExaminationSubmission", "CandidateSubmission", "InvestigationSubmission", "VerificationSubmission", "PublicationVerificationSubmission"):
                            if name in definitions:
                                definitions[name]["required"] = list(dict.fromkeys([
                                    *definitions[name].get("required", []), "evidence_ids"]))
                                definitions[name]["properties"]["evidence_ids"]["items"]["enum"] = sorted(active_evidence_ids)
                payload["evidence_requirements"] = {
                    "examinations": "For examined, cite enough current source chunks to cover the entire goal range. "
                        "Citing only the defect location does not establish full-range examination. blocked may cite none.",
                    "candidates": "Cite the smallest visible source chunks establishing the concrete defect and necessary support.",
                    "verification": "For supported/refuted, include every required_evidence_id plus any necessary "
                        "new counterevidence read during verification. Otherwise submit insufficient_evidence.",
                    "required_evidence_ids": work_item.required_evidence_ids}
            payload["review_contract"] = {
                "inspect": "Find actionable defects introduced by the fixed changes; analyze the entire selected range. "
                    "Examinations and candidates are independent: submit both when an examined range contains a defect. "
                    "examined records work performed and does not mean defect-free. Report every concrete defect with "
                    "its actual triggering conditions, failure and visible evidence. The server will verify candidates.",
                "investigate": "Answer only the stated proposition from visible source and necessary counterevidence. "
                    "If the answer exposes a defect, also submit a candidate; refuted is a valid investigation outcome.",
                "verify": "Independently test the candidate's specific triggering conditions against source and counterevidence. "
                    "supported requires actual code support; insufficient_evidence is required when a necessary premise is unknown.",
            }[work_item.kind.value]
            system = ("You are a code review agent looking for actionable defects, not merely summarizing edits. "
                "Follow the server's review_contract for this task. Compare actual old and new operations, "
                "their values, control flow and downstream contracts under concrete inputs. "
                "A change's apparent intention, comments, names or tests are unverified claims; "
                "derive behavior from code rather than assuming the intended fix works. "
                "Use scoped readonly tools for missing caller/callee or contract evidence. "
                "Do not invent external contracts or manufacture style issues. "
                "No findings is allowed only when your analysis supports it; missing evidence stays blocked or unresolved. "
                "Return one direct JSON object matching either read or submit schema, without transport fields or wrappers. "
                "Never emit type=json_object or response_format. Do not change task identity, snapshot or goals. "
                "Use actual goal/evidence IDs; diff context is not a source evidence anchor. "
                "All explanatory fields must use Simplified Chinese; quote code verbatim.")
            if self.spec.prompt_version in {"task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5"}:
                system += (" The top-level discriminator is exactly kind, never type: use {\"kind\":\"read\",...} "
                    "or {\"kind\":\"submit\",...}. primary_anchor and target_id must equal an actual goal_id, "
                    "never a path, line number or code fragment. Examples describe JSON structure only; "
                    "replace explanatory placeholders and omit absent candidates.")
            if self.spec.prompt_version == "task-review-prompt-v5":
                system += " Do not emit fields outside the schema; omit optional fields instead of sending null values."
                for schema in payload["response"]["one_of"]:
                    definition = schema.get("$defs", {}).get("ExaminationSubmission")
                    if definition:
                        definition["properties"]["target_id"]["enum"] = [goal.goal_id for goal in goals]
                for change in payload["canonical_changes"]:
                    change["lines"] = [line for line in change["lines"] if line["kind"] != "context"]
                    change["presentation"] = "changed_lines_only_full_head_range_in_active_evidence"
                payload["review_contract"] += (
                    " Trace the exact branches selected by the stated trigger and the values passed to downstream code. "
                    "Distinguish guaranteed values from assumptions; check the meaning of operation arguments rather "
                    "than guessing from their spelling. Compare removed and added operations before drawing conclusions. "
                    "A nearby alternative branch is not evidence of what the triggered branch does. "
                    "Read missing local implementations or callers; if a necessary premise remains unknown, "
                    "report the evidence gap instead of inventing behavior.")
        if work_item.kind.value == "verify" and self.spec.publication_contract == "claim-publication-v1":
            from app.services.review_publication import verification_subject_hash
            payload["publication_contract"] = self.spec.publication_contract
            payload["verification_subject_hash"] = verification_subject_hash(work_item.verification_candidate, self.spec)
            payload["publication_rules"] = (
                "decision evaluates the original claim AND its original failure_scenario. Check version, triggered "
                "branch, value source, actual result and impact scope of the text to be displayed. retain is allowed "
                "only if all material original statements hold. narrow may only remove overstatement or narrow "
                "affected_behavior/recommendation; it cannot change the original claim or failure_scenario. "
                "If a core premise is false, use refuted + none; never replace it with a different true mechanism. "
                "An erroneous causal premise inside affected_behavior is still a core premise, not a removable detail. "
                "If necessary premises are unknown, use insufficient_evidence + none. Copy verification_subject_hash. "
                "A different mechanism may be submitted in candidates with its own evidence and triggering scenario; "
                "it is a NEW candidate and receives a separate verification, never inherits this decision. "
                "For narrow, provide the exact narrowed non-core text and a source-based basis; do not merely polish style.")
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return PreparedModelRequest(
            model=self.spec.model_name, operation="task_review_v1",
            system=system,
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
