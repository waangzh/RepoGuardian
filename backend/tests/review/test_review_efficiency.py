import json
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import inspect

from app.review.agents import build_agent
from app.review.budget import ReviewBudget, ReviewBudgetExceeded
from app.review.context import AgentContext
from app.review.materials import prepare_materials
from app.review.models import AgentResult, ReviewLimits, VerificationOutput
from app.review.nodes.agent_execution import AgentUsage, run_agent
from app.review.planning import plan_review
from app.review.tools import read_discussion
from app.review.verification_batches import split_batch_result, verification_batches
from app.services.review.review_repository import ReviewLeaseLost, ReviewStopped
from tests.review.test_native_agents import ToolCallingModel
from tests.review.test_native_graph import CHANGE, review_context
from tests.review.test_native_lifecycle import preview_and_create
from tests.review.test_retrieval import commit_sources

pytest_plugins = ["tests.review.test_native_lifecycle"]


def test_general_boundaries_do_not_require_an_unknown_verdict():
    for decision in ("confirmed", "refuted"):
        result = VerificationOutput(decision=decision, explanation="源码已建立当前结论",
            attribution="introduced", boundary_notes=["未运行测试，只读静态审查"])
        assert result.boundary_notes and not result.limitations
        with pytest.raises(ValueError, match="limitations_require"):
            result.model_copy().model_validate({**result.model_dump(), "limitations": ["缺少必要调用方"]})
    description = VerificationOutput.model_json_schema()["properties"]["limitations"]["description"]
    assert "MUST be empty" in description and "insufficient_evidence" in description


class FinalizingModel(ToolCallingModel):
    tools_seen: list[list[str]] = []
    calls: int = 0
    correct_once: bool = False
    never_finalize: bool = False

    def bind_tools(self, tools, **kwargs):
        self.tools_seen.append([tool.name if hasattr(tool, "name") else tool["function"]["name"]
                                for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1 or self.never_finalize:
            calls = [{"name": "read_file", "args": {"path": "safe.py", "start": 1, "end": 1},
                      "id": f"read-{self.calls}"}]
        else:
            calls = [{"name": "VerificationOutput", "id": f"final-{self.calls}", "args": {
                "decision": "refuted", "explanation": "读取源码后候选不成立", "attribution": "uncertain",
                "evidence_ids": [], "boundary_notes": ["未执行测试"],
                "limitations": ["缺少关键前提"] if self.correct_once and self.calls == 2 else [],
            }}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls,
            usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
                            "input_token_details": {"cache_read": 10}}))])


@pytest.mark.parametrize("enabled,discussions,visible", [(False, [], False), (False, [{}], False),
                                                       (True, [], False), (True, [{}], True)])
async def test_discussion_tool_disclosure_and_execution_are_both_guarded(source_service, enabled, discussions, visible):
    model = FinalizingModel()
    context = AgentContext(evidence=source_service, discussions_enabled=enabled, discussions=discussions)
    await build_agent(model, "verifier", ReviewLimits()).ainvoke(
        {"messages": [("user", "Review fixture")]}, context=context)
    assert ("read_discussion" in model.tools_seen[0]) is visible
    if not enabled:
        result = await read_discussion.coroutine(index=0, runtime=SimpleNamespace(context=context))
        assert result == {"ok": False, "error": "discussion_disabled"}


async def test_finalization_reserves_schema_correction_inside_original_budget(source_service):
    model = FinalizingModel(correct_once=True)
    result = await build_agent(model, "verifier", ReviewLimits(model_calls=3)).ainvoke(
        {"messages": [("user", "Review fixture")]}, context=AgentContext(evidence=source_service))
    assert result["structured_response"].decision == "refuted" and model.calls == 3
    assert model.tools_seen[1] == model.tools_seen[2] == ["VerificationOutput"]
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage)
              and str(message.content).startswith("Error:")]
    assert len(errors) == 1


