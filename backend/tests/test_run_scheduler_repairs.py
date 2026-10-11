import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import IssueMetrics, IssueStatus, IssueVerification
from app.services.issue_verifier import IssueVerifierService
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
from test_issue_validation import _issue, _unit, _state
from app.services.unit_worksets import build_unit_worksets
from app.models.review import (AgentAction, ChangedFile, ChangedLine, DiffLine, DiffHunk,
    ExecutionBudget, ReviewUnit, UnitReviewResponse, UnitReviewRecord)
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.model_request_budgeter import ModelRequestProfile, request_budget_reserver
from test_unit_review_summary import _state as review_state


def large_file(*, count=100, width=800):
    lines = [DiffLine(kind="added", content=str(i) + "x" * width, new_line_no=i + 1) for i in range(count)]
    return ChangedFile(file_path="a.py", change_type="modified", additions=count, deletions=0,
        hunks=[DiffHunk(old_start=1, old_length=0, new_start=1, new_length=count, lines=lines,
            added_lines=[ChangedLine(line_no=line.new_line_no, content=line.content) for line in lines])])


def review_unit():
    return ReviewUnit(id="large", primary_files=["a.py"], estimated_tokens=300000,
                      complexity="large", fingerprint="large", grouping_reason="test")


class WorksetProvider(OpenAICompatibleProvider):
    supports_review_step = False

    def __init__(self):
        super().__init__("key", "https://example.test/v1", "unknown-model",
                         request_profile=ModelRequestProfile(context_window=50000))
        self.requests = []

    async def review_unit(self, pr, files, diff, model, record_input):
        await request_budget_reserver.get()(self.unit_diagnosis_admission(pr, files, diff, model, record_input))
        self.requests.append(files)
        return UnitReviewResponse(review_record=UnitReviewRecord(change_summary="检查", target_checks=[{
            "target": target, "status": "checked", "reason": "核对完整证据",
            "evidence_ids": [item["id"] for item in record_input["evidence"]],
        } for target in record_input["targets"]]))

    async def decide(self, state, model):
        await request_budget_reserver.get()({"reserved_tokens": 1000})
        return AgentAction(action="task_done" if state["issue_round_completed"] else "report_issue", reason="检查")

    async def plan_review_unit(self, state, model):
        from app.agents.providers import LLMProviderError
        raise LLMProviderError("optional_plan_unavailable")


def runner_and_state(provider=None):
    runner = ReviewUnitExecutor(provider or WorksetProvider(), concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=64,
        max_diagnosis_attempts=32, max_token_usage=1000000, max_context_retrievals=12)
    state = review_state()
    state["changed_files"] = [large_file().model_dump(mode="json")]
    return runner, state


def test_budget_refusal_keeps_original_hunk_and_reason():
    calls = []
    def reject(*args):
        calls.append(args)
        return {"admitted": False, "kind": "budget", "can_defer": True,
                "can_split": False, "reason": "budget_tokens_insufficient"}
    worksets = build_unit_worksets(review_unit(), [large_file(count=4, width=2)], reject)
    assert len(calls) == len(worksets) == 1
    assert worksets[0][2]["reason"] == "budget_tokens_insufficient"
    assert len(worksets[0][1][0].hunks[0].lines) == 4


async def test_initially_admitted_batch_blocked_by_holdback_is_drained():
    runner, state = runner_and_state()
    original = runner._workset_holdback
    count = 0
    def holdback(budget, future, current, growth):
        nonlocal count
        count += 1
        if count == 1:
            return {"model_calls": budget.max_model_calls, "diagnosis_attempts": 0,
                    "token_usage": 0, "workset_ids": []}
        return original(budget, future, current, growth)
    runner._workset_holdback = holdback
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_batches[0].status == "completed", result.error
    assert all(item.status == "completed" for item in result.diff_batches)
    assert len(runner.provider.requests) == len(result.diff_batches)


