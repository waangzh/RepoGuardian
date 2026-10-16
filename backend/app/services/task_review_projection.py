"""独立的 task-goals-v1 报告投影，不推断旧 Unit 或缓存语义。"""

from app.models.review_runtime import TaskRuntimeReport


def runtime_failure_summary(diagnostics):
    messages = {
        "model_connection_error": "模型服务连接失败（model_connection_error）",
        "model_timeout": "模型请求超时（model_timeout）",
        "model_authentication_error": "模型服务认证失败（model_authentication_error）",
        "model_permission_denied": "模型服务拒绝访问（model_permission_denied）",
        "model_rate_limited": "模型服务限流（model_rate_limited）",
        "model_request_rejected": "模型服务拒绝请求（model_request_rejected）",
        "model_server_error": "模型服务内部错误（model_server_error）",
        "model_request_failed": "模型请求失败（model_request_failed）",
    }
    for diagnostic in reversed(diagnostics):
        failure = diagnostic.get("provider_failure")
        if not isinstance(failure, dict) or failure.get("category") not in messages:
            continue
        summary = messages[failure["category"]]
        attempts = failure.get("attempts")
        if type(attempts) is int and 0 <= attempts <= 1000:
            summary += f"；本次请求已尝试 {attempts} 次"
        return summary
    return "审查遇到技术故障（technical_failure）；未记录可分类的失败原因"


def project_task_runtime(runtime, steps, goals, *, termination_reason, budget=None):
    outcomes = []
    for goal in goals:
        result = next((step.executor.projection.examinations.get(goal.goal_id)
                       for step in steps.values() if goal.goal_id in step.executor.projection.examinations), None)
        status = "unsupported" if goal.unsupported_reason else result.disposition.value if result else "not_examined"
        outcomes.append({**goal.model_dump(mode="json"), "status": status,
                         "reason": goal.unsupported_reason or (result.reason if result else "no_accepted_examination"),
                         "result_id": result.result_id if result else None})
    derivation = runtime.derivation
    investigations = []
    for step in steps.values():
        item = step.executor.work_item
        if item.kind.value != "investigate":
            continue
        result = step.executor.projection.investigation
        investigations.append({"work_item_id": item.work_item_id, "unit_id": item.unit_id,
            "proposition": item.proposition, "status": "not_executed" if result is None else
                "insufficient_evidence" if result.answer.value == "insufficient_evidence" else "answered",
            "answer": result.answer.value if result else None,
            "reason": item.reason if not result else result.reason,
            "limitations": result.limitations if result else [],
            "relationship_leads": relationship_leads_for_item(
                derivation.data["leads"] if derivation else {}, item.work_item_id)})
    findings = list(derivation.data["candidates"].values()) if derivation else []
    represented = {identity for record in findings for identity in record.get("source_candidate_ids", [])}
    for identity, step in steps.items():
        for candidate in step.executor.projection.candidates.values():
            if candidate.candidate_id not in represented:
                findings.append({"candidate": candidate.model_dump(mode="json"), "status": "unresolved",
                    "reason": "candidate_derivation_incomplete", "issue": None, "origin": identity,
                    "source_candidate_ids": [candidate.candidate_id], "source_result_ids": candidate.source_result_ids})
    leads = [{"lead_id": key, **value} for key, value in derivation.data["leads"].items()
             if value["status"] in {"not_scheduled", "duplicate"}] if derivation else []
    budget = budget or (next(iter(steps.values())).budget if steps else None)
    attempts = list(budget.attempts.values()) if budget else []
    unknown = [attempt for attempt in attempts if attempt.sent and not attempt.usage_available]
    integrity = "unknown" if unknown else "complete"
    if any(item.state.value == "unknown" and item.reason == "attempt_ledger_inconsistent"
           for item in runtime._items.values()):
        integrity = "inconsistent"
    execution = ("cancelled" if runtime.cancelled else "failed" if termination_reason == "technical_failure"
                 else "running" if termination_reason == "running" else "completed")
    spec = next(iter(steps.values())).executor.spec if steps else None
    return TaskRuntimeReport(execution_status=execution, termination_reason=termination_reason,
        failure_summary=runtime_failure_summary(runtime.failures) if execution == "failed" else None,
        readable_scope_hash=spec.readable_scope_hash if spec and spec.readable_scope_files else None,
        readable_file_count=len(spec.readable_scope_files) if spec else None,
        scope_complete=all(value["status"] == "examined" for value in outcomes),
        initial_scope_coverage=outcomes, admitted_investigations=investigations,
        unscheduled_leads=leads, findings=findings, usage_integrity=integrity,
        relationship_hints=[{"lead_id": key, **value} for key, value in derivation.data["leads"].items()
                            if value["status"] == "hint"] if derivation else [],
        sent_calls=sum(attempt.sent for attempt in attempts), settled_tokens=budget.settled_actual if budget else 0,
        unknown_attempts=len(unknown), total_tokens=budget.settled_actual if budget and integrity == "complete" else None,
        diagnostics=runtime.failures,
        work_items=[{**item, "last_observation": steps[item["work_item_id"]].last_observation,
                     "tool_observations": steps[item["work_item_id"]].tool_observations}
                    for item in runtime.snapshot()])


