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
    InvestigationResult, InvestigationSubmitResponse,
    LeadSubmission,
    ProposedLead,
    ReviewGoal,
    ReviewRunSpec,
    ReviewWorkItem,
    RuntimeReadResponse,
    RuntimeSubmitResponse,
    VerificationSubmission, VerificationResult, VerificationSubmitResponse,
    PublicationVerificationSubmission, PublicationVerificationSubmitResponse,
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
    response: RuntimeReadResponse | RuntimeSubmitResponse | InvestigationSubmitResponse | VerificationSubmitResponse | None
    errors: list[RuntimeParseError] = field(default_factory=list)
    item_indices: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class TaskProjection:
    examinations: dict[str, ExaminationResult] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    leads: list[ProposedLead] = field(default_factory=list)
    investigation: InvestigationResult | None = None
    verification: VerificationResult | None = None
    accepted_target_ids: set[str] = field(default_factory=set)
    pending_target_ids: set[str] = field(default_factory=set)
    invalidated_results: list[dict] = field(default_factory=list)


class RuntimeResponseParser:
    """先验收顶层判别字段，再对 submit 的兄弟项逐项验收。"""

    _read_adapter = TypeAdapter(RuntimeReadResponse)

    def parse(self, raw: Any, *, task_kind: str = "inspect", publication_contract: str = "legacy") -> ParsedRuntimeResponse:
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
        return self._parse_submit(raw, task_kind, publication_contract)

    def _parse_submit(self, raw: dict[str, Any], task_kind: str, publication_contract: str) -> ParsedRuntimeResponse:
        schemas = {"inspect": RuntimeSubmitResponse, "investigate": InvestigationSubmitResponse,
                   "verify": VerificationSubmitResponse}
        response_schema = schemas[task_kind]
        if task_kind == "verify" and publication_contract == "claim-publication-v1":
            response_schema = PublicationVerificationSubmitResponse
        if set(raw) - set(response_schema.model_fields):
            return ParsedRuntimeResponse(None, [RuntimeParseError("top_level", None, "unknown_submit_field")])
        errors: list[RuntimeParseError] = []
        values: dict[str, Any] = {"kind": "submit"}
        item_indices = {}
        component_schemas = {
            "examinations": ExaminationSubmission,
            "candidates": CandidateSubmission,
            "leads": LeadSubmission,
        }
        for item_field, schema in component_schemas.items():
            if item_field not in response_schema.model_fields:
                continue
            raw_items = raw.get(item_field, [])
            if not isinstance(raw_items, list):
                errors.append(RuntimeParseError(item_field, None, "field_must_be_array"))
                values[item_field] = []
                continue
            values[item_field] = []
            item_indices[item_field] = []
            limit = response_schema.model_fields[item_field].metadata[0].max_length
            if len(raw_items) > limit:
                errors.append(RuntimeParseError(item_field, None, "array_limit_exceeded"))
                continue
            for index, item in enumerate(raw_items):
                try:
                    values[item_field].append(schema.model_validate(item))
                    item_indices[item_field].append(index)
                except ValidationError as exc:
                    errors.append(RuntimeParseError(item_field, index, str(exc)))
        # 命题答案失败不丢掉候选兄弟项；分量存在与否由执行器分别处理。
        for key, schema in (("investigation", InvestigationSubmission), ("verification", VerificationSubmission)):
            if key not in response_schema.model_fields:
                continue
            try:
                if key == "verification" and publication_contract == "claim-publication-v1":
                    schema = PublicationVerificationSubmission
                values[key] = schema.model_validate(raw.get(key))
            except ValidationError as exc:
                errors.append(RuntimeParseError(key, None, str(exc)))
        try:
            result_field = {"investigate": "investigation", "verify": "verification"}.get(task_kind)
            if result_field and result_field not in values:
                # 只为保留合法兄弟项创建内部部分解析结果；不能绕过后续分量验收。
                response = response_schema.model_construct(**values)
            else:
                response = response_schema.model_validate(values)
        except ValidationError as exc:
            errors.append(RuntimeParseError("submit", None, str(exc)))
            response = None
        return ParsedRuntimeResponse(response, errors, item_indices)


