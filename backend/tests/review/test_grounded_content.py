"""Fixed source fixtures test contracts and recovery, never live model quality."""

import asyncio
import json
from datetime import timedelta
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
from app.review.models import ReviewLimits, ReviewReport, ReviewSnapshot, VerificationOutput
from app.review.observations import collect_observations, validated_observations
from app.review.synthesis import single_request_model, synthesize, synthesis_materials, validate_synthesis
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_presentation import selected_presentation
from tests.review.test_native_graph import ReviewModel
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]


@pytest.fixture(autouse=True)
def immediate_editorial_retry(monkeypatch):
    monkeypatch.setattr("app.review.synthesis.RETRY_DELAY", 0)

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
    grounding_calls: int = 0
    synthesis_mode: str = "ready"
    grounding_groups: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(item.content for item in messages if item.type == "human"))
        if "output" in payload and "facts" in payload:
            self.grounding_calls += 1
            self.grounding_groups = payload.get("required_observation_groups", {})
            supported = "可以安全上线" not in json.dumps(payload["output"], ensure_ascii=False)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="", tool_calls=[{"name": "SynthesisGrounding", "id": "grounding",
                    "args": {"supported": supported, "explanation": "fixture groundedness judgment"}}],
                usage_metadata={"input_tokens": 23, "output_tokens": 11, "total_tokens": 34,
                                "input_token_details": {"cache_read": 2}}))])
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
            if self.synthesis_mode == "paraphrase":
                args["change_overview"][0]["text"] = "VALUE 由 1 改为 2，直接读取它的调用方会受到影响。"
            if self.synthesis_mode == "invalid":
                args = {"change_overview": "invalid"}
            if self.synthesis_mode == "missing_status":
                args["disclosed_status_ids"] = []
            if self.synthesis_mode == "citation_correction" and self.synthesis_calls == 1:
                args["actions"][0]["source_ids"] = [observation]
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
    assert synthesis.model_calls == 2 and synthesis.input_tokens == 54
    assert synthesis.cached_input_tokens == 11 and model.synthesis_calls == model.grounding_calls == 1
    assert set(model.grounding_groups) == {"safe.py"}
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
    assert task.review_report.budget["model_calls"] == 4 + 2 * (not no_findings)
    reopened = pipeline[1].get_task(task.id)
    assert reopened.review_report.synthesis == synthesis
    assert PublicationPlanner(pipeline[1]._artifacts).build(reopened).presentation == plan.presentation
    selected = selected_presentation(plan, [])
    assert selected.change_summary == plan.presentation.change_summary
    assert selected.review_summary == plan.presentation.review_summary
    assert selected.delivery.selected == 0
    assert selected.delivery.confirmed == (not no_findings)
    assert model.synthesis_calls == 1


async def test_rejected_implementation_observation_cannot_publish_test_only_overview(pipeline):
    from app.review.observations import observation_gaps

    task, plan, _ = await run_grounded(pipeline, no_findings=True)
    report = task.review_report
    observation = report.observations[0].model_copy(deep=True)
    observation.kind = "test_source"
    observation.sources = [ref.model_copy(update={"path": "tests/test_safe.py"})
                           for ref in observation.sources]
    report.observations = [observation]
    report.observation_errors = ["assignment:0:observation_comparison_source_missing"]
    version, material = synthesis_materials(report, plan, [observation])
    assert observation_gaps(report, [observation]) == ["safe.py"]
    calls = []

    async def facts(_report):
        return version, material

    class NeverCalled:
        def bind_tools(self, *args, **kwargs):
            calls.append(True)
            raise AssertionError("incomplete materials must not call model")

    context = ReviewContext(load=None, model=NeverCalled(), synthesis_facts=facts)
    context.budget = ReviewBudget(context.limits)
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    assert not calls
    assert result["synthesis"]["error"] == "synthesis_observations_incomplete"
    from app.services.publication.review_presentation import build_presentation, render_presentation
    presentation = build_presentation(plan, report=report, observations=[observation])
    assert presentation.synthesis_status == "fallback" and presentation.summary_source == "metadata"
    plan.presentation = presentation
    comment = render_presentation(plan)
    assert "总览未完整生成" in comment and "测试源码变化" not in comment
    assert report.complete and report.termination == "completed"


