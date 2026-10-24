"""执行器与实验管理共用的摘要与门槛规则。"""

from pathlib import Path

from app.evaluation.data import sha256
from app.evaluation.runtime_baseline import reconcile_wire_usage
from app.core.fingerprints import stable_hash


GROUPS = {
    "protocol_180": {"investigations_enabled": False, "unit_timeout_seconds": 180},
    "full_180": {"investigations_enabled": True, "unit_timeout_seconds": 180},
    "full_runtime": {"investigations_enabled": True, "unit_timeout_seconds": None},
}


def compare_fixed_scope(result, baseline):
    observed = {}
    for outcome in (result.get("task_runtime_report") or {}).get("initial_scope_coverage", []):
        key = (outcome.get("target_ref"), outcome.get("start_line"), outcome.get("end_line"))
        status = outcome.get("status")
        if key in observed and observed[key] != status:
            observed[key] = "conflicting"
        else:
            observed[key] = status
    fixed, missing = [], []
    for scope in baseline["fixed_scope"]:
        key = (scope["file_path"], scope["start_line"], scope["end_line"])
        status = observed.get(key)
        if status is None:
            missing.append(scope["hunk_id"])
            status = "unexamined"
        fixed.append({**scope, "new_status": status})
    total = len(fixed)
    examined = sum(item["new_status"] == "examined" for item in fixed)
    old_unexamined = sum(item["status"] != "reviewed" for item in fixed)
    return {"total_fixed_targets": total, "examined": examined, "missing_targets": missing,
            "unexamined_ratio": (total - examined) / total if total else 0,
            "baseline_unexamined": old_unexamined,
            "baseline_unexamined_ratio": old_unexamined / total if total else 0,
            "not_worse_than_baseline": (total - examined) / total <= old_unexamined / total if total else True,
            "items": fixed}


def implementation_digest() -> str:
    code_root = Path(__file__).parents[1]
    content = b"".join(path.relative_to(code_root).as_posix().encode() + b"\0" +
                       path.read_bytes() + b"\0" for path in sorted(code_root.rglob("*.py")))
    return sha256(content)


def first_sample_gate(result, plan, group):
    identity = plan["first_sample_id"]
    baseline = plan["samples"][identity]
    accounting = reconcile_wire_usage(result)
    report = result.get("task_runtime_report") or {}
    configuration = result.get("task_runtime_configuration") or {}
    errors = []
    if result.get("sample") != baseline["sample"] or result.get("diff_input") != baseline["diff_input"]:
        errors.append("sample_or_diff_changed")
    if report.get("readable_scope_hash") != baseline["readable_scope_hash"]:
        errors.append("readable_scope_changed")
    coverage = compare_fixed_scope(result, baseline)
    if coverage["missing_targets"]:
        errors.append("fixed_scope_targets_missing")
    if configuration.get("experiment") != GROUPS[group]:
        errors.append("experiment_group_changed")
    if plan.get("runtime_versions") is not None and any(
            configuration.get(key) != value for key, value in plan["runtime_versions"].items()):
        errors.append("runtime_versions_changed")
    if configuration.get("provider") != "deepseek" or configuration.get("model") != "deepseek-flash":
        errors.append("provider_or_model_changed")
    if accounting["integrity"] != "complete" or accounting["wire_attempts"] < 1:
        errors.append("wire_usage_incomplete")
    if not report or result.get("error_type") or report.get("execution_status") in {"failed", "cancelled"}:
        errors.append("runtime_technical_failure")
    if any(item.get("state") in {"unknown", "result_missing", "failed"} for item in report.get("work_items", [])):
        errors.append("work_item_technical_failure")
    return {"version": "runtime-first-gate-v1", "plan_sha256": plan["plan_sha256"], "group": group,
        "sample_id": identity, "result_sha256": stable_hash(result), "admitted": not errors, "errors": errors,
        "accounting": accounting, "coverage_complete": report.get("scope_complete", False),
        "tool_read_events": len(result.get("tool_reads") or []),
        "tool_connection": "observed" if result.get("tool_reads") else "not_exercised_in_real_sample",
        "fixed_scope_coverage": coverage,
        "quality_proven": False, "default_switch_allowed": False}