class ReviewTaskExecutor:
    """P1 的单步服务端投影器；外部调度器每次只调用一个动作。"""

    def __init__(self, spec: ReviewRunSpec, work_item: ReviewWorkItem,
                 goals: list[ReviewGoal], *, active_evidence_ids: set[str],
                 request_id: str, evidence_catalog: dict[str, dict], attempt_id: str = "first"):
        if work_item.kind.value == "investigate" and not work_item.proposition:
            raise ValueError("investigation requires a server-bound proposition")
        if work_item.kind.value == "verify" and work_item.verification_candidate is None:
            raise ValueError("verification requires a server-bound candidate")
        if any(goal.kind != work_item.kind for goal in goals if goal.goal_id in work_item.goal_ids):
            raise ValueError("work item goal kind mismatch")
        goal_map = {goal.goal_id: goal for goal in goals}
        if len(goal_map) != len(goals):
            raise ValueError("duplicate goal identity")
        if set(work_item.goal_ids) - set(goal_map):
            raise ValueError("work item references unknown goal")
        self.spec = spec
        self.work_item = work_item
        if work_item.readable_scope_hash is not None:
            if work_item.readable_scope_hash != spec.readable_scope_hash:
                raise ValueError("frozen_readable_scope_hash_mismatch")
            if set(work_item.scope_files) - set(spec.readable_scope_files):
                raise ValueError("task_seed_outside_frozen_readable_scope")
        if set(work_item.related_goal_ids) - set(goal_map):
            raise ValueError("work item references unknown related goal")
        self.goals = {identity: goal_map[identity] for identity in
                      [*work_item.goal_ids, *work_item.related_goal_ids]}
        self.active_evidence_ids = set(active_evidence_ids)
        self.evidence_catalog = evidence_catalog
        self.request_id = request_id
        self.attempt_id = attempt_id
        self.parser = RuntimeResponseParser()
        self.projection = TaskProjection(pending_target_ids=set(work_item.goal_ids))
        self.pages = []
        self.page_index = 0
        self.page_examinations: dict[str, dict[str, ExaminationResult]] = {}

    @property
    def readable_files(self) -> set[str]:
        return set(self.spec.readable_scope_files if self.work_item.readable_scope_hash is not None
                   else self.work_item.scope_files)

    def bind_pages(self, pages) -> None:
        if self.work_item.attempt_count or self.projection.examinations or self.pages:
            raise ValueError("context pages are frozen before scheduling")
        covered = {}
        for page in pages:
            if page.page_id != "page-" + stable_hash([self.work_item.work_item_id, page.ranges])[:32]:
                raise ValueError("invalid page manifest hash")
            for interval in page.ranges:
                goal = self.goals.get(interval["goal_id"])
                if (goal is None or interval["file_path"] != goal.target_ref
                        or interval["original_start_line"] != goal.start_line
                        or interval["original_end_line"] != goal.end_line
                        or not goal.start_line <= interval["start_line"] <= interval["end_line"] <= goal.end_line):
                    raise ValueError("invalid page goal binding")
                covered.setdefault(goal.goal_id, []).append((interval["start_line"], interval["end_line"]))
            if self.evidence_error_for_page(page):
                raise ValueError("invalid page source evidence")
        for goal in self.goals.values():
            cursor = goal.start_line
            for start, end in sorted(covered.get(goal.goal_id, [])):
                if start > cursor:
                    break
                cursor = max(cursor, end + 1)
            if cursor <= goal.end_line:
                raise ValueError("incomplete frozen page manifest")
        self.pages = list(pages)
        self.active_evidence_ids = set(self.pages[0].evidence_ids)

    def evidence_error_for_page(self, page):
        checker = set(self.active_evidence_ids)
        self.active_evidence_ids = set(page.evidence_ids)
        try:
            return self.evidence_error(list(page.evidence_ids))
        finally:
            self.active_evidence_ids = checker

    def current_goals(self):
        if not self.pages:
            return [goal for goal in self.goals.values() if goal.goal_id in self.projection.pending_target_ids]
        page = self.pages[self.page_index]
        return [self.goals[value["goal_id"]].model_copy(update={
                    "start_line": value["start_line"], "end_line": value["end_line"]})
                for value in page.ranges
                if value["goal_id"] in self.projection.pending_target_ids
                and page.page_id not in self.page_examinations.get(value["goal_id"], {})]

    def advance_page(self) -> bool:
        if not self.pages or self.current_goals() or self.page_index + 1 >= len(self.pages):
            return False
        self.page_index += 1
        self.active_evidence_ids = set(self.pages[self.page_index].evidence_ids)
        return True

    def parse_and_apply(self, raw: Any) -> ParsedRuntimeResponse:
        parsed = self.parser.parse(raw, task_kind=self.work_item.kind.value,
                                   publication_contract=self.spec.publication_contract)
        if parsed.response is None:
            return parsed
        if isinstance(parsed.response, RuntimeReadResponse):
            path = getattr(parsed.response.arguments, "file_path", None)
            if path is not None and (path not in self.readable_files or is_sensitive_repository_path(path)):
                parsed.errors.append(RuntimeParseError("read", None, "read_path_outside_work_item_scope"))
                parsed.response = None
            return parsed
        if isinstance(parsed.response, VerificationSubmitResponse):
            self._apply_verification(getattr(parsed.response, "verification", None), parsed.errors)
            if isinstance(parsed.response, PublicationVerificationSubmitResponse):
                self._apply_submit(parsed.response, parsed.errors, parsed.item_indices)
        else:
            if isinstance(parsed.response, InvestigationSubmitResponse):
                self._apply_investigation(getattr(parsed.response, "investigation", None), parsed.errors)
            self._apply_submit(parsed.response, parsed.errors, parsed.item_indices)
        return parsed

    def _apply_investigation(self, item, errors):
        if item is None:
            return
        error = self.evidence_error(item.evidence_ids)
        if error is None and item.answer.value != "insufficient_evidence":
            if not item.evidence_ids or set(self.work_item.required_evidence_ids) - set(item.evidence_ids):
                error = "incomplete_investigation_evidence"
            else:
                for request in self.work_item.evidence_requests:
                    goal = self.goals[self.work_item.goal_ids[0]].model_copy(update={
                        "target_ref": request.file_path, "start_line": request.start_line,
                        "end_line": request.end_line, "target_kind": "source_range"})
                    if not self._covers(goal, item.evidence_ids):
                        error = "incomplete_investigation_evidence"
                        break
        if error:
            errors.append(RuntimeParseError("investigation", None, error))
            return
        if self.projection.investigation is None:
            self.projection.investigation = InvestigationResult(**item.model_dump(),
                work_item_id=self.work_item.work_item_id, proposition=self.work_item.proposition,
                request_id=self.request_id, attempt_id=self.attempt_id,
                snapshot_head_sha=self.spec.snapshot_head_sha)
        self.projection.pending_target_ids.clear()

    def _apply_verification(self, item, errors):
        if item is None:
            return
        error = self.evidence_error(item.evidence_ids)
        if item.candidate_id != self.work_item.verification_candidate.candidate_id:
            error = "verification_candidate_identity_mismatch"
        if self.spec.publication_contract == "claim-publication-v1":
            from app.services.review_publication import verification_subject_hash
            if item.subject_hash != verification_subject_hash(self.work_item.verification_candidate, self.spec):
                error = "verification_subject_hash_mismatch"
            if item.narrowing is not None and all(getattr(item.narrowing, key) in
                    {None, getattr(self.work_item.verification_candidate, key)}
                    for key in ("affected_behavior", "recommendation")):
                error = "publication_narrowing_has_no_change"
        required = {*self.work_item.required_evidence_ids, *self.work_item.counter_evidence_ids}
        if error is None and item.decision.value != "insufficient_evidence":
            if not item.evidence_ids or required - set(item.evidence_ids):
                error = "incomplete_verification_evidence"
        if error:
            errors.append(RuntimeParseError("verification", None, error))
            return
        if self.projection.verification is None:
            self.projection.verification = VerificationResult(**item.model_dump(),
                work_item_id=self.work_item.work_item_id, request_id=self.request_id,
                attempt_id=self.attempt_id, snapshot_head_sha=self.spec.snapshot_head_sha)
        self.projection.pending_target_ids.clear()

    def _apply_submit(self, response: RuntimeSubmitResponse,
                      errors: list[RuntimeParseError], item_indices: dict[str, list[int]]) -> None:
        grouped = {}
        for item in getattr(response, "examinations", []):
            grouped.setdefault(item.target_id, []).append(item)
        conflicts = {identity for identity, items in grouped.items()
                     if len({stable_hash(item.model_dump(mode="json")) for item in items}) > 1}
        valid_examinations: list[ExaminationSubmission] = []
        for index, item in enumerate(getattr(response, "examinations", [])):
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
            goal = next((goal for goal in self.current_goals() if goal.goal_id == item.target_id), None)
            if goal is None:
                errors.append(RuntimeParseError("examinations", index, "target_not_in_current_page"))
                continue
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
            if self.pages and item.disposition.value == "examined":
                page = self.pages[self.page_index]
                fragments = self.page_examinations.setdefault(target_id, {})
                fragments.setdefault(page.page_id, result)
                needed = {p.page_id for p in self.pages
                          if any(value["goal_id"] == target_id for value in p.ranges)}
                if needed - set(fragments):
                    continue
                result = result.model_copy(update={
                    "evidence_ids": list(dict.fromkeys(e for fragment in fragments.values() for e in fragment.evidence_ids)),
                    "reason": "\n".join(fragment.reason for fragment in fragments.values()),
                })
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
                affected_behavior=item.affected_behavior, failure_scenario=item.failure_scenario,
                recommendation=item.recommendation, category=item.category, severity=item.severity,
                evidence_requests=item.evidence_requests,
            )
        for index, item in enumerate(getattr(response, "leads", [])):
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
                evidence_requests=item.evidence_requests,
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
            if evidence.get("source") == "file_read_diff":
                return "diff_observation_requires_source_anchor"
            if (evidence.get("head_sha") != self.spec.snapshot_head_sha
                    or evidence.get("base_sha") != self.spec.snapshot_base_sha):
                return "stale_evidence_snapshot"
            if (evidence.get("file_path") not in self.readable_files
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
