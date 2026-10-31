import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from sqlalchemy import select

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.budget import BudgetGate
from app.review.context import AgentContext
from app.review.domain import VerificationOutput
from app.review.planner import tokens
from app.review.store import OVERVIEW_INPUT, VERIFICATION_INPUT, BudgetExceeded
from app.review.tools import READ_TOOLS
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_run, setup_source_run, submit_candidate

PROMPT = (Path(__file__).resolve().parents[2] / "app" / "review" / "prompts" / "verifier.md").read_text(
    encoding="utf-8")


def verifier_packet(initial):
    return {
        "messages": [SystemMessage(content=PROMPT).model_dump(),
                     HumanMessage(content=json.dumps(initial, ensure_ascii=False)).model_dump()],
        "tools": [convert_to_openai_tool(tool) for tool in READ_TOOLS],
        "response_format": VerificationOutput.model_json_schema(),
    }


def verifier_estimate(initial):
    return tokens(verifier_packet(initial)) + 512


def verifier_work(store, run, lease):
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    candidate = submit_candidate(store, run, lease, reviewer, run.plan.units[0].required_hunk_ids[0])
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    return candidate, store.claim(run.id, check.id, lease)


def test_verifier_initial_packet_fits_protection(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    candidate, check = verifier_work(store, run, lease)
    context = AgentContext(provider=provider, work=check, budget=None)
    initial = asyncio.run(provider.initial(context, candidate,
        request_budget=VERIFICATION_INPUT, estimate_request=verifier_estimate))
    assert tokens(verifier_packet(initial)) + 512 <= VERIFICATION_INPUT
    assert initial["candidate_materials"]
    assert not initial["missing_candidate_materials"]


def test_verifier_material_capacity_is_reported(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    candidate, check = verifier_work(store, run, lease)
    unbounded = asyncio.run(provider.initial(AgentContext(provider, check, None), candidate))
    metadata = {**unbounded, "materials": [], "candidate_materials": [],
        "missing_material": {key: "material_capacity" for key in run.plan.units[0].required_hunk_ids},
        "missing_candidate_materials": [
            {"path": ref.path, "side": ref.side, "start_line": ref.start_line,
             "end_line": ref.end_line, "reason": "material_capacity"} for ref in candidate.sources]}
    context = AgentContext(provider=provider, work=check, budget=None)
    request_budget = verifier_estimate(metadata)
    initial = asyncio.run(provider.initial(context, candidate, request_budget=request_budget,
                                           estimate_request=verifier_estimate))
    assert verifier_estimate(initial) <= request_budget
    assert initial["candidate_materials"] == [] and initial["materials"] == []
    assert {item["reason"] for item in initial["missing_candidate_materials"]} == {"material_capacity"}
    assert set(initial["missing_material"].values()) == {"material_capacity"}
    assert not context.pending
    assert not context.pending_hunks


def test_verifier_rejects_budget_smaller_than_required_metadata(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    candidate, check = verifier_work(store, run, lease)
    context = AgentContext(provider, check, None)
    with pytest.raises(BudgetExceeded, match="input_token_limit"):
        asyncio.run(provider.initial(context, candidate, request_budget=100,
                                     estimate_request=verifier_estimate))
    assert not context.pending
    assert not context.pending_hunks


def test_verifier_bounds_optional_history_before_source_material(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    candidate, check = verifier_work(store, run, lease)
    run.plan.history = [{"claim": '"x"' * 500, "head_sha": "a" * 40} for _ in range(20)]
    initial = asyncio.run(provider.initial(AgentContext(provider, check, None), candidate,
        request_budget=6000, estimate_request=verifier_estimate))
    assert verifier_estimate(initial) <= 6000
    assert len(initial["history_untrusted"]) < 20
    assert initial["candidate"] == candidate.proposal.model_dump(exclude={"source_refs"})


def test_reviewer_cannot_starve_pending_verifier(tmp_path):
    store, run, lease, _ = setup_run(tmp_path, input_tokens=VERIFICATION_INPUT + OVERVIEW_INPUT)
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    submit_candidate(store, run, lease, reviewer)
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    assert store.can_start(run.id, reviewer) == "input_token_limit"
    assert store.can_start(run.id, check) is None
    with pytest.raises(BudgetExceeded, match="input_token_limit"):
        store.reserve(run.id, reviewer, 1000, lease)
    check = store.claim(run.id, check.id, lease)
    _, available = store.reserve(run.id, check, VERIFICATION_INPUT, lease, with_remaining=True)
    assert available["input_tokens"] == VERIFICATION_INPUT


async def test_quoted_source_packet_fits_protected_reservation(tmp_path):
    source = 'DATA = [' + ', '.join('"x"' for _ in range(1200)) + ']\n'
    store, run, lease, provider, _ = setup_source_run(tmp_path, head_source=source)
    run.configuration.options.limits.total_input_tokens = VERIFICATION_INPUT + OVERVIEW_INPUT
    with store.sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    candidate, check = verifier_work(store, run, lease)
    context = AgentContext(provider, check, None)
    initial = await provider.initial(context, candidate,
        request_budget=VERIFICATION_INPUT, estimate_request=verifier_estimate)
    estimate = tokens(verifier_packet(initial)) + 512
    assert estimate <= VERIFICATION_INPUT
    assert initial["candidate_materials"]
    assert set(initial["missing_material"].values()) == {"material_capacity"}
    assert not context.pending_hunks
    assert set(context.pending) == {
        material["source_ref"] for material in initial["candidate_materials"]}
    await AgentRunner(ReviewModel()).run(check, provider, BudgetGate(store, run, lease))
    saved = next(item for item in store.work_items(run.id) if item.id == check.id)
    assert saved.status == "completed", saved.error_code
    assert saved.material_delivery == "incomplete"
    assert store.candidates(run.id)[0].verification.verdict == "insufficient"
    with store.sessions() as session:
        ticket = session.scalar(select(ReviewRequestOrm).where(ReviewRequestOrm.kind == "verification"))
        assert ticket.reserved_input_tokens <= VERIFICATION_INPUT


@pytest.mark.parametrize(("context_tokens", "total_input_tokens", "unknown_input"), [
    (16384, 100000, 0),
    (64000, 20000, 0),
    (64000, 30000, 10000),
])
async def test_small_verifier_starts_with_effective_input_budget(
    tmp_path, context_tokens, total_input_tokens, unknown_input,
):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    limits = run.configuration.options.limits
    limits.context_tokens = context_tokens
    limits.total_input_tokens = total_input_tokens
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    if unknown_input:
        ticket = store.reserve(run.id, reviewer, unknown_input, lease)
        store.settle(run.id, ticket, None, lease)
    submit_candidate(store, run, lease, reviewer, run.plan.units[0].required_hunk_ids[0])
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    assert store.can_start(run.id, check) is None
    check = store.claim(run.id, check.id, lease)
    await AgentRunner(ReviewModel()).run(check, provider, BudgetGate(store, run, lease))
    saved = next(item for item in store.work_items(run.id) if item.id == check.id)
    assert saved.status == "completed", saved.error_code
    assert store.candidates(run.id)[0].finding is not None
    usage = store.usage(run.id)
    assert usage["unknown_calls"] == bool(unknown_input)
    assert usage["committed_input_tokens"] >= unknown_input