async def test_finalization_rejects_hallucinated_exploration_not_only_hiding_tools(source_service):
    model = FinalizingModel(never_finalize=True)
    reads = 0
    original = source_service.read

    async def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)

    source_service.read = read
    with pytest.raises(Exception, match="finalization_required"):
        await build_agent(model, "verifier", ReviewLimits(model_calls=3)).ainvoke(
            {"messages": [("user", "Never finishes")]}, context=AgentContext(evidence=source_service))
    assert model.calls == 2 and reads == 1


async def test_search_policy_and_paging_are_not_tool_failures(caplog):
    observer = AgentUsage("review", "agent", "reviewer")
    with caplog.at_level(logging.INFO, logger="RepoGuardian.Review"):
        await observer.on_tool_start({"name": "search_code"}, "", run_id="search")
        await observer.on_tool_end(json.dumps({"ok": True, "policy_exclusions": 2,
            "scope_complete": False, "unreadable_files": 1, "has_more": True,
            "pagination_id": "scope", "next_cursor": "cursor"}), run_id="search")
    assert observer.tool_failures == 0 and observer.policy_exclusions == 2
    assert observer.incomplete_searches == 1 and observer.values()["unconsumed_pages"] == 1
    assert "工具拒绝" not in caplog.text and "搜索范围诊断" in caplog.text


async def test_prepared_sources_are_diff_first_with_frozen_evidence(source_service):
    packet, ids = await prepare_materials(AgentContext(evidence=source_service, changed_files=[CHANGE]),
        {"goals": [{"path": "safe.py", "side": side, "start_line": 1, "end_line": 1}
                   for side in ("head", "base")]})
    diff = packet["change_materials"]
    assert diff["complete"] and diff["included_hunks"] == 1 and not diff["omitted_hunks"]
    assert "-     1 VALUE = 1" in diff["files"][0]["hunks"][0]["text"]
    assert "+     1 VALUE = 2" in diff["files"][0]["hunks"][0]["text"]
    assert diff["digest"] not in ids
    assert len(ids) == 2
    assert all(item["total_lines"] == 1 and item["max_read_lines"] == 240 for item in packet["files"])
    for item in packet["source_materials"]:
        ref = source_service.resolve(item["evidence"]["id"])
        assert ref.side in {"head", "base"} and ref.path == "safe.py"
    denied, ids = await prepare_materials(AgentContext(evidence=source_service, denied_paths=frozenset({"safe.py"})),
        {"goals": [{"path": "safe.py", "side": "head", "start_line": 1, "end_line": 1}]})
    assert not ids and not denied["source_materials"] and not denied["files"]
    assert not denied["change_materials"]["files"]


async def test_quick_depth_preloads_no_source_context(source_service):
    packet, ids = await prepare_materials(AgentContext(evidence=source_service, changed_files=[CHANGE]),
        {"goals": [{"path": "safe.py", "side": side, "start_line": 1, "end_line": 1}
                   for side in ("head", "base")]}, "quick")
    assert not ids and not packet["source_materials"] and not packet["files"]
    assert packet["change_materials"]["files"][0]["file_path"] == "safe.py"
    assert "preloads no source context" in packet["material_boundary"]


async def test_candidate_context_covers_head_anchor_and_base_comparison(source_service):
    packet, ids = await prepare_materials(AgentContext(evidence=source_service, changed_files=[CHANGE]),
        {"candidate": {"affected_path": "safe.py", "line": 1}})
    assert {item["evidence"]["side"] for item in packet["source_materials"]} == {"head", "base"}
    assert all(item["evidence"]["path"] == "safe.py" for item in packet["source_materials"])
    assert len(ids) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["rename-first", "added-first"])