def test_change_overview_must_cite_implementation_and_test_groups():
    material = {"required_status_ids": [], "facts": {
        "implementation": {"kind": "observation"}, "test": {"kind": "observation"},
        "advice": {"kind": "status"}}, "required_observation_groups": {
            "safe.py": ["implementation"], "tests/test_safe.py": ["test"]}}
    def paragraph(identity):
        return {"source_ids": [identity], "text": "已有事实。"}
    output = {"change_overview": [paragraph("test")], "review_overview": [paragraph("advice")],
              "actions": [paragraph("advice")]}
    with pytest.raises(ValueError, match="synthesis_change_coverage_missing") as caught:
        validate_synthesis(output, material)
    assert "safe.py" in caught.value.feedback
    output["change_overview"].append(paragraph("implementation"))
    assert validate_synthesis(output, material)


async def test_summary_file_omission_correction_shares_the_original_two_attempts():
    from app.review.synthesis import _request
    from app.review.models import ReviewSynthesis, SynthesisOutput
    import time

    class Model(GroundedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            response = super()._generate(messages, stop, run_manager, **kwargs)
            args = response.generations[0].message.tool_calls[0]["args"]
            args["change_overview"] = [{"source_ids": ["test"], "text": "测试变化。"}]
            if self.synthesis_calls > 1:
                args["change_overview"].append({"source_ids": ["implementation"], "text": "实现变化。"})
            return response

    material = {"facts": {"test": {"kind": "observation", "text": "测试变化。"},
                           "implementation": {"kind": "observation", "text": "实现变化。"},
                           "status:conclusion": {"kind": "status", "text": "静态结论。"},
                           "status:advice": {"kind": "status", "text": "结合 CI。"}},
                "required_status_ids": [],
                "required_observation_groups": {"safe.py": ["implementation"], "test.py": ["test"]}}
    model = Model()
    context = ReviewContext(load=None, model=model)
    context.budget = ReviewBudget(context.limits)
    report = SimpleNamespace(synthesis=None, budget={})
    saved = ReviewSynthesis(material_version="fixture", started_at=time.time())
    output = await _request(context, report, saved, "generation", SynthesisOutput, "fixture", material,
                            validate=lambda value: validate_synthesis(value, material))
    assert len(output.change_overview) == model.synthesis_calls == 2
    assert saved.attempts[0].error == "synthesis_change_coverage_missing"
    assert "safe.py" in saved.attempts[0].feedback
    assert saved.attempts[1].status == "completed"
    assert (await context.budget.summary())["model_calls"] == 2


@pytest.mark.parametrize("mode,error", [
    ("invented", "synthesis_source_unknown"), ("timeout", "synthesis_timeout"),
    ("invalid", "ValidationError"), ("missing_status", "synthesis_status_disclosure_missing"),
])
async def test_synthesis_failure_is_expression_only_and_observations_remain(pipeline, mode, error):
    task, plan, model = await run_grounded(pipeline, synthesis_mode=mode)
    assert task.review_report.synthesis.status == "fallback"
    assert task.review_report.synthesis.error == error
    assert task.review_report.complete and len(task.review_report.findings) == 1
    assert plan.presentation.summary_source == "observations"
    assert "变更总览未生成" in plan.review_comment_markdown
    assert model.synthesis_calls == 2
    if mode == "timeout":
        assert task.review_report.budget["unknown_calls"] == 2


async def test_expanded_claim_falls_back_without_pretending_synthesis_succeeded(pipeline):
    task, plan, _ = await run_grounded(pipeline, synthesis_mode="expanded")
    assert task.review_report.synthesis.status == "fallback"
    assert task.review_report.synthesis.error == "synthesis_unsupported_prose"
    assert not task.review_report.synthesis.grounding.supported
    assert "可以安全上线" not in plan.review_comment_markdown
    assert "返回常量由 1 调整为 2" in plan.review_comment_markdown


async def test_supported_paraphrase_survives_generation_projection_and_reopen(pipeline):
    task, plan, model = await run_grounded(pipeline, synthesis_mode="paraphrase")
    text = "VALUE 由 1 改为 2，直接读取它的调用方会受到影响。"
    assert plan.presentation.change_summary == text
    assert task.review_report.synthesis.status == "ready"
    assert model.grounding_calls == 1
    assert PublicationPlanner(pipeline[1]._artifacts).build(pipeline[1].get_task(task.id)).presentation.change_summary == text
    task.review_report.synthesis.output.change_overview[0].text = "篡改后的正文"
    assert PublicationPlanner(pipeline[1]._artifacts).build(task).presentation.synthesis_status == "fallback"


async def test_citation_kind_error_is_corrected_once_with_durable_feedback(pipeline):
    task, plan, model = await run_grounded(pipeline, synthesis_mode="citation_correction")
    saved = task.review_report.synthesis
    assert saved.status == "ready" and model.synthesis_calls == 2
    first, second = [item for item in saved.attempts if item.stage == "generation"]
    assert first.status == "failed" and first.error == "synthesis_source_kind_invalid"
    assert '"section":"actions"' in first.feedback and '"allowed_kinds"' in first.feedback
    assert first.retryable and second.status == "completed"
    assert saved.model_calls == 3 and saved.input_tokens == 85
    assert plan.presentation.synthesis_status == "ready"
    with pipeline[1]._session_factory() as session:
        rows = pipeline[1]._model_budget_rows(session, task.id)
    sizes = {item["agent_id"]: item["reserved_input_bytes"] for item in rows}
    assert sizes[second.request_id] > sizes[first.request_id]

    class ResumedModel(GroundedModel):
        received_feedback: bool = False

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.received_feedback |= any(
                str(item.content).startswith("Error: synthesis_submission_needs_correction")
                for item in messages if item.type == "tool")
            return super()._generate(messages, stop, run_manager, **kwargs)

    version, material = synthesis_materials(task.review_report, plan, task.review_report.observations)

    async def facts(_report):
        return version, material

    partial = saved.model_copy(deep=True)
    partial.status, partial.output, partial.grounding = "fallback", None, None
    partial.grounded_output_hash = None
    partial.attempts = [first.model_copy(deep=True)]
    resumed_model = ResumedModel()
    context = ReviewContext(load=None, model=resumed_model, limits=task.configuration.limits,
                            synthesis_facts=facts, previous_synthesis=partial)
    context.budget = ReviewBudget(context.limits)
    ticket = await context.budget.reserve(first.request_id, 100, stage="finalize")
    await context.budget.settle(ticket, {"input_tokens": 31, "output_tokens": 17,
                                       "input_token_details": {"cache_read": 9}})
    recovered = await synthesize({"report": task.review_report.model_dump()}, SimpleNamespace(context=context))
    assert recovered["synthesis"]["status"] == "ready"
    assert resumed_model.received_feedback and resumed_model.synthesis_calls == 1
    assert recovered["synthesis"]["model_calls"] == 3


@pytest.mark.parametrize("mode", ["neutral", "resurrect", "invalid"])
async def test_refuted_test_criticism_cannot_return_via_observation_analysis(pipeline, mode):
    rejected = "负向对照缺乏区分力"

    class ObservationModel(GroundedModel):
        seen_materials: list[dict] = []
        seen_checks: list[dict] = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(item.content for item in messages if item.type == "human"))
            response = super()._generate(messages, stop, run_manager, **kwargs)
            for call in response.generations[0].message.tool_calls:
                args = call["args"]
                if call["name"] == "DiscoveryOutput":
                    args["findings"][0]["title"] = rejected
                    args["observations"][0].update(
                        change=rejected if mode == "invalid" else "新增中间样本保持不变的对照测试。",
                        after=rejected, impact=rejected, kind="test_source")
                elif call["name"] in {"VerificationOutput", "BatchVerificationOutput"}:
                    for decision in args.get("decisions", [args]):
                        decision.update(decision="refuted", explanation="该测试故意验证中间样本保持不变。")
                elif call["name"] == "SynthesisOutput":
                    self.seen_materials.append(payload)
                    if mode == "resurrect":
                        observation = next(key for key in payload["facts"] if key.startswith("observation:"))
                        args["review_overview"] = [{"source_ids": [observation], "text": rejected}]
                elif call["name"] == "SynthesisGrounding":
                    self.seen_checks.append(payload)
                    args["supported"] = mode != "resurrect"
            return response

    service, repository, _, _, _ = pipeline
    model = ObservationModel(synthesis_mode="invalid" if mode == "invalid" else "ready")
    service._provider.agent_model = lambda *args, **kwargs: model
    _, _, task = await preview_and_create(service)
    await service._worker.run_once()
    task = repository.get_task(task.id)
    plan = PublicationPlanner(repository._artifacts).build(task)
    assert task.status == "completed" and task.review_report.decisions[0].decision == "refuted"
    assert task.review_report.observations[0].impact == rejected  # Original audit remains intact.
    assert rejected not in plan.review_comment_markdown
    assert rejected not in plan.presentation.change_summary
    material = model.seen_materials[0]
    assert material["refuted_claims"][0]["claim"] == rejected
    if mode != "invalid":
        assert all(rejected not in item["text"] for item in material["facts"].values()
                   if item["kind"] == "observation")
    if mode == "neutral":
        assert plan.presentation.synthesis_status == "ready"
    elif mode == "resurrect":
        assert model.seen_checks[0]["refuted_claims"] == material["refuted_claims"]
        assert task.review_report.synthesis.error == "synthesis_unsupported_prose"
    else:
        assert plan.presentation.summary_source == "metadata"


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
    identity = f"synthesis:{version}:generation:1"
    await context.budget.reserve(identity, 100, stage="finalize")
    result = await synthesize({"report": report.model_dump()}, runtime)
    assert result["report"]["synthesis"]["error"] == "synthesis_interrupted_unknown"
    assert result["report"]["synthesis"]["model_calls"] == 1
    assert model.synthesis_calls == 1
    persistent = ReviewBudget(context.limits, repository=pipeline[1], task_id=task.id)
    assert (await persistent.branch_state(identity))["final_requests"] == 1
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
    with pytest.raises(ValueError, match="synthesis_source_unknown"):
        validate_synthesis(output, material)


