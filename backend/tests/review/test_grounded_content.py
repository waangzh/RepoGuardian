"""Fixed source fixtures test contracts and recovery, never live model quality."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import httpx
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from openai import RateLimitError

from app.review.budget import ReviewBudget
from app.review.context import ReviewContext
from app.review.evidence import EvidenceArchive
from app.review.models import VerificationOutput
from app.review.observations import collect_observations, validated_observations
from app.review.synthesis import single_request_model, synthesize, synthesis_materials, validate_synthesis
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_presentation import selected_presentation
from tests.review.test_native_graph import ReviewModel
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]

COMMENT = {
    "trigger": "调用方读取 VALUE 时",
    "cause": "HEAD 将返回值由 1 改为 2",
    "impact": "仅依赖原值的调用方收到不符合契约的结果",
    "recommendation": "请恢复原值，或同步调整依赖原值的调用契约。",
    "body": "调用方依赖 VALUE 为 1 时，HEAD 将其改为 2，因此该调用方会收到不符合契约的结果。"
            "\n\n请恢复原值，或同步调整依赖原值的调用契约。",
}


class GroundedModel(ReviewModel):
    no_findings: bool = False
    synthesis_calls: int = 0
    synthesis_mode: str = "ready"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(item.content for item in messages if item.type == "human"))
        if "facts" in payload:
            self.synthesis_calls += 1
            if self.synthesis_mode == "timeout":
                raise TimeoutError("fixture")
            facts = payload["facts"]
            observation = next(key for key, item in facts.items() if item["kind"] == "observation")
            review = next((key for key, item in facts.items() if item["kind"] == "finding"),
                          "status:conclusion")

            def paragraph(key):
                return {"source_ids": [key], "text": facts[key]["text"]}

            args = {"change_overview": [paragraph(observation)],
                    "review_overview": [paragraph(review)],
                    "actions": [paragraph("status:advice")],
                    "disclosed_status_ids": payload["required_status_ids"]}
            if self.synthesis_mode == "invented":
                args["review_overview"][0]["source_ids"] = ["finding:excluded"]
            if self.synthesis_mode == "expanded":
                args["change_overview"][0]["text"] = "所有功能均正确，可以安全上线。"
            if self.synthesis_mode == "invalid":
                args = {"change_overview": "invalid"}
            if self.synthesis_mode == "missing_status":
                args["disclosed_status_ids"] = []
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="", tool_calls=[{"name": "SynthesisOutput", "id": "synthesis", "args": args}],
                usage_metadata={"input_tokens": 31, "output_tokens": 17, "total_tokens": 48,
                                "input_token_details": {"cache_read": 9}}))])
        result = super()._generate(messages, stop, run_manager, **kwargs)
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                identities = call["args"]["findings"][0]["evidence_ids"]
                call["args"]["observations"] = [{
                    "change": "VALUE 的返回常量由 1 调整为 2。",
                    "before": "读取 VALUE 得到 1。", "after": "读取 VALUE 得到 2。",
                    "impact": "直接读取该常量的调用方。", "kind": "implementation",
                    "evidence_ids": identities,
                }]
                if self.no_findings:
                    call["args"]["findings"] = []
            elif call["name"] in {"VerificationOutput", "BatchVerificationOutput"}:
                for decision in call["args"].get("decisions", [call["args"]]):
                    decision["author_comment"] = COMMENT
        return result


async def run_grounded(pipeline, **options):
    service, repository, _, _, _ = pipeline
    model = GroundedModel(**options)
    service._provider.agent_model = lambda *args, **kwargs: model
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    task = repository.get_task(task.id)
    assert task.status == "completed", task.error
    return task, PublicationPlanner(repository._artifacts).build(task), model


@pytest.mark.parametrize("no_findings", [False, True])
async def test_saved_content_shared_by_overview_comments_report_and_reopen(pipeline, no_findings):
    task, plan, model = await run_grounded(pipeline, no_findings=no_findings)
    assert task.review_report.complete and task.review_report.termination == "completed"
    synthesis = task.review_report.synthesis
    assert synthesis.status == "ready", synthesis
    assert synthesis.model_calls == 1 and synthesis.input_tokens == 31
    assert synthesis.cached_input_tokens == 9 and model.synthesis_calls == 1
    assert plan.presentation.synthesis_status == "ready"
    assert plan.presentation.summary_source == "model"
    assert "返回常量由 1 调整为 2" in plan.presentation.change_summary
    assert "返回常量由 1 调整为 2" in plan.review_comment_markdown
    assert "返回常量由 1 调整为 2" in task.report_markdown
    if no_findings:
        assert not plan.presentation.findings
        assert plan.presentation.review_summary == "已检查范围内未发现已确认问题。"
    else:
        draft = plan.inline_drafts[0]
        assert COMMENT["body"].split("\n\n")[0] in draft.body_markdown
        assert "代码证据（核验依据）" not in draft.body_markdown
        assert task.review_report.findings[0].verification.explanation in task.report_markdown
        assert plan.digest.findings[0].author_body == COMMENT["body"]
    assert task.review_report.budget["model_calls"] == 3 + 2 * (not no_findings)
    reopened = pipeline[1].get_task(task.id)
    assert reopened.review_report.synthesis == synthesis
    assert PublicationPlanner(pipeline[1]._artifacts).build(reopened).presentation == plan.presentation
    selected = selected_presentation(plan, [])
    assert selected.change_summary == plan.presentation.change_summary
    assert selected.review_summary == plan.presentation.review_summary
    assert selected.delivery.selected == 0
    assert selected.delivery.confirmed == (not no_findings)
    assert model.synthesis_calls == 1


@pytest.mark.parametrize("mode,error", [
    ("invented", "synthesis_source_invalid"), ("timeout", "synthesis_timeout"),
    ("invalid", "ValidationError"), ("missing_status", "synthesis_status_disclosure_missing"),
])
async def test_synthesis_failure_is_expression_only_and_observations_remain(pipeline, mode, error):
    task, plan, model = await run_grounded(pipeline, synthesis_mode=mode)
    assert task.review_report.synthesis.status == "fallback"
    assert task.review_report.synthesis.error == error
    assert task.review_report.complete and len(task.review_report.findings) == 1
    assert plan.presentation.summary_source == "observations"
    assert "变更总览未生成" in plan.review_comment_markdown
    assert model.synthesis_calls == 1
    if mode == "timeout":
        assert task.review_report.budget["unknown_calls"] == 1


async def test_expanded_claim_reverts_to_exact_saved_fact(pipeline):
    task, plan, _ = await run_grounded(pipeline, synthesis_mode="expanded")
    assert task.review_report.synthesis.status == "ready"
    assert task.review_report.synthesis.prose_fallbacks == 1
    assert "可以安全上线" not in plan.review_comment_markdown
    assert "返回常量由 1 调整为 2" in plan.review_comment_markdown


async def test_observation_evidence_scope_and_snapshot_are_revalidated(pipeline):
    task, plan, _ = await run_grounded(pipeline)
    report = task.review_report
    archive = EvidenceArchive(report.snapshot, pipeline[1]._artifacts)
    assignments = [item.model_dump() for item in report.assignments]
    units = [item.model_dump() for item in report.review_units]
    results = [item.model_dump() for item in report.discovery_results]
    results[0]["output"]["observations"][0]["evidence_ids"] = ["f" * 64]
    accepted, errors = collect_observations(results, assignments, units, archive)
    assert not accepted and errors
    results = [item.model_dump() for item in report.discovery_results]
    assert not collect_observations(results, [{**assignments[0], "reviewer": "contract"}], units, archive)[0]
    # A saved observation is also checked against its original branch output.
    report.observations[0].change = "伪造变化"
    assert not validated_observations(report, archive)
    rebuilt = PublicationPlanner(pipeline[1]._artifacts).build(task)
    assert rebuilt.presentation.summary_source == "metadata"
    assert rebuilt.presentation.synthesis_status == "fallback"
    assert "伪造变化" not in rebuilt.review_comment_markdown
    assert "变更范围：" in rebuilt.review_comment_markdown
    assert plan.outcome == rebuilt.outcome


async def test_replay_reuses_saved_version_and_unknown_request_never_repeats(pipeline):
    task, plan, model = await run_grounded(pipeline)
    report = task.review_report
    version, material = synthesis_materials(report, plan, report.observations)

    async def facts(_report):
        return version, material

    context = ReviewContext(load=None, model=model, limits=task.configuration.limits, synthesis_facts=facts)
    context.budget = ReviewBudget(context.limits)
    runtime = SimpleNamespace(context=context)
    state = {"report": report.model_dump(), "synthesis": report.synthesis.model_dump()}
    result = await synthesize(state, runtime)
    assert result["report"]["synthesis"] == state["synthesis"]
    assert model.synthesis_calls == 1
    await context.budget.reserve("synthesis:" + version, 100, stage="finalize")
    result = await synthesize({"report": report.model_dump()}, runtime)
    assert result["report"]["synthesis"]["error"] == "synthesis_interrupted_unknown"
    assert result["report"]["synthesis"]["model_calls"] == 1
    assert model.synthesis_calls == 1
    persistent = ReviewBudget(context.limits, repository=pipeline[1], task_id=task.id)
    assert (await persistent.branch_state("synthesis:" + version))["final_requests"] == 1
    context.budget = persistent
    recovered = await synthesize({"report": report.model_dump()}, runtime)
    assert recovered["report"]["synthesis"]["error"] == "synthesis_interrupted_unknown"
    assert model.synthesis_calls == 1

    class CancelledModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise asyncio.CancelledError()

    context.model, context.budget = CancelledModel(), ReviewBudget(context.limits)
    with pytest.raises(asyncio.CancelledError):
        await synthesize({"report": report.model_dump()}, runtime)
    assert (await context.budget.summary())["unknown_calls"] == 1


async def test_budget_denial_does_not_issue_request_or_change_source_result(pipeline):
    task, plan, model = await run_grounded(pipeline)
    version, material = synthesis_materials(task.review_report, plan, task.review_report.observations)

    async def facts(_report):
        return version, material

    limits = task.configuration.limits.model_copy(update={"total_model_calls": 1})
    context = ReviewContext(load=None, model=model, limits=limits, synthesis_facts=facts)
    context.budget = ReviewBudget(limits)
    ticket = await context.budget.reserve("discovery", 100)
    await context.budget.settle(ticket, {"input_tokens": 10, "output_tokens": 5})
    result = await synthesize({"report": task.review_report.model_dump()}, SimpleNamespace(context=context))
    assert result["report"]["synthesis"]["error"] == "synthesis_budget_exhausted"
    assert result["report"]["complete"]
    assert model.synthesis_calls == 1
    material["facts"] = {key: value for key, value in material["facts"].items()
                         if value["kind"] != "observation"}
    result = await synthesize({"report": task.review_report.model_dump()}, SimpleNamespace(context=context))
    assert result["report"]["synthesis"]["error"] == "synthesis_observations_missing"
    assert model.synthesis_calls == 1

    async def unavailable(_report):
        raise OSError("fixture material read error")

    context.synthesis_facts = unavailable
    result = await synthesize({"report": task.review_report.model_dump()}, SimpleNamespace(context=context))
    assert result["report"]["synthesis"]["error"] == "synthesis_material_preparation_failed"
    assert result["report"]["complete"]


@pytest.mark.parametrize("body", ["", "<script>bad</script>", {"invalid": True}, "x" * 3001])
def test_invalid_comment_body_falls_back_locally_without_losing_decision(body):
    output = VerificationOutput(decision="confirmed", explanation="仅此输入发生故障。",
        attribution="introduced", author_comment={**COMMENT, "body": body})
    assert output.decision == "confirmed"
    assert output.author_comment.body is None
    assert output.author_comment.impact == COMMENT["impact"]


async def test_bounded_material_omission_and_invalid_references(pipeline):
    task, plan, _ = await run_grounded(pipeline)
    observations = [task.review_report.observations[0].model_copy(
        update={"id": str(index), "change": "界" * 600}) for index in range(30)]
    _, material = synthesis_materials(task.review_report, plan, observations)
    assert len(json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode()) <= 24000
    assert material["omitted"]["observations"] > 0
    output = task.review_report.synthesis.output.model_dump()
    output["change_overview"][0]["source_ids"] = ["finding:excluded"]
    with pytest.raises(ValueError, match="synthesis_source_invalid"):
        validate_synthesis(output, material)


async def test_synthesis_disables_sdk_retries_without_changing_review_client():
    attempts = []

    def reject(request):
        attempts.append(request)
        return httpx.Response(429, json={"error": {"message": "fixture", "type": "rate_limit"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        original = ChatOpenAI(model="fixture", api_key="fixture", base_url="https://example.test/v1",
                              max_retries=2, http_async_client=client)
        single = single_request_model(original)
        assert original.max_retries == original.root_async_client.max_retries == 2
        assert single.max_retries == single.root_async_client.max_retries == 0
        with pytest.raises(RateLimitError):
            await single.ainvoke("fixture")
        assert len(attempts) == 1