@pytest.mark.parametrize("modified", [False, True], ids=["pure-rename", "modified-rename"])
async def test_materials_distinguish_renamed_file_from_reused_old_path(source_service, reverse, modified):
    renamed_source = "VALUE = 2\n" if modified else "VALUE = 1\n"
    commit_sources(source_service, {"renamed.py": renamed_source, "safe.py": "REPLACEMENT = 3\n"})
    renamed = {"file_path": "renamed.py", "old_file_path": "safe.py", "change_type": "renamed",
               "additions": int(modified), "deletions": int(modified),
               "hunks": CHANGE["hunks"] if modified else []}
    added = {"file_path": "safe.py", "change_type": "added", "additions": 1, "deletions": 0,
             "hunks": [{"old_start": 0, "old_length": 0, "new_start": 1, "new_length": 1,
                        "lines": [{"kind": "added", "new_line_no": 1, "content": "REPLACEMENT = 3"}]}]}
    changes = [added, renamed] if reverse else [renamed, added]
    context = AgentContext(evidence=source_service, changed_files=changes)
    plan = plan_review(changes, depth="standard")
    for assignment in plan.assignments:
        packet, ids = await prepare_materials(context, {
            "assignment": assignment.model_dump(),
            "goals": [item.model_dump() for item in plan.goals if item.id in assignment.goal_ids],
        })
        diff = packet["change_materials"]
        assert [item["file_path"] for item in diff["files"]] == assignment.focus_paths
        assert diff["complete"] and not diff["omitted_hunks"]
        if assignment.focus_paths == ["safe.py"]:
            assert diff["included_hunks"] == 1
            assert "REPLACEMENT = 3" in diff["files"][0]["hunks"][0]["text"]
            assert {item["evidence"]["side"] for item in packet["source_materials"]} == {"head"}
            assert ids
        else:
            assert diff["included_hunks"] == int(modified)

    base_only, _ = await prepare_materials(context, {
        "goals": [{"path": "safe.py", "side": "base", "start_line": 1, "end_line": 1}],
    })
    assert [item["file_path"] for item in base_only["change_materials"]["files"]] == ["renamed.py"]
    assert base_only["source_materials"][0]["content"] == "VALUE = 1\n"

    for path in ("safe.py", "renamed.py"):
        packet, ids = await prepare_materials(context, {"candidate": {"affected_path": path, "line": 1}})
        assert [item["file_path"] for item in packet["change_materials"]["files"]] == [path]
        sources = {(item["evidence"]["side"], item["evidence"]["path"]): item["content"]
                   for item in packet["source_materials"]}
        expected = ({("head", "safe.py"): "REPLACEMENT = 3\n"} if path == "safe.py" else {
            ("head", "renamed.py"): renamed_source, ("base", "safe.py"): "VALUE = 1\n",
        })
        assert sources == expected
        assert ids == {item["evidence"]["id"] for item in packet["source_materials"]}
        for identity in ids:
            source_service.resolve(identity)


async def test_large_diff_records_omitted_hunks_without_fake_evidence(source_service):
    change = {"file_path": "safe.py", "change_type": "modified", "additions": 2, "deletions": 0,
              "hunks": [
                  {"old_start": 1, "old_length": 0, "new_start": 1, "new_length": 1,
                   "lines": [{"kind": "added", "new_line_no": 1, "content": "A" * 7_000}]},
                  {"old_start": 1, "old_length": 0, "new_start": 2, "new_length": 1,
                   "lines": [{"kind": "added", "new_line_no": 2, "content": "B" * 7_000}]}]}
    packet, ids = await prepare_materials(AgentContext(evidence=source_service, changed_files=[change]),
        {"candidate": {"affected_path": "safe.py", "line": 1}})
    diff = packet["change_materials"]
    assert not diff["complete"] and diff["included_hunks"] == 1
    assert diff["omitted_hunks"] == [{"file_path": "safe.py", "hunk_index": 1,
        "old_range": [1, 0], "new_range": [2, 2]}]
    assert diff["files"][0]["omitted_hunk_indexes"] == [1]
    assert diff["digest"] not in ids


def candidate(identity, line=1, path="safe.py"):
    return {"id": identity, "line": line, "affected_path": path}