def relationship_leads_for_item(leads, identity):
    return [{"lead_id": key, "relationship": value["relationship"],
             "related_goal_ids": value.get("related_goal_ids", [])}
            for key, value in leads.items() if value.get("work_item_id") == identity and value.get("relationship")]


def group_relationship_leads(leads):
    groups = {}
    for index, item in enumerate(leads):
        relation = item.get("relationship")
        relation = relation if isinstance(relation, dict) else {}
        key = (tuple(str(value) for value in (relation.get("source_file"), relation.get("target_file"),
                    relation.get("type"), item.get("reason"), item.get("status")))
               if relation else ("legacy", index))
        label = (f"{relation.get('source_file')} → {relation.get('target_file')}（{relation.get('type')}）"
                 if relation else item.get("proposition", "未命名线索"))
        groups.setdefault(key, {"label": label, "reason": item.get("reason"),
                                "status": item.get("status"), "members": []})["members"].append(item)
    return list(groups.values())


def relationship_description(item):
    relation = item.get("relationship")
    relation = relation if isinstance(relation, dict) else {}
    sites = ", ".join(f"{site.get('line', '?')}: {site.get('expression', '?')}"
                      for site in (relation.get("call_sites") or []) if isinstance(site, dict))
    return (f"{item.get('lead_id', '?')} · {relation.get('source', '?')} → {relation.get('target', '?')}"
            f" · 调用位置 {sites or '未记录'} · {relation.get('resolution_status') or 'unknown'}"
            f" / {relation.get('resolution_basis') or 'unknown'}"
            f" · 关联目标 {', '.join(item.get('related_goal_ids', [])) or '未记录'}")


def task_runtime_coverage(report, changed):
    outcomes = report.initial_scope_coverage
    paths = {file.file_path for file in changed}
    reviewed = {path for path in paths if (selected := [item for item in outcomes if item["target_ref"] == path])
                and all(item["status"] == "examined" for item in selected)}
    partial = {item["target_ref"] for item in outcomes if item["status"] == "examined"} - reviewed
    groups = {item["unit_id"] for item in outcomes}
    complete_groups = {group for group in groups if all(item["status"] == "examined"
                      for item in outcomes if item["unit_id"] == group)}
    files = []
    for path in sorted(paths):
        selected = [item for item in outcomes if item["target_ref"] == path]
        status = "reviewed" if path in reviewed else "partial" if path in partial else (
            "unsupported" if any(item["status"] == "unsupported" for item in selected) else "pending")
        files.append({"file_path": path, "eligible": True, "status": status,
            "review_unit_ids": sorted({item["unit_id"] for item in selected if item["unit_id"] != "excluded"}),
            "reason": None if status == "reviewed" else "；".join(item.get("reason") or item["status"] for item in selected),
            "omitted_targets": [item["goal_id"] for item in selected if item["status"] != "examined"]})
    return {"coverage_version": "task-goals-v1", "review_complete": report.scope_complete,
        "changed_files": len(paths), "eligible_files": len(paths), "reviewed_files": len(reviewed),
        "partial_files": len(partial), "coverage_rate": len(reviewed) / len(paths) if paths else 1,
        "total_units": len(groups), "completed_units": len(complete_groups),
        "unit_coverage_rate": len(complete_groups) / len(groups) if groups else 1,
        "coordination_status": "unknown", "coordination_reason": "独立 task-goals-v1 调查口径", "files": files}


def task_runtime_issues(report):
    """仅向旧 findings 展示字段投影，绝不合成旧 Unit 成功记录。"""
    from app.models.review import ReviewIssue, IssueStatus
    output = []
    for record in report.findings:
        if not record.get("issue"):
            continue  # 无完整 Issue 的候选仍保留在新报告 findings 中。
        issue = ReviewIssue.model_validate(record["issue"])
        output.append(issue.model_copy(update={"status": IssueStatus(record["status"]),
            "unresolved_reason": record.get("reason"), "source_issue_ids": record["source_candidate_ids"]}).model_dump(mode="json"))
    return output
