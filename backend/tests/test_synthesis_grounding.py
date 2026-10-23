"""模拟检查器响应验证交付契约；不据此声称真实模型的语义准确率。"""

import asyncio
from copy import deepcopy

import pytest

from app.models.review_synthesis import ReviewSynthesisArtifact
from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import markdown_text, render_review_comment, render_summary
from app.services.review_synthesis_repository import SynthesisStore
from app.services.review_synthesis_service import freeze_review_synthesis
from test_phase6a_persistence import persistence as persistence
from test_review_synthesis import stub_provider, synthesis_task


def bodies(plan):
    return (render_summary(plan), render_review_comment(plan), render_review_comment(plan, detail="full"))


@pytest.mark.asyncio
@pytest.mark.parametrize("text,source", [
    ("测试代码断言请求失败时不写入缓存。", "assert failed_request.cache is None"),
    ("检查未通过时停止部署。", "if not checks_passed: return"),
    ("Tests cover failed requests, not build success.", "assert failed_request.cache is None"),
    ("仅当输入为空时返回默认值。", "if not value: return default"),
])
async def test_supported_code_conditions_survive_all_delivery_modes(persistence, text, source):
    repo, _, _ = persistence
    task = synthesis_task()
    task.changed_files[0].hunks[0].lines[0].content = source
    repo.create_task(task)
    provider, calls = stub_provider(change=lambda value: value["change_summary"].update(text=text))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "ready"
    assert all(markdown_text(text) in body for body in bodies(PublicationPlanner().build(repo.get_task(task.id))))
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text,verdict", [
    ("全部测试通过，可以安全合并。", "out_of_scope"),
    ("本次测试未通过。", "out_of_scope"),
    ("CI is green; ship it.", "out_of_scope"),
    ("所有高风险问题均已修复。", "out_of_scope"),
    ("增加了参数化测试。", "unsupported"),
    ("对所有输入均返回默认值。", "contradicted"),
])
async def test_rejected_claims_fall_back_without_mutating_review(persistence, text, verdict):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    before = PublicationPlanner().build(task)
    provider, calls = stub_provider(change=lambda value: value["change_summary"].update(text=text),
        check_change=lambda value: value["checks"][0].update(verdict=verdict, evidence=[]))
    artifact = await freeze_review_synthesis(task, repo, provider)
    after = PublicationPlanner().build(repo.get_task(task.id))
    assert artifact.failure_code == "grounding_" + verdict
    assert all(text not in body for body in bodies(after))
    assert after.outcome == before.outcome
    assert after.digest.merge_decision == before.digest.merge_decision
    assert after.presentation.findings == before.presentation.findings
    assert after.presentation.validation_state == "not_executed"
    assert repo.get_task(task.id).model_usage_summary.overall.actual_total_tokens == 120
    await freeze_review_synthesis(task, repo, provider)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    (lambda value: value["checks"].pop(), "grounding_incomplete"),
    (lambda value: value["checks"].append(deepcopy(value["checks"][0])), "grounding_incomplete"),
    (lambda value: value["checks"][0].update(field_id="invented"), "grounding_incomplete"),
    (lambda value: value["checks"][0].update(evidence=[]), "grounding_evidence_missing"),
    (lambda value: value["checks"][0]["evidence"][0].update(quote="不存在的原文"), "grounding_evidence_mismatch"),
    (lambda value: value["checks"][0]["evidence"][0].update(source_ref="finding:key-a"), "grounding_evidence_mismatch"),
])
async def test_incomplete_or_unbound_check_cannot_authorize_text(persistence, change, code):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, _ = stub_provider(check_change=change)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback" and artifact.failure_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["change_summary", "review_summary", "pr_purpose", "key_changes", "finding_summaries"])
async def test_all_editorial_fields_are_checked_and_rebound(persistence, field):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)

    def add_fields(value):
        ref = value["change_summary"]["source_refs"]
        value["pr_purpose"] = {"text": "调整返回值。", "source_kind": "code_inferred", "source_refs": ref}
        value["key_changes"] = [{"text": "返回空值。", "source_refs": ref}]

    provider, _ = stub_provider(change=add_fields)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "ready"
    restored = repo.get_task(task.id)
    value = restored.review_synthesis["synthesis"][field]
    value = value[0] if isinstance(value, list) else value
    value["text"] = "全部测试通过，可以安全合并。"
    plan = PublicationPlanner().build(restored)
    assert plan.presentation.synthesis_status == "fallback"
    assert all(value["text"] not in body for body in bodies(plan))


@pytest.mark.asyncio
async def test_unchecked_digest_cannot_reenter_fallback(persistence):
    from app.models.publication import ChangeUnderstanding
    from app.services.review_presentation import build_presentation

    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    text = "全部测试通过，可以安全合并。"
    plan.digest.change_understanding = ChangeUnderstanding(
        author_goal=text, actual_changes=text, implementation_changes=text, test_observations=text)
    plan.presentation = build_presentation(plan)
    assert all(text not in body for body in bodies(plan))
    assert plan.digest.change_understanding.actual_changes == text


