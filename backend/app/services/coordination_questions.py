"""协调规划问题的稳定绑定与运行投影；未覆盖的问题不能由其他补查关闭。"""

from app.models.review import CrossUnitPlanningQuestion, CrossUnitFollowupResult
from app.review.diff_base import review_diff_base
from app.services.fingerprints import stable_hash


def bind_plan_questions(plan):
    questions = []
    for index, text in enumerate(plan.unresolved_questions):
        linked = [request.id for request in plan.followups
                  if index in request.planning_question_indices or text == request.question]
        questions.append(CrossUnitPlanningQuestion(id="planning-question-" + stable_hash(text)[:32],
            question=text, followup_ids=linked,
            reason="尚未执行关联补查" if linked else "没有补查覆盖此规划问题"))
    return plan.model_copy(update={"question_states": questions})


def refresh_plan_questions(plan, results, state):
    if not plan.question_states:
        plan = bind_plan_questions(plan)
    known = {item.question for item in plan.question_states}
    additions = [item for item in bind_plan_questions(plan).question_states if item.question not in known]
    plan = plan.model_copy(update={"question_states": [*plan.question_states, *additions]})
    by_id = {raw["request_id"]: CrossUnitFollowupResult.model_validate(raw) for raw in results}
    requests = {item.id: item for item in plan.followups}
    questions = []
    for question in plan.question_states:
        linked = [by_id.get(identity) for identity in question.followup_ids]
        statuses, reasons = [], []
        if not linked or any(item is None for item in linked):
            statuses.append("pending")
            reasons.append("关联补查尚未执行或未保存结果" if linked else "没有补查覆盖此规划问题")
        for item in linked:
            if item is None:
                continue
            unit = item.unit_result
            summary = unit.review_summary if unit else None
            record = summary.record if summary else None
            if summary and summary.latest_attempt_status == "invalid":
                statuses.append("invalid")
                reasons.append(item.reason)
                continue
            snapshot = summary.last_valid_snapshot if summary else {}
            request = requests.get(item.request_id)
            closed = {update.obligation_id for update in record.obligation_updates} if record else set()
            refs = {reference.id: reference.file_path for reference in summary.evidence} if summary else {}
            cited = {refs[key] for check in record.hypothesis_checks for key in check.evidence_ids if key in refs} if record else set()
            refuted = (record and record.hypothesis_checks and all(check.status == "refuted" and check.evidence_ids
                and set(check.evidence_ids) <= refs.keys() for check in record.hypothesis_checks)
                and request and (set(request.verification_files) <= cited if request.verification_files else all(
                    set(raw["primary_files"]) & cited for raw in state.get("review_units", []) if raw["id"] in request.unit_ids)))
            valid = (unit and unit.status == "completed" and summary.status == "reported"
                and summary.latest_attempt_status == "reported" and record
                and snapshot.get("head_sha") == state.get("head_sha")
                and snapshot.get("base_sha", "") == review_diff_base(state)
                and record.target_checks and all(check.status == "checked" for check in record.target_checks)
                and not record.unresolved_questions
                and all(check.status == "verified" for check in record.contract_dependencies)
                and request and set(request.origin_obligation_ids) <= closed
                and item.validation_status == "completed"
                and item.outcome in {"candidate_found", "refuted"}
                and (refuted if item.outcome == "refuted" else bool(unit.issues))
                and not any(issue.status in {"candidate", "evidence_resolved", "unresolved", "needs_human"}
                            for issue in unit.issues))
            if not valid:
                statuses.append("pending" if item.reason.startswith("deferred_budget_not_executed") else "unresolved")
            reasons.append(item.reason)
        status = next((value for value in ("invalid", "unresolved", "pending") if value in statuses), "resolved")
        questions.append(question.model_copy(update={"status": status, "reason": "；".join(dict.fromkeys(reasons))[:1000]}))
    return plan.model_copy(update={"question_states": questions,
        "unresolved_questions": [item.question for item in questions if item.status != "resolved"]})
