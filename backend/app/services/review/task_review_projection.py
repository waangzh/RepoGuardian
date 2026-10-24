"""独立的 task-goals-v1 报告投影，不推断旧 Unit 或缓存语义。"""

from app.models.review_runtime import TaskRuntimeReport


REASON_HINTS = {
    "unknown_author_target": "作者目标引用不在冻结声明中",
    "intent_evidence_scope_mismatch": "目标对照引用超出本次检查范围",
    "intent_evidence_outside_examination": "目标对照引用了本次检查之外的证据",
    "intent_evidence_missing": "支持或反驳目标缺少证据",
    "intent_examination_not_examined": "目标对照绑定到未完成的检查",
    "missing_intent_assessment": "检查未提供作者目标对照",
    "invalid_intent_assessment_binding": "作者目标对照未通过验收",
    "invalid_json": "模型返回的 JSON 无法解析",
    "invalid_response_kind": "模型返回了不支持的响应类型",
    "incomplete_goal_evidence": "检查引用的证据未覆盖完整目标范围",
    "incomplete_investigation_evidence": "调查提交的证据引用未覆盖全部必要范围",
    "response_schema_invalid": "模型返回结构未通过校验",
}
_REJECTION_REASON_CODES = set(REASON_HINTS) | {
    "unknown_submit_field", "field_must_be_array", "array_limit_exceeded",
    "read_path_outside_work_item_scope", "incomplete_investigation_evidence",
    "discussion_resolution_mismatch", "verification_candidate_identity_mismatch",
    "verification_subject_hash_mismatch", "publication_narrowing_has_no_change",
    "incomplete_verification_evidence", "conflicting_duplicate_target", "unknown_target_id",
    "target_not_in_current_page", "unknown_primary_anchor", "anchor_evidence_mismatch",
    "unknown_related_target", "unknown_or_inactive_evidence", "diff_observation_requires_source_anchor",
    "stale_evidence_snapshot", "unknown_evidence_side", "evidence_outside_scope",
    "evidence_content_hash_mismatch", "invalid_evidence_source_range", "intent_contract_not_enabled",
    "repeated_read_no_progress", "response_validation_failed", "finalization_requires_submit",
}


def runtime_failure_summary(diagnostics, *, termination_reason=None):
    for diagnostic in diagnostics or []:
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("category") == "evidence_identity_conflict":
            return "运行时证据身份冲突（evidence_identity_conflict）；审查已停止，已接受结果保留。"
        if diagnostic.get("category") == "runtime_internal_error":
            error_type = diagnostic.get("error_type")
            detail = f"，{error_type}" if error_type in _ALLOWED_ERROR_TYPES else ""
            return f"审查运行时内部错误（runtime_internal_error{detail}）；审查已停止，已接受结果保留。"
    codes = []
    for diagnostic in diagnostics or []:
        if not isinstance(diagnostic, dict):
            continue
        for value in diagnostic.get("reason_codes") or []:
            if isinstance(value, str) and value in REASON_HINTS and value not in codes:
                codes.append(value)
    if codes:
        return "模型多次提交仍未通过服务端验收（" + "；".join(
            f"{REASON_HINTS[code]}（{code}）" for code in codes[:3]) + "）。请重试或检查模型输出。"
    if any(isinstance(diagnostic, dict) and diagnostic.get("category") == "model_response_rejected"
           for diagnostic in diagnostics or []):
        return "模型多次提交仍未通过服务端验收（model_response_rejected）；请重试或检查模型输出协议。"
    if termination_reason == "model_response_rejected":
        return "模型多次提交仍未通过服务端验收（model_response_rejected）；请重试或检查模型输出协议。"
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
        if not isinstance(diagnostic, dict):
            continue
        failure = diagnostic.get("provider_failure")
        if not isinstance(failure, dict) or failure.get("category") not in messages:
            continue
        summary = messages[failure["category"]]
        attempts = failure.get("attempts")
        if type(attempts) is int and 0 <= attempts <= 1000:
            summary += f"；本次请求已尝试 {attempts} 次"
        return summary
    return "审查遇到技术故障（technical_failure）；未记录可分类的失败原因"


