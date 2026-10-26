import json
from copy import deepcopy

import pytest

from app.models.review_runtime import WorkItemState
from app.services.review.review_run_budget import ReviewRunBudget, RunBudgetError
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from tests.review.test_review_citation_recovery import citation_slice, submission
from tests.support.runtime_harness import candidate, configured, inspect_submit, verification
from tests.support.runtime_harness import make_slice


def v2(step):
    step.executor.spec = step.executor.spec.model_copy(update={
        "stability_contract": "review-stability-v2", "prompt_version": "task-review-prompt-v8"})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)


async def test_visible_investigation_citation_failure_is_unresolved_not_supported(tmp_path):
    step, messages = citation_slice(tmp_path, [submission(["e1", "e2"])] * 2)
    v2(step)
    item = step.executor.work_item
    runtime = TaskReviewRuntime([item])
    assert await runtime.run({item.work_item_id: step}, deadline_seconds=10) == "plan_exhausted"
    result = step.executor.projection.investigation
    assert result.answer.value == "insufficient_evidence"
    assert result.reason == "citation_coverage_incomplete" and not result.evidence_ids
    assert item.state == WorkItemState.completed and len(messages) == 2
    assert step.executor.projection.invalidated_results[-1]["result"]["answer"] == "supported"
    from app.services.review.task_review_projection import project_task_runtime
    from app.services.review.review_outcome import derive_review_outcome
    from tests.support.review_tasks import completed_task as completed_task
    report = project_task_runtime(runtime, {item.work_item_id: step}, [], termination_reason="plan_exhausted")
    assert report.execution_status == "completed"
    assert derive_review_outcome(completed_task(
        admitted_investigations=report.admitted_investigations)).kind == "inconclusive"


@pytest.mark.parametrize("failure", ["inactive", "stale", "bad_hash"])
async def test_unavailable_source_never_gets_citation_guard(tmp_path, failure):
    step, _ = citation_slice(tmp_path, [submission(["e1", "e2"])] * 2)
    v2(step)
    if failure == "inactive":
        step.executor.active_evidence_ids.remove("archived")
    elif failure == "stale":
        step.executor.evidence_catalog["archived"]["head_sha"] = "f" * 40
    else:
        step.executor.evidence_catalog["archived"]["content_hash"] = "bad"
    step.stability["prepared"] = True
    step.stability["attempted_reads"] = [["src/a.py", 3, 4]]
    item = step.executor.work_item
    runtime = TaskReviewRuntime([item])
    assert await runtime.run({item.work_item_id: step}, deadline_seconds=10) != "plan_exhausted"
    assert not step.last_observation.get("guarded_unresolved")


async def test_inspect_correction_preserves_candidate_and_only_fills_real_gap(tmp_path):
    first = inspect_submit([candidate()])
    first["examinations"][0]["evidence_ids"] = []
    runtime, steps, derivation, messages = configured(tmp_path, [first, inspect_submit([candidate()]), verification])
    step = steps["w1"]
    v2(step)
    step.stability["prepared"] = True
    assert (await runtime.advance_one(steps))["correction_pending"]
    saved = deepcopy(TaskRuntimeCheckpoint._session(step))
    TaskRuntimeCheckpoint._restore_session(step, saved)
    assert await runtime.run(steps, deadline_seconds=10) == "plan_exhausted"
    assert len(derivation.data["candidates"]) == 1 and len(messages) == 3
    feedback = next(json.loads(line) for line in messages[1][1].content.splitlines()[1:]
                    if "goal_coverage_correction" in line)
    gap = feedback["goal_coverage_correction"][0]
    assert gap["side"] == "head" and gap["status"] == "ready"
    assert gap["uncovered_ranges"][0]["visible_evidence_ids"] == ["e1"]


async def test_closing_protection_reserves_calls_tokens_and_survives_restore():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2)
    closing = await budget.create_obligation(owner_task_id="inspect", reason="initial_inspect_closing",
                                             token_budget=60, call_budget=1)
    assert budget.available() == {"tokens": 40, "calls": 1}
    budget = ReviewRunBudget.restore(budget.snapshot(), recover_running=False)
    other = await budget.reserve(request_id="other", operation="review", estimated_tokens=40, task_id="other")
    with pytest.raises(RunBudgetError):
        await budget.reserve(request_id="steal", operation="review", estimated_tokens=1, task_id="other")
    own = await budget.reserve(request_id="own", operation="review", estimated_tokens=60, task_id="inspect")
    assert budget.obligations[closing.obligation_id].converted_attempt_id == own.attempt_id
    await budget.mark_dispatching(other.attempt_id)
    await budget.settle(other.attempt_id, actual_tokens=None)
    assert budget.unknown_reserved == 40 and budget.calls_consumed == 2
    assert budget.available() == {"tokens": 0, "calls": 0}