def test_public_observation_fallback_quarantines_the_whole_refuted_unit():
    from app.review.observations import fallback_observations

    report = SimpleNamespace(
        decisions=[SimpleNamespace(candidate_id="refuted", decision="refuted")],
        candidates=[SimpleNamespace(id="refuted", assignment_ids=["contract"])],
        assignments=[SimpleNamespace(id="contract", unit_id="disputed-unit")])
    observations = [SimpleNamespace(assignment_id="reviewer", unit_id="disputed-unit"),
                    SimpleNamespace(assignment_id="other", unit_id="other-unit")]
    assert fallback_observations(report, observations) == observations[1:]


@pytest.mark.parametrize("case,error", [
    ("unknown", "synthesis_source_unknown"),
    ("duplicate", "synthesis_source_duplicate"),
    ("kind", "synthesis_source_kind_invalid"),
])
def test_citation_validation_identifies_the_specific_protocol_error(case, error):
    material = {"required_status_ids": ["status:advice"], "facts": {
        "observation:1": {"kind": "observation", "text": "修改返回值。"},
        "status:advice": {"kind": "status", "text": "请结合 CI 判断。"}}}
    observation = {"source_ids": ["observation:1"], "text": "修改返回值。"}
    advice = {"source_ids": ["status:advice"], "text": "请结合 CI 判断。"}
    output = {"change_overview": [observation], "review_overview": [observation],
              "actions": [advice], "disclosed_status_ids": ["status:advice"]}
    output["actions"] = [{"source_ids": {
        "unknown": ["missing"], "duplicate": ["status:advice", "status:advice"],
        "kind": ["observation:1"]}[case], "text": "建议"}]
    with pytest.raises(ValueError, match=error) as caught:
        validate_synthesis(output, material)
    assert '"section":"actions"' in caught.value.feedback