def test_batch_groups_are_bounded_and_never_remove_candidate_identity():
    items = [candidate(str(i), i) for i in range(1, 6)] + [candidate("far", 100), candidate("other", 1, "other.py")]
    batches = verification_batches(items)
    assert max(len(item["candidates"]) for item in batches) == 3
    assert {item["id"] for batch in batches for item in batch["candidates"]} == {item["id"] for item in items}
    assert batches == verification_batches(list(reversed(items)))


@pytest.mark.parametrize("mode", ["missing", "duplicate", "extra", "valid"])
def test_batch_decisions_validate_each_identity_and_keep_unknowns(mode):
    decision = {"candidate_id": "a", "decision": "refuted", "explanation": "反证成立", "attribution": "uncertain"}
    decisions = [decision, {**decision, "candidate_id": "b"}]
    if mode == "missing":
        decisions.pop()
    elif mode == "duplicate":
        decisions.append(decision)
    elif mode == "extra":
        decisions.append({**decision, "candidate_id": "invented"})
    result = AgentResult(id="batch", status="completed", output={"decisions": decisions}, model_calls=1).model_dump()
    split = split_batch_result(result, [candidate("a"), candidate("b")])
    assert [item["id"] for item in split] == ["a", "b"]
    assert all(item["execution_id"] == "batch" for item in split)
    expected = {"valid": ["completed", "completed"], "missing": ["completed", "failed"],
                "duplicate": ["failed", "completed"], "extra": ["failed", "failed"]}[mode]
    assert [item["status"] for item in split] == expected


async def test_in_memory_budget_preserves_unknown_and_idempotent_settlement():
    budget = ReviewBudget(ReviewLimits(total_model_calls=2, total_input_tokens=1000))
    one = await budget.reserve("agent", 600)
    await budget.settle(one, {"input_tokens": 100, "output_tokens": 20,
                              "input_token_details": {"cache_read": 80}})
    await budget.settle(one, {"input_tokens": 0, "output_tokens": 0})
    two = await budget.reserve("agent", 600)
    await budget.settle(two, None)
    summary = await budget.summary()
    assert summary["input_token_factor"] == 0.167
    assert summary["committed_input_tokens"] == 200 and summary["input_tokens"] is None
    assert summary["outstanding_input_tokens"] == 100 and summary["unknown_calls"] == 1
    with pytest.raises(ReviewBudgetExceeded):
        await budget.reserve("agent", 1)


async def test_calibrated_factor_replaces_raw_byte_reservation():
    budget = ReviewBudget(ReviewLimits(total_input_tokens=1000))
    known = await budget.reserve("agent", 100)
    await budget.settle(known, {"input_tokens": 25, "output_tokens": 5})
    # 3000 bytes would exceed the limit if bytes were treated as tokens.
    ticket = await budget.reserve("agent", 3000)
    summary = await budget.summary()
    assert summary["input_token_factor"] == 0.25
    assert summary["committed_input_tokens"] == 775
    await budget.settle(ticket, None)
    summary = await budget.summary()
    assert summary["unknown_calls"] == 1 and summary["outstanding_input_tokens"] == 750
    assert summary["committed_input_tokens"] == 775


async def test_input_rejection_reports_dimension_and_amounts_while_calls_remain(caplog):
    budget = ReviewBudget(ReviewLimits(total_model_calls=64, total_input_tokens=1000))
    await budget.reserve("agent", 1000)
    with caplog.at_level(logging.WARNING, logger="RepoGuardian.Review"):
        with pytest.raises(ReviewBudgetExceeded) as excinfo:
            await budget.reserve("agent", 1)
    assert str(excinfo.value) == "input_tokens"
    for field in ("dimension=input_tokens", "stage=explore", "model_calls=1/64",
                  "committed_input=1000", "outstanding_input=1000", "estimate=1",
                  "held=0", "factor=1.0", "input_limit=1000"):
        assert field in caplog.text


