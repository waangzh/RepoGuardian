import asyncio
import json
import logging
from dataclasses import replace
from datetime import timedelta

import pytest
import aiosqlite
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.review.agents import agent_recursion_limit, build_agent
from app.review.context import AgentContext
from app.review.contracts import utcnow
from app.review.checkpointer import _checkpoint_serializer, review_thread_config
from app.review.graph import build_review_graph
from app.review.models import ReviewLimits
from app.review.nodes.agent_execution import run_agent
from app.review.recovery import retry_updates
from app.review.tools import read_base_file, read_file
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_prose import author_goal_text
from app.services.review import review_service
from tests.review.test_native_graph import ReviewModel, review_context
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]


def tool_runtime(source):
    return ToolRuntime(state={}, context=AgentContext(evidence=source), config={},
                       stream_writer=lambda *_: None, tool_call_id="fixture", store=None)


@pytest.mark.parametrize("operation,side", [(read_file, "head"), (read_base_file, "base")])
async def test_out_of_bounds_is_actionable_and_does_not_silently_clip(source_service, operation, side):
    result = await operation.coroutine(path="safe.py", start=1, end=2, runtime=tool_runtime(source_service))
    assert result == {"ok": False, "error": "source_range_out_of_bounds", "side": side,
        "requested_range": [1, 2], "available_range": [1, 1],
        "retryable_with_corrected_arguments": True, "truncated": False}
    corrected = await operation.coroutine(path="safe.py", start=1, end=1, runtime=tool_runtime(source_service))
    assert corrected["ok"] and corrected["evidence"]["end_line"] == 1
    assert corrected["evidence"]["side"] == side


@pytest.mark.parametrize("path", [".env", "../safe.py", "/safe.py", "missing.py"])
async def test_refused_paths_do_not_disclose_available_ranges(source_service, path):
    result = await read_file.coroutine(path=path, start=1, end=2, runtime=tool_runtime(source_service))
    assert not result["ok"]
    assert "available_range" not in result and "content" not in result


