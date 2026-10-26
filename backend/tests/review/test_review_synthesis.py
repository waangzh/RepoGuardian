import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.llm.providers import OpenAICompatibleProvider
from app.core.config import Settings
from app.models.review import ChangedFile, DiffHunk, DiffLine
from app.models.review import PullRequestInfo, PullRequestRef
from app.models.review_synthesis import (
    ReviewSynthesisArtifact, ReviewSynthesisDraft, ReviewSynthesisOutput,
    ReviewSynthesisInput, SynthesisSource,
)
from app.core.fingerprints import stable_hash
from app.services.publication.publication_planner import PublicationPlanner, summary_projection
from app.services.review.synthesis.review_synthesis_input import SOURCE_POLICY_VERSION, build_synthesis_input
from app.services.review.synthesis.review_synthesis_repository import SynthesisStore
from app.services.review.synthesis.review_synthesis_service import freeze_review_synthesis, bound_synthesis
from app.services.review.synthesis.review_synthesis_validator import validate_review_synthesis
from tests.support.persistence import persistence as persistence
from tests.support.review_tasks import publishable_task as publishable_task
from tests.review.test_review_presentation import new_output


def synthesis_task():
    task = new_output(publishable_task())
    task.task_runtime_configuration.update(provider="fixture", model="fixture")
    task.task_runtime_configuration["output_policy"] = Settings(_env_file=None).review_output_policy()
    task.task_runtime_configuration["output_policy"]["synthesis_enabled"] = True
    task.changed_files = [ChangedFile(file_path="src/a.py", change_type="modified", additions=1, deletions=0,
        hunks=[DiffHunk(old_start=7, old_length=1, new_start=7, new_length=1,
            lines=[DiffLine(kind="added", content="return None", new_line_no=7)])])]
    return task


def draft_for(facts):
    ref = next(source.id for source in facts.sources if source.kind == "diff_observation")
    return {"change_summary": {"text": "本次 PR 调整了返回值处理。", "source_refs": [ref]},
        "finding_summaries": [{"finding_key": "key-a", "text": "空返回值破坏调用方契约"}]}


def stub_provider(*, change=None, error=None, delay=0):
    calls = []
    provider = OpenAICompatibleProvider("fixture", "https://example.invalid/v1", "fixture",
        provider_name="fixture", retry_backoff_seconds=0, request_attempts=2)

    async def invoke(messages):
        calls.append(messages)
        if delay:
            await asyncio.sleep(delay)
        if error:
            raise error
        facts = json.loads(messages[1].content.split("\nFact Bundle:\n", 1)[1])
        ref = next(source["id"] for source in facts["sources"]
                   if source["kind"] in {"diff_observation", "test_source_observation"})
        result = {"change_summary": {"text": "本次 PR 调整了返回值处理。", "source_refs": [ref]},
            "finding_summaries": [{"finding_key": "key-a", "text": "空返回值破坏调用方契约"}]}
        if change:
            change(result)
        return AIMessage(content=json.dumps(result, ensure_ascii=False),
            usage_metadata={"input_tokens": 20, "output_tokens": 30, "total_tokens": 50})

    provider._build_chat_model = lambda *args: SimpleNamespace(ainvoke=invoke)
    return provider, calls