async def test_parallel_units_recheck_budget_after_other_attempt_settles():
    import asyncio
    from app.services.model_request_budgeter import ModelRequestProfile
    from app.models.review import ChangedFile
    budget = ReviewRunBudget("run", max_tokens=25000, max_calls=5)
    entered = asyncio.Event()
    class Provider(WorksetProvider):
        supports_review_step = True
        async def review_step(self, pr, files, diff, model, record_input):
            self.requests.append(files)
            if files[0].file_path == "a.py":
                await self._request_json_content("x" * 20000, "unknown-model", "review_step", "s", 10)
            else:
                await self._request_json_content("x" * 6000, "unknown-model", "review_step", "s", 10)
            return UnitReviewResponse(review_record=UnitReviewRecord(change_summary="检查", target_checks=[{
                "target": target, "status": "checked", "reason": "完整证据",
                "evidence_ids": [item["id"] for item in record_input["evidence"]],
            } for target in record_input["targets"]]))
        def unit_review_step_admission(self, *args):
            return {"reserved_tokens": 6000, "estimated_input_tokens": 5900}
    provider = Provider()
    provider._request_profile = ModelRequestProfile(context_window=100000)
    class Chat:
        async def ainvoke(self, messages):
            if len(messages[-1].content) > 10000:
                entered.set()
                await asyncio.sleep(0.05)
            return AIMessage(content="{}", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    provider._build_chat_model = lambda *args: Chat()
    runner, state = runner_and_state(provider)
    runner.concurrency = 2
    file = ChangedFile(file_path="a.py", change_type="modified", additions=1, deletions=0,
                       hunks=large_file(count=1, width=1).hunks)
    state["changed_files"] = [file.model_dump(mode="json"), file.model_copy(update={"file_path": "b.py"}).model_dump(mode="json")]
    state["file_index"] = [{"path": "a.py"}, {"path": "b.py"}]
    units = [review_unit().model_copy(update={"id": path, "primary_files": [path], "fingerprint": path})
             for path in ["a.py", "b.py"]]
    execute_unit = runner.execute_unit
    async def gated(unit, state):
        if unit.id == "b.py":
            await asyncio.wait_for(entered.wait(), 5)
        return await execute_unit(unit, state)
    runner.execute_unit = gated
    with activate_run_budget(budget):
        results = await runner.execute(units, state)
    assert entered.is_set()
    assert budget.calls_consumed == 2
    assert all(result.error is None for result in results), [result.error for result in results]


@pytest.mark.parametrize("release", [True, False])
async def test_verifier_defers_without_consuming_slot_then_rechecks(release):
    budget = ReviewRunBudget("run", max_tokens=1000, max_calls=4)
    class Provider(OpenAICompatibleProvider):
        async def verify_issue(self, request, model):
            prompt = "x" * (70 if request.issue.id == "large" else 1)
            await self._request_json_content(prompt, "fixture", "verify_issue", "s", 5)
            if release and request.issue.id == "small":
                await budget.release_obligation(obligation.obligation_id)
            return IssueVerification(issue_id=request.issue.id, decision="keep", reason="证据充分")
    provider = Provider("key", "https://example.test/v1", "fixture", request_attempts=1)
    small = provider._prepare_json_request("x", "fixture", "verify_issue", "s", 5)
    required = small.estimate(provider.resolve_request_profile("fixture").profile)["reserved_tokens"]
    obligation = await budget.create_obligation(owner_task_id="other", token_budget=1000 - required, reason="pending")
    sent = []
    class Chat:
        async def ainvoke(self, messages):
            sent.append(messages)
            return AIMessage(content="{}", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    provider._build_chat_model = lambda *args: Chat()
    verifier = IssueVerifierService(provider, enabled=True, fail_mode="unresolved", max_calls_per_unit=1)
    unit = _unit()
    with activate_run_budget(budget):
        result = await verifier.verify_issues([_issue("large"), _issue("small")], [unit], _state(unit), IssueMetrics())
    assert result.issues[1].status == IssueStatus.confirmed, result.warnings
    assert result.metrics.verifier_call_count == len(sent) == (2 if release else 1)
    assert result.metrics.verifier_token_count == 2 * len(sent)
    if release:
        assert result.issues[0].status == IssueStatus.confirmed
    else:
        assert result.issues[0].status == IssueStatus.unresolved
        assert "deferred_budget_not_executed" in result.issues[0].unresolved_reason
