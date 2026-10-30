"""Conflict admission and durable requests, not live model judgment quality."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.review.budget import ReviewBudget
from app.review.context import ReviewContext
from app.review.models import ReviewLimits, ReviewReport, ReviewSnapshot, ReviewSynthesis
from app.review.synthesis import output_hash, synthesize
from app.services.review.review_repository import ReviewLeaseLost


class ConsistencyModel:
    def __init__(self, mode="ready"):
        self.mode = mode
        self.requests = []
        self.received_feedback = False
        self.expired = False
        self.lease_lost = False

    def bind_tools(self, tools, *, tool_choice):
        assert len(tools) == 1
        self.schema = tool_choice
        return self

    async def ainvoke(self, messages):
        payload = json.loads(next(item.content for item in messages if item.type == "human"))
        self.requests.append(self.schema)
        if self.schema == "SynthesisOutput":
            self.expired = self.mode == "deadline"
            args = {"change_overview": [{"source_ids": ["change:1"], "text": "VALUE now returns 2."}]}
            if self.mode == "large_output":
                args["change_overview"] = [{"source_ids": ["change:1"], "text": "x" * 600}] * 3
        else:
            assert self.schema == "SynthesisConsistency"
            assert payload["refuted_claims"][0]["candidate_id"] == "candidate"
            assert "overview" in payload and "facts" not in payload
            if self.mode == "cancel":
                raise asyncio.CancelledError()
            if self.mode == "failure":
                raise ValueError("PRIVATE_PROVIDER_ERROR")
            self.lease_lost = self.mode == "lease"
            first = self.requests.count("SynthesisConsistency") == 1
            if self.mode == "timeout" and first:
                raise TimeoutError()
            self.received_feedback = any(
                item.type == "tool" and "synthesis_submission_needs_correction" in str(item.content)
                for item in messages)
            args = {"consistent": self.mode != "conflict", "explanation": "PRIVATE_CHECK_REASON"}
            if self.mode == "schema" and first:
                args.pop("consistent")
        return AIMessage(content="", tool_calls=[{"name": self.schema, "id": "fixture", "args": args}],
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def setup(mode="ready", *, refuted=True, calls=8):
    report = ReviewReport(snapshot=ReviewSnapshot(review_id="consistency", repository="local/repo",
        pr_number=1, base_sha="a" * 40, head_sha="b" * 40), assignments=[], goals=[],
        discovery_results=[], verification_results=[], candidates=[], decisions=[], findings=[],
        complete=True, termination="completed")
    material = {"facts": {"change:1": {"kind": "change", "path": "safe.py", "text": "VALUE = 2"}},
                "refuted_claims": [{"candidate_id": "candidate", "claim": "VALUE raises",
                                    "reason": "The constant does not raise"}] if refuted else [],
                "omitted": {}, "omitted_changes": [], "required_change_groups": {"unit": ["change:1"]}}

    async def facts(_):
        return "fixed-version", material

    model = ConsistencyModel(mode)
    persisted = []

    async def save(current):
        persisted.append(current.model_copy(deep=True))

    context = ReviewContext(load=None, model=model, limits=ReviewLimits(total_model_calls=calls),
        synthesis_facts=facts, save_report=save, remaining_seconds=lambda: 0 if model.expired else 1000)
    context.budget = ReviewBudget(context.limits)
    return report, material, context, persisted


async def test_no_refuted_candidate_keeps_one_generation_request():
    report, _, context, _ = setup(refuted=False)
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    saved = ReviewSynthesis.model_validate(result["synthesis"])
    assert saved.status == "ready" and saved.model_calls == 1
    assert context.model.requests == ["SynthesisOutput"]
    assert saved.consistency is None and saved.checked_output_hash is None


async def test_consistent_overview_reuses_durable_check_without_new_model_requests():
    report, _, context, _ = setup()
    runtime = SimpleNamespace(context=context)
    first = await synthesize({"report": report.model_dump()}, runtime)
    saved = ReviewSynthesis.model_validate(first["synthesis"])
    assert saved.status == "ready" and saved.consistency.consistent
    assert saved.checked_output_hash == output_hash(saved.output)
    assert saved.model_calls == 2 and saved.input_tokens == 20 and saved.output_tokens == 10
    state = {"report": first["report"], "synthesis": first["synthesis"]}
    second = await synthesize(state, runtime)
    assert second["synthesis"] == first["synthesis"]
    assert context.model.requests == ["SynthesisOutput", "SynthesisConsistency"]
    assert (await context.budget.summary())["model_calls"] == 2
    # A crash after response persistence but before ready reuses both stage results.
    state["synthesis"]["status"] = "fallback"
    state["synthesis"]["consistency"] = state["synthesis"]["checked_output_hash"] = None
    recovered = await synthesize(state, runtime)
    assert recovered["synthesis"] == first["report"]["synthesis"]
    assert context.model.requests == ["SynthesisOutput", "SynthesisConsistency"]


@pytest.mark.parametrize("mode,calls,error,requests", [
    ("conflict", 8, "synthesis_refuted_claim_reintroduced", 2),
    ("failure", 8, "ValueError", 2),
    ("ready", 1, "synthesis_budget_exhausted", 1),
    ("deadline", 8, "synthesis_deadline", 1),
])
async def test_check_refusal_keeps_core_report_and_does_not_regenerate(mode, calls, error, requests):
    report, _, context, _ = setup(mode, calls=calls)
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert result["synthesis"]["status"] == "fallback"
    assert result["synthesis"]["error"] == error
    assert result["report"]["complete"] and result["report"]["termination"] == "completed"
    assert len(context.model.requests) == requests
    assert context.model.requests.count("SynthesisOutput") == 1
    assert (await context.budget.summary())["model_calls"] == requests


@pytest.mark.parametrize("mode", ["timeout", "schema"])
async def test_check_retry_has_its_own_attempts_usage_and_feedback(mode, monkeypatch):
    monkeypatch.setattr("app.review.synthesis.RETRY_DELAY", 0)
    report, _, context, _ = setup(mode)
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    saved = ReviewSynthesis.model_validate(result["synthesis"])
    assert saved.status == "ready" and saved.model_calls == 3
    assert [(item.stage, item.number, item.status) for item in saved.attempts] == [
        ("generation", 1, "completed"), ("consistency", 1, "timeout" if mode == "timeout" else "failed"),
        ("consistency", 2, "completed")]
    assert len({item.reservation_id for item in saved.attempts}) == 3
    assert (await context.budget.summary())["unknown_calls"] == (mode == "timeout")
    assert context.model.received_feedback is (mode == "schema")
    assert saved.input_tokens == (None if mode == "timeout" else 30)


async def test_cancelled_check_stays_unknown_and_is_not_reissued_on_resume():
    report, _, context, persisted = setup("cancel")
    runtime = SimpleNamespace(context=context)
    with pytest.raises(asyncio.CancelledError):
        await synthesize({"report": report.model_dump()}, runtime)
    saved = persisted[-1].synthesis
    assert saved.status == "fallback" and saved.attempts[-1].stage == "consistency"
    assert saved.attempts[-1].status == "unknown"
    assert (await context.budget.summary())["unknown_calls"] == 1
    resumed = await synthesize(
        {"report": persisted[-1].model_dump(), "synthesis": saved.model_dump()}, runtime)
    assert resumed["synthesis"]["error"] == "synthesis_interrupted_unknown"
    assert context.model.requests == ["SynthesisOutput", "SynthesisConsistency"]
    assert (await context.budget.summary())["model_calls"] == 2


async def test_lost_lease_during_check_cannot_persist_a_late_ready_result():
    report, _, context, persisted = setup("lease")

    async def active():
        if context.model.lease_lost:
            raise ReviewLeaseLost()

    context.check_running = active
    with pytest.raises(ReviewLeaseLost):
        await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert all(item.synthesis.status == "fallback" for item in persisted)
    assert persisted[-1].synthesis.attempts[-1].status == "started"
    assert (await context.budget.summary())["unknown_calls"] == 1
    assert context.model.requests == ["SynthesisOutput", "SynthesisConsistency"]


@pytest.mark.parametrize("mode", ["conflict", "failure"])
async def test_check_diagnostics_include_stage_without_source_or_judgment_body(mode, caplog):
    report, _, context, _ = setup(mode)
    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert result["synthesis"]["status"] == "fallback"
    assert "stage=consistency" in caplog.text
    assert "PRIVATE_" not in caplog.text


async def test_check_reservation_without_saved_response_is_never_replayed():
    report, _, context, _ = setup()
    identity = "synthesis:fixed-version:consistency:1"
    await context.budget.reserve(identity, 100, stage="finalize")
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    saved = ReviewSynthesis.model_validate(result["synthesis"])
    assert saved.status == "fallback" and saved.error == "synthesis_interrupted_unknown"
    assert saved.attempts[-1].stage == "consistency" and saved.attempts[-1].status == "unknown"
    assert context.model.requests == ["SynthesisOutput"]
    assert (await context.budget.summary())["model_calls"] == 2


@pytest.mark.parametrize("tamper", ["body", "missing_check", "negative_check", "hash"])
async def test_ready_reuse_rejects_missing_or_changed_consistency_binding(tamper):
    report, _, context, _ = setup()
    runtime = SimpleNamespace(context=context)
    first = await synthesize({"report": report.model_dump()}, runtime)
    saved = first["synthesis"]
    if tamper == "body":
        saved["output"]["change_overview"][0]["text"] = "The dismissed defect is real."
    elif tamper == "missing_check":
        saved["consistency"] = None
    elif tamper == "negative_check":
        saved["consistency"]["consistent"] = False
    else:
        saved["checked_output_hash"] = "0" * 64
    result = await synthesize({"report": first["report"], "synthesis": saved}, runtime)
    assert result["synthesis"]["status"] == "fallback"
    assert result["synthesis"]["error"] == "synthesis_consistency_missing"
    assert context.model.requests == ["SynthesisOutput", "SynthesisConsistency"]


async def test_oversized_refuted_material_falls_back_without_a_generation_request():
    report, material, context, _ = setup()
    material["refuted_claims"][0]["reason"] = "x" * 24_000
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert result["synthesis"]["status"] == "fallback"
    assert result["synthesis"]["error"] == "synthesis_required_material_too_large"
    assert context.model.requests == []


async def test_check_payload_respects_material_limit_after_adding_overview():
    report, material, context, _ = setup("large_output")
    material["refuted_claims"][0]["reason"] = "x" * 23_000
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert result["synthesis"]["status"] == "fallback"
    assert result["synthesis"]["error"] == "synthesis_required_material_too_large"
    assert context.model.requests == ["SynthesisOutput"]