@pytest.mark.asyncio
async def test_once_frozen_and_repeated_reads_never_call_model(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider()
    first = await freeze_review_synthesis(task, repo, provider)
    assert first.status == "ready"
    assert first.prompt_version == "review-synthesis-prompt-v7"
    assert first.source_policy_version == SOURCE_POLICY_VERSION
    prompt = calls[0][1].content
    assert "示例输入" not in prompt and "示例 change_summary" not in prompt
    assert "示例 test_changes" not in prompt and prompt.count("\nFact Bundle:\n") == 1
    schema = json.loads(prompt.split("Schema:\n", 1)[1].split("\nFact Bundle:\n", 1)[0])
    assert "test_changes" not in schema["properties"]
    assert first.synthesis.schema_version == "review-synthesis-v2"
    assert "test_changes" not in first.model_dump(mode="json")["synthesis"]
    assert first.budget["calls_consumed"] == 1
    assert first.usage["actual_total_tokens"] == 50
    for _ in range(10):
        restored = repo.get_task(task.id)
        plan = PublicationPlanner().build(restored)
        assert plan.presentation.synthesis_status == "ready"
        assert "调整了返回值处理" in plan.summary_markdown
        assert "调整了返回值处理" in plan.review_comment_markdown
        assert plan.presentation.verdict == plan.digest.merge_decision
        assert plan.presentation.validation_state == "not_executed"
        assert summary_projection(plan).presentation.findings == []
        assert restored.model_usage_summary.overall.actual_total_tokens == 50
        assert restored.model_usages[-1].operation == "review_synthesis_v1"
    again = await freeze_review_synthesis(task, repo, provider)
    assert first.fact_bundle_hash == again.fact_bundle_hash and len(calls) == 1
    assert "synthesis" not in (task.task_runtime_report or {})


@pytest.mark.parametrize("change", [
    lambda value: value.update(severity="high"),
    lambda value: value.update(validation_state="passed"),
    lambda value: value.update(verdict="approve"),
    lambda value: value.update(confirmed_count=0),
    lambda value: value["change_summary"].update(source_refs=["nonexistent"]),
    lambda value: value["change_summary"].update(source_refs=["finding:key-a"]),
    lambda value: value["change_summary"].update(source_refs=[]),
    lambda value: value.update(finding_summaries=[{"finding_key": "injected", "text": "新问题"}]),
    lambda value: value["change_summary"].update(text="包含不可见控制字符\u202e。"),
    lambda value: value.update(test_changes=[{"text": "新增测试用例", "source_refs": ["finding:key-a"]}]),
])
def test_schema_refs_finding_keys_and_control_characters_rejected(change):
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan)
    value = draft_for(facts)
    change(value)
    with pytest.raises((ValueError, ValidationError)):
        validate_review_synthesis(facts, value)


@pytest.mark.parametrize("text", [
    "测试代码包含失败响应不缓存的断言。",
    "测试代码包含 low < point < high 的断言。",
    "返回值 > 0 且 < 1 时保留原区间。",
    "Tests cover failed requests, not build success.",
    "检查失败分支，而非声明检查已通过。",
])
def test_normal_source_descriptions_are_not_rejected_by_keywords(text):
    facts = ReviewSynthesisInput(review_id="offline", head_sha="a" * 40,
        effective_base_sha="b" * 40,
        sources=[SynthesisSource(id="source", kind="test_source_observation", text=text)])
    value = ReviewSynthesisOutput(change_summary={"text": text, "source_refs": ["source"]})
    before = facts.model_dump(mode="json")
    assert validate_review_synthesis(facts, value).change_summary.text == text
    assert facts.model_dump(mode="json") == before


def test_author_purpose_is_not_classified_by_audit_keywords():
    facts = ReviewSynthesisInput(review_id="offline", head_sha="a" * 40,
        effective_base_sha="b" * 40, sources=[
            SynthesisSource(id="author", kind="author_claim", text="Found by the deep-review audit"),
            SynthesisSource(id="source", kind="diff_observation", text="调整返回值处理。")])
    value = ReviewSynthesisOutput(
        pr_purpose={"text": "Found by the deep-review audit", "source_kind": "author_claim", "source_refs": ["author"]},
        change_summary={"text": "调整返回值处理。", "source_refs": ["source"]})
    assert validate_review_synthesis(facts, value).pr_purpose.text == value.pr_purpose.text