class LoopModel(ReviewModel):
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"path": "safe.py", "start": 1, "end": 2},
             "id": f"read-{self.calls}"}], usage_metadata={
                 "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))])


async def test_graph_step_guard_does_not_preempt_the_configured_model_budget(source_service):
    limits = ReviewLimits(model_calls=24)
    model = LoopModel()
    with pytest.raises(Exception, match="finalization_required") as failure:
        await build_agent(model, "reviewer", limits).ainvoke(
            {"messages": [("user", "Offline loop fixture")]},
            context=AgentContext(evidence=source_service),
            config={"recursion_limit": agent_recursion_limit(limits)})
    assert type(failure.value).__name__ == "FinalizationRequired"
    assert model.calls == 23  # 图步数不再提前终止；最后两次只允许提交。


async def test_exception_retains_tool_failures_and_does_not_leak_error_body(source_service, caplog):
    model = LoopModel()
    context = review_context(source_service, model)
    context.limits = ReviewLimits(model_calls=2)
    with caplog.at_level(logging.INFO, logger="RepoGuardian.Review"):
        result = await run_agent(context, "assignment", "reviewer", {"goals": []})
    assert result["status"] == "failed"
    assert result["error"] == "ModelCallLimitExceededError"
    assert result["model_calls"] == result["tool_calls"] == result["tool_failures"] == 2
    assert result["input_tokens"] == 20
    assert result["output_tokens"] == 10
    assert "review_id=review" in caplog.text
    assert "审查分支失败" in caplog.text
    assert "VALUE" not in caplog.text


async def test_retry_cannot_reset_remaining_model_budget(source_service):
    model = LoopModel()
    context = review_context(source_service, model)
    context.retry_results[("reviewer", "assignment")] = {
        "id": "assignment", "status": "failed", "model_calls": 22, "tool_calls": 22,
        "input_tokens": 220, "output_tokens": 110, "evidence_ids": [],
        "tool_failures": 22, "unconsumed_pages": 0, "error": "RuntimeError",
    }
    result = await run_agent(context, "assignment", "reviewer", {"goals": []})
    assert model.calls == 2
    assert result["error"] == "ModelCallLimitExceededError"
    assert result["model_calls"] == 24 and result["tool_calls"] == 24
    assert result["tool_failures"] == 24
    assert result["input_tokens"] == 240 and result["output_tokens"] == 120
    assert result["previous_attempts"][0]["model_calls"] == 22


async def test_new_attempt_cannot_cite_previous_attempts_without_reading_again(source_service):
    model = ReviewModel()
    context = review_context(source_service, model)
    old = await source_service.read("other.py", 1, 1)
    context.retry_results[("reviewer", "assignment")] = {
        "id": "assignment", "status": "failed", "model_calls": 1, "tool_calls": 1,
        "input_tokens": 10, "output_tokens": 5, "evidence_ids": [old["evidence"]["id"]],
        "tool_failures": 0, "unconsumed_pages": 0, "error": "RuntimeError",
    }
    result = await run_agent(context, "assignment", "reviewer", {"goals": []})
    assert result["status"] == "completed"
    assert old["evidence"]["id"] not in result["evidence_ids"]
    assert old["evidence"]["id"] in result["previous_attempts"][0]["evidence_ids"]
    assert result["tool_calls"] == 4  # 一次旧读取、两次新读取及结构化提交。


class FailingReviewModel(ReviewModel):
    fail_roles: list[str] = ["reviewer"]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        role = payload.get("assignment", {}).get("reviewer", "verifier")
        responses = [message for message in messages if isinstance(message, ToolMessage)]
        if role in self.fail_roles and responses:
            raise RuntimeError("PRIVATE_UPSTREAM_BODY credentials-must-not-appear")
        result = super()._generate(messages, stop, run_manager, **kwargs)
        if role in self.fail_roles and not responses:
            result.generations[0].message.tool_calls.append({
                "name": "read_file", "args": {"path": "safe.py", "start": 1, "end": 2},
                "id": "invalid-range", "type": "tool_call"})
        return result


def failing_pipeline(pipeline, roles):
    service, repository, _, _, _ = pipeline
    model = FailingReviewModel(fail_roles=roles)
    service._provider.agent_model = lambda *args, **kwargs: model
    return service, repository, model


async def test_all_failed_discoveries_have_failed_verdict_and_honest_report(pipeline, caplog):
    service, repository, model = failing_pipeline(pipeline, ["reviewer"])
    with caplog.at_level(logging.INFO, logger="RepoGuardian.Review"):
        _, _, task = await preview_and_create(service)
        assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == saved.review_report.termination == "failed"
    assert not saved.review_report.complete
    assert all(item.tool_failures == 1 and item.evidence_ids for item in saved.review_report.discovery_results)
    assert all(item.model_calls == 2 and item.tool_calls == 3 for item in saved.review_report.discovery_results)
    assert "PRIVATE_UPSTREAM_BODY" not in saved.report_markdown + caplog.text
    assert "尚无完整结论" in saved.report_markdown
    assert "执行失败原因" in saved.report_markdown
    assert "RuntimeError" in saved.report_markdown
    assert "（HEAD）" in saved.report_markdown and "（BASE）" in saved.report_markdown
    assert "未保存可归纳" not in saved.report_markdown
    assert "未保存逐目标" not in saved.report_markdown
    assert len(model.observed) == 1


@pytest.mark.parametrize("roles", [["reviewer"], ["verifier"]])
async def test_single_retry_reuses_completed_branches_and_accumulates_usage(pipeline, roles):
    service, repository, model = failing_pipeline(pipeline, roles)
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    original = repository.get_task(task.id)
    completed = {item.id: item for item in original.review_report.discovery_results
                 if item.status == "completed"}
    failed = [item for item in original.review_report.discovery_results + original.review_report.verification_results
              if item.status == "failed"]
    before = len(model.observed)
    model.fail_roles = []
    resumed = await service.resume_review(task.id)
    assert resumed.status == "queued" and resumed.retry_count == 1
    assert resumed.created_at == original.created_at and resumed.snapshot == original.snapshot
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed", saved.error
    assert saved.review_report.complete
    results = saved.review_report.discovery_results + saved.review_report.verification_results
    for identity, result in completed.items():
        assert next(item for item in results if item.id == identity) == result
    for previous in failed:
        result = next(item for item in results if item.id == previous.id)
        assert len(result.previous_attempts) == 1
        assert result.previous_attempts[0].status == "failed"
        assert result.model_calls == previous.model_calls + 2
        assert result.tool_calls == previous.tool_calls + 3  # 两次源码读取与一次结构化提交
        assert result.tool_failures == previous.tool_failures
    # A retried discovery also lets the pending verification run; a retried verifier is the last step.
    assert len(model.observed) - before == (2 if roles == ["reviewer"] else 1)
    with pytest.raises(ValueError, match="unavailable"):
        await service.resume_review(task.id)


async def test_retry_claim_is_atomic_and_a_failed_retry_cannot_be_repeated(pipeline):
    service, repository, _ = failing_pipeline(pipeline, ["reviewer"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    responses = await asyncio.gather(service.resume_review(task.id), service.resume_review(task.id),
                                     return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in responses) == 1
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "failed" and saved.retry_count == 1
    failed = next(item for item in saved.review_report.discovery_results if item.status == "failed")
    assert failed.model_calls == 4
    with pytest.raises(ValueError, match="retry_limit"):
        await service.resume_review(task.id)


@pytest.mark.parametrize("overrides,code", [
    ({"model_calls": None}, "usage_unknown"), ({"tool_calls": None}, "usage_unknown"),
    ({"model_calls": 24}, "budget_exhausted"), ({"tool_calls": 80}, "budget_exhausted"),
])
def test_retry_refuses_unknown_or_exhausted_branch_budgets(overrides, code):
    state = {"assignments": [{"id": "a", "reviewer": "reviewer"}],
             "discovery_results": [{"id": "a", "status": "failed", "model_calls": 2,
                                     "tool_calls": 2, **overrides}]}
    with pytest.raises(ValueError, match=code):
        retry_updates(state, ReviewLimits())


def test_author_source_sentence_does_not_erase_the_rest_of_the_goal():
    text = "Found by the deep-review audit (#92), reproduced before acting.\n\nKeep point inside its interval."
    assert author_goal_text(text) == "Keep point inside its interval."
    assert author_goal_text("Found by the deep-review audit (#92).") == ""


async def test_report_falls_back_to_title_when_body_only_describes_audit_source(pipeline):
    service, repository, _, pr, _ = pipeline
    pr.body = "Found by the deep-review audit (#92)."
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    plan = PublicationPlanner(repository._artifacts).build(repository.get_task(task.id))
    assert plan.digest.change_understanding.author_goal_source == "title"
    assert f"**PR 目标：** {pr.title}" in plan.user_report_markdown


async def test_partial_checkpoint_survives_maintenance_until_original_deadline(pipeline):
    from app.models.orm import ReviewTaskOrm

    service, repository, _ = failing_pipeline(pipeline, ["verifier"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    assert repository.get_task(task.id).status == "completed_with_warnings"
    assert repository.list_checkpoint_gc_thread_ids([task.id]) == []
    with repository._session_factory.begin() as session:
        row = session.get(ReviewTaskOrm, task.id)
        row.created_at = utcnow() - timedelta(seconds=task.configuration.limits.timeout_seconds + 1)
    assert repository.list_checkpoint_gc_thread_ids([task.id]) == [task.id]
    with pytest.raises(ValueError, match="review_deadline"):
        await service.resume_review(task.id)


@pytest.mark.parametrize("publication_state", ["queued", "preparing", "publishing", "uncertain"])
async def test_pending_publication_blocks_retry_without_claiming_the_attempt(pipeline, publication_state):
    from app.models.orm import PublicationAttemptOrm

    service, repository, _ = failing_pipeline(pipeline, ["reviewer"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    with repository._session_factory.begin() as session:
        session.add(PublicationAttemptOrm(id="publication", review_id=task.id, repository_id=1,
            pr_number=1, publisher_identity="fixture", head_sha=task.snapshot.head_sha,
            plan_hash="plan", selection_hash="selection", logical_key="logical",
            state=publication_state, plan={}, selection={}, context={}))
    with pytest.raises(ValueError, match="publication_pending"):
        await service.resume_review(task.id)
    saved = repository.get_task(task.id)
    assert saved.retry_count == 0 and saved.status == "failed"
    assert not await service._worker.run_once()


async def test_retry_cancel_and_lost_lease_cannot_prepare_or_overwrite_checkpoint(pipeline):
    from app.services.review.review_repository import ReviewLeaseLost

    service, repository, model = failing_pipeline(pipeline, ["reviewer"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    await service.resume_review(task.id)
    job = service._task_queue.claim(worker_id="owner")
    saver = await review_service.get_checkpointer()
    graph = build_review_graph(saver)
    before = await graph.aget_state(review_thread_config(task.id))
    calls = len(model.observed)
    with pytest.raises(ReviewLeaseLost):
        await service._run_graph(task.id, job=replace(job, lease_owner="other"))
    after = await graph.aget_state(review_thread_config(task.id))
    assert before.config == after.config and len(model.observed) == calls
    assert service.cancel_task(task.id)
    await service._run_graph(task.id, job=job)
    assert repository.get_task(task.id).status == "cancelled"
    assert len(model.observed) == calls


async def test_retry_preparation_checkpoint_survives_worker_restart_without_second_reset(pipeline):
    service, repository, model = failing_pipeline(pipeline, ["reviewer"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    original = repository.get_task(task.id)
    await service.resume_review(task.id)
    saver = await review_service.get_checkpointer()
    graph = build_review_graph(saver)
    config = review_thread_config(task.id)
    state = await graph.aget_state(config)
    await graph.aupdate_state(config, retry_updates(state.values, task.configuration.limits), as_node="plan")
    model.fail_roles = []
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed", saved.error
    assert saved.retry_count == 1
    failed_id = next(item.id for item in original.review_report.discovery_results if item.status == "failed")
    retried = next(item for item in saved.review_report.discovery_results if item.id == failed_id)
    assert retried.previous_attempts and retried.model_calls is None
    assert retried.tool_calls is None  # 进程中断前外部请求未知，不把新观测当作完整累计。


async def test_claimed_retry_without_checkpoint_refuses_full_review_fallback(pipeline):
    service, repository, model = failing_pipeline(pipeline, ["reviewer"])
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    await service.resume_review(task.id)
    saver = await review_service.get_checkpointer()
    await saver.adelete_thread(task.id)
    calls = len(model.observed)
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "failed" and saved.retry_count == 1
    assert len(model.observed) == calls


async def test_failed_branch_retry_reopens_sqlite_without_replaying_completed_branch(pipeline, tmp_path, monkeypatch):
    service, repository, model = failing_pipeline(pipeline, ["verifier"])
    database = str(tmp_path / "retry-checkpoints.sqlite")

    async def run_with_saver(saver):
        async def get_saver():
            return saver
        monkeypatch.setattr(review_service, "get_checkpointer", get_saver)

    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await saver.setup()
        await run_with_saver(saver)
        _, _, task = await preview_and_create(service)
        assert await service._worker.run_once()
        original = repository.get_task(task.id)
        completed = next(item for item in original.review_report.discovery_results if item.status == "completed")
    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await run_with_saver(saver)
        model.fail_roles = []
        await service.resume_review(task.id)
        assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed", saved.error
    assert saved.review_report.complete
    assert next(item for item in saved.review_report.discovery_results if item.id == completed.id) == completed
