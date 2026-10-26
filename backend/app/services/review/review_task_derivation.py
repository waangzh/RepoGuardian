"""确定性派生调查/验证工作项；仅统一 Runtime 有权执行队列。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath

from app.evidence import DiffIndex, EvidenceResolver
from app.review.evidence_coverage import request_covered
from app.models.review import EvidenceAnchor, ReviewIssue, ReviewUnit, ReviewUnitComplexity
from app.models.review_runtime import (Candidate, VerificationResult, ReviewWorkItem, RuntimeReadArguments, WorkItemKind,
                                      RuntimeReadResponse)
from app.review.tool_scope import is_sensitive_repository_path
from app.core.fingerprints import stable_hash
from app.services.review.issue_policy import IssuePolicyService
from app.services.review.runtime.review_runtime import EvidenceIdentityConflict
from app.services.review.runtime.review_task_executor import ReviewTaskExecutor
from app.services.review.runtime.review_task_step import InspectTaskStep
from app.services.publication.review_publication import verification_subject_hash, publication_view


def evidence_key(value):
    return {**{key: value.get(key) for key in
               ("file_path", "start_line", "end_line", "content_hash", "head_sha", "base_sha")},
            "side": value.get("side", "head")}


def strict_candidate_key(candidate, goal, catalog, spec, scope):
    """仅精确等价；忽略服务端来源 ID，保留代码大小写、条件和证据区间。"""
    value = {"head": spec.snapshot_head_sha, "base": spec.snapshot_base_sha,
        "claim": candidate.claim.strip(), "behavior": candidate.affected_behavior.strip(),
        "failure": candidate.failure_scenario.strip(),
        "position": [goal.target_ref, goal.start_line, goal.end_line],
        "evidence": sorted((evidence_key(catalog[e]) for e in candidate.supporting_evidence),
                           key=stable_hash),
        "scope": sorted(scope), "category": candidate.category.value,
        "evidence_requests": [request.model_dump(mode="json") for request in candidate.evidence_requests]}
    if spec.publication_contract != "legacy":
        value.update(recommendation=candidate.recommendation.strip(), severity=candidate.severity.value,
                     publication_contract=spec.publication_contract)
    if spec.stability_contract == "review-stability-v2":
        # Exact claims only; citation/request ordering cannot manufacture another defect.
        for key in ("evidence", "evidence_requests"):
            unique = {stable_hash(item): item for item in value[key]}
            value[key] = [unique[identity] for identity in sorted(unique)]
    return stable_hash(value)


class TaskReviewDerivation:
    """结果 → Policy → 严格归并 → 取证/独立验证。没有私有异步消费循环。"""

    def __init__(self, runtime, steps, *, changed_files=None, rule_ids=None,
                 risk_catalog=None, counter_evidence_ids=None, max_derived_items=256,
                 investigations_enabled=True, risk_planner_version="legacy"):
        if not steps or not 1 <= max_derived_items <= 1_000:
            raise ValueError("invalid derived task configuration")
        if risk_planner_version not in {"legacy", "risk-planner-v2", "risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
            raise ValueError("unsupported risk planner version")
        self.runtime, self.steps = runtime, steps
        self.initial_ids = set(steps)
        self.initial_goals = {key: goal for step in steps.values() for key, goal in step.executor.goals.items()}
        # 允许范围仍由初始目标决定；删除目标额外允许其 Base 取证路径作为种子。
        self.allowed_files = set(path for step in steps.values() for path in step.executor.readable_files)
        self.base_evidence_files = {goal.target_ref for goal in self.initial_goals.values()
                                    if goal.target_kind == "base_source_range"}
        self.changed_files = changed_files or []
        self.rule_ids = rule_ids or {}
        self.risk_catalog = risk_catalog or []
        self.counter_evidence_ids = counter_evidence_ids or {}
        self.max_derived_items = max_derived_items
        self.investigations_enabled = investigations_enabled
        self.risk_planner_version = risk_planner_version
        self.policy = IssuePolicyService()
        self.data = {"candidates": {}, "lead_keys": {}, "leads": {}, "processed_candidates": [],
                     "processed_leads": [], "risk_catalog_processed": False, "verification_cache": {},
                     "invalidations": []}
        runtime.derivation = self

    def _all_evidence(self):
        catalog = {}
        for step in self.steps.values():
            for identity, value in step.executor.evidence_catalog.items():
                if identity in catalog:
                    previous = catalog[identity]
                    # 来源/检索元数据可随读取更新；身份绑定的原文与快照必须一致。
                    if (evidence_key(previous) != evidence_key(value)
                            or previous.get("content") != value.get("content")):
                        raise EvidenceIdentityConflict("conflicting evidence identity across runtime")
                    value = {**value, **previous}
                catalog[identity] = value
        return catalog

    def _root(self, origin):
        while origin not in self.initial_ids:
            origin = self.steps[origin].executor.work_item.origin_work_item_id
        return self.steps[origin]

    def frozen_configuration(self):
        result = {"changed_files": [value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                                  for value in self.changed_files], "rule_ids": self.rule_ids,
                "risk_catalog": self.risk_catalog, "max_derived_items": self.max_derived_items,
                "counter_evidence_ids": self.counter_evidence_ids, "initial_ids": sorted(self.initial_ids)}
        if not self.investigations_enabled:
            result["investigations_enabled"] = False
        if self.risk_planner_version != "legacy":
            result["risk_planner_version"] = self.risk_planner_version
        return result

    def _new_item(self, origin, kind, identity, goal, *, scope, **subject):
        root = self._root(origin)
        if identity in self.steps:
            return self.steps[identity]
        if len(self.steps) - len(self.initial_ids) >= self.max_derived_items:
            return None
        item = ReviewWorkItem(work_item_id=identity, kind=kind, goal_ids=[goal.goal_id],
            scope_files=sorted(scope), unit_id=root.executor.work_item.unit_id or goal.unit_id,
            sequence=len(self.steps), scheduling_class="verification" if kind == "verify" else "investigation",
            generation=1, origin_work_item_id=root.executor.work_item.work_item_id,
            readable_scope_hash=root.executor.work_item.readable_scope_hash, **subject)
        return self.restore_item(item, {goal.goal_id: goal})

    def restore_item(self, item, derived_goals):
        if item.generation != 1 or item.origin_work_item_id not in self.initial_ids:
            raise ValueError("invalid derived task lineage")
        if set(item.scope_files) - self.allowed_files - self.base_evidence_files:
            raise ValueError("derived task cannot expand frozen run scope")
        root = self.steps[item.origin_work_item_id]
        if item.readable_scope_hash != root.executor.work_item.readable_scope_hash:
            raise ValueError("derived task readable scope identity changed")
        goals = {**self.initial_goals, **derived_goals}
        catalog = deepcopy(self._all_evidence())
        active = {identity for identity, value in catalog.items() if value["file_path"] in item.scope_files}
        if item.kind.value == "verify" and root.executor.spec.prompt_version in {"task-review-prompt-v4", "task-review-prompt-v5", "task-review-prompt-v6", "task-review-prompt-v7", "task-review-prompt-v8"}:
            active = {*item.required_evidence_ids, *item.counter_evidence_ids}
        if item.kind.value == "investigate" and self.risk_planner_version in {"risk-planner-v2", "risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
            active = set(item.required_evidence_ids)
        executor = ReviewTaskExecutor(root.executor.spec, item, list(goals.values()),
            active_evidence_ids=active, evidence_catalog=catalog, request_id="not-dispatched")
        scope = root.scope.model_copy(update={"review_unit_id": item.work_item_id,
            "readable_files": executor.readable_files, "seed_files": set(item.scope_files),
            "commentable_files": {goals[key].target_ref for key in item.related_goal_ids}})
        step = InspectTaskStep(executor, root.provider, root.budget, scope,
                               git_tool=root.git, context_tool=root.tools, search_tool=root.search_tool,
                               changed_files=root.changed_files, file_index=root.file_index,
                               symbol_index=root.symbol_index, repository_graph=root.repository_graph)
        step.discussion_store = root.discussion_store
        self.steps[item.work_item_id] = step
        self.runtime.enqueue(item)
        if self.runtime.checkpoint is not None:
            step.checkpoint = self.runtime.checkpoint
        return step

    def _policy_issue(self, candidate, goal, catalog, root):
        primary = next((catalog[e] for e in candidate.supporting_evidence
                        if catalog[e]["file_path"] == goal.target_ref), None)
        if primary is None:
            return None, ["primary_evidence_missing"]
        if (len(candidate.affected_behavior.strip()) < 3 or len(candidate.failure_scenario.strip()) < 3
                or not candidate.recommendation.strip()):
            return None, ["candidate_behavior_or_failure_or_recommendation_missing"]
        if any(len(catalog[e]["content"]) > 8_000 for e in candidate.supporting_evidence):
            return None, ["candidate_anchor_exceeds_policy_capacity"]
        def anchor(value):
            return EvidenceAnchor(file_path=value["file_path"], existing_code=value["content"],
                                  expected_side=value.get("side", "head"))
        unit = ReviewUnit(id=goal.unit_id, primary_files=[goal.target_ref],
            related_files=sorted(set(root.executor.work_item.scope_files) - {goal.target_ref}),
            rule_ids=self.rule_ids.get(goal.unit_id, []), estimated_tokens=0,
            complexity=ReviewUnitComplexity.small, fingerprint="task-policy", grouping_reason="task-policy-view")
        issue = ReviewIssue(id=candidate.candidate_id, review_unit_id=unit.id, title=candidate.claim,
            category=candidate.category, severity=candidate.severity, confidence=0.5,
            affected_behavior=candidate.affected_behavior, failure_scenario=candidate.failure_scenario,
            recommendation=candidate.recommendation, primary_evidence=anchor(primary),
            supporting_evidence=[anchor(catalog[e]) for e in candidate.supporting_evidence if catalog[e] is not primary])
        # 定位仅在服务器已校验的原始证据视图中进行，不加载未获授权的目标代码。
        def load_file(path, side):
            pieces = sorted((value for value in catalog.values() if value["file_path"] == path
                             and value.get("side", "head") == side), key=lambda value: value["start_line"])
            if not pieces:
                return None
            lines = {}
            for value in pieces:
                for index, line in enumerate(value["content"].splitlines(), value["start_line"]):
                    if index in lines and lines[index] != line:
                        return None
                    lines[index] = line
            return "\n".join(lines.get(index, "") for index in range(1, max(lines) + 1))
        resolver = EvidenceResolver(DiffIndex(self.changed_files), file_loader=load_file,
                                    repository_files=root.executor.readable_files)
        issue = resolver.resolve_issue(issue, unit)
        check = self.policy.check(issue, [unit])
        if issue.primary_evidence.resolved_start_line != primary["start_line"]:
            return issue, ["resolved_anchor_range_mismatch"]
        return issue.model_copy(update={"severity": check.normalized_severity}), check.reasons

    def _candidate(self, candidate, origin):
        if (candidate.candidate_id in self.data["processed_candidates"]
                and any(candidate.candidate_id in record["source_candidate_ids"]
                        for record in self.data["candidates"].values())):
            return
        source = self.steps[origin]
        goal = source.executor.goals[candidate.primary_anchor]
        root = self._root(origin)
        catalog = self._all_evidence()
        # 历史证据进入独立请求前重新激活并验收；归档 ID 本身不授予信任。
        checker = ReviewTaskExecutor(source.executor.spec, source.executor.work_item,
            list(source.executor.goals.values()), active_evidence_ids=set(candidate.supporting_evidence),
            evidence_catalog=catalog, request_id="candidate-policy")
        error = checker.evidence_error(candidate.supporting_evidence)
        issue, reasons = (None, [error]) if error else self._policy_issue(candidate, goal, catalog, source)
        key = strict_candidate_key(candidate, goal, catalog, root.executor.spec, source.executor.work_item.scope_files)
        existing = self.data["candidates"].get(key)
        if existing:
            existing["source_candidate_ids"] = sorted(set([*existing["source_candidate_ids"], candidate.candidate_id]))
            existing["source_result_ids"] = sorted(set([*existing["source_result_ids"], *candidate.source_result_ids]))
            return
        record = {"candidate": candidate.model_dump(mode="json"), "key": key,
            "source_candidate_ids": [candidate.candidate_id], "source_result_ids": candidate.source_result_ids,
            "status": "unresolved" if reasons else "candidate", "reason": ";".join(reasons) if reasons else None,
            "issue": issue.model_dump(mode="json") if issue else None, "origin": origin,
            "verification_work_item_id": None, "investigation_work_item_id": None}
        if source.executor.work_item.kind.value == "verify":
            record["discovery_origin"] = {"kind": "verify", "work_item_id": origin,
                "original_candidate_id": source.executor.work_item.verification_candidate.candidate_id,
                "request_id": source.executor.request_id, "attempt_id": source.executor.attempt_id}
        if reasons:
            self.data["candidates"][key] = record
            return
        invalid_requests = [request for request in candidate.evidence_requests
                            if request.file_path not in self.allowed_files or is_sensitive_repository_path(request.file_path)]
        if invalid_requests:
            record.update(status="unresolved", reason="evidence_request_outside_frozen_scope")
            self.data["candidates"][key] = record
            return
        missing = [request for request in candidate.evidence_requests
                   if not request_covered(request, catalog)]
        if missing:
            investigation = self._investigation(origin, key, "核对候选前提：" + candidate.failure_scenario,
                [goal.goal_id], candidate.evidence_requests, source="candidate")
            if investigation:
                record["investigation_work_item_id"] = investigation.executor.work_item.work_item_id
            else:
                record.update(status="unresolved", reason="evidence_preparation_not_scheduled")
            self.data["candidates"][key] = record
            return
        self._verify(record, catalog)
        self.data["candidates"][key] = record

    @staticmethod
    def _request_covered(request, catalog):
        return request_covered(request, catalog)

    def _verify(self, record, catalog):
        candidate = Candidate.model_validate(record["candidate"])
        root = self._root(record["origin"])
        goal = self.initial_goals[candidate.primary_anchor]
        # 必要前提原文和已知反证都进入独立 Prompt；没有生成者置信度字段。
        prerequisite = {identity for identity, value in catalog.items()
                        if any(value["file_path"] == request.file_path
                               and value["start_line"] <= request.end_line and value["end_line"] >= request.start_line
                               for request in candidate.evidence_requests)}
        counter = set(self.counter_evidence_ids.get(goal.goal_id, []))
        if counter - set(catalog):
            record.update(status="unresolved", reason="known_counterevidence_missing")
            return
        required = sorted({*candidate.supporting_evidence, *prerequisite, *counter})
        if len(required) > 20:
            record.update(status="unresolved", reason="verification_evidence_capacity_exceeded")
            return
        scope = sorted({goal.target_ref, *(catalog[e]["file_path"] for e in required)})
        checker = ReviewTaskExecutor(root.executor.spec, root.executor.work_item,
            list(root.executor.goals.values()), active_evidence_ids=set(required),
            evidence_catalog=catalog, request_id="verification-evidence-readiness")
        # 为同一冻结运行里显式扩展的调查范围建立受控视图，不能扩大运行 scope。
        checker.work_item = checker.work_item.model_copy(update={"scope_files": scope})
        error = checker.evidence_error(required)
        if error:
            record.update(status="unresolved", reason=error)
            return
        premise = {"candidate_key": record["key"], "evidence": sorted((evidence_key(catalog[e]) for e in required), key=stable_hash),
                   "scope": scope, "head": root.executor.spec.snapshot_head_sha,
                   "base": root.executor.spec.snapshot_base_sha, "model": root.executor.spec.model_name,
                   "provider": root.executor.spec.model_provider, "protocol": root.executor.spec.protocol_version,
                   "endpoint": getattr(root.provider, "_base_url", None),
                   "policy": root.executor.spec.policy_version, "output_tokens": 4096,
                   "rules": self.rule_ids.get(goal.unit_id, [])}
        if root.executor.spec.publication_contract != "legacy":
            premise.update(publication_contract=root.executor.spec.publication_contract,
                           subject_hash=verification_subject_hash(candidate, root.executor.spec))
        cache_key = stable_hash(premise)
        record["verification_key"] = cache_key
        if cache_key in self.data["verification_cache"]:
            cached = self.data["verification_cache"][cache_key]
            self._apply_verified_record(record, VerificationResult.model_validate(cached["result"]), known=True)
            if record["status"] != "unresolved":
                record["reason"] = "strict_verified_input_reuse"
            return
        identity = "verify-" + cache_key[:32]
        derived = goal.model_copy(update={"goal_id": "verify-goal-" + cache_key[:32],
                                          "kind": WorkItemKind.verify, "origin": "derived"})
        step = self._new_item(record["origin"], "verify", identity, derived, scope=scope,
            related_goal_ids=[goal.goal_id], verification_candidate=candidate,
            required_evidence_ids=required, counter_evidence_ids=sorted({*prerequisite, *counter}))
        if step is None:
            record.update(status="unresolved", reason="derived_task_limit")
        else:
            record["verification_work_item_id"] = identity

    def _investigation(self, origin, source_id, proposition, target_ids, requests, *, source):
        if not self.investigations_enabled:
            self.data["leads"][source_id] = {"status": "not_scheduled",
                "reason": "experiment_investigations_disabled", "proposition": proposition, "source": source}
            return None
        source_step = self.steps[origin]
        targets = [self.initial_goals[identity] for identity in target_ids if identity in self.initial_goals]
        if not targets or not requests or any(request.file_path not in self.allowed_files
                or is_sensitive_repository_path(request.file_path) for request in requests):
            self.data["leads"][source_id] = {"status": "not_scheduled", "reason": "no_legal_evidence_path",
                                           "proposition": proposition, "source": source}
            return None
        if source_step.executor.work_item.generation:
            self.data["leads"][source_id] = {"status": "not_scheduled", "reason": "second_generation_disabled",
                                           "proposition": proposition, "source": source}
            return None
        root = self._root(origin)
        scope = sorted({*(goal.target_ref for goal in targets), *(request.file_path for request in requests)})
        key = stable_hash([proposition.strip(), scope, [request.model_dump(mode="json") for request in requests],
                           root.executor.spec.snapshot_head_sha, root.executor.spec.snapshot_base_sha,
                           sorted((evidence_key(value) for value in self._all_evidence().values()
                                   if value["file_path"] in scope), key=stable_hash)])
        existing = self.data["lead_keys"].get(key)
        if existing:
            self.data["leads"][source_id] = {"status": "duplicate", "reason": "equivalent_investigation",
                "work_item_id": existing, "proposition": proposition, "source": source}
            return self.steps[existing]
        identity = "investigate-" + key[:32]
        goal = targets[0].model_copy(update={"goal_id": "investigation-goal-" + key[:32],
                                           "kind": WorkItemKind.investigate, "origin": "derived"})
        catalog = self._all_evidence()
        required = sorted(identity for identity, value in catalog.items()
                          if value["file_path"] in {target.target_ref for target in targets})
        if self.risk_planner_version in {"risk-planner-v2", "risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
            requested = {identity for identity, value in catalog.items()
                if any(value["file_path"] == request.file_path and value.get("side", "head") == "head"
                    and value["start_line"] <= request.end_line and value["end_line"] >= request.start_line
                    for request in requests)}
            required = sorted(requested if source == "deterministic_risk" else requested | set(required))
        # 删除目标的 Base 原文是命题的必要前提；Head 侧目标维持原有口径，避免扩大既有取证集。
        base_targets = {target.target_ref for target in targets if target.target_kind == "base_source_range"}
        if base_targets:
            required = sorted(set(required) | {identity for identity, value in catalog.items()
                                               if value["file_path"] in base_targets})
        if len(required) > 20:
            self.data["leads"][source_id] = {"status": "not_scheduled", "reason": "necessary_evidence_capacity_exceeded",
                                           "proposition": proposition, "source": source}
            return None
        step = self._new_item(origin, "investigate", identity, goal, scope=scope,
            proposition=proposition, related_goal_ids=target_ids, evidence_requests=requests,
            required_evidence_ids=required)
        if step is None:
            self.data["leads"][source_id] = {"status": "not_scheduled", "reason": "derived_task_limit",
                                           "proposition": proposition, "source": source}
            return None
        self.data["lead_keys"][key] = identity
        self.data["leads"][source_id] = {"status": "scheduled", "reason": None,
            "work_item_id": identity, "proposition": proposition, "source": source}
        self.data["leads"][source_id]["related_goal_ids"] = target_ids
        # 必要原文由受控读取先准备；随后模型可以继续读，或者提交命题答案。
        missing = next((request for request in requests if not request_covered(request, self._all_evidence())), None)
        if missing:
            step.pending_read = RuntimeReadResponse(tool="file_read", arguments=missing, purpose="准备明确缺失证据")
        return step

    def _apply_verified_record(self, record, result, *, known):
        status = {"supported": "confirmed", "refuted": "dismissed", "insufficient_evidence": "unresolved"}[result.decision.value]
        record.update(status=status if known else "unresolved", verification=result.model_dump(mode="json"),
                      reason=None if known else "verification_usage_unknown")
        source = self.steps[record["origin"]]
        if source.executor.spec.publication_contract == "legacy":
            return
        candidate = Candidate.model_validate(record["candidate"])
        try:
            view = publication_view(candidate, result, source.executor.spec, usage_known=known)
        except ValueError as exc:
            record.update(status="unresolved", reason=str(exc))
            record.pop("publication", None)
            return
        record.setdefault("original_issue", deepcopy(record.get("issue")))
        record["publication"] = view
        if view["published_text"] is not None:
            narrowed = candidate.model_copy(update=view["published_text"])
            goal = source.executor.goals[candidate.primary_anchor]
            issue, reasons = self._policy_issue(narrowed, goal, self._all_evidence(), source)
            if reasons:
                record.update(status="unresolved", reason="publication_policy:" + ";".join(reasons))
                view["published_text"] = None
            else:
                record["issue"] = issue.model_dump(mode="json")
        else:
            record["issue"] = deepcopy(record["original_issue"])

    def refresh(self):
        # 拷贝工作项视图，派生项进入同一队列，下一轮调度才执行。
        for identity, step in list(self.steps.items()):
            for candidate in list(step.executor.projection.candidates.values()):
                self._candidate(candidate, identity)
                if candidate.candidate_id not in self.data["processed_candidates"]:
                    self.data["processed_candidates"].append(candidate.candidate_id)
            for lead in step.executor.projection.leads:
                if lead.lead_id in self.data["processed_leads"]:
                    continue
                self.data["processed_leads"].append(lead.lead_id)
                self._investigation(identity, lead.lead_id, lead.proposition, lead.related_targets,
                                    lead.evidence_requests, source="model_lead")
        if not self.data["risk_catalog_processed"]:
            self.data["risk_catalog_processed"] = True
            for risk in self.risk_catalog:
                if risk.get("hint"):
                    self.data["leads"][risk["id"]] = {"status": "hint", "reason": "relationship_hint_only",
                        "proposition": risk["proposition"], "source": "deterministic_risk",
                        "related_goal_ids": risk["related_goal_ids"], "relationship": risk["relationship"]}
                    continue
                if risk.get("not_scheduled_reason"):
                    self.data["leads"][risk["id"]] = {"status": "not_scheduled",
                        "reason": risk["not_scheduled_reason"], "proposition": risk["proposition"],
                        "source": "deterministic_risk", "related_goal_ids": risk["related_goal_ids"]}
                    if risk.get("relationship"):
                        self.data["leads"][risk["id"]]["relationship"] = deepcopy(risk["relationship"])
                    continue
                self._investigation(risk["origin_work_item_id"], risk["id"], risk["proposition"],
                    risk["related_goal_ids"], [RuntimeReadArguments.model_validate(v) for v in risk["evidence_requests"]],
                    source="deterministic_risk")
                if risk.get("relationship"):
                    self.data["leads"][risk["id"]].update(relationship=deepcopy(risk["relationship"]),
                        related_goal_ids=risk["related_goal_ids"])
        for lead in self.data["leads"].values():
            identity = lead.get("work_item_id")
            if identity and lead["status"] != "duplicate":
                result = self.steps[identity].executor.projection.investigation
                if result:
                    lead.update(status="answered" if result.answer.value != "insufficient_evidence" else "insufficient_evidence",
                                answer=result.answer.value)
                    if result.answer.value != "insufficient_evidence" and not lead.get("resumed"):
                        lead["resumed"] = True
                        # 调查答案不算初始覆盖；只唤醒明确绑定的 blocked 目标重新检查。
                        for initial_id in self.initial_ids:
                            parent = self.steps[initial_id]
                            projection = parent.executor.projection
                            affected = {key for key in lead.get("related_goal_ids", [])
                                        if key in projection.examinations
                                        and projection.examinations[key].disposition.value == "blocked"}
                            if not affected:
                                continue
                            for key in affected:
                                del projection.examinations[key]
                                projection.accepted_target_ids.discard(key)
                            projection.pending_target_ids.update(affected)
                            for e, value in self.steps[identity].executor.evidence_catalog.items():
                                if value["file_path"] in parent.executor.readable_files:
                                    parent.executor.evidence_catalog[e] = value
                                    parent.executor.active_evidence_ids.add(e)
                            item = parent.executor.work_item
                            if item.state.value not in {"running", "unknown", "cancelled"}:
                                item.state = type(item.state)("ready")
                                item.scheduling_class = "continuation"
        for record in self.data["candidates"].values():
            investigation_id = record.get("investigation_work_item_id")
            if investigation_id and not record.get("verification_work_item_id"):
                result = self.steps[investigation_id].executor.projection.investigation
                if result:
                    if result.answer.value == "insufficient_evidence":
                        record.update(status="unresolved", reason="investigation_insufficient_evidence")
                    else:
                        catalog = self._all_evidence()
                        candidate = Candidate.model_validate(record["candidate"])
                        if all(request_covered(request, catalog) for request in candidate.evidence_requests):
                            self._verify(record, catalog)
                        else:
                            record.update(status="unresolved", reason="required_source_still_missing")
            identity = record.get("verification_work_item_id")
            if not identity:
                continue
            step = self.steps[identity]
            result = step.executor.projection.verification
            if result:
                attempt = step.budget.attempts.get(result.attempt_id)
                known = attempt is not None and attempt.usage_available and attempt.settled
                self._apply_verified_record(record, result, known=known)
                if known and result.decision.value != "insufficient_evidence" and record["status"] != "unresolved":
                    self.data["verification_cache"][record["verification_key"]] = {
                        "status": record["status"], "result": result.model_dump(mode="json")}
            elif step.executor.work_item.state.value in {"failed", "cancelled", "unknown", "result_missing", "waiting_evidence"}:
                record.update(status="unresolved", reason="verification_" + step.executor.work_item.state.value)

    def invalidate_evidence(self, evidence_ids, *, reason):
        """显式失效事件只撤销依赖该证据的结果，不影响独立目标。"""
        affected = set(evidence_ids)
        self.data["invalidations"].append({"evidence_ids": sorted(affected), "reason": reason})
        for step in self.steps.values():
            projection = step.executor.projection
            step.executor.active_evidence_ids.difference_update(affected)
            for key, result in list(projection.examinations.items()):
                if affected & set(result.evidence_ids):
                    projection.invalidated_results.append({"kind": "examination", "result": result.model_dump(mode="json"),
                                                           "reason": reason})
                    del projection.examinations[key]
                    projection.accepted_target_ids.discard(key)
                    projection.pending_target_ids.add(key)
            for key, fragments in list(step.executor.page_examinations.items()):
                for page_id, result in list(fragments.items()):
                    if affected & set(result.evidence_ids):
                        del fragments[page_id]
                        projection.pending_target_ids.add(key)
            if projection.investigation and affected & set(projection.investigation.evidence_ids):
                projection.invalidated_results.append({"kind": "investigation",
                    "result": projection.investigation.model_dump(mode="json"), "reason": reason})
                projection.investigation = None
                projection.pending_target_ids.update(step.executor.work_item.goal_ids)
                step.executor.work_item.state = type(step.executor.work_item.state)("waiting_evidence")
            if projection.verification and affected & set(projection.verification.evidence_ids):
                projection.invalidated_results.append({"kind": "verification",
                    "result": projection.verification.model_dump(mode="json"), "reason": reason})
                projection.verification = None
                projection.pending_target_ids.update(step.executor.work_item.goal_ids)
                step.executor.work_item.state = type(step.executor.work_item.state)("waiting_evidence")
        for record in self.data["candidates"].values():
            candidate = Candidate.model_validate(record["candidate"])
            verification = record.get("verification", {})
            if affected & {*candidate.supporting_evidence, *verification.get("evidence_ids", [])}:
                record.update(status="unresolved", reason=reason, verification_work_item_id=None)
                record.pop("verification", None)
                if record.get("publication"):
                    record.setdefault("invalidated_publications", []).append(record.pop("publication"))
                    record["issue"] = deepcopy(record.get("original_issue"))
                self.data["verification_cache"].pop(record.get("verification_key"), None)

    def revalidate_candidate(self, key):
        record = self.data["candidates"][key]
        candidate = Candidate.model_validate(record["candidate"])
        source = self.steps[record["origin"]]
        catalog = self._all_evidence()
        if not all(request_covered(request, catalog) for request in candidate.evidence_requests):
            record.update(status="unresolved", reason="required_source_still_missing")
            return
        goal = source.executor.goals[candidate.primary_anchor]
        issue, reasons = self._policy_issue(candidate, goal, catalog, source)
        if reasons:
            record.update(status="unresolved", reason=";".join(reasons))
            return
        record["issue"] = issue.model_dump(mode="json")
        self._verify(record, catalog)


def _module_names_for_path(file_path: str) -> set[str]:
    """被删路径的候选模块名；只做确定性推导，不猜包结构之外的形式。"""
    path = PurePosixPath(file_path)
    if path.suffix != ".py":
        return set()
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    names = {".".join(parts)}
    # 仓库内常见的源码前缀：src/ 与 backend/ 下的模块通常按去掉前缀后的名字导入。
    for prefix in ("src", "backend"):
        if parts[0] == prefix and len(parts) > 1:
            names.add(".".join(parts[1:]))
    return {name for name in names if name}


def dangling_deleted_references(deleted_paths, file_index):
    """Head 索引中仍导入本次被删除模块的文件；仅返回可举证的引用行。

    删除后该模块在 Head 已不存在，因此这里的命中都是真实悬空引用；只产出
    线索/调查绑定，不代替模型核对，也不猜测符号语义。
    """
    deleted = {path for path in deleted_paths if not is_sensitive_repository_path(path)}
    if not deleted:
        return []
    modules = {}
    for path in sorted(deleted):
        for name in _module_names_for_path(path):
            modules.setdefault(name, []).append(path)
    if not modules:
        return []
    results = []
    for item in file_index or []:
        source = item.get("path")
        if not source or source in deleted:
            continue
        references = []
        for ref in item.get("import_refs") or []:
            module = str(ref.get("module") or "").strip().lstrip(".")
            if module in modules:
                references.append({"module": module, "line": ref.get("line"),
                    "imported_name": ref.get("imported_name"), "local_name": ref.get("local_name"),
                    "target_paths": sorted(modules[module])})
        if references:
            results.append({"file_path": source, "references": sorted(
                references, key=lambda value: (value["line"] or 0, value["module"]))})
    return sorted(results, key=lambda value: value["file_path"])


def build_task_risk_catalog(goals, work_items, repository_graph, *, planner_version="legacy", max_lines_per_read=240,
                            dangling_references=()):
    """绑定关系符号；v3 调查任一侧变更影响，未变更端只作为证据。"""
    by_path = {}
    owners = {goal: item.work_item_id for item in work_items for goal in item.goal_ids}
    for goal in goals:
        if goal.origin == "initial" and goal.target_kind in {"source_range", "base_source_range"}:
            by_path.setdefault(goal.target_ref, []).append(goal)
    output = {}
    if planner_version not in {"legacy", "risk-planner-v2", "risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
        raise ValueError("unsupported risk planner version")
    if planner_version == "legacy":
        return _build_legacy_risk_catalog(repository_graph, by_path, owners)
    expected_resolution = {"risk-planner-v5": "import-evidence-v4", "risk-planner-v6": "import-evidence-v5",
                           "risk-planner-v7": "import-evidence-v5", "risk-planner-v8": "import-evidence-v6"}
    if (planner_version in expected_resolution and
            repository_graph.get("metadata", {}).get("call_resolution_version") != expected_resolution[planner_version]):
        raise ValueError("risk planner repository graph version mismatch")
    if not 1 <= max_lines_per_read <= 2_000:
        raise ValueError("invalid risk planner read limit")
    single_changed_side = planner_version in {"risk-planner-v3", "risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}

    symbols = {}
    for value in repository_graph.get("symbols", []):
        identity = value.get("id")
        if identity:
            # 重复身份不能由输入顺序决定要读哪个定义。
            symbols[identity] = value if identity not in symbols else None
    def valid_range(symbol):
        start, end = symbol.get("start_line"), symbol.get("end_line")
        return type(start) is int and type(end) is int and 1 <= start <= end <= 1_000_000

    def overlaps(goal, symbol):
        start, end = symbol.get("start_line"), symbol.get("end_line")
        return (valid_range(symbol)
                and goal.start_line is not None and goal.end_line is not None
                and goal.start_line <= end and goal.end_line >= start)

    for edge in repository_graph.get("edges", []):
        if edge.get("type") not in {"calls", "imports", "test_of", "configures"}:
            continue
        source = str(edge.get("source_file") or edge.get("source", "").split("::", 1)[0])
        target = str(edge.get("target_file") or edge.get("target", "").split("::", 1)[0])
        if source == target or (source not in by_path and target not in by_path):
            continue
        if not single_changed_side and (source not in by_path or target not in by_path):
            continue
        source_symbol = symbols.get(str(edge.get("source")))
        target_symbol = symbols.get(str(edge.get("target")))
        if source_symbol and source_symbol.get("file") != source:
            source_symbol = None
        if target_symbol and target_symbol.get("file") != target:
            target_symbol = None
        callers = [goal for goal in by_path.get(source, [])
                   if source_symbol and overlaps(goal, source_symbol)]
        callees = [goal for goal in by_path.get(target, [])
                   if target_symbol and overlaps(goal, target_symbol)]
        relationship = {"type": edge["type"], "source_file": source, "target_file": target,
            "source_symbol": source_symbol, "target_symbol": target_symbol,
            "source": edge.get("source"), "target": edge.get("target"),
            "resolution_status": edge.get("resolution_status"),
            "resolution_basis": edge.get("resolution_basis"),
            "relationship_level": edge.get("relationship_level"),
            "call_sites": edge.get("call_sites", [])}
        if planner_version in {"risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"} and (edge["type"] != "calls" or edge.get("relationship_level") == "search_hint"):
            related = sorted(goal.goal_id for path in (source, target) for goal in by_path.get(path, []))
            key = "relationship-hint-" + stable_hash([relationship, related])[:32]
            output[key] = {"id": key, "hint": True, "related_goal_ids": related,
                "proposition": f"{source} → {target}（{edge['type']}，取证提示，尚未核验）",
                "relationship": relationship, "evidence_requests": []}
            continue
        missing_changed_binding = not (callers or callees) if single_changed_side else not callers or not callees
        accepted_basis = ({"python_lexical_import", "qualified_import_module"}
                          if planner_version in {"risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"} else {"qualified_import_module"})
        unresolved_call = (planner_version in {"risk-planner-v4", "risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"} and edge["type"] == "calls"
                           and (edge.get("resolution_status") != "resolved"
                                or edge.get("resolution_basis") not in accepted_basis))
        if (edge["type"] != "calls" or not source_symbol or not target_symbol
                or not valid_range(source_symbol) or not valid_range(target_symbol) or missing_changed_binding
                or unresolved_call):
            if (edge["type"] == "calls" and source_symbol and target_symbol
                    and valid_range(source_symbol) and valid_range(target_symbol)
                    and missing_changed_binding):
                # 已定位且变更未触及所需符号，不将无关 hunk 当作风险。
                continue
            # 关系保留为可见线索，不猜测符号或行号，也不生成泛化调查。
            changed = [goal for path in (source, target) for goal in by_path.get(path, [])]
            origin_goals = changed if single_changed_side else by_path[source]
            origin = next((owners.get(goal.goal_id) for goal in sorted(origin_goals, key=lambda goal: goal.goal_id)
                           if owners.get(goal.goal_id)), None)
            related = sorted(goal.goal_id for goal in changed)
            key = "risk-unbound-" + stable_hash([edge.get("type"), edge.get("source"), edge.get("target"),
                                               related, planner_version])[:32]
            if planner_version in {"risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
                key = "risk-unbound-" + stable_hash([relationship, related, planner_version])[:32]
            output[key] = {"id": key, "origin_work_item_id": origin,
                    "related_goal_ids": related,
                    "proposition": f"{source} 与 {target} 的 {edge['type']} 关系缺少可绑定的具体符号与变更范围",
                    "evidence_requests": [], "not_scheduled_reason": ("relationship_resolution_unverified"
                        if unresolved_call else "relationship_not_bound_to_symbols"),
                    "relationship_evidence": {key: edge.get(key) for key in
                        ("resolution_status", "resolution_basis", "call_expression")}}
            if planner_version in {"risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
                output[key]["relationship"] = relationship
                output[key]["proposition"] = f"待核验调用：{edge.get('source')} → {edge.get('target')}"
            if planner_version in {"risk-planner-v7", "risk-planner-v8"}:
                # A name/binding hint is not an admitted semantic obligation.
                output[key].pop("not_scheduled_reason")
                output[key]["hint"] = True
            continue
        bindings = ([sorted([*callers, *callees], key=lambda goal: goal.goal_id)] if single_changed_side else
                    [[caller, callee] for caller in callers for callee in callees if caller.unit_id != callee.unit_id])
        for binding in bindings:
            related = [goal.goal_id for goal in binding]
            origin_goal = (next((goal for goal in binding if goal.goal_id in owners), binding[0])
                           if single_changed_side else binding[0])
            proposition = (f"待源码核对的 calls 命题：调用方 {source_symbol['id']}"
                           f"（定义范围 {source_symbol['start_line']}-{source_symbol['end_line']}）"
                           f"对 {target_symbol['id']}"
                           f"（定义范围 {target_symbol['start_line']}-{target_symbol['end_line']}）的调用，"
                           "在本次变更后仍兼容该调用链的实际参数传递、返回值消费与异常处理路径")
            key = "risk-" + stable_hash([related, edge.get("type"), source_symbol["id"],
                                       target_symbol["id"], proposition])[:32]
            requests = []
            capacity_exceeded = False
            unavailable_source = False
            if planner_version in {"risk-planner-v7", "risk-planner-v8"}:
                from app.services.review.review_stability import companion_definitions, source_requests
                definitions = [source_symbol, target_symbol, *companion_definitions(
                    edge, repository_graph, symbols, nested_only=planner_version == "risk-planner-v8")]
                allowed = {symbol["file"] for symbol in definitions}
                unavailable_source = any(is_sensitive_repository_path(symbol["file"])
                                         or not valid_range(symbol) for symbol in definitions)
                prepared, capacity_exceeded = source_requests(definitions, allowed, max_lines_per_read, limit=20)
                requests = [request.model_dump(mode="json") for request in prepared]
            for symbol in (source_symbol, target_symbol):
                if planner_version in {"risk-planner-v7", "risk-planner-v8"}:
                    break
                for start in range(symbol["start_line"], symbol["end_line"] + 1, max_lines_per_read):
                    requests.append(RuntimeReadArguments(file_path=symbol["file"], start_line=start,
                        end_line=min(symbol["end_line"], start + max_lines_per_read - 1)).model_dump(mode="json"))
                    if len(requests) > 20:
                        break
                if len(requests) > 20:
                    break
            reason = ("no_legal_evidence_path" if unavailable_source else
                      "necessary_evidence_capacity_exceeded" if len(requests) > 20 or capacity_exceeded else
                      "origin_work_item_missing" if origin_goal.goal_id not in owners else
                      "relationship_target_capacity_exceeded" if len(related) > 40 else
                      "relationship_proposition_capacity_exceeded" if len(proposition) > 2_000 else None)
            output[key] = {"id": key, "origin_work_item_id": owners.get(origin_goal.goal_id),
                "related_goal_ids": related, "proposition": proposition,
                "evidence_requests": requests if not reason else [], "not_scheduled_reason": reason}
            if planner_version in {"risk-planner-v5", "risk-planner-v6", "risk-planner-v7", "risk-planner-v8"}:
                output[key]["relationship"] = relationship
    _append_dangling_references(output, dangling_references, by_path, owners)
    return [output[key] for key in sorted(output)]


def _append_dangling_references(output, references, by_path, owners):
    """Head 仍导入已删模块：绑定明确 import 行则准入调查，否则保留未核验线索。"""
    for entry in references or []:
        source = entry["file_path"]
        targets = sorted({path for ref in entry["references"] for path in ref["target_paths"]})
        related = [goal.goal_id for path in (source, *targets) for goal in by_path.get(path, [])]
        # 只有删除目标在冻结目标集合中时才可能绑定；否则保留线索不生成泛化调查。
        binding = [goal for target in targets for goal in by_path.get(target, [])]
        origin = next((owners.get(goal.goal_id) for goal in sorted(binding, key=lambda goal: goal.goal_id)
                       if owners.get(goal.goal_id)), None)
        requests = []
        for ref in entry["references"]:
            line = ref.get("line")
            if line:
                requests.append(RuntimeReadArguments(file_path=source, start_line=int(line),
                    end_line=int(line)).model_dump(mode="json"))
        key = "risk-dangling-" + stable_hash([source, targets, entry["references"]])[:32]
        relationship = {"type": "imports", "source_file": source,
            "target_file": targets[0] if len(targets) == 1 else None, "source": source,
            "target": targets[0] if len(targets) == 1 else None,
            "resolution_status": "dangling_deleted_target", "resolution_basis": "head_import_ref",
            "call_sites": [{"line": ref.get("line"), "expression": ref.get("module")}
                           for ref in entry["references"]]}
        entry_value = {"id": key, "origin_work_item_id": origin, "related_goal_ids": related,
            "proposition": (f"Head 文件 {source} 仍导入本次删除的模块 "
                + "、".join(f"{ref.get('module')}（原 {', '.join(ref['target_paths'])}）" for ref in entry["references"])
                + "；需核对删除后该引用是否仍然成立"),
            "evidence_requests": requests, "relationship": relationship}
        if not origin or not related or not requests or len(requests) > 20:
            entry_value.update(evidence_requests=[], related_goal_ids=related,
                not_scheduled_reason=("origin_work_item_missing" if not origin or not related
                                      else "necessary_evidence_capacity_exceeded"),
                hint=not origin or not related)
        output[key] = entry_value


def _build_legacy_risk_catalog(repository_graph, by_path, owners):
    """保持旧任务的文件关系语义，供缺少冻结规划版本的历史任务恢复。"""
    output = {}
    for edge in repository_graph.get("edges", []):
        if edge.get("type") not in {"calls", "imports", "test_of", "configures"}:
            continue
        source = str(edge.get("source_file") or edge.get("source", "").split("::", 1)[0])
        target = str(edge.get("target_file") or edge.get("target", "").split("::", 1)[0])
        if source == target or source not in by_path or target not in by_path:
            continue
        for caller in by_path[source]:
            for callee in by_path[target]:
                if caller.unit_id == callee.unit_id:
                    continue
                related = [caller.goal_id, callee.goal_id]
                proposition = f"{source} 与 {target} 的 {edge['type']} 关系在本次变更后仍满足调用或数据契约"
                key = "risk-" + stable_hash([related, proposition])[:32]
                output[key] = {"id": key, "origin_work_item_id": owners[caller.goal_id],
                    "related_goal_ids": related, "proposition": proposition,
                    "evidence_requests": [RuntimeReadArguments(file_path=goal.target_ref,
                        start_line=goal.start_line, end_line=goal.end_line).model_dump(mode="json")
                        for goal in (caller, callee)]}
    return [output[key] for key in sorted(output)]