@pytest.mark.asyncio
async def test_editorial_words_do_not_overwrite_authoritative_review_state(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    before = PublicationPlanner().build(task)
    # Deliberately misleading prose is not a structured status or a verification result.
    text = "全部测试通过，可以安全合并。"
    provider, _ = stub_provider(change=lambda value: value["change_summary"].update(text=text))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "ready"
    after = PublicationPlanner().build(repo.get_task(task.id))
    # 权威状态、问题等级与合并建议不因编辑文本改变。
    assert after.outcome == before.outcome
    assert after.digest.merge_decision == before.digest.merge_decision
    assert after.presentation.validation_state == "not_executed"
    assert after.presentation.verdict == before.presentation.verdict
    assert after.presentation.findings == before.presentation.findings
    # 问题概述不再由模型改写，越权结论不能借该字段进入交付。
    assert after.presentation.review_summary == "本轮已确认 1 项问题：空返回值破坏调用方契约。"
    assert text not in after.presentation.review_summary
    # 变更概述的内容级准入由独立证据检查负责；本测试只锁定结构化状态。


@pytest.mark.asyncio
async def test_failed_synthesis_delivers_only_deterministic_diff_metadata(persistence):
    from app.services.publication.publication_renderer import markdown_text, render_review_comment, render_summary

    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, _ = stub_provider(change=lambda value: value["change_summary"].update(source_refs=["bad"]))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback"
    plan = PublicationPlanner().build(repo.get_task(task.id))
    assert plan.presentation.summary_source == "metadata"
    assert plan.presentation.change_summary.startswith("本次 PR 共变更 1 个文件")
    assert plan.presentation.review_summary == "本轮已确认 1 项问题：空返回值破坏调用方契约。"
    for markdown in (render_summary(plan), render_review_comment(plan), render_review_comment(plan, detail="full")):
        overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
        assert overview == markdown_text(plan.presentation.change_summary, 1200)
        assert "符合作者描述" not in overview
    assert plan.presentation.validation_state == "not_executed"


@pytest.mark.asyncio
async def test_model_merged_review_summary_is_not_requested_or_delivered(persistence):
    """问题概述由已核验 Finding 确定性生成，模型字段不进入交付。"""
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider(change=lambda value: value.update(
        review_summary={"text": "所有问题均已修复，可以合并。", "source_refs": ["finding:key-a"]}))
    from app.llm.providers import LLMProviderError
    with pytest.raises(LLMProviderError, match="review synthesis schema invalid"):
        await provider.synthesize_review(build_synthesis_input(task, PublicationPlanner().build(task)), "fixture")
    assert len(calls) == 1
    schema = json.loads(calls[0][1].content.split("Schema:\n", 1)[1].split("\nFact Bundle:\n", 1)[0])
    assert "review_summary" not in schema["properties"]


@pytest.mark.asyncio
async def test_untrusted_editorial_markup_is_encoded_at_render_boundary(persistence):
    from app.services.publication.publication_renderer import markdown_text, render_review_comment, render_summary

    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    payload = ('<img src=x onerror=alert(1)> ![p](https://evil.invalid/x) '
               '[open](javascript:alert(1)) @everyone https://evil.invalid low < point < high')

    def add_markup(value):
        value["change_summary"]["text"] = payload

    provider, _ = stub_provider(change=add_markup)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "ready"
    plan = PublicationPlanner().build(repo.get_task(task.id))
    expected = markdown_text(payload, 1200).replace(":", "&#58;")
    for markdown in (render_summary(plan), render_review_comment(plan), render_review_comment(plan, detail="full")):
        overview = markdown.split("### 本次 PR 主要变更\n\n", 1)[1].split("\n\n### 审查结果", 1)[0]
        assert overview == expected
        assert "<img" not in markdown and "@everyone" not in markdown
        assert "https://evil" not in markdown and "javascript:" not in markdown
        assert "low &lt; point &lt; high" in overview
    # 问题概述只引用已核验 Finding 的标题，不再透传模型文本。
    full = render_review_comment(plan, detail="full")
    assert "问题概述：本轮已确认 1 项问题" in full
    assert markdown_text(payload, 600) not in full


@pytest.mark.asyncio
@pytest.mark.parametrize("test_changes", [[], [{"text": "包含空响应断言。", "source_refs": ["tests"]}]])
async def test_new_provider_output_rejects_obsolete_test_changes_field(test_changes):
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan)
    provider, calls = stub_provider(change=lambda value: value.update(test_changes=test_changes))
    from app.llm.providers import LLMProviderError
    with pytest.raises(LLMProviderError, match="review synthesis schema invalid"):
        await provider.synthesize_review(facts, "fixture")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_test_only_pr_can_use_test_sources_for_the_single_change_summary(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    task.changed_files[0].file_path = "tests/test_response.py"
    repo.create_task(task)
    text = "补充空响应处理的回归用例。"
    provider, _ = stub_provider(change=lambda value: value["change_summary"].update(text=text))
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "ready"
    plan = PublicationPlanner().build(repo.get_task(task.id))
    assert plan.presentation.change_summary == text
    assert plan.presentation.test_changes == []
    assert text in plan.summary_markdown


@pytest.mark.parametrize("prompt_version", [
    "review-synthesis-prompt-v1", "review-synthesis-prompt-v2",
    "review-synthesis-prompt-v3", "review-synthesis-prompt-v4",
])
def test_saved_test_changes_remain_readable_without_being_appended_to_new_overview(prompt_version):
    task = synthesis_task()
    task.changed_files.append(task.changed_files[0].model_copy(update={"file_path": "tests/test_response.py"}))
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan, source_policy_version="review-synthesis-sources-v2")
    draft = draft_for(facts)
    test_ref = next(source.id for source in facts.sources if source.kind == "test_source_observation")
    observation = "包含空响应处理的回归断言。"
    draft["test_changes"] = [{"text": observation, "source_refs": [test_ref]}]
    artifact = ReviewSynthesisArtifact(
        prompt_version=prompt_version, review_id=task.id, head_sha=facts.head_sha,
        effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")),
        status="ready", synthesis=ReviewSynthesisDraft.model_validate(draft),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons,
        omitted_files=facts.omitted_files, omitted_hunks=facts.omitted_hunks)
    saved = artifact.model_dump(mode="json")
    task.review_synthesis = deepcopy(saved)
    bound = bound_synthesis(task, plan)
    assert bound is not None and bound.model_dump(mode="json") == saved
    assert bound.synthesis.test_changes[0].text == observation
    current = PublicationPlanner().build(task)
    assert current.presentation.test_changes == []
    assert observation not in current.summary_markdown
    assert current.presentation.change_summary == draft["change_summary"]["text"]
    assert ReviewSynthesisOutput.model_validate(
        {key: value for key, value in draft.items() if key != "test_changes"}
    ).schema_version == "review-synthesis-v2"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["invalid_ref", "timeout", "disabled", "budget", "transport"])