async def test_unsent_closing_reservation_restores_protection_but_unknown_does_not():
    budget = ReviewRunBudget("run", max_tokens=60, max_calls=1)
    closing = await budget.create_obligation(owner_task_id="inspect", reason="initial_inspect_closing",
                                             token_budget=60, call_budget=1)
    attempt = await budget.reserve(request_id="unsent", operation="review", estimated_tokens=60, task_id="inspect")
    await budget.release_unsent(attempt.attempt_id)
    assert budget.available() == {"tokens": 0, "calls": 0}
    assert budget.obligations[closing.obligation_id].state.value == "active"
    next_attempt = await budget.reserve(request_id="sent", operation="review", estimated_tokens=60, task_id="inspect")
    await budget.mark_dispatching(next_attempt.attempt_id)
    await budget.settle(next_attempt.attempt_id, actual_tokens=None)
    assert budget.obligations[closing.obligation_id].state.value == "converted"
    assert budget.unknown_reserved == 60 and budget.calls_consumed == 1


async def test_closing_recovery_does_not_create_another_protection(tmp_path):
    step, _ = make_slice(tmp_path, [])
    v2(step)
    await step.protect_closing()
    original = step.budget.snapshot()
    step.budget = ReviewRunBudget.restore(original, recover_running=False)
    step.stability.pop("closing_protection")  # crash after ledger write, before session checkpoint
    await step.protect_closing()
    assert step.budget.snapshot() == original
    assert len(step.budget.obligations) == 1


@pytest.mark.parametrize("transport_failure", [False, True])
async def test_frozen_round_has_same_admission_and_derivation_under_reversed_delays(tmp_path, transport_failure):
    import asyncio
    from app.services.review.review_task_derivation import TaskReviewDerivation

    async def run(name, delays):
        steps, traces, prompts = {}, [], []
        for index, delay in enumerate(delays, 1):
            folder = tmp_path / name / str(index)
            folder.mkdir(parents=True)
            target, identity = f"t{index}", f"w{index}"
            def submit(payload, target=target):
                value = inspect_submit([candidate(claim=f"defect {target}")])
                value["examinations"][0]["target_id"] = target
                value["candidates"][0]["primary_anchor"] = target
                return value
            step, _ = make_slice(folder, [submit, verification], max_calls=5 if transport_failure else 3)
            step.provider._request_attempts = 2 if transport_failure else 1
            step.provider._retry_backoff_seconds = 0
            v2(step)
            executor = step.executor
            executor.work_item.work_item_id = identity
            executor.work_item.unit_id = f"u{index}"
            executor.work_item.sequence = index
            executor.work_item.goal_ids = [target]
            goal = executor.goals.pop("t1").model_copy(update={"goal_id": target, "unit_id": f"u{index}"})
            executor.goals[target] = goal
            executor.projection.pending_target_ids = {target}
            step.scope = step.scope.model_copy(update={"review_unit_id": identity})
            original = step.provider._build_chat_model
            def delayed(*args, original=original, delay=delay, identity=identity):
                chat = original(*args)
                invoke = chat.ainvoke
                failed = False
                async def ainvoke(payload):
                    nonlocal failed
                    request = json.loads(payload[1].content.splitlines()[0])
                    prompts.append((identity, request["task"]["kind"], bool(request.get("finalization"))))
                    traces.append((identity, "start"))
                    await asyncio.sleep(delay)
                    if transport_failure and not failed and request["task"]["kind"] == "inspect":
                        failed = True
                        raise TimeoutError("fixture transport interruption")
                    result = await invoke(payload)
                    traces.append((identity, "end"))
                    return result
                chat.ainvoke = ainvoke
                return chat
            step.provider._build_chat_model = delayed
            steps[identity] = step
        budget = steps["w1"].budget
        steps["w2"].budget = budget
        runtime = TaskReviewRuntime([step.executor.work_item for step in steps.values()], concurrency=2)
        derivation = TaskReviewDerivation(runtime, steps, max_derived_items=1)
        termination = await runtime.run(steps, deadline_seconds=10)
        if delays[0] > delays[1]:
            assert traces.index(("w2", "start")) < traces.index(("w1", "end"))
        records = sorted((record["candidate"]["claim"], record["status"])
                         for record in derivation.data["candidates"].values())
        queue = [(item.kind.value, item.unit_id, item.state.value, item.reason)
                 for item in runtime._items.values()]
        return termination, queue, records, sorted(prompts), budget.calls_consumed, budget.settled_actual

    assert await run("slow-first", [.06, .001]) == await run("fast-first", [.001, .06])


@pytest.mark.parametrize("source,expected", [
    ("def run():\n    iv = wilson(30, 40)\n    return weighted_posterior([(iv, 1.0)])\n", {"weighted_posterior"}),
    ("def run():\n    iv = wilson(30, 40)\n    iv = other()\n    return weighted_posterior(iv)\n", set()),
    ("def run():\n    iv = wilson(30, 40)\n    if iv:\n        return weighted_posterior(iv)\n", set()),
])
def test_one_hop_context_is_a_conservative_syntax_hint(source, expected):
    from app.services.review.review_stability import one_hop_consumers
    assert one_hop_consumers(source) == expected


