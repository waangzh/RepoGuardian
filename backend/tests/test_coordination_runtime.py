import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.agents.providers import LLMProviderError
from app.models.orm import ReviewUnitOrm, SideEffectOrm, WorkerJobOrm
from app.models.review import AgentAction, ExecutionBudget, ReviewTask, TaskStatus
from app.services.coordination_runtime import CoordinationRuntime, CoordinationLeaseLost, coordination_fingerprint
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.model_usage import model_request_budget_hook
from app.services.review_manifest import build_review_manifest
from app.services.review_repository import ReviewRepository
from app.services.task_queue import ReviewWorker
from test_cross_unit_coordination import Provider, state
from test_phase6a_persistence import persistence as persistence
from test_provider import fake_chat as fake_chat


def prepare(tmp_path, repository):
    value = state(tmp_path)
    value["status"] = "verifying_issues"
    repository.create_task(ReviewTask(id=value["task_id"], pr_url=value["pr_info"]["html_url"]))
    return value


@pytest.mark.asyncio
async def test_restart_reuses_completed_unit_and_preserves_budget_and_metrics(tmp_path, persistence):
    repository, _, sessions = persistence
    value = prepare(tmp_path, repository)
    provider = Provider()
    stopped = False
    def progress(delta):
        nonlocal stopped
        if delta["followup_results"] and not stopped:
            stopped = True
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await CrossUnitCoordinationService(provider, repository=repository, progress=progress).run(value)
    restarted = ReviewRepository(sessions, repository._artifacts, require_migration=False)
    result = await CrossUnitCoordinationService(provider, repository=restarted).run(value)
    assert provider.calls == ["coordinate", "decide", "review", "decide", "verify"]
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 5
    assert result["coordination_plan"]["runtime_metrics"]["model_calls"] == 5
    assert result["coordination_plan"]["runtime_metrics"]["resume_count"] == 1
    assert result["coordination_plan"]["runtime_metrics"]["cache_hits"] >= 1
    assert result["issue_metrics"]["candidate_issue_count"] == 1
    replay = await CrossUnitCoordinationService(provider, repository=restarted).run(value)
    assert replay["issue_metrics"] == result["issue_metrics"]
    assert len(replay["review_issues"]) == 1
    assert provider.calls == ["coordinate", "decide", "review", "decide", "verify"]
    manifest = build_review_manifest({**value, **result}, datetime.now(timezone.utc))
    assert manifest.coverage.total_units == 2
    assert manifest.coordination_metrics.confirmed_count == 1
    with sessions() as session:
        assert list(session.scalars(select(ReviewUnitOrm))) == []


@pytest.mark.asyncio
async def test_inflight_unknown_call_is_not_reissued_after_restart(tmp_path, persistence):
    repository, _, sessions = persistence
    value = prepare(tmp_path, repository)
    entered = asyncio.Event()
    class Hanging(Provider):
        async def review_unit(self, *args):
            self.calls.append("review")
            entered.set()
            await asyncio.Event().wait()
    provider = Hanging()
    task = asyncio.create_task(CrossUnitCoordinationService(provider, repository=repository).run(value))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restarted = ReviewRepository(sessions, repository._artifacts, require_migration=False)
    result = await CrossUnitCoordinationService(provider, repository=restarted).run(value)
    assert provider.calls.count("review") == 1
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 3
    assert result["coordination_plan"]["runtime_metrics"]["unknown_calls"] == 1
    assert result["followup_results"][0]["outcome"] == "failed"
    assert result["coordination_plan"]["status"] == "unresolved"


