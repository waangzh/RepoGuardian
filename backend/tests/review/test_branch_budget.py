import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text

from app.review.agents import ReviewGuardMiddleware
from app.review.budget import ReviewBudget, ReviewBudgetExceeded
from app.review.context import AgentContext
from app.review.configuration import discovery_limits
from app.review.models import ReviewLimits, VerificationOutput
from app.review.tools import REVIEW_TOOLS
from app.services.review.review_repository import ReviewRepository
from tests.review.test_native_agents import ToolCallingModel
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]


async def test_branch_input_and_call_reservations_survive_settlement():
    budget = ReviewBudget(ReviewLimits(total_input_tokens=1000))
    first = await budget.reserve("a", 300, hold=2)
    await budget.settle(first, {"input_tokens": 100, "output_tokens": 10})
    summary = await budget.summary()
    assert summary["held_input_tokens"] == 600 and summary["held_model_calls"] == 2
    with pytest.raises(ReviewBudgetExceeded, match="input_tokens"):
        await budget.reserve("b", 900, hold=2)
    final = await budget.reserve("a", 900, stage="finalize", hold=1)
    await budget.settle(final, {"input_tokens": 300, "output_tokens": 10})
    correction = await budget.reserve("a", 900, stage="finalize", hold=1)
    await budget.settle(correction, {"input_tokens": 300, "output_tokens": 10})
    summary = await budget.summary()
    assert summary["input_tokens"] == 700 and summary["held_input_tokens"] == 0
    assert summary["held_model_calls"] == 0
    with pytest.raises(ReviewBudgetExceeded, match="finalization_attempts"):
        await budget.reserve("a", 1, stage="finalize")


async def test_other_branches_cannot_consume_protected_model_calls():
    budget = ReviewBudget(ReviewLimits(total_model_calls=4))
    first = await budget.reserve("a", 10, hold=2)
    await budget.settle(first, {"input_tokens": 10, "output_tokens": 1})
    await budget.reserve("b", 10)
    with pytest.raises(ReviewBudgetExceeded, match="model_calls"):
        await budget.reserve("b", 10)
    final = await budget.reserve("a", 10, stage="finalize", hold=1)
    await budget.settle(final, {"input_tokens": 10, "output_tokens": 1})
    await budget.reserve("a", 10, stage="finalize", hold=1)
    assert (await budget.summary())["model_calls"] == 4


async def test_finish_releases_only_unused_holds_not_unknown_usage():
    budget = ReviewBudget(ReviewLimits(total_input_tokens=1000))
    ticket = await budget.reserve("a", 100, hold=2)
    await budget.settle(ticket, None)
    assert (await budget.summary())["committed_input_tokens"] == 300
    await budget.finish("a")
    await budget.finish("a")
    summary = await budget.summary()
    assert summary["committed_input_tokens"] == 100 and summary["unknown_calls"] == 1
    assert summary["held_model_calls"] == 0


async def test_discovery_stage_counts_unknown_calls_and_all_branch_holds():
    budget = ReviewBudget(ReviewLimits(total_model_calls=8))
    scope = {"discovery_agent_ids": frozenset({"a", "b"})}
    ticket = await budget.reserve("a", 1, hold=2, **scope)
    await budget.settle(ticket, None)
    with pytest.raises(ReviewBudgetExceeded, match="model_calls"):
        await budget.reserve("b", 1, hold=2, **scope)
    await budget.finish("a")
    await budget.reserve("b", 1, hold=2, **scope)
    assert (await budget.summary())["unknown_calls"] == 2


async def test_verification_usage_does_not_consume_discovery_quota_on_retry():
    budget = ReviewBudget(ReviewLimits(total_model_calls=8, total_input_tokens=10_000))
    scope = {"discovery_agent_ids": frozenset({"a"})}
    await budget.reserve("a", 1000, **scope)
    for _ in range(3):
        await budget.reserve("verifier", 1000)
    await budget.reserve("a", 1000, **scope)
    assert (await budget.summary())["model_calls"] == 5


async def test_discovery_input_limit_preserves_unknown_reservations():
    budget = ReviewBudget(ReviewLimits(total_input_tokens=10_000))
    scope = {"discovery_agent_ids": frozenset({"a", "b"})}
    ticket = await budget.reserve("a", 2000, hold=2, **scope)
    await budget.settle(ticket, None)
    with pytest.raises(ReviewBudgetExceeded, match="input_tokens"):
        await budget.reserve("b", 500, hold=2, **scope)
    await budget.finish("a")
    with pytest.raises(ReviewBudgetExceeded, match="input_tokens"):
        await budget.reserve("b", 4501, **scope)
    await budget.reserve("verifier", 6000)
    summary = await budget.summary()
    assert summary["committed_input_tokens"] == 8000 and summary["unknown_calls"] == 2


