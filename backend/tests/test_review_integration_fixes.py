import json
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.graph.nodes.cross_unit_coordination import cross_unit_coordination_node
from app.graph.nodes.report import complete_node, report_node
from app.models.review import ChangedFile, CrossUnitCoordinationPlan, PullRequestInfo, ReviewCoverage, ReviewUnitResult
from app.review.issue_audit import capture_issue_audit, issue_audit_unit
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.review_manifest import build_review_manifest
from app.services.unit_review_summary import build_record_input, validate_record
from test_cross_unit_coordination import Provider, state
from test_provider import FakeChatOpenAI


def planned_state():
    value = state()
    value["review_plan"] = {"changed_files": [
        {key: item[key] for key in ("file_path", "change_type", "additions", "deletions")}
        | {"included": True} for item in value["changed_files"]
    ]}
    return value


@pytest.mark.asyncio
async def test_large_evidence_catalog_reaches_coordinator_without_losing_references():
    value = planned_state()
    for index, raw in enumerate(value["changed_files"]):
        raw["hunks"][0]["added_lines"][0]["content"] = "source-code" * 12_000
        inputs = build_record_input([ChangedFile.model_validate(raw)], [], None, "h", "b")
        record = ReviewUnitResult.model_validate(value["review_unit_results"][index]).review_summary.record
        summary = validate_record(record, inputs, {raw["file_path"]})
        value["review_unit_results"][index]["review_summary"] = summary.model_dump(mode="json")
    value["cross_unit_risk"].update(decision="uncertain", index_status="available", reasons=[{
        "code": "relationship_unknown", "detail": "检查两端关系",
    }])

    class Skip(Provider):
        async def coordinate_cross_units(self, payload, model):
            self.calls.append("coordinate")
            assert len(json.dumps(payload, ensure_ascii=False)) < 48_000
            assert len(payload["evidence"]) == 2
            assert all("content" not in item and item["content_hash"] for item in payload["evidence"])
            assert all("record_history" not in item["summary"] for item in payload["summaries"])
            return CrossUnitCoordinationPlan(decision="skip", reason="两侧记录已核验",
                relationship_ids=[item["id"] for item in payload["risk"]["relationships"]],
                evidence_ids=[item["id"] for item in payload["evidence"]])

    provider = Skip()
    result = await CrossUnitCoordinationService(provider).run(value)
    assert provider.calls == ["coordinate"]
    assert result["coordination_plan"]["status"] == "completed"
    assert build_review_manifest({**value, **result}, datetime.now(timezone.utc)).coverage.review_complete is True


@pytest.mark.parametrize("status", ["failed", "unresolved", "cancelled", "proposed", None])
@pytest.mark.asyncio
async def test_incomplete_coordination_preserves_local_coverage_but_not_overall_success(status):
    value = planned_state()
    if status:
        value["coordination_plan"] = CrossUnitCoordinationPlan(status=status, reason="协调未完成").model_dump(mode="json")
    manifest = build_review_manifest(value, datetime.now(timezone.utc))
    assert manifest.coverage.coverage_rate == manifest.coverage.unit_coverage_rate == 1
    assert manifest.coverage.review_complete is False
    assert manifest.coverage.coordination_status == (status if status in {"failed", "unresolved", "cancelled"} else "not_run")
    assert (await complete_node(value))["status"] == "completed_with_warnings"
    report = await report_node(value)
    assert report["review_coverage"]["review_complete"] is False
    assert "整体审查：未完整完成" in report["report_markdown"]
    assert ReviewCoverage.model_validate({"coverage_rate": 1}).review_complete is None


@pytest.mark.asyncio
async def test_oversized_metadata_remains_explicit_failure_and_step_is_not_completed():
    value = planned_state()
    value["review_units"][0]["grouping_reason"] = "x" * 50_000
    value["_provider"] = Provider()
    result = await cross_unit_coordination_node(value)
    assert result["coordination_plan"]["status"] == "failed"
    assert "coordination_catalog_exceeds" in result["coordination_plan"]["reason"]
    assert result["step_progress"][-1]["status"] == "failed"
    assert not value["_provider"].calls




def candidate():
    return {"title": "空值问题", "severity": "high", "category": "correctness", "confidence": .9,
        "affected_behavior": "读取字段失败", "failure_scenario": "返回空值后读取字段",
        "recommendation": "检查空值", "primary_evidence": {"file_path": "a.py", "existing_code": "return None"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["human_autofix", "confidence", "scalar"])
async def test_schema_rejection_audits_entire_batch_without_exposing_raw_values(monkeypatch, bad):
    invalid = candidate() | {"title": "secret-token"}
    if bad == "human_autofix":
        invalid.update(requires_human_confirmation=True, auto_fix_eligible=True)
    elif bad == "confidence":
        invalid["confidence"] = "secret-token%"
    else:
        invalid = "secret-token"
    FakeChatOpenAI.responses = [AIMessage(content=json.dumps({"issues": [candidate(), invalid]}),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    monkeypatch.setattr("app.agents.providers.ChatOpenAI", FakeChatOpenAI)
    provider = OpenAICompatibleProvider("secret-token", "https://example.com", "model")
    events = []
    with capture_issue_audit(events.append), issue_audit_unit("unit-a"):
        with pytest.raises(LLMProviderError, match="schema validation failed") as raised:
            await provider.review_unit(PullRequestInfo.model_validate(state()["pr_info"]), [], "", None, {})
    assert raised.value.usage.actual_total_tokens == 15
    assert "secret-token" not in str(raised.value)
    assert "secret-token" not in json.dumps(events)
    inputs = [event for event in events if event["stage"] == "schema_input"]
    assert [event["candidate_index"] for event in inputs] == [0, 1]
    assert len({event["batch_id"] for event in inputs}) == 1
    assert all(event["review_unit_id"] == "unit-a" for event in events)
    validations = [event for event in events if event["stage"] == "schema_validation"]
    assert [event["reason"] for event in validations] == ["batch_schema_rejected", "schema_rejected"]


@pytest.mark.parametrize("content", ['secret-token', '{"issues": [secret-token}', '{}', '{"issues": "secret-token"}'])
def test_response_decode_rejections_are_audited_and_safe(content):
    provider = OpenAICompatibleProvider("secret-token", "https://example.com", "model")
    events = []
    with capture_issue_audit(events.append), issue_audit_unit("u"):
        with pytest.raises(LLMProviderError) as raised:
            provider._parse_issues(content, require_array=True)
    assert events[0]["stage"] == "response_decode"
    assert events[1]["status"] == "dismissed"
    assert "secret-token" not in json.dumps(events) + str(raised.value)






@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"other": "secret-token"}, {"issues": None}])
async def test_legacy_review_rejects_missing_issue_array_with_audit(monkeypatch, payload):
    FakeChatOpenAI.responses = [AIMessage(content=json.dumps(payload))]
    monkeypatch.setattr("app.agents.providers.ChatOpenAI", FakeChatOpenAI)
    provider = OpenAICompatibleProvider("secret-token", "https://example.com", "model")
    events = []
    with capture_issue_audit(events.append):
        with pytest.raises(LLMProviderError, match="invalid_issue_envelope"):
            await provider.review(PullRequestInfo.model_validate(state()["pr_info"]), [], "", None)
    assert events[0]["stage"] == "response_decode"
    assert "secret-token" not in json.dumps(events)
