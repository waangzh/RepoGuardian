"""单请求能力诊断：冻结后显式发送，复用生产 Provider 与持久预算。"""

import argparse
import asyncio
import hashlib
from dataclasses import asdict
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.llm.providers import build_provider
from app.core.config import settings
from app.evaluation.runner import implementation_digest
from app.core.fingerprints import stable_hash
from app.services.review.review_run_budget import ReviewRunBudget, activate_run_budget
from app.review.run_audit import capture_run_audit


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def current_provider():
    return build_provider(settings.repoguardian_provider, settings.openai_api_key,
                          settings.openai_base_url, settings.repoguardian_model)


def provider_configuration(provider):
    endpoint = urlsplit(provider._base_url)
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("credential-bearing endpoint cannot be frozen")
    resolved = provider.resolve_request_profile(provider._default_model)
    return {"provider": provider._provider_name, "model": provider._default_model,
        "endpoint": provider._base_url, "thinking_disabled": provider._disable_thinking,
        "request_attempts": provider._request_attempts, "retry_backoff_seconds": provider._retry_backoff_seconds,
        "timeout_seconds": provider._request_timeout_seconds,
        "profile_source": resolved.source, "profile": resolved.profile.model_dump(mode="json")}


def freeze_diagnostic(stage, output, *, task_expression=None, provider=None, source_roots=None, source_layout=False,
                      source_fixture=None, dependency_contracts=None):
    stage, output = Path(stage), Path(output)
    baseline = read_json(stage / "manifest.json")
    case, request = read_json(stage / "case.json"), read_json(stage / "request.json")
    if (stable_hash(case) != baseline["input_sha256"] or stable_hash(request["system"]) != baseline["system_sha256"]
            or stable_hash(request["prompt"]) != baseline["prompt_sha256"]):
        raise ValueError("diagnostic frozen input changed")
    if stable_hash(read_json(stage / "rubric.json")) != baseline["rubric_sha256"]:
        raise ValueError("diagnostic rubric changed")
    verification = "not_checked"
    if source_roots is not None and source_fixture is not None:
        raise ValueError("choose one diagnostic source owner")
    if source_roots is not None:
        from app.evaluation.runtime_adjudication import _validate_evidence
        from app.evaluation.cli.runtime_adjudication_cli import make_source_reader
        reader = make_source_reader(read_json(source_roots), Path(__file__).resolve().parents[2] / ".repoguardian/evaluation-runs")
        for item in case["evidence"]:
            _validate_evidence({"path": item["path"], "side": item["side"], "snapshot_sha": item["snapshot_sha"],
                "start_line": item["start_line"], "end_line": item["end_line"], "code": item["content"]},
                {"snapshot": case["snapshot"]}, reader)
        verification = "fixed_sha_text_verified_not_semantic_proof"
    if source_fixture is not None:
        from app.evaluation.runtime_adjudication import _validate_evidence
        fixture = read_json(source_fixture)
        if fixture.get("version") != "owned-diagnostic-source-v1" or fixture["repository"] != case["snapshot"]["repository"]:
            raise ValueError("owned diagnostic source identity mismatch")
        by_sha = {}
        for side in ("base", "head"):
            digest = hashlib.sha1(json.dumps(fixture["sources"][side], sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
            if digest != case["snapshot"][side + "_sha"]:
                raise ValueError("owned diagnostic source hash mismatch")
            by_sha[digest] = fixture["sources"][side]
        def reader(repository, sha, path):
            return by_sha.get(sha, {}).get(path) if repository == fixture["repository"] else None
        for item in case["evidence"]:
            _validate_evidence({"path": item["path"], "side": item["side"], "snapshot_sha": item["snapshot_sha"],
                "start_line": item["start_line"], "end_line": item["end_line"], "code": item["content"]},
                {"snapshot": case["snapshot"]}, reader)
        verification = "fixed_sha_text_verified_not_semantic_proof"
    if task_expression is not None:
        request["system"] = task_expression
    if source_layout:
        if task_expression is not None:
            raise ValueError("source layout comparison cannot also change task expression")
        from app.evaluation.runtime_source_layout import source_layout_prompt, layout_inventory
        organized = json.loads(source_layout_prompt(case))
        if layout_inventory(organized) != sorted(case["evidence"], key=stable_hash):
            raise ValueError("source layout changed fixed evidence")
        request["prompt"] = json.dumps(organized, ensure_ascii=False, sort_keys=True)
    if dependency_contracts is not None:
        if source_layout or task_expression is not None:
            raise ValueError("dependency contracts must be the only changed variable")
        contracts = read_json(dependency_contracts)
        expected_symbols = set(read_json(stage / "dependency-symbols.json"))
        actual_symbols = {symbol for item in contracts for symbol in item["symbols"]}
        if actual_symbols != expected_symbols or any(item["version"] != "3.12" or
                not item["source_url"].startswith("https://docs.python.org/3.12/") for item in contracts):
            raise ValueError("dependency contracts symbol or version mismatch")
        payload = json.loads(request["prompt"])
        payload["dependency_contracts"] = contracts
        request["prompt"] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    provider = provider or current_provider()
    configuration = provider_configuration(provider)
    expected = baseline["configuration"]
    if any(configuration[key] != expected[key] for key in ("provider", "model", "endpoint", "request_attempts", "timeout_seconds", "profile", "profile_source")):
        raise ValueError("diagnostic baseline provider changed")
    prepared = provider._prepare_json_request(request["prompt"], provider._default_model, "task_review_v1",
                                              request["system"], expected["output_tokens"])
    if json.loads(prepared.extra_body_json) != expected["extra_body"]:
        raise ValueError("diagnostic baseline thinking changed")
    admission = prepared.estimate(provider.resolve_request_profile(provider._default_model).profile)
    output.mkdir(exist_ok=False)
    manifest = {"version": "runtime-capability-live-v1", "stage0_sha256": stable_hash(baseline),
        "implementation_sha256": implementation_digest(), "input_sha256": stable_hash(case),
        "rubric_sha256": baseline["rubric_sha256"], "configuration": configuration,
        "request": asdict(prepared), "admission": admission,
        "variable": ("dependency_contracts" if dependency_contracts is not None else
            "source_layout" if source_layout else "task_expression" if task_expression is not None else "baseline"),
        "source_owner": "owned_diagnostic_fixture" if source_fixture is not None else "git_snapshot",
        "evidence_verification": verification, "limits": {"max_calls": provider._request_attempts, "max_tokens": 50_000},
        "quality_status": "not_run", "default_switch_allowed": False}
    write_new(output / "manifest.json", {**manifest, "manifest_sha256": stable_hash(manifest)})
    return manifest


class DiagnosticBudgetStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self, run_id):
        return read_json(self.path) if self.path.exists() else None

    def save(self, run_id, payload, revision):
        previous = self.load(run_id)
        if (previous is not None and (previous["run_id"] != run_id or previous["revision"] != revision - 1)
                or previous is None and revision != 0):
            raise ValueError("diagnostic budget revision mismatch")
        temporary = self.path.with_suffix(".pending")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


async def run_diagnostic(output, *, provider=None):
    output = Path(output)
    manifest = read_json(output / "manifest.json")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest["manifest_sha256"] != stable_hash(payload) or implementation_digest() != manifest["implementation_sha256"]:
        raise ValueError("diagnostic implementation or manifest changed")
    if manifest["evidence_verification"] != "fixed_sha_text_verified_not_semantic_proof":
        raise ValueError("diagnostic source evidence must be verified before dispatch")
    provider = provider or current_provider()
    if provider_configuration(provider) != manifest["configuration"]:
        raise ValueError("diagnostic provider configuration changed")
    frozen = manifest["request"]
    prepared = provider._prepare_json_request(frozen["prompt"], frozen["model"], frozen["operation"],
                                              frozen["system"], frozen["output_tokens"])
    admission = prepared.estimate(provider.resolve_request_profile(prepared.model).profile)
    if asdict(prepared) != frozen or admission != manifest["admission"]:
        raise ValueError("diagnostic prepared request changed")
    # 同一目录不能再次派发；不把失败运行覆盖为成功。
    write_new(output / "dispatch.json", {"manifest_sha256": manifest["manifest_sha256"]})
    budget = await ReviewRunBudget.load_or_create(output.name, store=DiagnosticBudgetStore(output / "budget.json"),
                                                  **manifest["limits"])
    events, raw, usage, error, parsed = [], None, None, None, None
    def record(event):
        events.append(event)
        with (output / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        with activate_run_budget(budget), capture_run_audit(model_sink=record, tool_sink=lambda event: None):
            result = await provider.task_review_step(prepared)
        raw, usage = result.value, result.usage.model_dump(mode="json")
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            pass
    except Exception as exc:
        error = type(exc).__name__  # 不将可能带凭据的第三方错误正文写入报告。
    finally:
        snapshot = budget.snapshot()
        unknown = any(item["sent"] and not item["usage_available"] for item in snapshot["attempts"].values())
        report = {"manifest_sha256": manifest["manifest_sha256"], "raw_response": raw, "parsed": parsed,
            "execution_status": "failed" if error else "completed" if raw is not None else "interrupted",
            "usage": usage, "error_type": error, "budget": snapshot,
            "wire_attempts": sum(item["sent"] for item in snapshot["attempts"].values()),
            "actual_total_tokens": None if unknown else snapshot["settled_actual"],
            "usage_integrity": "unknown" if unknown else "complete", "quality_status": "pending_independent_adjudication",
            "schema_valid": isinstance(parsed, dict) and all(key in parsed for key in
                ("operation_semantics", "branch_behavior", "failure_conditions", "evidence_gaps", "conclusion")),
            "default_switch_allowed": False}
        write_new(output / "result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "run"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--source-roots", type=Path)
    parser.add_argument("--task-expression", type=Path)
    parser.add_argument("--source-layout", action="store_true")
    parser.add_argument("--source-fixture", type=Path)
    parser.add_argument("--dependency-contracts", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        if not args.stage:
            parser.error("freeze requires --stage")
        freeze_diagnostic(args.stage, args.output, source_roots=args.source_roots,
            task_expression=args.task_expression.read_text(encoding="utf-8") if args.task_expression else None,
            source_layout=args.source_layout, source_fixture=args.source_fixture,
            dependency_contracts=args.dependency_contracts)
        print("diagnostic frozen; no request sent")
    else:
        result = asyncio.run(run_diagnostic(args.output))
        print(json.dumps({key: result[key] for key in ("wire_attempts", "actual_total_tokens", "usage_integrity", "schema_valid", "error_type")}))


if __name__ == "__main__":
    main()