async def test_discovery_budget_rejects_an_unassigned_branch():
    budget = ReviewBudget(ReviewLimits())
    with pytest.raises(ValueError, match="invalid_discovery_budget_scope"):
        await budget.reserve("other", 1, discovery_agent_ids={"a"})
    assert (await budget.summary())["model_calls"] == 0


class CorrectingModel(ToolCallingModel):
    budget: Any = None
    calls: int = 0
    tools_seen: list[list[str]] = []

    def bind_tools(self, tools, **kwargs):
        self.tools_seen.append([tool.name if hasattr(tool, "name") else tool["function"]["name"]
                                for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        tokens = list(self.budget.rows.values())[-1]["reserved_input_bytes"] if self.budget else 100
        args = {"decision": "refuted", "explanation": "schema correction fixture",
                "attribution": "uncertain", "limitations": ["missing"] if self.calls == 1 else []}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
            tool_calls=[{"name": "VerificationOutput", "id": f"final-{self.calls}", "args": args}],
            usage_metadata={"input_tokens": tokens, "output_tokens": 10, "total_tokens": tokens + 10}))])


async def test_actual_schema_correction_can_spend_the_last_reserved_input():
    async def run(limit):
        limits = ReviewLimits(total_input_tokens=limit)
        budget = ReviewBudget(limits)
        model = CorrectingModel(budget=budget)
        agent = create_agent(model, tools=[], response_format=ToolStrategy(VerificationOutput),
            middleware=[ReviewGuardMiddleware(limits)], context_schema=AgentContext)
        result = await agent.ainvoke(
            {"messages": [("user", "Review fixture")], "review_finalizing": True},
            context=AgentContext(evidence=None, budget=budget, budget_agent_id="a"))
        assert result["structured_response"].decision == "refuted"
        assert model.calls == 2 and result["review_final_requests"] == 2
        return await budget.summary()

    baseline = await run(500_000)
    bounded = await run(baseline["input_tokens"] + 50)
    assert bounded["model_calls"] == 2 and bounded["held_input_tokens"] == 0


@pytest.mark.parametrize("with_budget", [False, True])
async def test_finalizing_survives_checkpoint_and_agent_rebuild(with_budget):
    class PressureBudget(ReviewBudget):
        rejected = False

        async def reserve(self, agent_id, encoded_bytes, *, stage="explore", hold=0):
            if stage == "explore" and not self.rejected:
                self.rejected = True
                raise ReviewBudgetExceeded("input_tokens")
            return await super().reserve(agent_id, encoded_bytes, stage=stage, hold=hold)

    saver, limits, model = InMemorySaver(), ReviewLimits(), CorrectingModel()
    budget = PressureBudget(limits) if with_budget else None
    context = AgentContext(evidence=None, budget=budget, budget_agent_id="a")
    config = {"configurable": {"thread_id": "review"}}

    def build(interrupt):
        return create_agent(model, tools=REVIEW_TOOLS, response_format=ToolStrategy(VerificationOutput),
            middleware=[ReviewGuardMiddleware(limits)], context_schema=AgentContext, checkpointer=saver,
            interrupt_after=["model"] if interrupt else None)

    first = build(True)
    await first.ainvoke({"messages": [("user", "Frozen fixture")],
                         "review_finalizing": not with_budget}, context=context, config=config)
    state = await first.aget_state(config)
    assert state.values["review_finalizing"] and state.values["review_final_requests"] == 1
    result = await build(False).ainvoke(None, context=context, config=config)
    assert result["structured_response"].decision == "refuted"
    assert model.tools_seen == [["VerificationOutput"], ["VerificationOutput"]]