@pytest.mark.asyncio
async def test_partial_verified_batch_survives_interruption(tmp_path, persistence):
    repository, _, _ = persistence
    value = prepare(tmp_path, repository)
    class Two(Provider):
        async def coordinate_cross_units(self, payload, model):
            proposed = await super().coordinate_cross_units(payload, model)
            second = proposed.followups[0].model_copy(update={"id": "f2", "primary_files": ["caller.py"]})
            return proposed.model_copy(update={"followups": [*proposed.followups, second]})
        async def review_unit(self, pr, files, diff, model, catalog):
            result = await super().review_unit(pr, files, diff, model, catalog)
            issue = result.issues[0]
            issue.primary_evidence.file_path = files[0].file_path
            return result
    provider = Two()
    stopped = False
    def progress(delta):
        nonlocal stopped
        if not stopped and any(item["validation_status"] == "completed" for item in delta["followup_results"]):
            stopped = True
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await CrossUnitCoordinationService(provider, repository=repository, progress=progress).run(value)
    result = await CrossUnitCoordinationService(provider, repository=repository).run(value)
    assert len(result["review_issues"]) == 2
    assert all(item["status"] == "confirmed" for item in result["review_issues"])
    assert result["issue_metrics"]["candidate_issue_count"] == 2
    assert result["issue_metrics"]["verifier_call_count"] == 2
    assert provider.calls.count("review") == 2 and provider.calls.count("verify") == 2
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 9


@pytest.mark.asyncio
async def test_budget_is_atomic_and_shared_across_runtime_identities(tmp_path, persistence):
    repository, _, _ = persistence
    prepare(tmp_path, repository)
    runtime = CoordinationRuntime("task", "a" * 64, ExecutionBudget(max_model_calls=1), repository)
    await runtime.load()
    await runtime.persist()
    class Count:
        def __init__(self):
            self.calls = 0
        async def decide(self, *args):
            self.calls += 1
            await asyncio.sleep(.01)
            return AgentAction(action="task_done", reason="完成")
    provider = Count()
    results = await asyncio.gather(runtime.call(provider, "decide", ({"x": 1}, None), 1200),
                                   runtime.call(provider, "decide", ({"x": 2}, None), 1200),
                                   return_exceptions=True)
    assert provider.calls == 1
    assert sum(isinstance(item, LLMProviderError) for item in results) == 1
    other = CoordinationRuntime("task", "b" * 64, ExecutionBudget(max_model_calls=16), repository)
    await other.load()
    await other.persist()
    with pytest.raises(LLMProviderError, match="budget_exhausted"):
        await other.call(provider, "decide", ({"x": 3}, None), 1200)
    assert provider.calls == 1 and other.budget.model_calls == 1


@pytest.mark.asyncio
async def test_transport_retries_also_reserve_budget(tmp_path, persistence):
    repository, _, _ = persistence
    value = prepare(tmp_path, repository)
    class Retry(Provider):
        async def review_unit(self, *args):
            await model_request_budget_hook.get()()
            return await super().review_unit(*args)
    result = await CrossUnitCoordinationService(Retry(), repository=repository).run(value)
    assert result["coordination_plan"]["execution_budget"]["model_calls"] == 6
    assert result["coordination_plan"]["runtime_metrics"]["model_calls"] == 6