@pytest.mark.asyncio
async def test_old_policy_does_not_silently_add_calls(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    task.task_runtime_configuration["output_policy"].pop("grounding_policy")
    repo.create_task(task)
    provider, calls = stub_provider()
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.failure_code == "grounding_policy_missing" and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["timeout", "transport", "schema"])
async def test_check_failures_keep_generation_usage_and_never_resend(persistence, case):
    repo, _, _ = persistence
    task = synthesis_task()
    task.task_runtime_configuration["output_policy"]["timeout_seconds"] = 2
    repo.create_task(task)
    provider, calls = stub_provider(check_delay=5 if case == "timeout" else 0,
        check_error=ConnectionError("offline") if case == "transport" else None,
        check_change=(lambda value: value.update(checks=[])) if case == "schema" else None)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback"
    restored = repo.get_task(task.id)
    assert restored.model_usages[-2].actual_total_tokens == 50
    assert restored.model_usages[-1].operation == "review_synthesis_grounding_v1"
    assert restored.model_usages[-1].actual_total_tokens == (70 if case == "schema" else None)
    await freeze_review_synthesis(task, repo, provider)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancel_during_check_preserves_first_usage_and_unknown_second(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    task.task_runtime_configuration["output_policy"]["timeout_seconds"] = 10
    repo.create_task(task)
    provider, calls = stub_provider(check_delay=10)
    operation = asyncio.create_task(freeze_review_synthesis(task, repo, provider))

    async def wait_check():
        while len(calls) < 2:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_check(), timeout=5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    restored = repo.get_task(task.id)
    assert restored.review_synthesis["status"] == "fallback"
    assert restored.model_usages[-2].actual_total_tokens == 50
    assert restored.model_usages[-1].actual_total_tokens is None
    assert restored.review_synthesis["budget"]["unknown_reserved"] > 0
    await freeze_review_synthesis(task, repo, provider)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_generation_checkpoint_failure_prevents_second_dispatch(persistence, monkeypatch):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider()

    def fail(*args):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(SynthesisStore, "checkpoint", fail)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback" and len(calls) == 1
    assert artifact.usage["actual_total_tokens"] == 50


def test_legacy_artifact_roundtrip_keeps_absent_check_fields_absent():
    saved = ReviewSynthesisArtifact(review_id="legacy", head_sha="a" * 40,
        effective_base_sha="b" * 40, fact_bundle_hash="c" * 64).model_dump(mode="json")
    assert "grounding" not in saved and "grounding_usage" not in saved
    assert ReviewSynthesisArtifact.model_validate(saved).model_dump(mode="json") == saved


@pytest.mark.asyncio
async def test_check_budget_rejection_preserves_paid_generation(persistence, monkeypatch):
    from app.services.model_request_budgeter import PreparedModelRequest

    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    estimate = PreparedModelRequest.estimate

    def oversized_check(request, profile):
        admission = estimate(request, profile)
        if request.operation == "review_synthesis_grounding_v1":
            admission["reserved_tokens"] = 100_000
        return admission

    monkeypatch.setattr(PreparedModelRequest, "estimate", oversized_check)
    provider, calls = stub_provider()
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.failure_code == "grounding_budget_exhausted"
    assert len(calls) == 1
    restored = repo.get_task(task.id)
    assert restored.model_usages[-1].actual_total_tokens == 50
    assert not any(item.operation == "review_synthesis_grounding_v1" for item in restored.model_usages)
    assert artifact.budget["calls_consumed"] == 1


@pytest.mark.asyncio
async def test_model_observation_cannot_be_its_own_only_evidence(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, _ = stub_provider(change=lambda value: value["change_summary"].update(
        source_refs=["observation:implementation"]))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback" and artifact.failure_code == "grounding_source_insufficient"


@pytest.mark.asyncio
async def test_grounding_fact_hash_is_required_on_read(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider()
    await freeze_review_synthesis(task, repo, provider)
    restored = repo.get_task(task.id)
    restored.review_synthesis["grounding"]["fact_bundle_hash"] = "f" * 64
    assert PublicationPlanner().build(restored).presentation.synthesis_status == "fallback"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_no_partial_approved_text_is_delivered_after_truncation(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    text = "调整返回值。" * 110 + "但仅在输入为空时生效。"
    provider, _ = stub_provider(change=lambda value: value["change_summary"].update(text=text))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback" and artifact.failure_code == "grounding_text_too_long"
    assert all("调整返回值。" not in body for body in bodies(PublicationPlanner().build(repo.get_task(task.id))))
