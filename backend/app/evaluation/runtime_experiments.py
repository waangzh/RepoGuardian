"""P5 无答案实验冻结与首样本门；不把运行终态当成质量分数。"""

from __future__ import annotations

import json
from pathlib import Path

from app.evaluation.data import DatasetLock, sha256
from app.evaluation.diff_inputs import DiffInputLock
from app.evaluation.runtime_baseline import reconcile_wire_usage
from app.tools.diff_parser import DiffParser
from app.services.review_planner import DeterministicReviewPlanner
from app.review.tool_scope import is_sensitive_repository_path, list_git_tracked_files
from app.services.fingerprints import stable_hash
from app.tools.git_tool import GitTool


GROUPS = {
    "protocol_180": {"investigations_enabled": False, "unit_timeout_seconds": 180},
    "full_180": {"investigations_enabled": True, "unit_timeout_seconds": 180},
    "full_runtime": {"investigations_enabled": True, "unit_timeout_seconds": None},
}
FIXED_SETTINGS = {
    "repoguardian_provider": "deepseek", "repoguardian_model": "deepseek-flash",
    "repoguardian_review_engine": "task_review_v1", "repoguardian_unit_input_mode": "canonical",
    "repoguardian_review_unit_concurrency": 4,
    "repoguardian_model_request_timeout_seconds": 60.0, "repoguardian_model_request_attempts": 2,
    "repoguardian_task_review_timeout_seconds": 1800.0,
    "repoguardian_run_max_calls": 200, "repoguardian_run_max_tokens": 1_000_000,
    "repoguardian_run_max_cost_microusd": None,
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def freeze_experiment_plan(lock_path, diff_path, selection_path, baseline_runs, source_roots):
    """只读复核旧轨迹、快照及权限；输出不含候选、质量标签或参考答案。"""
    lock_bytes, diff_bytes = Path(lock_path).read_bytes(), Path(diff_path).read_bytes()
    lock = DatasetLock.model_validate_json(lock_bytes)
    diff = DiffInputLock.model_validate_json(diff_bytes)
    diff.validate_dataset(lock)
    selection = read_json(selection_path)
    ids = [value["sample"]["instance_id"] for value in selection["samples"]]
    if len(ids) != 5 or len(set(ids)) != 5 or selection["first_sample_id"] != ids[0]:
        raise ValueError("expected frozen first-one/remaining-four selection")
    frozen = {value.instance_id: value for value in diff.samples}
    samples = {value.instance_id: value for value in lock.samples}
    roots = {value["repository"]: value for value in read_json(source_roots)}
    baselines = {}
    expected_profile = None
    effective_profile = None
    artifacts = []
    for run in baseline_runs:
        run = Path(run)
        summary = read_json(run / "run.json")
        if (summary["lock_sha256"] != sha256(lock_bytes) or summary["diff_lock_sha256"] != sha256(diff_bytes)
                or summary["timeout_seconds"] != 1800):
            raise ValueError("baseline lock or timeout mismatch")
        for name, value in FIXED_SETTINGS.items():
            if name in {"repoguardian_review_engine", "repoguardian_task_review_timeout_seconds",
                        "repoguardian_run_max_tokens"}:
                continue  # r9 安全配置摘要主动省略 token 名称；从每样本账本核对。
            if summary["config"].get(name) != value:
                raise ValueError("baseline setting mismatch: " + name)
        profile = summary["config"]["repoguardian_model_request_profile"]
        if expected_profile is not None and profile != expected_profile:
            raise ValueError("baseline request profile mismatch")
        expected_profile = profile
        artifacts.append({"kind": "baseline_run", "path": str((run / "run.json").resolve()),
                          "sha256": sha256((run / "run.json").read_bytes())})
        for record in summary["samples"]:
            identity = record["instance_id"]
            if identity not in ids:
                continue
            if identity in baselines:
                raise ValueError("duplicate baseline sample")
            path = run / record["result_file"]
            result = read_json(path)
            sample, fact = samples[identity], frozen[identity]
            if result["sample"] != sample.model_dump(mode="json") or result["diff_input"] != fact.model_dump(mode="json"):
                raise ValueError("baseline sample or canonical diff mismatch")
            if result["run_budget"]["limits"] != {"max_calls": 200, "max_tokens": 1_000_000,
                                                    "max_cost_microusd": None}:
                raise ValueError("baseline total budget mismatch")
            accounting = reconcile_wire_usage(result)
            if accounting["integrity"] != "complete":
                raise ValueError("baseline usage is not independently reconcilable")
            if effective_profile is not None and result["effective_model_profile"] != effective_profile:
                raise ValueError("baseline effective profile mismatch")
            effective_profile = result["effective_model_profile"]
            root = roots[sample.repository]
            git = GitTool()
            if (root["head_sha"] != fact.head_sha or root["base_sha"] != fact.effective_base_sha
                    or git._run([git._git, "-C", root["source_root"], "rev-parse", "HEAD"]).strip() != fact.head_sha):
                raise ValueError("baseline source snapshot mismatch")
            text = git._run([git._git, "-C", root["source_root"], "diff", "--no-ext-diff", "--unified=80",
                             fact.effective_base_sha, fact.head_sha])
            if sha256(text.encode()) != fact.diff_sha256:
                raise ValueError("baseline source canonical diff mismatch")
            changed = DiffParser().parse(text)
            fixed_scope = []
            for file in changed:
                if file.hunks:
                    for index, hunk in enumerate(file.hunks):
                        fixed_scope.append({"file_path": file.file_path, "hunk_id":
                            DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json")),
                            "start_line": max(1, hunk.new_start),
                            "end_line": max(1, hunk.new_start + hunk.new_length - 1)})
                else:
                    fixed_scope.append({"file_path": file.file_path, "hunk_id": "file:" + file.file_path,
                                        "start_line": 1, "end_line": 1, "metadata_only": True})
            fixed_status = {}
            for unit in result.get("unit_results", []):
                manifest, ledger = unit.get("diff_manifest") or {}, unit.get("coverage_ledger") or {}
                latest = unit.get("review_summary") or {}
                valid = (manifest.get("input_hash") == ledger.get("manifest_hash")
                    and manifest.get("snapshot") == ledger.get("snapshot")
                    and (manifest.get("snapshot") or {}).get("head_sha") == fact.head_sha
                    and (manifest.get("snapshot") or {}).get("base_sha") == fact.effective_base_sha
                    and latest.get("status") == "reported" and latest.get("record") is not None
                    and latest.get("latest_attempt_status") not in {"missing", "invalid", "failed", "not_executed", "unknown"})
                for hunk in manifest.get("hunks") or []:
                    identity_key = (hunk.get("file_path"), hunk.get("hunk_id"))
                    check = ledger.get("diff_hunks", {}).get(hunk.get("id")) or {}
                    cursor = 0
                    for left, right in sorted(check.get("reviewed_ranges") or []):
                        if left > cursor or not 0 <= left <= right <= hunk["line_count"]:
                            break
                        cursor = max(cursor, right)
                    status = "reviewed" if valid and check.get("status") == "reviewed" and cursor >= hunk["line_count"] else "unexamined"
                    if identity_key in fixed_status and fixed_status[identity_key] != status:
                        raise ValueError("conflicting baseline fixed-scope coverage")
                    fixed_status[identity_key] = status
            fixed_coverage = [{**scope, "status": fixed_status.get((scope["file_path"], scope["hunk_id"]),
                                                                     "unexamined")}
                              for scope in fixed_scope]
            paths = sorted(path for path in list_git_tracked_files(root["source_root"])
                           if not is_sensitive_repository_path(path))
            baselines[identity] = {"sample": sample.model_dump(mode="json"), "diff_input": fact.model_dump(mode="json"),
                "readable_scope_hash": stable_hash(paths), "readable_file_count": len(paths),
                "fixed_scope": fixed_coverage,
                "wire_attempts": accounting["wire_attempts"], "tokens": accounting["actual_total_tokens"],
                "baseline_result_sha256": sha256(path.read_bytes()),
                "baseline_result_path": str(path.resolve())}
    if set(baselines) != set(ids):
        raise ValueError("missing baseline sample")
    from app.evaluation.runner import implementation_digest
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    configuration = task_runtime_configuration("deepseek-flash")
    payload = {"version": "runtime-experiments-v1", "lock_sha256": sha256(lock_bytes),
        "diff_lock_sha256": sha256(diff_bytes), "first_sample_id": ids[0], "sample_ids": ids,
        "groups": GROUPS, "settings": FIXED_SETTINGS, "request_profile": expected_profile,
        "effective_model_profile": effective_profile,
        "implementation_sha256": implementation_digest(),
        "runtime_versions": {key: configuration[key] for key in ("prompt_version", "risk_planner_version")},
        "timeout_seconds": 1800, "output_tokens": 4096, "samples": baselines,
        "baseline_artifacts": artifacts, "maximum_new_samples": 15,
        "maximum_new_wire_attempts": 3_000, "maximum_new_tokens": 15_000_000,
        "screening": {"token_reduction": 0.25, "unexamined_scope_increase": 0,
                      "valid_defect_decrease": 0, "false_positive_increase": 0},
        "quality_status": "independent_semantic_adjudication_incomplete",
        "default_switch_allowed": False}
    return {**payload, "plan_sha256": stable_hash(payload)}


def validate_experiment_plan(plan, lock_bytes, diff_bytes, group, stage, sample_ids,
                             first_gate=None, quality_gate=None):
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (plan["version"] != "runtime-experiments-v1" or plan["plan_sha256"] != stable_hash(payload)
            or plan["groups"] != GROUPS or plan["settings"] != FIXED_SETTINGS
            or plan["lock_sha256"] != sha256(lock_bytes) or plan["diff_lock_sha256"] != sha256(diff_bytes)):
        raise ValueError("experiment frozen plan mismatch")
    versions = plan.get("runtime_versions")
    if versions is not None:
        if (not isinstance(versions, dict) or set(versions) != {"prompt_version", "risk_planner_version"}
                or versions.get("prompt_version") not in {f"task-review-prompt-v{index}" for index in range(1, 6)}
                or versions.get("risk_planner_version") not in {"legacy", "risk-planner-v2", "risk-planner-v3", "risk-planner-v4"}):
            raise ValueError("experiment runtime versions invalid")
    if group not in GROUPS or stage not in {"first", "remaining"}:
        raise ValueError("unknown experiment group or stage")
    expected = [plan["first_sample_id"]] if stage == "first" else plan["sample_ids"][1:]
    if sample_ids != expected:
        raise ValueError("experiment requires exact first-one/remaining-four order")
    if stage == "remaining":
        if (not first_gate or first_gate.get("plan_sha256") != plan["plan_sha256"]
                or first_gate.get("group") != group or first_gate.get("admitted") is not True
                or not isinstance(first_gate.get("result_sha256"), str)
                or len(first_gate["result_sha256"]) != 64):
            raise ValueError("remaining samples require a successful same-group first gate")
        from app.evaluation.runtime_quality_gate import validate_material_quality_gate
        validate_material_quality_gate(plan, group, first_gate, quality_gate)
        if (not quality_gate or quality_gate.get("version") != "runtime-quality-gate-v2"
                or quality_gate.get("gate_sha256") != stable_hash({key: value for key, value in quality_gate.items()
                                                                   if key != "gate_sha256"})
                or quality_gate.get("plan_sha256") != plan["plan_sha256"]
                or quality_gate.get("group") != group
                or quality_gate.get("sample_id") != plan["first_sample_id"]
                or quality_gate.get("result_sha256") != first_gate.get("result_sha256")
                or quality_gate.get("adjudication_status") != "complete"
                or quality_gate.get("quality_passed") is not True
                or quality_gate.get("expand_allowed") is not True):
            raise ValueError("remaining samples require an independent quality gate")
        metrics = quality_gate.get("metrics") or {}
        reduction = metrics.get("token_reduction")
        counts = [metrics.get(name) for name in ("lost_baseline_defects", "new_false_positives",
                  "baseline_false_positives", "new_cannot_determine")]
        if (not isinstance(reduction, (float, int)) or isinstance(reduction, bool)
                or not 0 <= reduction <= 1
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts)):
            raise ValueError("remaining samples require valid quality metrics")
        if (first_gate.get("fixed_scope_coverage", {}).get("not_worse_than_baseline") is not True
                or metrics.get("token_reduction", -1) < plan["screening"]["token_reduction"]
                or metrics.get("lost_baseline_defects") != 0
                or metrics.get("new_false_positives", 1) > metrics.get("baseline_false_positives", 0)
                or metrics.get("new_cannot_determine", 1) != 0):
            raise ValueError("remaining samples fail joint quality/scope/cost screening")