async def test_exploration_reserves_its_final_submission_and_correction(source_service):
    class RecordingBudget(ReviewBudget):
        def __init__(self, limits):
            super().__init__(limits)
            self.reserves = []

        async def reserve(self, agent_id, encoded_bytes, *, stage="explore", hold=0):
            self.reserves.append((stage, hold, encoded_bytes))
            return await super().reserve(agent_id, encoded_bytes, stage=stage, hold=hold)

    budget = RecordingBudget(ReviewLimits(model_calls=3))
    context = AgentContext(evidence=source_service, budget=budget, budget_agent_id="agent")
    result = await build_agent(FinalizingModel(), "verifier", ReviewLimits(model_calls=3)).ainvoke(
        {"messages": [("user", "Review fixture")]}, context=context)
    assert result["structured_response"].decision == "refuted"
    assert [stage for stage, _, _ in budget.reserves] == ["explore", "finalize"]
    assert [hold for _, hold, _ in budget.reserves] == [2, 1]
    # The submission carries no exploration tools, so its reservation is smaller.
    assert budget.reserves[1][2] < budget.reserves[0][2]


async def test_input_rejection_finalizes_instead_of_failing_the_branch(source_service):
    class RejectingBudget(ReviewBudget):
        def __init__(self, limits):
            super().__init__(limits)
            self.stages = []

        async def reserve(self, agent_id, encoded_bytes, *, stage="explore", hold=0):
            self.stages.append(stage)
            if stage == "explore":
                raise ReviewBudgetExceeded("input_tokens")
            return await super().reserve(agent_id, encoded_bytes, stage=stage, hold=hold)

    class ConvergingModel(ToolCallingModel):
        tools_seen: list[list[str]] = []
        calls: int = 0

        def bind_tools(self, tools, **kwargs):
            self.tools_seen.append([tool.name if hasattr(tool, "name") else tool["function"]["name"]
                                    for tool in tools])
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            if self.tools_seen[-1] == ["VerificationOutput"]:
                calls = [{"name": "VerificationOutput", "id": f"final-{self.calls}", "args": {
                    "decision": "refuted", "explanation": "输入预算压力下直接提交",
                    "attribution": "uncertain", "evidence_ids": [],
                    "boundary_notes": ["未执行测试"], "limitations": []}}]
            else:
                calls = [{"name": "read_file", "args": {"path": "safe.py", "start": 1, "end": 1},
                          "id": f"read-{self.calls}"}]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                tool_calls=calls, usage_metadata={"input_tokens": 20, "output_tokens": 5,
                    "total_tokens": 25, "input_token_details": {"cache_read": 10}}))])

    model = ConvergingModel()
    budget = RejectingBudget(ReviewLimits())
    context = AgentContext(evidence=source_service, budget=budget, budget_agent_id="agent")
    result = await build_agent(model, "verifier", ReviewLimits(model_calls=8)).ainvoke(
        {"messages": [("user", "Review fixture")]}, context=context)
    assert result["structured_response"].decision == "refuted"
    # The rejected exploration never reaches the model; the branch submits immediately.
    assert model.calls == 1 and budget.stages == ["explore", "finalize"]
    assert model.tools_seen == [["VerificationOutput"]]


async def test_model_middleware_settles_actual_usage_and_reuses_round_budget(source_service):
    model = FinalizingModel()
    context = review_context(source_service, model)
    context.budget = ReviewBudget(ReviewLimits(total_model_calls=3))
    result = await run_agent(context, "agent", "verifier", {"candidate": {"id": "c", "affected_path": "safe.py", "line": 1}})
    assert result["status"] == "completed"
    summary = await context.budget.summary()
    assert summary["model_calls"] == 2 and summary["input_tokens"] == 40
    assert summary["cached_input_tokens"] == 20 and summary["unknown_calls"] == 0
    assert summary["held_input_tokens"] == summary["held_model_calls"] == 0