def project_task_runtime(runtime, steps, goals, *, termination_reason, budget=None,
                         reused_coverage=None):
    reused_coverage = reused_coverage or {}
    outcomes = []
    for goal in goals:
        result = next((step.executor.projection.examinations.get(goal.goal_id)
                       for step in steps.values() if goal.goal_id in step.executor.projection.examinations), None)
        if goal.goal_id in reused_coverage:
            outcomes.append({**reused_coverage[goal.goal_id], **goal.model_dump(mode="json"),
                             "reason": "reused_from_previous_review"})
            continue
        status = "unsupported" if goal.unsupported_reason else result.disposition.value if result else "not_examined"
        outcomes.append({**goal.model_dump(mode="json"), "status": status,
                         "reason": goal.unsupported_reason or (result.reason if result else "no_accepted_examination"),
                         "result_id": result.result_id if result else None,
                         **({"evidence_ids": result.evidence_ids,
                             "snapshot_head_sha": result.snapshot_head_sha,
                             "snapshot_base_sha": next(step.executor.spec.snapshot_base_sha for step in steps.values()
                                 if goal.goal_id in step.executor.projection.examinations),
                             "intent_assessments": [value.model_dump(mode="json") for value in result.intent_assessments]}
                            if result and result.intent_assessments else {})})
    derivation = runtime.derivation
    investigations = []
    for step in steps.values():
        item = step.executor.work_item
        if item.kind.value != "investigate":
            continue
        result = step.executor.projection.investigation
        investigations.append({"work_item_id": item.work_item_id, "unit_id": item.unit_id,
            "proposition": item.proposition, "status": (
                "failed" if item.state.value == "failed" else "incomplete" if item.started else "not_executed")
                if result is None else
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
    execution = ("cancelled" if runtime.cancelled else "failed" if termination_reason in {
                     "technical_failure", "model_response_rejected"}
                 else "running" if termination_reason == "running" else "completed")
    spec = next(iter(steps.values())).executor.spec if steps else None
    diagnostics = _rejection_diagnostics(runtime, steps, termination_reason)
    work_items = []
    for item in runtime.snapshot():
        step = steps[item["work_item_id"]]
        record = {**item, "last_observation": step.last_observation,
                  "tool_observations": step.tool_observations}
        if spec and spec.stability_contract == "review-stability-v2":
            projection = step.executor.projection
            record["retryable_gap"] = item["state"] not in {"unknown", "result_missing"} and bool(
                projection.pending_target_ids or any(result.disposition.value == "blocked"
                    for result in projection.examinations.values())
                or (projection.investigation is not None
                    and projection.investigation.answer.value == "insufficient_evidence"))
        work_items.append(record)
    return TaskRuntimeReport(execution_status=execution, termination_reason=termination_reason,
        pr_intent_context=spec.pr_intent_context if spec else None,
        pr_intent_context_truncated=spec.pr_intent_context_truncated if spec else None,
        failure_summary=(runtime_failure_summary(diagnostics, termination_reason=termination_reason)
                         if execution == "failed" else None),
        readable_scope_hash=spec.readable_scope_hash if spec and spec.readable_scope_files else None,
        readable_file_count=len(spec.readable_scope_files) if spec else None,
        scope_complete=all(value["status"] == "examined" for value in outcomes),
        initial_scope_coverage=outcomes, admitted_investigations=investigations,
        unscheduled_leads=leads, findings=findings, usage_integrity=integrity,
        relationship_hints=[{"lead_id": key, **value} for key, value in derivation.data["leads"].items()
                            if value["status"] == "hint"] if derivation else [],
        sent_calls=sum(attempt.sent for attempt in attempts), settled_tokens=budget.settled_actual if budget else 0,
        unknown_attempts=len(unknown), total_tokens=budget.settled_actual if budget and integrity == "complete" else None,
        diagnostics=diagnostics,
        work_items=work_items)


_PROVIDER_FIELDS = {"category", "error_type", "attempts", "status_code", "cause_types"}
_DIAGNOSTIC_FIELDS = {"stage", "error_type", "provider_failure", "work_item_id", "category",
                      "reason_codes", "format_corrections", "frames"}
_ALLOWED_ERROR_TYPES = {
    "APIConnectionError", "APITimeoutError", "AuthenticationError", "PermissionDeniedError",
    "RateLimitError", "BadRequestError", "NotFoundError", "UnprocessableEntityError",
    "InternalServerError", "LLMProviderError",
    "ValueError", "TypeError", "KeyError", "RuntimeError", "OSError", "TimeoutError",
    "EvidenceIdentityConflict",
}
_RUNTIME_FRAME_FUNCTIONS = {
    "advance_one", "advance_round", "_advance_round", "run", "refresh", "_candidate",
    "_all_evidence", "_policy_issue", "_verify", "_investigation", "restore_item",
    "advance", "apply_response", "_read", "_other_read", "persist", "_persist_locked",
    "restore", "_restore_step", "task_review_runtime_node",
}


def _safe_diagnostic(failure: dict) -> dict:
    """审计导出只保留固定分类、计数与受控原因代码，不携带异常文本。"""
    value = {key: failure[key] for key in _DIAGNOSTIC_FIELDS if key in failure}
    if value.get("error_type") not in _ALLOWED_ERROR_TYPES:
        value.pop("error_type", None)
    if not isinstance(value.get("reason_codes"), list):
        value.pop("reason_codes", None)
    else:
        value["reason_codes"] = [code for code in value["reason_codes"]
                                 if isinstance(code, str) and code in _REJECTION_REASON_CODES]
    if "frames" in value:
        value["frames"] = [{"function": frame["function"], "line": frame["line"]}
                           for frame in value["frames"][:12]
                           if (isinstance(frame, dict)
                               and isinstance(frame.get("function"), str)
                               and frame.get("function") in _RUNTIME_FRAME_FUNCTIONS
                               and type(frame.get("line")) is int and 0 < frame["line"] <= 10_000_000)
                           ] if isinstance(value["frames"], list) else []
    provider = failure.get("provider_failure")
    if isinstance(provider, dict):
        safe = {key: provider[key] for key in _PROVIDER_FIELDS if key in provider}
        # error_type 与 cause_types 只接受固定枚举，旧记录中的自由文本一律丢弃。
        if safe.get("error_type") not in _ALLOWED_ERROR_TYPES:
            safe.pop("error_type", None)
        if isinstance(safe.get("cause_types"), list):
            safe["cause_types"] = [value for value in safe["cause_types"]
                                   if value in _ALLOWED_ERROR_TYPES or value in {
                                       "ConnectError", "ProxyError", "ConnectTimeout", "ReadTimeout",
                                       "WriteTimeout", "PoolTimeout", "ReadError", "WriteError",
                                       "RemoteProtocolError", "LocalProtocolError", "ConnectionRefusedError",
                                       "ConnectionResetError", "SSLCertVerificationError", "SSLError", "gaierror"}]
        value["provider_failure"] = safe
    return value


def _rejection_diagnostics(runtime, steps, termination_reason):
    """模型输出被拒时补充可诊断类别；不导出模型原文或自由文本。"""
    diagnostics = [_safe_diagnostic(failure) for failure in runtime.failures if isinstance(failure, dict)]
    rejected = [item for item in runtime._items.values()
                if item.state.value == "failed" and item.reason == "model_response_rejected"]
    if not rejected:
        return diagnostics
    codes: list[str] = []
    corrections = 0
    for item in rejected:
        step = steps.get(item.work_item_id)
        observation = getattr(step, "last_observation", {}) or {}
        for error in observation.get("errors") or []:
            reason = error.get("reason") if isinstance(error, dict) else None
            if isinstance(reason, str):
                code = (reason if reason in _REJECTION_REASON_CODES else
                        "response_schema_invalid" if "validation error" in reason else "response_validation_failed")
                if code not in codes:
                    codes.append(code)
        if step is not None:
            corrections = max(corrections, step.executor.work_item.format_corrections)
    diagnostics.append({"category": "model_response_rejected", "stage": "task_advance",
                        "reason_codes": codes, "format_corrections": corrections,
                        "detail": "exception_text_omitted_for_safety"})
    return diagnostics


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


def task_runtime_summary(report: TaskRuntimeReport | dict) -> dict:
    """Return one semantic summary for UI and report consumers.

    Findings, investigations, and scope outcomes use different state machines;
    this summary keeps their denominators separate instead of inferring them
    from the legacy ``ReviewTask.issues`` projection.
    """
    value = report if isinstance(report, TaskRuntimeReport) else TaskRuntimeReport.model_validate(report)
    findings = value.findings
    finding_statuses = [str(item.get("status", "unknown")) for item in findings]
    investigations = value.admitted_investigations
    investigation_statuses = [str(item.get("status", "unknown")) for item in investigations]
    scope_statuses = [str(item.get("status", "unknown")) for item in value.initial_scope_coverage]
    publishable = sum(
        1 for item in findings
        if item.get("status") in {"confirmed", "published"}
        and bool((item.get("publication") or {}).get("published_text"))
    )
    return {
        "execution": {
            "status": value.execution_status,
            "termination_reason": value.termination_reason,
            "scope_complete": value.scope_complete,
        },
        "findings": {
            "total": len(findings),
            "active": sum(status != "dismissed" for status in finding_statuses),
            "unknown": sum(status not in {
                "confirmed", "published", "candidate", "unresolved", "dismissed"}
                for status in finding_statuses),
            "confirmed": sum(status in {"confirmed", "published"} for status in finding_statuses),
            "dismissed": finding_statuses.count("dismissed"),
            "unresolved": sum(status in {"candidate", "unresolved"} for status in finding_statuses),
            "without_issue": sum(not item.get("issue") for item in findings),
            "publishable": publishable,
        },
        "investigations": {
            "total": len(investigations),
            "answered": investigation_statuses.count("answered"),
            "insufficient_evidence": investigation_statuses.count("insufficient_evidence"),
            "not_executed": investigation_statuses.count("not_executed"),
            "failed": investigation_statuses.count("failed"),
            "incomplete": investigation_statuses.count("incomplete"),
        },
        "scope": {
            "total": len(scope_statuses),
            "examined": scope_statuses.count("examined"),
            "incomplete": sum(status != "examined" for status in scope_statuses),
            "blocked": scope_statuses.count("blocked"),
            "unsupported": scope_statuses.count("unsupported"),
            "not_examined": scope_statuses.count("not_examined"),
        },
        "usage": {
            "integrity": value.usage_integrity,
            "unknown_attempts": value.unknown_attempts,
        },
    }


def task_runtime_issues(report):
    """仅向旧 findings 展示字段投影，绝不合成旧 Unit 成功记录。"""
    from app.models.review import ReviewIssue, IssueStatus
    output = []
    for record in report.findings:
        if not record.get("issue"):
            continue  # 无完整 Issue 的候选仍保留在新报告 findings 中。
        issue = ReviewIssue.model_validate(record["issue"])
        output.append(issue.model_copy(update={"status": IssueStatus(record["status"]),
            "unresolved_reason": record.get("reason"), "source_issue_ids": record.get("source_candidate_ids", [])}).model_dump(mode="json"))
    return output
