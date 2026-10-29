"""Summary diagnostics identify the failure without disclosing source or prose."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.review.budget import ReviewBudget
from app.review.context import ReviewContext
from app.review.models import ReviewReport, ReviewSnapshot, SynthesisOutput
from app.review.synthesis import log_projection_fallback, synthesize, validate_synthesis
from app.review.synthesis_materials import omitted_changes


def test_real_response_needs_no_model_maintained_omission_manifest():
    raw = json.loads((Path(__file__).parent / "fixtures/onpar_95_omission_response.json").read_text(encoding="utf-8"))
    assert raw["omitted_source_ids"] and all(item.startswith("source:") for item in raw["omitted_source_ids"])
    # Replay the actual prose and citations under the new single-purpose schema.
    output = SynthesisOutput(change_overview=raw["change_overview"])
    assert set(SynthesisOutput.model_fields) == {"change_overview"}
    facts = {identity: {"kind": "source"} for identity in raw["omitted_source_ids"]}
    for paragraph in output.change_overview:
        for identity in paragraph.source_ids:
            facts[identity] = {"kind": "change" if identity.startswith("change:") else "source",
                               "path": identity}
    material = {"facts": facts, "omitted_changes": [],
                "required_change_groups": {"implementation": ["change:1"]}}
    assert omitted_changes(validate_synthesis(output, material), material) == []
    facts["change:3"] = {"kind": "change", "path": "secondary.py"}
    assert omitted_changes(validate_synthesis(output, material), material) == [
        {"path": "secondary.py", "hunk_index": None, "reason": "editorial_omission"}]


class ScriptedModel:
    def __init__(self, mode):
        self.mode, self.generation_calls = mode, 0

    def bind_tools(self, tools, *, tool_choice):
        self.schema = tool_choice
        return self

    async def ainvoke(self, messages):
        if self.mode == "permanent":
            raise ValueError("PRIVATE_UPSTREAM_BODY")
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        if self.schema == "SynthesisOutput":
            self.generation_calls += 1
            if self.mode == "timeout" and self.generation_calls == 1:
                raise TimeoutError("PRIVATE_UPSTREAM_BODY")
            identity = "PRIVATE_INVALID_REFERENCE" if self.mode == "citation" and self.generation_calls == 1 else "change:1"
            if self.mode == "example" and self.generation_calls == 1:
                identity = "demo:range-impl"
            args = {"change_overview": [{"source_ids": [identity], "text": "PRIVATE_RESPONSE_BODY"}]}
            if self.mode == "schema" and self.generation_calls == 1:
                args["change_overview"][0]["text"] = ["PRIVATE_SCHEMA_INPUT"]
        else:
            args = {"supported": self.mode != "unsupported", "explanation": "PRIVATE_GROUNDING_BODY"}
        return AIMessage(content="", tool_calls=[{"id": "fixture", "name": self.schema, "args": args}],
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


async def run_diagnostic(mode):
    report = ReviewReport(snapshot=ReviewSnapshot(review_id="diagnostics-review", repository="local/repo",
        pr_number=1, base_sha="a" * 40, head_sha="b" * 40), assignments=[], goals=[],
        discovery_results=[], verification_results=[], candidates=[], decisions=[], findings=[],
        complete=True, termination="completed")
    material = {"facts": {"change:1": {"kind": "change", "text": "PRIVATE_SOURCE_BODY"}},
                "omitted": {}, "omitted_changes": [], "required_change_groups": {"unit": ["change:1"]}}

    async def facts(_):
        if mode == "materials":
            raise OSError("PRIVATE_SOURCE_PATH")
        return "fixture-version", material

    context = ReviewContext(load=None, model=ScriptedModel(mode), synthesis_facts=facts)
    if mode == "budget":
        context.limits = context.limits.model_copy(update={"total_model_calls": 1})
    context.budget = ReviewBudget(context.limits)
    if mode == "budget":
        ticket = await context.budget.reserve("spent", 10)
        await context.budget.settle(ticket, {"input_tokens": 10, "output_tokens": 5})
    return await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))


@pytest.mark.parametrize("mode", ["ready", "citation", "timeout", "schema", "example"])
async def test_success_and_correction_are_correlated_without_body_logging(mode, caplog, monkeypatch):
    monkeypatch.setattr("app.review.synthesis.RETRY_DELAY", 0)
    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    result = await run_diagnostic(mode)
    assert result["synthesis"]["status"] == "ready"
    text = caplog.text
    assert "总览材料准备完成" in text and "material_version=fixture-version" in text
    assert "review_id=diagnostics-review" in text
    assert "总览请求开始" in text and "stage=grounding" in text and "status=ready" in text
    if mode in {"citation", "example"}:
        assert "error=synthesis_source_unknown" in text and "unknown_sources=1" in text
        assert "section=change_overview paragraph=0" in text
        assert "总览请求准备重试" in text and "next_attempt=2" in text
    if mode == "timeout":
        assert "error=synthesis_timeout" in text and "timeout_seconds=120" in text
        assert "unknown_calls=1" in text
    if mode == "schema":
        assert "error=ValidationError" in text and "schema_errors=1" in text
    assert "PRIVATE_" not in text


@pytest.mark.parametrize("mode,stage,error", [
    ("materials", "materials", "synthesis_material_preparation_failed"),
    ("permanent", "generation", "ValueError"),
    ("unsupported", "grounding", "synthesis_unsupported_prose"),
    ("budget", "generation", "synthesis_budget_exhausted"),
])
async def test_fallback_logs_precise_stage_and_safe_code(mode, stage, error, caplog):
    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    result = await run_diagnostic(mode)
    assert result["synthesis"]["error"] == error
    assert result["report"]["complete"]
    assert "总览回退" in caplog.text and f"stage={stage}" in caplog.text
    assert f"error={error}" in caplog.text and "diagnostic=review_report.synthesis" in caplog.text
    assert "PRIVATE_" not in caplog.text


async def test_cancelled_request_records_unknown_and_logs_interruption(caplog):
    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    with pytest.raises(asyncio.CancelledError):
        await run_diagnostic("cancel")
    assert "总览请求中断" in caplog.text and "status=unknown" in caplog.text
    assert "总览阶段中断" in caplog.text and "PRIVATE_" not in caplog.text


def test_repeated_projection_failure_logs_once_and_never_logs_error_body(caplog):
    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    log_projection_fallback.cache_clear()
    for _ in range(2):
        log_projection_fallback("projection-review", "old", "new", "PRIVATE_ERROR\nBODY")
    assert caplog.text.count("总览投影回退") == 1
    assert "stage=projection error=unclassified_error" in caplog.text
    assert "PRIVATE_" not in caplog.text