async def test_failures_preserve_findings_and_unknown_cost(persistence, case):
    repo, _, _ = persistence
    task = synthesis_task()
    policy = task.task_runtime_configuration["output_policy"]
    if case == "timeout":
        policy["timeout_seconds"] = 0.02
    if case == "disabled":
        policy["synthesis_enabled"] = False
    if case == "budget":
        policy["max_tokens"] = 0
    repo.create_task(task)
    provider, calls = stub_provider(
        change=(lambda value: value["change_summary"].update(source_refs=["bad"])) if case == "invalid_ref" else None,
        delay=0.2 if case == "timeout" else 0, error=ConnectionError("offline") if case == "transport" else None)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback"
    restored = repo.get_task(task.id)
    plan = PublicationPlanner().build(restored)
    assert plan.outcome.confirmed_count == 1 and plan.presentation.synthesis_status == "fallback"
    assert plan.presentation.validation_state == "not_executed"
    assert len(calls) <= 1
    await freeze_review_synthesis(task, repo, provider)
    assert len(calls) <= 1
    if case in {"timeout", "transport"} and calls:
        assert restored.model_usages[-1].actual_total_tokens is None
        assert restored.model_usages[-1].cost_microusd is None
    if case == "timeout" and not calls:
        # Timeout may occur after the durable dispatch intent but before the stub is invoked.
        sent = any(attempt.get("sent") for attempt in
                   (restored.review_synthesis["budget"].get("attempts") or {}).values())
        assert bool(restored.model_usages) == sent
        if sent:
            assert restored.model_usages[-1].actual_total_tokens is None
            assert restored.model_usages[-1].cost_microusd is None
    if case == "invalid_ref":
        assert restored.model_usages[-1].actual_total_tokens == 50
        # 失败码保留校验器的固定分类，便于定位是哪条结构检查拒绝。
        assert artifact.failure_code == "synthesis_reference_not_allowed"


