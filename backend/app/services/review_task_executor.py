"""task_review_v1 单任务推进、局部校验和结果投影。

该执行器只处理已冻结的 WorkItem。它不创建私有队列、不调用旧
ReviewUnitExecutor，也不让模型修改目标身份、快照或任务完成状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.models.review_runtime import (
    Candidate,
    CandidateSubmission,
    ExaminationResult,
    ExaminationSubmission,
    InvestigationSubmission,
    LeadSubmission,
    ProposedLead,
    ReviewGoal,
    ReviewRunSpec,
    ReviewWorkItem,
    RuntimeReadResponse,
    RuntimeSubmitResponse,
)
from app.services.fingerprints import stable_hash
from app.review.tool_scope import is_sensitive_repository_path


@dataclass(frozen=True)
class RuntimeParseError:
    item_type: str
    index: int | None
    reason: str


@dataclass
class ParsedRuntimeResponse:
    response: RuntimeReadResponse | RuntimeSubmitResponse | None
    errors: list[RuntimeParseError] = field(default_factory=list)
    item_indices: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class TaskProjection:
    examinations: dict[str, ExaminationResult] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    leads: list[ProposedLead] = field(default_factory=list)
    investigation: InvestigationSubmission | None = None
    accepted_target_ids: set[str] = field(default_factory=set)
    pending_target_ids: set[str] = field(default_factory=set)


class RuntimeResponseParser:
    """先验收顶层判别字段，再对 submit 的兄弟项逐项验收。"""

    _read_adapter = TypeAdapter(RuntimeReadResponse)

    def parse(self, raw: Any) -> ParsedRuntimeResponse:
        if isinstance(raw, str):
            def unique_object(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("duplicate_json_key")
                    obj[key] = value
                return obj

            try:
                raw = json.loads(raw, object_pairs_hook=unique_object)
            except (ValueError, TypeError):
                return ParsedRuntimeResponse(None, [RuntimeParseError("top_level", None, "invalid_json")])
        if not isinstance(raw, dict) or raw.get("kind") not in ("read", "submit"):
            return ParsedRuntimeResponse(None, [RuntimeParseError("top_level", None, "invalid_response_kind")])
        if raw["kind"] == "read":
            try:
                return ParsedRuntimeResponse(self._read_adapter.validate_python(raw))
            except ValidationError as exc:
                return ParsedRuntimeResponse(None, [RuntimeParseError("read", None, str(exc))])
        return self._parse_submit(raw)

    def _parse_submit(self, raw: dict[str, Any]) -> ParsedRuntimeResponse:
        if set(raw) - {"kind", "examinations", "candidates", "leads"}:
            return ParsedRuntimeResponse(None, [RuntimeParseError("top_level", None, "unknown_submit_field")])
        errors: list[RuntimeParseError] = []
        values: dict[str, Any] = {"kind": "submit"}
        item_indices = {}
        schemas = {
            "examinations": ExaminationSubmission,
            "candidates": CandidateSubmission,
            "leads": LeadSubmission,
        }
        for item_field, schema in schemas.items():
            raw_items = raw.get(item_field, [])
            if not isinstance(raw_items, list):
                errors.append(RuntimeParseError(item_field, None, "field_must_be_array"))
                values[item_field] = []
                continue
            values[item_field] = []
            item_indices[item_field] = []
            limit = RuntimeSubmitResponse.model_fields[item_field].metadata[0].max_length
            if len(raw_items) > limit:
                errors.append(RuntimeParseError(item_field, None, "array_limit_exceeded"))
                continue
            for index, item in enumerate(raw_items):
                try:
                    values[item_field].append(schema.model_validate(item))
                    item_indices[item_field].append(index)
                except ValidationError as exc:
                    errors.append(RuntimeParseError(item_field, index, str(exc)))
        try:
            response = RuntimeSubmitResponse.model_validate(values)
        except ValidationError as exc:
            errors.append(RuntimeParseError("submit", None, str(exc)))
            response = None
        return ParsedRuntimeResponse(response, errors, item_indices)


class ReviewTaskExecutor:
    """P1 的单步服务端投影器；外部调度器每次只调用一个动作。"""

    def __init__(self, spec: ReviewRunSpec, work_item: ReviewWorkItem,
                 goals: list[ReviewGoal], *, active_evidence_ids: set[str],
                 request_id: str, evidence_catalog: dict[str, dict], attempt_id: str = "first"):
        if work_item.kind.value != "inspect":
            raise ValueError("P1 executor requires inspect work item")
        if any(goal.kind != work_item.kind for goal in goals if goal.goal_id in work_item.goal_ids):
            raise ValueError("work item goal kind mismatch")
        goal_map = {goal.goal_id: goal for goal in goals}
        if len(goal_map) != len(goals):
            raise ValueError("duplicate goal identity")
        if set(work_item.goal_ids) - set(goal_map):
            raise ValueError("work item references unknown goal")
        self.spec = spec
        self.work_item = work_item
        self.goals = {identity: goal_map[identity] for identity in work_item.goal_ids}
        self.active_evidence_ids = set(active_evidence_ids)
        self.evidence_catalog = evidence_catalog
        self.request_id = request_id
        self.attempt_id = attempt_id
        self.parser = RuntimeResponseParser()
        self.projection = TaskProjection(pending_target_ids=set(work_item.goal_ids))

    def parse_and_apply(self, raw: Any) -> ParsedRuntimeResponse:
        parsed = self.parser.parse(raw)
        if parsed.response is None:
            return parsed
        if isinstance(parsed.response, RuntimeReadResponse):
            path = parsed.response.arguments.file_path
            if path not in self.work_item.scope_files or is_sensitive_repository_path(path):
                parsed.errors.append(RuntimeParseError("read", None, "read_path_outside_work_item_scope"))
                parsed.response = None
            return parsed
        self._apply_submit(parsed.response, parsed.errors, parsed.item_indices)
        return parsed

    def _apply_submit(self, response: RuntimeSubmitResponse,
                      errors: list[RuntimeParseError], item_indices: dict[str, list[int]]) -> None:
        grouped = {}
        for item in response.examinations:
            grouped.setdefault(item.target_id, []).append(item)
        conflicts = {identity for identity, items in grouped.items()
                     if len({stable_hash(item.model_dump(mode="json")) for item in items}) > 1}
        valid_examinations: list[ExaminationSubmission] = []
        for index, item in enumerate(response.examinations):
            if item.target_id in conflicts:
                errors.append(RuntimeParseError("examinations", index, "conflicting_duplicate_target"))
                continue
            if item.target_id not in self.goals:
                errors.append(RuntimeParseError("examinations", index, "unknown_target_id"))
                continue
            evidence_error = self.evidence_error(item.evidence_ids)
            if evidence_error:
                errors.append(RuntimeParseError("examinations", index, evidence_error))
                continue
            goal = self.goals[item.target_id]
            if item.disposition.value == "examined" and not self._covers(goal, item.evidence_ids):
                errors.append(RuntimeParseError("examinations", index, "incomplete_goal_evidence"))
                continue
            valid_examinations.append(item)
        grouped = {}
        for item in valid_examinations:
            grouped.setdefault(item.target_id, []).append(item)
        for target_id, items in grouped.items():
            item = items[0]
            if target_id in self.projection.examinations:
                continue  # 历史合法结果保留，不要求模型重写或后写覆盖。
            result = ExaminationResult(
                result_id=self._item_identity("result", target_id),
                goal_id=target_id, request_id=self.request_id,
                snapshot_head_sha=self.spec.snapshot_head_sha,
                disposition=item.disposition, evidence_ids=item.evidence_ids, reason=item.reason,
            )
            self.projection.examinations[target_id] = result
            self.projection.pending_target_ids.discard(target_id)
            self.projection.accepted_target_ids.add(target_id)
        for index, item in enumerate(response.candidates):
            index = item_indices.get("candidates", list(range(len(response.candidates))))[index]
            if item.primary_anchor not in self.goals:
                errors.append(RuntimeParseError("candidates", index, "unknown_primary_anchor"))
                continue
            evidence_error = self.evidence_error(item.evidence_ids)
            if evidence_error:
                errors.append(RuntimeParseError("candidates", index, evidence_error))
                continue
            goal = self.goals[item.primary_anchor]
            if not any(self.evidence_catalog[e]["file_path"] == goal.target_ref for e in item.evidence_ids):
                errors.append(RuntimeParseError("candidates", index, "anchor_evidence_mismatch"))
                continue
            candidate_id = self._item_identity("candidate", str(index))
            self.projection.candidates[candidate_id] = Candidate(
                candidate_id=candidate_id, claim=item.claim,
                primary_anchor=item.primary_anchor,
                supporting_evidence=item.evidence_ids, source_result_ids=[self.request_id],
            )
        for index, item in enumerate(response.leads):
            index = item_indices.get("leads", list(range(len(response.leads))))[index]
            if set(item.related_target_ids) - set(self.goals):
                errors.append(RuntimeParseError("leads", index, "unknown_related_target"))
                continue
            lead_id = self._item_identity("lead", str(index))
            if any(lead.lead_id == lead_id for lead in self.projection.leads):
                continue
            self.projection.leads.append(ProposedLead(
                lead_id=lead_id, proposition=item.proposition,
                related_targets=item.related_target_ids,
                missing_evidence=item.missing_evidence, origin_result_id=self.request_id,
            ))

    def _item_identity(self, kind: str, index: str) -> str:
        return kind + "-" + stable_hash([
            self.spec.run_id, self.work_item.work_item_id, self.attempt_id, kind, index,
        ])[:32]

    def evidence_error(self, identities: list[str]) -> str | None:
        for identity in identities:
            if identity not in self.active_evidence_ids or identity not in self.evidence_catalog:
                return "unknown_or_inactive_evidence"
            evidence = self.evidence_catalog[identity]
            if (evidence.get("head_sha") != self.spec.snapshot_head_sha
                    or evidence.get("base_sha") != self.spec.snapshot_base_sha):
                return "stale_evidence_snapshot"
            if (evidence.get("file_path") not in self.work_item.scope_files
                    or is_sensitive_repository_path(evidence.get("file_path", ""))):
                return "evidence_outside_scope"
            content = evidence.get("content")
            if (not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest()
                    != evidence.get("content_hash")):
                return "evidence_content_hash_mismatch"
            start, end = evidence.get("start_line"), evidence.get("end_line")
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 1 or end < start or end - start + 1 != len(content.splitlines())):
                return "invalid_evidence_source_range"
        return None

    def _covers(self, goal: ReviewGoal, identities: list[str]) -> bool:
        if goal.target_kind != "source_range":
            return False  # metadata/impact 覆盖在规划器接入后显式计算。
        ranges = sorted((self.evidence_catalog[e]["start_line"],
                         self.evidence_catalog[e]["end_line"])
                        for e in identities if self.evidence_catalog[e]["file_path"] == goal.target_ref)
        cursor = goal.start_line
        for start, end in ranges:
            if start > cursor:
                break
            cursor = max(cursor, end + 1)
        return cursor > goal.end_line