async def test_durable_branch_holds_and_final_phase_survive_reopen(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    limits = task.configuration.limits
    amount = limits.total_input_tokens // 3
    ticket = repository.reserve_model_call(task.id, "a", amount, limits, hold=2)
    repository.settle_model_call(task.id, ticket, {"input_tokens": amount // 3, "output_tokens": 1})
    reopened = ReviewRepository(repository._session_factory, repository._artifacts, require_migration=False)
    assert reopened.model_branch_budget(task.id, "a")["held_input_tokens"] == amount * 2
    with pytest.raises(ReviewBudgetExceeded):
        reopened.reserve_model_call(task.id, "b", limits.total_input_tokens * 4 // 5, limits, hold=2)
    final = reopened.reserve_model_call(task.id, "a", amount, limits, stage="finalize", hold=1)
    reopened.settle_model_call(task.id, final, None)
    again = ReviewRepository(repository._session_factory, repository._artifacts, require_migration=False)
    branch = again.model_branch_budget(task.id, "a")
    assert branch["finalizing"] and branch["final_requests"] == 1 and branch["held_model_calls"] == 1
    with pytest.raises(ValueError, match="already_finalizing"):
        again.reserve_model_call(task.id, "a", 1, limits, hold=2)
    again.release_branch_budget(task.id, "a")
    summary = again.model_budget_summary(task.id, limits)
    assert summary["unknown_calls"] == 1 and summary["held_input_tokens"] == 0


async def test_concurrent_branches_atomically_claim_future_submission_budget(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    limits = task.configuration.limits

    def reserve(agent):
        try:
            return repository.reserve_model_call(task.id, agent,
                limits.total_input_tokens // 3, limits, hold=2)
        except ReviewBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(reserve, ("a", "b")))
    assert sum(ticket is not None for ticket in tickets) == 1
    summary = repository.model_budget_summary(task.id, limits)
    assert summary["model_calls"] == 1 and summary["held_model_calls"] == 2
    assert summary["committed_input_tokens"] <= limits.total_input_tokens


async def test_discovery_stage_limits_are_atomic_and_survive_database_reopen(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    limits = task.configuration.limits
    scope = {"discovery_agent_ids": frozenset({"a", "b"})}
    amount = limits.total_input_tokens // 8

    def reserve(agent):
        try:
            return repository.reserve_model_call(task.id, agent, amount, limits, hold=2, **scope)
        except ReviewBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(reserve, ("a", "b")))
    assert sum(ticket is not None for ticket in tickets) == 1
    winner = "a" if tickets[0] else "b"
    loser = "b" if winner == "a" else "a"
    reopened = ReviewRepository(repository._session_factory, repository._artifacts, require_migration=False)
    with pytest.raises(ReviewBudgetExceeded, match="input_tokens"):
        reopened.reserve_model_call(task.id, loser, amount, limits, hold=2, **scope)
    reopened.release_branch_budget(task.id, winner)
    reopened.reserve_model_call(task.id, "verifier", limits.total_input_tokens // 4, limits)
    reopened.reserve_model_call(task.id, loser, amount, limits, hold=2, **scope)
    assert reopened.model_budget_summary(task.id, limits)["unknown_calls"] == 3


async def test_discovery_model_call_limit_survives_database_reopen(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    limits = task.configuration.limits
    scope = {"discovery_agent_ids": frozenset({"a", "b"})}
    capacity = discovery_limits(limits, task.configuration.options.depth).total_model_calls
    for index in range(capacity):
        repository.reserve_model_call(task.id, "a" if index % 2 else "b", 1, limits, **scope)
    reopened = ReviewRepository(repository._session_factory, repository._artifacts, require_migration=False)
    with pytest.raises(ReviewBudgetExceeded, match="model_calls"):
        reopened.reserve_model_call(task.id, "a", 1, limits, **scope)
    reopened.reserve_model_call(task.id, "verifier", 1, limits)
    assert reopened.model_budget_summary(task.id, limits)["model_calls"] == capacity + 1


def test_populated_budget_migration_preserves_usage_and_outstanding_holds(tmp_path, monkeypatch):
    monkeypatch.setattr("logging.config.fileConfig", lambda *args, **kwargs: None)
    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.sqlite').as_posix()}")
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "20261028_0004")
        from app.models.orm import ReviewTaskOrm, utcnow
        connection.execute(ReviewTaskOrm.__table__.insert().values(
            id="review", pr_url="https://github.com/local/repo/pull/1", mode="review",
            status="failed", current_phase="failed", provider="fixture", warnings=[],
            task_snapshot={"sentinel": "keep"}, thread_id="review", retention_until=utcnow()))
        connection.execute(text("""
            INSERT INTO review_model_calls
                (id, task_id, agent_id, reserved_input_bytes, held_input_tokens, input_tokens,
                 output_tokens, cached_input_tokens, settled, created_at)
            VALUES ('known', 'review', 'a', 100, 200, 25, 10, 20, 1, CURRENT_TIMESTAMP),
                   ('pending', 'review', 'a', 400, 800, NULL, NULL, NULL, 0, CURRENT_TIMESTAMP)
        """))
        command.upgrade(config, "head")
        assert connection.execute(text(
            "SELECT input_tokens, cached_input_tokens FROM review_model_calls WHERE id='known'"
        )).one() == (25, 20)
        branch = connection.execute(text(
            "SELECT held_input_tokens, held_model_calls, finalizing FROM review_branch_budgets"
        )).one()
        assert branch == (800, 2, 1)
        command.downgrade(config, "20261028_0004")
        assert connection.scalar(text(
            "SELECT held_input_tokens FROM review_model_calls WHERE id='pending'")) == 800
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT held_input_tokens FROM review_branch_budgets")) == 800
        assert json.loads(connection.scalar(text(
            "SELECT task_snapshot FROM review_tasks WHERE id='review'"))) == {"sentinel": "keep"}
    engine.dispose()