async def test_schema_retry_is_counted_even_when_the_agent_fails(source_service):
    context = review_context(source_service, FinalizingModel(correct_once=True))
    context.limits = ReviewLimits(model_calls=2)
    result = await run_agent(context, "agent", "verifier",
        {"candidate": candidate("a")})
    assert result["status"] == "failed" and result["error"] == "ModelCallLimitExceededError"
    assert result["structured_output_retries"] == 1


async def test_actual_usage_overrun_is_visible_and_blocks_followup_requests():
    budget = ReviewBudget(ReviewLimits(total_input_tokens=1000))
    ticket = await budget.reserve("agent", 500)
    await budget.settle(ticket, {"input_tokens": 1200, "output_tokens": 10})
    assert (await budget.summary())["committed_input_tokens"] == 1200
    with pytest.raises(ReviewBudgetExceeded):
        await budget.reserve("agent", 1)


async def test_round_call_limit_does_not_reset_after_agent_failure(pipeline):
    from app.models.orm import ReviewModelCallOrm
    from sqlalchemy import select

    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    limits = task.configuration.limits
    tickets = [repository.reserve_model_call(task.id, "fixture", 1, limits)
               for _ in range(limits.total_model_calls)]
    assert len(tickets) == limits.total_model_calls
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "failed" and saved.error == "ReviewBudgetExceeded"
    assert saved.review_report.budget["model_calls"] == limits.total_model_calls
    with repository._session_factory() as session:
        assert len(list(session.scalars(select(ReviewModelCallOrm)))) == limits.total_model_calls
    with pytest.raises(ValueError, match="budget_exhausted"):
        await service.resume_review(task.id)