@pytest.mark.asyncio
async def test_concurrent_claims_and_interrupted_intent_do_not_resend(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider(delay=0.02)
    await asyncio.gather(freeze_review_synthesis(task, repo, provider),
                         freeze_review_synthesis(task, repo, provider))
    assert len(calls) == 1
    store = SynthesisStore(repo, task.id)
    assert not store.claim(bound_synthesis(repo.get_task(task.id), PublicationPlanner().build(task)))[0]


@pytest.mark.asyncio
async def test_snapshot_bundle_tamper_and_legacy_are_read_only_fallback(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    provider, calls = stub_provider()
    await freeze_review_synthesis(task, repo, provider)
    restored = repo.get_task(task.id)
    artifact = deepcopy(restored.review_synthesis)
    artifact["head_sha"] = "d" * 40
    restored.review_synthesis = artifact
    assert bound_synthesis(restored, PublicationPlanner().build(task)) is None
    restored = repo.get_task(task.id)
    restored.changed_files[0].hunks[0].lines[0].content = "return modified"
    assert PublicationPlanner().build(restored).presentation.synthesis_status == "fallback"
    legacy = publishable_task()
    assert await freeze_review_synthesis(legacy, repo, provider) is None
    assert len(calls) == 1


def test_input_is_bounded_excludes_sensitive_paths_and_discloses_omissions():
    task = synthesis_task()
    task.changed_files += [task.changed_files[0].model_copy(update={"file_path": ".env"})]
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    encoded = json.dumps(facts.model_dump(mode="json"), ensure_ascii=False)
    assert '"path": ".env"' not in encoded
    assert facts.input_truncated and facts.omitted_files == 1 and facts.omitted_hunks == 1
    assert "sensitive_or_binary_excluded" in facts.truncation_reasons
    assert len(encoded) <= task.task_runtime_configuration["output_policy"]["max_input_chars"]
    assert stable_hash(facts.model_dump(mode="json")) == stable_hash(
        build_synthesis_input(task, PublicationPlanner().build(task)).model_dump(mode="json"))


def test_truncation_reasons_separate_author_context_diff_and_policy_exclusions():
    task = synthesis_task()
    task.pr = PullRequestInfo(
        owner="o", repo="r", number=1, title="Long context", body="x" * 4500,
        html_url="https://github.com/o/r/pull/1", clone_url="https://github.com/o/r.git",
        base=PullRequestRef(ref="main", sha="b" * 40, repo_clone_url="https://github.com/o/r.git"),
        head=PullRequestRef(ref="feature", sha="a" * 40, repo_clone_url="https://github.com/o/r.git"))
    task.changed_files[0].hunks[0].lines[0].content = "x" * 6200
    task.changed_files += [task.changed_files[0].model_copy(update={"file_path": ".env"})]
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    assert set(facts.truncation_reasons) == {
        "author_context_truncated", "diff_hunk_omitted", "sensitive_or_binary_excluded"}
    assert facts.omitted_files == 2 and facts.omitted_hunks == 2


def test_legacy_synthesis_hash_remains_bindable_with_derived_coverage():
    task = synthesis_task()
    task.changed_files += [task.changed_files[0].model_copy(update={"file_path": ".env"})]
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan, source_policy_version="review-synthesis-sources-v1")
    old_facts = facts.model_dump(mode="json", exclude={"truncation_reasons"})
    old_facts["schema_version"] = "review-synthesis-input-v1"
    artifact = ReviewSynthesisArtifact(
        schema_version="review-synthesis-artifact-v1",
        prompt_version="review-synthesis-prompt-v1",
        source_policy_version="review-synthesis-sources-v1",
        review_id=task.id, head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(old_facts), status="fallback", input_truncated=True,
        failure_code="disabled")
    task.review_synthesis = artifact.model_dump(mode="json")
    bound = bound_synthesis(task, plan)
    assert bound is not None
    assert bound.fact_bundle_hash == artifact.fact_bundle_hash
    assert bound.truncation_reasons == ["sensitive_or_binary_excluded"]
    assert bound.omitted_files == 1 and bound.omitted_hunks == 1


@pytest.mark.parametrize("prompt_version", ["review-synthesis-prompt-v2", "review-synthesis-prompt-v3"])
def test_frozen_editorial_summary_remains_bindable_without_keyword_filter(prompt_version):
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan, source_policy_version="review-synthesis-sources-v2")
    draft = draft_for(facts)
    draft["change_summary"]["text"] = "代码变更符合作者描述。"
    artifact = ReviewSynthesisArtifact(
        prompt_version=prompt_version,
        review_id=task.id, head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")),
        status="ready", synthesis=ReviewSynthesisDraft.model_validate(draft),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons,
        omitted_files=facts.omitted_files, omitted_hunks=facts.omitted_hunks)
    task.review_synthesis = artifact.model_dump(mode="json")
    bound = bound_synthesis(task, plan)
    assert bound is not None and bound.synthesis.change_summary.text == draft["change_summary"]["text"]
    assert bound.prompt_version == prompt_version
    task.review_synthesis.pop("prompt_version")
    assert bound_synthesis(task, plan).prompt_version == "review-synthesis-prompt-v2"