def claim_experiment_slot(plan, group, stage, registry: Path, output: Path):
    """同一清单每组每阶段只能派发一次，限制总消费而非只限制单次命令。"""
    if output.exists():
        raise ValueError("experiment output already exists")
    slot = registry / plan["plan_sha256"] / (group + "-" + stage)
    slot.parent.mkdir(parents=True, exist_ok=True)
    try:
        slot.mkdir(exist_ok=False)
    except FileExistsError:
        raise ValueError("experiment stage already reserved; new audit plan required") from None
    (slot / "receipt.json").write_text(json.dumps({"plan_sha256": plan["plan_sha256"],
        "group": group, "stage": stage, "output": str(output.resolve())}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


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


def compare_group_run(plan, run_path, group):
    summary = read_json(Path(run_path) / "run.json")
    if summary.get("experiment", {}).get("plan_sha256") != plan["plan_sha256"] or summary.get("experiment", {}).get("group") != group:
        raise ValueError("experiment run manifest mismatch")
    output = []
    for item in summary["samples"]:
        if item["status"] == "not_run" or not item.get("result_file"):
            output.append({"sample_id": item["instance_id"], "status": "not_run"})
            continue
        result = read_json(Path(run_path) / item["result_file"])
        if plan.get("runtime_versions") is not None and any(
                (result.get("task_runtime_configuration") or {}).get(key) != value
                for key, value in plan["runtime_versions"].items()):
            raise ValueError("experiment result runtime versions mismatch")
        accounting = reconcile_wire_usage(result)
        scope = compare_fixed_scope(result, plan["samples"][item["instance_id"]])
        baseline = plan["samples"][item["instance_id"]]
        output.append({"sample_id": item["instance_id"], "status": item["status"],
            "wire_usage_integrity": accounting["integrity"], "wire_attempts": accounting["wire_attempts"],
            "tokens": accounting["actual_total_tokens"], "baseline_tokens": baseline["tokens"],
            "token_reduction": (1 - accounting["actual_total_tokens"] / baseline["tokens"]
                                if accounting["actual_total_tokens"] is not None and baseline["tokens"] else None),
            "fixed_scope": scope, "quality_status": plan["quality_status"], "quality_proven": False})
    tokens = [value["tokens"] for value in output if value.get("tokens") is not None]
    baseline_tokens = [value["baseline_tokens"] for value in output if value.get("tokens") is not None]
    complete = len(tokens) == len(output) and bool(tokens)
    return {"plan_sha256": plan["plan_sha256"], "group": group, "stage": summary["experiment"]["stage"],
        "samples": output, "total_tokens": sum(tokens) if complete else None,
        "baseline_total_tokens": sum(baseline_tokens) if complete else None,
        "token_reduction": 1 - sum(tokens) / sum(baseline_tokens) if complete and sum(baseline_tokens) else None,
        "fixed_scope_not_worse": bool(output) and all(value.get("fixed_scope", {}).get("not_worse_than_baseline", False)
                                                      for value in output),
        "quality_status": plan["quality_status"], "quality_passed": False, "default_switch_allowed": False}


def screen_empty_candidate_regression(result, baseline_packets, independent_adjudication):
    """无新候选时可确定质量门失败；无法据此批准一般质量门。"""
    baseline_ids = {packet["blind_id"] for packet in baseline_packets
                    if packet["snapshot"]["repository"] == result["sample"]["repository"]
                    and packet["snapshot"]["head_sha"] == result["sample"]["head_sha"]}
    accepted = independent_adjudication.get("accepted", [])
    complete = (independent_adjudication.get("adjudication_status") == "complete"
                and independent_adjudication.get("blind") is True
                and not independent_adjudication.get("rejected")
                and len({item["blind_id"] for item in accepted}) == len(baseline_packets)
                and {item["blind_id"] for item in accepted} == {packet["blind_id"] for packet in baseline_packets})
    valid = sum(item["label"] == "valid_defect" for item in accepted if item["blind_id"] in baseline_ids)
    findings = (result.get("task_runtime_report") or {}).get("findings")
    raw = [call.get("raw_response") for call in result.get("model_calls", [])]
    unknown = False
    generated = 0
    submits = 0
    for response in raw:
        try:
            value = json.loads(response)
            if not isinstance(value, dict) or value.get("kind") not in {"read", "submit"}:
                unknown = True
                continue
            if value["kind"] == "read":
                continue
            submits += 1
            candidates = value.get("candidates")
            if not isinstance(candidates, list):
                unknown = True
                continue
            generated += len(candidates)
        except (ValueError, TypeError, AttributeError):
            unknown = True
    report = result.get("task_runtime_report") or {}
    inspections = [item for item in report.get("work_items", []) if item.get("kind") == "inspect"]
    completed = (report.get("execution_status") == "completed" and not result.get("error_type")
        and report.get("scope_complete") is True and bool(inspections)
        and all(item.get("state") == "completed"
                and item.get("last_observation", {}).get("response_kind") == "submit"
                and not item["last_observation"].get("errors")
                and not item["last_observation"].get("pending_goals")
                and not item["last_observation"].get("correction_pending") for item in inspections))
    empty = (isinstance(findings, list) and not findings and submits > 0
             and completed and not unknown and generated == 0)
    failed = complete and bool(baseline_ids) and valid > 0 and empty
    return {"sample_id": result["sample"]["instance_id"], "status": "failed" if failed else "not_established",
        "reason": "independent_valid_defects_lost" if failed else "requires_general_independent_quality_scoring",
        "baseline_independent_valid_defects": valid if complete else None,
        "new_generated_candidates": generated if not unknown else None,
        "new_valid_defects": 0 if empty else None, "expand_allowed": False,
        "default_switch_allowed": False, "reference_recall": None,
        "result_sha256": stable_hash(result), "adjudication_sha256": stable_hash(independent_adjudication)}
