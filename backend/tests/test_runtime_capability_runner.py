import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.evaluation.runtime_capability_runner import (freeze_diagnostic, provider_configuration,
                                                    run_diagnostic, write_new, read_json)
from app.evaluation.runtime_capability import investigation_decision_changes
from app.services.fingerprints import stable_hash


def setup_stage(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    provider = OpenAICompatibleProvider("stub", "https://example.test/v1", "fixture", request_attempts=1)
    case = {"snapshot": {"repository": "repo", "base_sha": "b" * 40, "head_sha": "a" * 40},
            "evidence": [{"path": "src/a.py", "side": "head", "snapshot_sha": "a" * 40,
                          "start_line": 1, "end_line": 1, "content": "return None"}]}
    request = {"system": "fixed task", "prompt": "fixed evidence"}
    rubric = {"version": "frozen"}
    config = provider_configuration(provider)
    config.update(output_tokens=100, extra_body={})
    manifest = {"configuration": config, "input_sha256": stable_hash(case), "rubric_sha256": stable_hash(rubric),
                "system_sha256": stable_hash(request["system"]), "prompt_sha256": stable_hash(request["prompt"])}
    for name, value in (("case", case), ("request", request), ("rubric", rubric), ("manifest", manifest)):
        write_new(stage / (name + ".json"), value)
    roots = tmp_path / "roots.json"
    write_new(roots, [])
    monkeypatch.setattr("app.evaluation.runtime_adjudication_cli.make_source_reader", lambda *args: lambda *args: "return None")
    return stage, roots, provider


async def test_frozen_diagnostic_uses_durable_budget_and_cannot_dispatch_twice(tmp_path, monkeypatch):
    stage, roots, provider = setup_stage(tmp_path, monkeypatch)
    sent = []
    class SDK:
        async def ainvoke(self, messages):
            sent.append(messages)
            return AIMessage(content=json.dumps(dict(operation_semantics="x", branch_behavior="x", failure_conditions="x",
                evidence_gaps="x", conclusion="x")), usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    output = tmp_path / "run"
    freeze_diagnostic(stage, output, provider=provider, source_roots=roots)
    result = await run_diagnostic(output, provider=provider)
    assert result["wire_attempts"] == 1 and result["actual_total_tokens"] == 15
    assert result["schema_valid"] and result["quality_status"] == "pending_independent_adjudication"
    assert read_json(output / "budget.json")["settled_actual"] == 15
    with pytest.raises(FileExistsError):
        await run_diagnostic(output, provider=provider)
    assert len(sent) == 1


async def test_manifest_or_provider_change_rejects_before_sending(tmp_path, monkeypatch):
    stage, roots, provider = setup_stage(tmp_path, monkeypatch)
    output = tmp_path / "run"
    freeze_diagnostic(stage, output, provider=provider, source_roots=roots)
    provider._disable_thinking = True
    with pytest.raises(ValueError, match="configuration changed"):
        await run_diagnostic(output, provider=provider)
    assert not (output / "dispatch.json").exists()
    provider._disable_thinking = False
    manifest = read_json(output / "manifest.json")
    manifest["request"]["prompt"] = "tampered"
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="implementation or manifest changed"):
        await run_diagnostic(output, provider=provider)


def test_decision_changes_count_unique_judgments_and_preserve_unknown_unchanged_cost():
    before = {"decision": "candidate", "evidence_status": "sufficient"}
    after = {"decision": "false_positive", "evidence_status": "sufficient"}
    item = {"investigation_id": "i1", "judgment_id": "j1", "adjudication_status": "complete", "blind": True,
            "reason": "独立核对轨迹", "trace_material_sha256": "a" * 64,
            "before": before, "after": after, "effect": "correct_false_positive_rejection", "tokens": 30,
            "usage_integrity": "complete"}
    unchanged = {**item, "investigation_id": "i2", "before": after, "after": after, "effect": "unchanged",
                 "tokens": None, "usage_integrity": "unknown"}
    report = investigation_decision_changes([item, unchanged])
    assert report["changed_final_judgments"] == 1 and report["effects"]["correct_false_positive_rejection"] == 1
    assert report["unchanged_total_tokens"] is None and report["unchanged_unknown_costs"] == ["i2"]
    with pytest.raises(ValueError, match="contradicts"):
        investigation_decision_changes([{**item, "effect": "unchanged"}])