@pytest.mark.parametrize("field,value", [
    ("input_truncated", True),
    ("truncation_reasons", ["author_context_truncated"]),
    ("omitted_files", 99),
    ("omitted_hunks", 99),
])
def test_current_synthesis_rejects_modified_coverage_metadata(field, value):
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan)
    task.review_synthesis = ReviewSynthesisArtifact(
        source_policy_version=SOURCE_POLICY_VERSION,
        review_id=task.id, head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons,
        omitted_files=facts.omitted_files, omitted_hunks=facts.omitted_hunks).model_dump(mode="json")
    task.review_synthesis[field] = value
    assert bound_synthesis(task, plan) is None


def test_oversized_hunk_keeps_changed_lines_instead_of_dropping_the_source():
    """大 hunk 曾因整段超过 6000 字符被丢弃，模型无 diff 可引用，摘要必然失败。"""
    from app.models.review import ChangedFile, DiffHunk, DiffLine

    task = synthesis_task()
    lines = [DiffLine(kind="context", content=f"context {index}", old_line_no=index, new_line_no=index)
             for index in range(400)]
    lines += [DiffLine(kind="added", content=f"added_value_{index} = compute({index})", new_line_no=500 + index)
              for index in range(20)]
    task.changed_files = [ChangedFile(file_path="src/a.py", change_type="modified", additions=20, deletions=0,
        hunks=[DiffHunk(old_start=1, old_length=400, new_start=1, new_length=420, lines=lines)])]
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    kinds = [source.kind for source in facts.sources]
    assert "diff_observation" in kinds
    diff = next(source for source in facts.sources if source.kind == "diff_observation")
    assert "added_value_0" in diff.text and len(diff.text) <= 6000
    assert "diff_hunk_truncated" in facts.truncation_reasons
    assert facts.input_truncated
    assert facts.omitted_hunks == 0


def test_metadata_only_change_does_not_claim_input_length_was_exceeded():
    task = synthesis_task()
    task.changed_files[0].hunks = []
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    assert facts.truncation_reasons == ["diff_metadata_only"]
    assert facts.omitted_files == 1 and facts.omitted_hunks == 0


def test_unknown_purpose_and_duplicate_keys_are_rejected():
    task = synthesis_task()
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    value = draft_for(facts)
    value["pr_purpose"] = {"text": "Found by deep-review audit", "source_kind": "unknown", "source_refs": ["pr-description:1"]}
    with pytest.raises(ValueError):
        validate_review_synthesis(facts, value)
    value = draft_for(facts)
    value["finding_summaries"] *= 2
    with pytest.raises(ValueError):
        validate_review_synthesis(facts, value)
    assert ReviewSynthesisDraft.model_validate(draft_for(facts)).pr_purpose.source_kind == "unknown"


@pytest.mark.asyncio
async def test_cancel_after_dispatch_keeps_unknown_usage_and_does_not_resend(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    task.task_runtime_configuration["output_policy"]["timeout_seconds"] = 5
    repo.create_task(task)
    provider, calls = stub_provider(delay=2)
    generation = asyncio.create_task(freeze_review_synthesis(task, repo, provider))
    async def dispatched():
        while not calls:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(dispatched(), timeout=3)
    generation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await generation
    restored = repo.get_task(task.id)
    assert restored.review_synthesis["status"] == "fallback"
    assert restored.model_usages[-1].actual_total_tokens is None
    assert restored.review_synthesis["budget"]["unknown_reserved"] > 0
    await freeze_review_synthesis(task, repo, provider)
    assert len(calls) == 1


@pytest.mark.parametrize("name,value", [
    ("repoguardian_review_synthesis_max_input_chars", 100),
    ("repoguardian_review_synthesis_max_tokens", -1),
    ("repoguardian_review_synthesis_output_tokens", 0),
    ("repoguardian_review_synthesis_timeout_seconds", 0),
    ("repoguardian_review_synthesis_max_cost_microusd", -1),
])
def test_synthesis_configuration_rejects_invalid_limits(name, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{name: value})