@pytest.mark.parametrize("timeout", [60, 120])
async def test_synthesis_disables_sdk_retries_without_changing_review_client(timeout):
    attempts = []

    def reject(request):
        attempts.append(request)
        return httpx.Response(429, json={"error": {"message": "fixture", "type": "rate_limit"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        original = ChatOpenAI(model="fixture", api_key="fixture", base_url="https://example.test/v1",
                              max_retries=2, http_async_client=client)
        single = single_request_model(original, timeout=timeout)
        assert original.max_retries == original.root_async_client.max_retries == 2
        assert single.max_retries == single.root_async_client.max_retries == 0
        assert single.request_timeout == timeout
        with pytest.raises(RateLimitError):
            await single.ainvoke("fixture")
        assert len(attempts) == 1
        assert attempts[0].extensions["timeout"]["read"] == timeout


async def test_timeout_retries_once_with_separate_durable_reservations(pipeline):
    class TransientModel(GroundedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(item.content for item in messages if item.type == "human"))
            if "facts" in payload and "output" not in payload and self.synthesis_calls == 0:
                self.synthesis_calls += 1
                raise TimeoutError("first request")
            return super()._generate(messages, stop, run_manager, **kwargs)

    service, repository, _, _, _ = pipeline
    model = TransientModel(synthesis_mode="paraphrase")
    service._provider.agent_model = lambda *args, **kwargs: model
    _, _, task = await preview_and_create(service)
    await service._worker.run_once()
    report = repository.get_task(task.id).review_report
    saved = report.synthesis
    assert saved.status == "ready"
    attempts = [item for item in saved.attempts if item.stage == "generation"]
    assert [item.timeout_seconds for item in attempts] == [60, 120]
    assert [item.status for item in attempts] == ["timeout", "completed"]
    assert len({item.reservation_id for item in saved.attempts}) == 3
    assert saved.model_calls == 3 and saved.input_tokens is None
    assert report.budget["unknown_calls"] == 1
    assert len(repository.get_task(task.id).review_report.synthesis.attempts) == 3


async def test_deadline_after_generation_preserves_completed_business_report(pipeline, monkeypatch):
    from app.services.review import review_service

    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    real_now = review_service.utcnow
    clock = {"expired": None}
    monkeypatch.setattr(review_service, "utcnow", lambda: clock["expired"] or real_now())

    class DeadlineModel(GroundedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(item.content for item in messages if item.type == "human"))
            response = super()._generate(messages, stop, run_manager, **kwargs)
            if "facts" in payload:
                clock["expired"] = task.created_at + timedelta(seconds=task.configuration.limits.timeout_seconds + 1)
            return response

    service._provider.agent_model = lambda *args, **kwargs: DeadlineModel()
    await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed" and saved.error is None
    assert saved.review_report.complete and saved.review_report.termination == "completed"
    assert saved.review_report.synthesis.error == "synthesis_deadline"
    assert saved.report_markdown
    assert PublicationPlanner(repository._artifacts).build(saved).outcome.confirmed_count == 1
    # A worker restarted after expiry must also finalize the durable facts
    # without entering the graph or requesting another model response.
    calls = saved.review_report.budget["model_calls"]
    saved.status, saved.phase, saved.error = "failed", "failed", "review_deadline"
    repository.save_task(saved)
    await service._run_graph(task.id)
    restarted = repository.get_task(task.id)
    assert restarted.status == "completed" and restarted.error is None
    assert restarted.review_report.budget["model_calls"] == calls


async def test_outer_deadline_during_request_preserves_report_and_unknown_attempt(pipeline, monkeypatch):
    from app.services.review import review_service

    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    deadlines = []
    loop = asyncio.get_running_loop()

    def timeout(seconds):
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(review_service, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": timeout}))

    class InterruptedModel(GroundedModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(item.content for item in messages if item.type == "human"))
            if "facts" in payload:
                deadlines[0].reschedule(loop.time() + 0.01)
                await asyncio.sleep(0.05)
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    service._provider.agent_model = lambda *args, **kwargs: InterruptedModel()
    await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed" and saved.error is None
    assert saved.review_report.complete and saved.report_markdown
    assert saved.review_report.synthesis.error == "synthesis_deadline"
    assert saved.review_report.synthesis.attempts[0].status == "unknown"
    assert saved.review_report.budget["unknown_calls"] == 1


async def test_interrupted_retry_is_not_replayed_after_worker_restart(pipeline, monkeypatch):
    from app.review import synthesis

    service, repository, _, _, _ = pipeline
    model = GroundedModel()
    service._provider.agent_model = lambda *args, **kwargs: model
    calls = []

    class InterruptedEditorial:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                raise TimeoutError("first request")
            raise asyncio.CancelledError()

    monkeypatch.setattr(synthesis, "single_request_model", lambda _, **kwargs: InterruptedEditorial())
    _, _, task = await preview_and_create(service)
    await service._worker.run_once()
    interrupted = repository.get_task(task.id)
    assert interrupted.review_report.complete
    assert [item.status for item in interrupted.review_report.synthesis.attempts] == ["timeout", "unknown"]
    await service._run_graph(task.id)
    restored = repository.get_task(task.id)
    assert restored.status == "completed"
    assert restored.review_report.synthesis.error == "synthesis_interrupted_unknown"
    assert len(calls) == 2
    assert restored.review_report.budget["unknown_calls"] == 2


@pytest.mark.parametrize("mode", ["permanent", "deadline", "grounding_budget"])
async def test_editorial_refusal_paths_do_not_retry_or_admit_unchecked_prose(mode):
    report = ReviewReport(
        snapshot=ReviewSnapshot(review_id="review", repository="local/repo", pr_number=1,
                                base_sha="a" * 40, head_sha="b" * 40),
        assignments=[], goals=[], discovery_results=[], verification_results=[],
        candidates=[], decisions=[], findings=[], complete=False, termination="partial")
    material = {"omitted": {}, "required_status_ids": ["status:conclusion"],
                "facts": {"observation:1": {"kind": "observation", "text": "返回值由 1 改为 2。"},
                          "status:conclusion": {"kind": "status", "text": "尚无完整结论。"}}}
    calls = []

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            if mode == "permanent":
                raise ValueError("non-retryable response")
            def paragraph(identity):
                return {"source_ids": [identity], "text": material["facts"][identity]["text"]}
            return AIMessage(content="", tool_calls=[{"id": "generation", "name": "SynthesisOutput",
                "args": {"change_overview": [paragraph("observation:1")],
                         "review_overview": [paragraph("status:conclusion")],
                         "actions": [paragraph("status:conclusion")],
                         "disclosed_status_ids": ["status:conclusion"]}}],
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    async def facts(_):
        return "version", material

    context = ReviewContext(load=None, model=Model(), limits=ReviewLimits(total_model_calls=1),
        synthesis_facts=facts, remaining_seconds=(lambda: 3) if mode == "deadline" else None)
    context.budget = ReviewBudget(context.limits)
    result = await synthesize({"report": report.model_dump()}, SimpleNamespace(context=context))
    saved = result["report"]["synthesis"]
    assert saved["status"] == "fallback"
    assert saved["error"] == {"permanent": "ValueError", "deadline": "synthesis_deadline",
                             "grounding_budget": "synthesis_budget_exhausted"}[mode]
    assert len(calls) == (0 if mode == "deadline" else 1)
    assert saved["model_calls"] == len(calls)
    assert saved["grounded_output_hash"] is None