@pytest.mark.asyncio
async def test_cancel_and_lease_fence_prevent_new_requests(tmp_path, persistence):
    repository, queue, sessions = persistence
    prepare(tmp_path, repository)
    queue.enqueue(task_id="task")
    job = queue.claim(worker_id="worker-a")
    lease = {"job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts}
    runtime = CoordinationRuntime("task", "a" * 64, ExecutionBudget(), repository, lease)
    await runtime.load()
    await runtime.persist()
    with sessions.begin() as session:
        session.get(WorkerJobOrm, job.id).lease_owner = "worker-b"
    provider = Provider()
    with pytest.raises(CoordinationLeaseLost):
        await runtime.call(provider, "decide", ({}, None), 1200)
    assert not provider.calls
    with sessions() as session:
        row = session.get(SideEffectOrm, "cross-unit-budget:task")
        assert row.result["budget"]["model_calls"] == 0
    repository.cancel_task("task")
    stale = repository.get_task("task")
    stale.status = TaskStatus.completed
    repository.save_task(stale)
    assert repository.get_task("task").status == TaskStatus.cancelled


@pytest.mark.parametrize("change", ["sha", "model", "unit", "summary", "policy"])
def test_fingerprint_invalidates_stale_runtime(change):
    value = state()
    modified = deepcopy(value)
    if change == "sha":
        modified["head_sha"] = "new-head"
    elif change == "model":
        modified["model"] = "another-model"
    elif change == "unit":
        modified["review_units"][0]["fingerprint"] = "changed"
    elif change == "summary":
        modified["review_unit_results"][0]["review_summary"]["record"]["change_summary"] = "新的检查结论"
    else:
        modified["cross_unit_risk"]["policy_version"] = "new-policy"
    assert coordination_fingerprint(value) != coordination_fingerprint(modified)


@pytest.mark.asyncio
async def test_followup_cannot_pollute_normal_unit_cache(tmp_path, persistence):
    repository, _, _ = persistence
    value = prepare(tmp_path, repository)
    result = await CrossUnitCoordinationService(Provider(), repository=repository).run(value)
    from app.models.review import ReviewUnit, ReviewUnitResult
    followup = ReviewUnitResult.model_validate(result["followup_results"][0]["unit_result"])
    unit = ReviewUnit(id=followup.review_unit_id, primary_files=["callee.py"], estimated_tokens=1,
        complexity="small", fingerprint=result["followup_results"][0]["fingerprint"], grouping_reason="cross_unit_followup")
    with pytest.raises(ValueError, match="isolated"):
        repository.record_unit_result(task_id="task", unit=unit, result=followup)


@pytest.mark.asyncio
async def test_worker_heartbeat_cancels_handler_on_lost_lease(monkeypatch):
    class Queue:
        def heartbeat(self, *args):
            return False
    worker = ReviewWorker(Queue(), lambda job: None)
    async def sleep(_delay):
        return None
    monkeypatch.setattr("app.services.task_queue.asyncio.sleep", sleep)
    entered = asyncio.Event()
    async def handler():
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(handler())
    await entered.wait()
    await worker._heartbeat("job", task)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_checkpoint_resume_after_worker_shutdown(tmp_path, persistence, monkeypatch):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, StateGraph
    from app.graph.state import ReviewState, ReviewRunContext
    from app.graph.nodes.cross_unit_coordination import cross_unit_coordination_node
    from app.graph.checkpointer import review_thread_config
    from app.services.review_service import ReviewService

    repository, queue, _ = persistence
    value = prepare(tmp_path, repository)
    git = value.pop("_git_tool")
    entered = asyncio.Event()
    class Hanging(Provider):
        async def review_unit(self, *args):
            self.calls.append("review")
            entered.set()
            await asyncio.Event().wait()
    provider = Hanging()
    graph = StateGraph(ReviewState, context_schema=ReviewRunContext)
    async def seed(state):
        return {}
    async def coordinate(raw, config, runtime):
        return await cross_unit_coordination_node({**raw, "_git_tool": git, "_provider": provider}, config, runtime)
    async def finish(raw):
        return {"status": "completed_with_warnings" if raw.get("warnings") else "completed"}
    graph.add_node("seed", seed)
    graph.add_node("cross_unit_coordination", coordinate)
    graph.add_node("finish", finish)
    graph.set_entry_point("seed")
    graph.add_edge("seed", "cross_unit_coordination")
    graph.add_edge("cross_unit_coordination", "finish")
    graph.add_edge("finish", END)
    cleaned = []
    deleted = []
    async def cleanup(path):
        cleaned.append(path)
    async def delete_checkpoints(task_id):
        deleted.append(task_id)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        await saver.setup()
        compiled = graph.compile(checkpointer=saver)
        await compiled.aupdate_state(review_thread_config("task"), value, as_node="seed")
        async def get_checkpointer():
            return saver
        monkeypatch.setattr("app.services.review_service.get_checkpointer", get_checkpointer)
        monkeypatch.setattr("app.services.review_service.build_review_graph", lambda phase: graph)
        monkeypatch.setattr("app.services.review_service._cleanup_repo", cleanup)
        monkeypatch.setattr("app.services.review_service.delete_thread_checkpoints", delete_checkpoints)
        service = ReviewService(None, None, None, provider, None, repository=repository, task_queue=queue)
        running = asyncio.create_task(service._run_graph("task"))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert repository.get_task("task").status != TaskStatus.cancelled
        assert repository.get_task("task").coordination_plan is not None
        assert not cleaned and not deleted
        assert (await compiled.aget_state(review_thread_config("task"))).next == ("cross_unit_coordination",)
        await service._run_graph("task")
        loaded = repository.get_task("task")
        assert loaded.status == TaskStatus.completed_with_warnings
        assert loaded.coordination_plan.runtime_metrics.unknown_calls == 1
        assert provider.calls.count("review") == 1
        assert cleaned and deleted == ["task"]
        # 图已完成、任务快照尚未落库的重试，复用图终态而不重新调用模型。
        await service._run_graph("task")
        assert provider.calls.count("review") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_real_provider_transport_retry_respects_shared_budget(tmp_path, persistence, fake_chat, limit):
    from langchain_core.messages import AIMessage
    from app.agents.providers import OpenAICompatibleProvider
    repository, _, _ = persistence
    prepare(tmp_path, repository)
    runtime = CoordinationRuntime("task", "f" * 64, ExecutionBudget(max_model_calls=limit), repository)
    await runtime.load()
    await runtime.persist()
    class APIConnectionError(RuntimeError):
        pass
    fake_chat.responses = [APIConnectionError("temporary outage"), AIMessage(content='{"decision":"uncertain","reason":"信息不足"}')]
    provider = OpenAICompatibleProvider("test-key", "https://example.com/v1", "model", retry_backoff_seconds=0)
    if limit == 1:
        with pytest.raises(LLMProviderError, match="before_transport_retry"):
            await runtime.call(provider, "coordinate_cross_units", ({}, None), 4096)
        assert len(fake_chat.responses) == 1
    else:
        result = await runtime.call(provider, "coordinate_cross_units", ({}, None), 4096)
        assert result.decision == "uncertain"
        assert not fake_chat.responses
    assert runtime.budget.model_calls == limit
    assert runtime.metrics().model_calls == limit


@pytest.mark.asyncio
async def test_reported_usage_corrects_budget_and_is_not_counted_again_on_replay(tmp_path, persistence):
    from app.models.review import ModelUsage, ModelCallResult
    repository, _, _ = persistence
    prepare(tmp_path, repository)
    runtime = CoordinationRuntime("task", "a" * 64, ExecutionBudget(max_token_usage=10_000), repository)
    await runtime.load()
    await runtime.persist()
    class Usage:
        async def decide(self, *args):
            return ModelCallResult(AgentAction(action="task_done", reason="完成"), ModelUsage(
                provider="test", model="model", operation="decide", latency_ms=1,
                actual_total_tokens=50_000, usage_available=True, cost_microusd=12,
            ))
    await runtime.call(Usage(), "decide", ({"x": 1}, None), 1200)
    assert runtime.budget.token_usage == 50_000
    await runtime.call(Usage(), "decide", ({"x": 1}, None), 1200)
    assert runtime.budget.token_usage == 50_000
    assert runtime.metrics().actual_tokens == 50_000
    assert runtime.metrics().cost_microusd == 12
    with pytest.raises(LLMProviderError, match="budget_exhausted"):
        await runtime.call(Usage(), "decide", ({"x": 2}, None), 1200)


@pytest.mark.asyncio
async def test_ainvoke_compatibility_path_injects_custom_repository(persistence):
    from app.services.review_service import ReviewService
    repository, queue, _ = persistence
    class InvokeOnly:
        async def ainvoke(self, value, *, config, context):
            assert context["coordination_repository"] is repository
            return value
    service = ReviewService(None, None, None, Provider(), None, repository=repository, task_queue=queue)
    task = ReviewTask(id="task", pr_url="https://github.com/local/sample/pull/1")
    assert await service._invoke_graph_with_progress(InvokeOnly(), {"task_id": "task"}, {}, task) == {"task_id": "task"}