async def test_durable_concurrent_budget_claims_and_reopen_keep_reservations(pipeline):
    from app.services.review.review_repository import ReviewRepository

    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    task.configuration.limits.total_model_calls = 2
    # Budget policy is frozen in production; use original limit and concurrently exhaust it.
    task = repository.get_task(task.id)
    limits = task.configuration.limits

    def reserve(_):
        try:
            return repository.reserve_model_call(task.id, "agent", limits.total_input_tokens // 2, limits)
        except ReviewBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert sum(item is not None for item in results) == 2
    reopened = ReviewRepository(repository._session_factory, repository._artifacts, require_migration=False)
    summary = reopened.model_budget_summary(task.id, limits)
    assert summary["unknown_calls"] == 2 and summary["committed_input_tokens"] == limits.total_input_tokens
    with pytest.raises(ReviewBudgetExceeded):
        reopened.reserve_model_call(task.id, "agent", 1, limits)
    job = service._task_queue.claim(worker_id="owner")
    lease = {"job_id": job.id, "owner": "wrong", "attempt": job.attempts}
    with pytest.raises(ReviewLeaseLost):
        reopened.reserve_model_call(task.id, "agent", 1, limits, lease=lease)
    assert service.cancel_task(task.id)
    with pytest.raises(ReviewStopped):
        reopened.settle_model_call(task.id, next(item for item in results if item), {"input_tokens": 1, "output_tokens": 1})


def test_budget_migration_upgrades_existing_database_without_losing_tasks(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    monkeypatch.setattr("logging.config.fileConfig", lambda *args, **kwargs: None)

    engine = create_engine(f"sqlite:///{(tmp_path / 'upgrade.sqlite').as_posix()}")
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "20261027_0002")
        assert "review_model_calls" not in inspect(connection).get_table_names()
        from app.models.orm import ReviewTaskOrm, utcnow
        connection.execute(ReviewTaskOrm.__table__.insert().values(
            id="preserved", pr_url="https://github.com/local/repo/pull/1", mode="review",
            status="failed", current_phase="failed", provider="fixture",
            warnings=[], task_snapshot={"sentinel": "keep"}, thread_id="preserved",
            retention_until=utcnow()))
        command.upgrade(config, "head")
        assert "review_model_calls" in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261028_0005"
        assert connection.scalar(text("SELECT task_snapshot FROM review_tasks WHERE id='preserved'")) == '{"sentinel": "keep"}'
        command.downgrade(config, "20261027_0002")
        assert "review_model_calls" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
    engine.dispose()


async def test_same_snapshot_packet_and_batch_comparison_preserves_verdicts_and_evidence(source_service, monkeypatch, tmp_path):
    from langchain_core.messages import ToolMessage
    from app.review.graph import build_review_graph
    from app.review import graph as graph_module
    from app.review.nodes import agent_execution
    from tests.review.test_finding_aggregation import EquivalentReviewModel

    class PacketModel(EquivalentReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(message.content for message in messages if message.type == "human"))
            tools = [message for message in messages if isinstance(message, ToolMessage)]
            if not tools and payload.get("source_materials"):
                self.observed.append(payload)
                messages = messages + [ToolMessage(content=json.dumps(packet), name="read_file",
                    tool_call_id=f"packet-{index}") for index, packet in enumerate(payload["source_materials"])]
            result = super()._generate(messages, stop, run_manager, **kwargs)
            count = max(1, sum(len(str(message.content).encode("utf-8")) for message in messages) // 4)
            result.generations[0].message.usage_metadata = {
                "input_tokens": count, "output_tokens": 100, "total_tokens": count + 100}
            return result

    prepare = agent_execution.prepare_materials
    batching = graph_module.verification_batches

    async def no_packet(*args):
        return {}, set()

    async def run(with_packets, batched):
        monkeypatch.setattr(agent_execution, "prepare_materials", prepare if with_packets else no_packet)
        monkeypatch.setattr("app.review.nodes.verification.verification_batches",
            batching if batched else lambda candidates: [
                {"id": item["id"], "candidates": [item]} for item in candidates])
        context = review_context(source_service, PacketModel())
        context.budget = ReviewBudget(context.limits)
        state = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
        return state["report"]

    baseline = await run(False, False)
    optimized = await run(True, True)
    for report in (baseline, optimized):
        assert report["complete"] and report["termination"] == "completed"
        assert len(report["candidates"]) == len(report["decisions"]) == 2
        assert len(report["findings"]) == 1
    assert baseline["snapshot"] == optimized["snapshot"]
    assert {item["id"] for item in baseline["candidates"]} == {item["id"] for item in optimized["candidates"]}
    assert baseline["decisions"] == optimized["decisions"]
    assert baseline["findings"] == optimized["findings"]
    assert optimized["budget"]["model_calls"] == 2 < baseline["budget"]["model_calls"] == 6
    assert optimized["budget"]["input_tokens"] < baseline["budget"]["input_tokens"]
    result = {"kind": "deterministic_offline_fixture_not_live_quality",
        "snapshot": optimized["snapshot"], "candidate_count": 2,
        "baseline": baseline["budget"], "optimized": optimized["budget"]}
    (tmp_path / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


async def test_partial_batch_retry_preserves_completed_member_and_shared_budget(pipeline):
    from tests.review.test_finding_aggregation import EquivalentReviewModel

    class PartialBatchModel(EquivalentReviewModel):
        omit: bool = True

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            for call in result.generations[0].message.tool_calls:
                if call["name"] == "BatchVerificationOutput" and self.omit:
                    call["args"]["decisions"] = call["args"]["decisions"][:1]
            return result

    service, repository, _, _, _ = pipeline
    model = PartialBatchModel()
    service._provider.agent_model = lambda *args, **kwargs: model
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    first = repository.get_task(task.id)
    completed = next(item for item in first.review_report.verification_results
                     if item.status == "completed" and item.execution_id)
    assert first.status == "completed_with_warnings"
    before = first.review_report.budget["model_calls"]
    model.omit = False
    await service.resume_review(task.id)
    assert await service._worker.run_once()
    final = repository.get_task(task.id)
    assert final.status == "completed", final.error
    assert next(item for item in final.review_report.verification_results if item.id == completed.id) == completed
    assert final.review_report.budget["model_calls"] > before
    assert final.retry_count == 1
