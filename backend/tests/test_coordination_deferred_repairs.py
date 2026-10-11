import asyncio

from app.agents.providers import LLMProviderError
from app.models.review import ReviewUnitResult, UnitBudgetRejection, IssueMetrics
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget, current_run_budget
from test_coordination_reaudit import verifier_and_chat
from test_cross_unit_coordination import Provider, state, plan
from test_issue_validation import _issue, _unit, _state


async def test_permanent_unsent_budget_rejection_is_not_charged():
    from app.services.issue_verifier import IssueVerifierService

    class Provider:
        async def verify_issue(self, *args):
            error = LLMProviderError("budget permanently exhausted")
            error.admission_rejection = {"kind": "budget", "can_defer": False}
            raise error

    service = IssueVerifierService(Provider(), enabled=True, fail_mode="unresolved", max_calls_per_unit=5)
    result = await service.verify_issues([_issue("i")], [_unit()], _state(_unit()), IssueMetrics())
    assert result.issues[0].status == "unresolved"
    assert result.metrics.verifier_call_count == result.metrics.verifier_token_count == 0


async def test_legacy_verifier_holdback_rejection_is_unsent_and_retryable():
    verifier, wrapper, sent = verifier_and_chat()
    wrapper.holdback = {"followup_id": "f1", "phase": "verification", "token_usage": 100000}
    rejected = await verifier.verify_issues([_issue("i")], [_unit()], _state(_unit()), IssueMetrics())
    assert rejected.issues[0].status == "unresolved"
    assert "deferred_budget_not_executed:" in rejected.issues[0].unresolved_reason
    assert rejected.metrics.verifier_call_count == len(sent) == 0
    wrapper.holdback = {"followup_id": "f1", "phase": "verification"}
    retried = await verifier.verify_issues([_issue("i")], [_unit()], _state(_unit()), IssueMetrics())
    assert retried.issues[0].status == "confirmed"
    assert len(sent) == wrapper.budget.model_calls == 1


async def test_catalog_waits_for_inflight_release_and_retries_same_request():
    rejected = asyncio.Event()
    budget = ReviewRunBudget("task", max_tokens=100, max_calls=100)
    reserved = await budget.reserve(request_id="other", operation="review_step", estimated_tokens=100)

    class BudgetProvider(Provider):
        supports_request_admission = True

        async def coordinate_cross_units(self, payload, model):
            if current_run_budget().available()["tokens"] < 10:
                rejected.set()
                error = LLMProviderError("run_budget_admission_rejected")
                error.admission_rejection = {"kind": "budget", "reason": str(error),
                    "can_defer": True, "budget_revision": budget.revision}
                raise error
            return await super().coordinate_cross_units(payload, model)

    async def release():
        await rejected.wait()
        await budget.release_unsent(reserved.attempt_id)

    task = asyncio.create_task(release())
    provider = BudgetProvider(candidate=False)
    with activate_run_budget(budget):
        service = CrossUnitCoordinationService(provider)
        result = await asyncio.wait_for(service.run(state()), 5)
    await task
    assert provider.calls.count("coordinate") == 1, result["coordination_plan"]["reason"]
    assert all(not row.get("deferred_budget") for row in service.runtime.data["catalog_batches"].values())
    assert not any(row["status"] == "deferred_budget" for row in service.runtime.data["calls"].values())


async def test_followup_budget_rejection_is_retried_after_next_followup(monkeypatch):
    value = state()
    attempts = []

    class TwoFollowups(Provider):
        async def coordinate_cross_units(self, payload, model):
            result = plan(payload)
            return result.model_copy(update={"followups": [result.followups[0],
                result.followups[0].model_copy(update={"id": "f2", "question": "另一个独立契约问题"})]})

    async def execute(executor, unit, local):
        identity = local["cross_unit_followup"]["id"]
        attempts.append(identity)
        if attempts == ["f1"]:
            return ReviewUnitResult(review_unit_id=unit.id, status="failed",
                budget_rejection=UnitBudgetRejection(kind="budget", reason="unit_request_budget_exhausted",
                    can_defer=True, budget_revision=0), error="unit_request_budget_exhausted")
        return ReviewUnitResult(review_unit_id=unit.id, status="completed", terminal_reason="no_issue")

    monkeypatch.setattr("app.services.cross_unit_coordination.ReviewUnitExecutor.execute_unit", execute)
    service = CrossUnitCoordinationService(TwoFollowups(candidate=False))
    result = await service.run(value)
    assert attempts == ["f1", "f2", "f1"]
    assert not service.runtime.data["deferred_followups"]
    assert len(result["followup_results"]) == 2


async def test_pending_verification_is_rechecked_without_repeating_policy(monkeypatch, tmp_path):
    policy_calls = []
    from app.graph.nodes.issue_validation import issue_policy_node

    async def policy(batch):
        policy_calls.append(1)
        return await issue_policy_node(batch)

    async def defer(batch):
        issue = dict(batch["review_issues"][0])
        issue.update(status="unresolved", unresolved_reason="deferred_budget_not_executed: holdback")
        return {"review_issues": [issue]}

    monkeypatch.setattr("app.graph.nodes.issue_validation.issue_policy_node", policy)
    monkeypatch.setattr("app.graph.nodes.issue_validation.issue_verifier_node", defer)
    provider = Provider()
    service = CrossUnitCoordinationService(provider)
    result = await service.run(state(tmp_path))
    assert policy_calls == [1]
    assert provider.calls.count("verify") == 1, result["coordination_plan"]["reason"]
    assert result["review_issues"][0]["status"] == "confirmed"
    assert result["issue_metrics"]["candidate_issue_count"] == 1
    assert result["issue_metrics"]["verifier_call_count"] == 1
    assert not any(batch.get("pending_verifications") for batch in service.runtime.data["batches"].values())
