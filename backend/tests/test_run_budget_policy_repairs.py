import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.core.config import Settings
from app.models.review import ExecutionBudget, ReviewTask
from app.services.review_run_budget import ReviewRunBudget, ReviewRepositoryRunBudgetStore, activate_run_budget
from test_phase6a_persistence import persistence as persistence


@pytest.mark.parametrize("field", ["repoguardian_run_max_tokens", "repoguardian_run_max_calls",
                                   "repoguardian_run_max_cost_microusd"])
def test_run_policy_rejects_negative_limit(field):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: -1})


async def test_effective_policy_is_saved_and_resume_does_not_replace_it(persistence):
    repository, _, _ = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1"))
    store = ReviewRepositoryRunBudgetStore(repository)
    policy = Settings(_env_file=None, repoguardian_run_max_tokens=456789,
        repoguardian_run_max_calls=45, repoguardian_run_max_cost_microusd=1234)
    budget = await ReviewRunBudget.load_or_create("run", store=store, **policy.run_budget_limits())
    assert store.load("run")["limits"] == policy.run_budget_limits()
    restored = await ReviewRunBudget.load_or_create("run", store=store, recover_running=True,
                                                   **Settings(_env_file=None).run_budget_limits())
    assert restored.snapshot() == budget.snapshot()


async def test_default_run_can_send_more_than_old_unit_call_cap():
    policy = Settings(_env_file=None)
    budget = await ReviewRunBudget.load_or_create("run", **policy.run_budget_limits())
    assert budget.max_tokens > ExecutionBudget().max_token_usage
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1)
    class Chat:
        async def ainvoke(self, messages):
            return AIMessage(content="{}", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    provider._build_chat_model = lambda *args: Chat()
    with activate_run_budget(budget):
        for _ in range(ExecutionBudget().max_model_calls + 2):
            await provider._request_json_content("input", "fixture", "review_step", "s", 10)
    assert budget.calls_consumed == 8