def test_goal_citation_gaps_respect_base_side_and_current_page(tmp_path):
    import hashlib
    step, _ = make_slice(tmp_path, [])
    v2(step)
    executor = step.executor
    goal = executor.goals["t1"].model_copy(update={"target_kind": "base_source_range", "start_line": 1, "end_line": 4})
    executor.goals["t1"] = goal
    template = deepcopy(executor.evidence_catalog["e1"])
    for identity, start, end, content, side in [("e1", 1, 2, "a\nb", "base"),
            ("e2", 3, 4, "c\nd", "base"), ("head", 1, 4, "a\nb\nc\nd", "head")]:
        executor.evidence_catalog[identity] = {**template, "start_line": start, "end_line": end,
            "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(), "side": side}
    executor.active_evidence_ids = {"e1", "e2", "head"}
    feedback = executor.goal_citation_gaps(goal, ["e1", "head"])
    assert feedback["side"] == "base" and feedback["status"] == "ready"
    assert feedback["uncovered_ranges"] == [{"start_line": 3, "end_line": 4, "visible_evidence_ids": ["e2"]}]
    page = goal.model_copy(update={"start_line": 3})
    assert executor.goal_citation_gaps(page, ["e2"])["uncovered_ranges"] == []


@pytest.mark.parametrize("extension", ["js", "ts", "go", "java", "rs"])
def test_multilanguage_hints_preload_whole_source_without_upgrading_binding(tmp_path, extension):
    from app.services.review.review_stability import inspection_requests, resolved_call
    step, _ = make_slice(tmp_path, [])
    v2(step)
    path = f"src/a.{extension}"
    executor = step.executor
    executor.work_item.scope_files = [path]
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"target_ref": path, "start_line": 2, "end_line": 3})
    definitions = [{"id": "caller", "symbol_id": "caller", "file": path, "start_line": 1, "end_line": 4}]
    target = {"id": "target", "file": path, "start_line": 6, "end_line": 9}
    edge = {"source": "caller", "target": "target", "type": "calls", "resolution_status": "unresolved",
            "resolution_basis": "unique_name", "relationship_level": "search_hint"}
    graph = {"symbols": [*definitions, target], "edges": [edge]}
    before = deepcopy(graph)
    requests = inspection_requests(executor, definitions, graph, 240)
    assert {"file_path": path, "start_line": 6, "end_line": 9} in requests
    assert graph == before and not resolved_call(edge)


async def test_conservative_clustering_ignores_duplicate_citations_but_keeps_distinct_failures(tmp_path):
    duplicate = candidate()
    duplicate["evidence_ids"] = ["e1", "e1"]
    different = candidate(claim="字段重命名破坏兼容性", scenario="旧调用方仍访问旧字段")
    runtime, steps, derivation, messages = configured(tmp_path, [
        inspect_submit([candidate(), duplicate, different]), verification, verification])
    v2(steps["w1"])
    steps["w1"].stability["prepared"] = True
    assert await runtime.run(steps, deadline_seconds=10) == "plan_exhausted"
    assert len(derivation.data["candidates"]) == 2 and len(messages) == 3
    assert all(record["status"] == "confirmed" for record in derivation.data["candidates"].values())


async def test_cancelling_frozen_round_keeps_sent_usage_unknown(tmp_path):
    import asyncio
    steps = {}
    started = asyncio.Event()
    sends = []
    for index in (1, 2):
        folder = tmp_path / str(index)
        folder.mkdir()
        step, _ = make_slice(folder, [], max_calls=2)
        v2(step)
        identity = f"w{index}"
        step.executor.work_item.work_item_id = identity
        step.executor.work_item.unit_id = f"u{index}"
        step.stability["prepared"] = True
        class PendingSDK:
            async def ainvoke(self, payload):
                sends.append(payload)
                if len(sends) == 2:
                    started.set()
                await asyncio.Future()
        step.provider._build_chat_model = lambda *args: PendingSDK()
        steps[identity] = step
    budget = steps["w1"].budget
    steps["w2"].budget = budget
    runtime = TaskReviewRuntime([step.executor.work_item for step in steps.values()], concurrency=2)
    runner = asyncio.create_task(runtime.run(steps, deadline_seconds=10))
    try:
        await asyncio.wait_for(started.wait(), 5)
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    await runtime.cancel(steps)
    assert budget.calls_consumed == 2 and budget.unknown_reserved > 0
    assert all(item.state.value == "cancelled" for item in runtime._items.values())
    assert all(attempt.sent and not attempt.usage_available for attempt in budget.attempts.values())
