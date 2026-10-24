import difflib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.models.review import PullRequestInfo, ReviewPlan
from app.models.review_synthesis import ReviewSynthesisArtifact, ReviewSynthesisDraft
from app.core.fingerprints import stable_hash
from app.services.publication.publication_planner import PublicationPlanner
from app.services.review.review_synthesis_input import SOURCE_POLICY_VERSION, build_synthesis_input
from app.services.review.review_synthesis_repository import SynthesisStore
from app.services.review.review_synthesis_service import bound_synthesis, freeze_review_synthesis
from app.tools.diff_parser import DiffParser
from test_incremental_review import PR_INFO, _previous, _result, _unit
from tests.support.persistence import persistence as persistence
from test_review_synthesis import draft_for, stub_provider, synthesis_task


FIXTURE = Path(__file__).parent / "fixtures" / "review_synthesis_sources_v2.json"


def legacy_case():
    saved = json.loads(FIXTURE.read_text(encoding="utf-8"))
    task = synthesis_task()
    task.id = saved["facts"]["review_id"]
    task.changed_files = DiffParser().parse(saved["patch"])
    return task, saved


def parsed_task(*, context=80, count=120, changed_count=1, content=None, path="src/a.py"):
    task = synthesis_task()
    before = [f"value_{index:03d} = stable({index})\n" for index in range(count)]
    after = before.copy()
    start = count // 2
    after[start:start + changed_count] = [
        (content or f"value_{index:03d} = updated({index})") + "\n"
        for index in range(changed_count)]
    patch = "".join(difflib.unified_diff(
        before, after, fromfile="a/" + path, tofile="b/" + path, n=context))
    task.changed_files = DiffParser().parse(patch)
    return task


def diff_sources(facts):
    return [source for source in facts.sources
            if source.kind in {"diff_observation", "test_source_observation"}]


def test_real_parser_input_is_compact_and_retains_both_sides():
    task = parsed_task(context=3)
    hunk = task.changed_files[0].hunks[0]
    assert hunk.added_lines and hunk.removed_lines
    original = hunk.model_dump(mode="json")
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    source = diff_sources(facts)[0]
    excerpt = json.loads(source.text)
    assert "hunk" not in excerpt and "added_lines" not in excerpt and "removed_lines" not in excerpt
    assert excerpt["original_range"]["old_start"] == hunk.old_start
    assert excerpt["omitted_context_lines"] == excerpt["omitted_changed_lines"] == 0
    assert [line["content"] for line in excerpt["retained_lines"] if line["kind"] == "added"] == [
        line.content for line in hunk.lines if line.kind == "added"]
    assert [line["old_line_no"] for line in excerpt["retained_lines"] if line["kind"] == "deleted"] == [
        line.old_line_no for line in hunk.lines if line.kind == "deleted"]
    assert not facts.input_truncated
    assert hunk.model_dump(mode="json") == original
    legacy = build_synthesis_input(task, PublicationPlanner().build(task),
                                   source_policy_version="review-synthesis-sources-v2")
    assert len(source.text) < len(diff_sources(legacy)[0].text)
    assert source.id == diff_sources(legacy)[0].id


def test_trimmed_context_sets_flag_without_counting_hunk_as_omitted():
    task = parsed_task()
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    excerpt = json.loads(diff_sources(facts)[0].text)
    assert facts.input_truncated and facts.truncation_reasons == ["diff_hunk_truncated"]
    assert facts.omitted_files == facts.omitted_hunks == 0
    assert excerpt["omitted_context_lines"] > 0 and excerpt["omitted_changed_lines"] == 0
    assert {line["kind"] for line in excerpt["retained_lines"]} >= {"added", "deleted"}


def test_large_change_keeps_complete_lines_and_discloses_missing_changes():
    task = parsed_task(count=300, changed_count=100, content='updated("\\\\\\u4e2d")')
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    source = diff_sources(facts)[0]
    excerpt = json.loads(source.text)
    originals = [line.model_dump(mode="json", exclude_none=True)
                 for line in task.changed_files[0].hunks[0].lines]
    assert len(source.text) <= 6000 and facts.input_truncated
    assert excerpt["omitted_changed_lines"] > 0
    assert excerpt["retained_lines"] and all(line in originals for line in excerpt["retained_lines"])
    assert facts.omitted_hunks == 0


def test_low_total_budget_preserves_diff_before_author_and_stays_bounded():
    task = parsed_task(count=200, changed_count=20)
    task.task_runtime_configuration["output_policy"]["max_input_chars"] = 2000
    task.pr = PullRequestInfo.model_validate(PR_INFO)
    task.pr.body = "untrusted author context " * 200
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    assert diff_sources(facts)
    assert [source.kind for source in facts.sources[:2]] == ["review_boundary", "diff_observation"]
    assert facts.input_truncated and "input_size_limited" in facts.truncation_reasons
    assert len(json.dumps(facts.model_dump(mode="json"), ensure_ascii=False)) <= 2000
    assert all(len(source.text) <= 6000 for source in facts.sources)


def test_unrepresentable_hunk_does_not_hide_other_hunks_in_same_file():
    task = parsed_task(context=3)
    good = task.changed_files[0].hunks[0].model_copy(deep=True)
    task.changed_files[0].hunks[0].lines[0].kind = "added"
    task.changed_files[0].hunks[0].lines[0].content = "x" * 6200
    task.changed_files[0].hunks.append(good)
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    assert len(diff_sources(facts)) == 1
    assert facts.omitted_hunks == 1 and facts.omitted_files == 0 and facts.input_truncated


def test_multiple_files_share_total_budget_and_report_exact_omissions():
    task = parsed_task(context=0)
    original = task.changed_files[0]
    task.changed_files = [
        original.model_copy(update={"file_path": f"src/file_{index:02d}.py"}, deep=True)
        for index in range(20)]
    task.task_runtime_configuration["output_policy"]["max_input_chars"] = 2000
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    included = {json.loads(source.text)["path"] for source in diff_sources(facts)}
    assert included and len(included) < 20
    assert facts.omitted_files == facts.omitted_hunks == 20 - len(included)
    assert facts.input_truncated and "input_size_limited" in facts.truncation_reasons
    assert len(json.dumps(facts.model_dump(mode="json"), ensure_ascii=False)) <= 2000


@pytest.mark.parametrize("old_path,allowed", [("src/old.py", True), (".env", False), ("../private.py", False)])
def test_rename_excerpt_preserves_old_path_and_path_policy(old_path, allowed):
    task = parsed_task(context=3)
    task.changed_files[0].old_file_path = old_path
    task.changed_files[0].change_type = "renamed"
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    if allowed:
        assert json.loads(diff_sources(facts)[0].text)["old_path"] == old_path
    else:
        assert not diff_sources(facts) and facts.omitted_files == 1
        assert old_path not in json.dumps(facts.model_dump(mode="json"))


@pytest.mark.parametrize("version", ["review-synthesis-sources-v1", "review-synthesis-sources-v2"])
@pytest.mark.parametrize("status", ["ready", "fallback"])
def test_prechange_fixture_stays_bound_and_only_projection_corrects_flag(version, status):
    task, saved = legacy_case()
    plan = PublicationPlanner().build(task)
    assert build_synthesis_input(task, plan, source_policy_version=version).model_dump(mode="json") == saved["facts"]
    artifact = deepcopy(saved["artifact"])
    artifact["source_policy_version"] = version
    if version.endswith("v1"):
        old_facts = {key: value for key, value in saved["facts"].items() if key != "truncation_reasons"}
        old_facts["schema_version"] = "review-synthesis-input-v1"
        artifact.update(schema_version="review-synthesis-artifact-v1", fact_bundle_hash=stable_hash(old_facts))
    if status == "fallback":
        artifact.update(status="fallback", synthesis=None, failure_code="disabled")
    task.review_synthesis = deepcopy(artifact)
    bound = bound_synthesis(task, plan)
    assert bound is not None and bound.status == status and bound.input_truncated
    assert bound.fact_bundle_hash == artifact["fact_bundle_hash"]
    assert task.review_synthesis == artifact and not artifact["input_truncated"]
    assert build_synthesis_input(task, plan).model_dump(mode="json") != saved["facts"]


@pytest.mark.parametrize("field,value", [
    ("input_truncated", True), ("omitted_files", 1), ("truncation_reasons", []),
    ("fact_bundle_hash", "0" * 64), ("source_policy_version", "future"),
])
def test_legacy_projection_does_not_bypass_tamper_checks(field, value):
    task, saved = legacy_case()
    task.review_synthesis = deepcopy(saved["artifact"])
    task.review_synthesis[field] = value
    assert bound_synthesis(task, PublicationPlanner().build(task)) is None


async def test_legacy_reads_correct_projection_without_rewriting_or_sending(persistence):
    repo, _, _ = persistence
    task, saved = legacy_case()
    repo.create_task(task)
    store = SynthesisStore(repo, task.id)
    artifact = ReviewSynthesisArtifact.model_validate(saved["artifact"])
    assert store.claim(artifact)[0]
    store.finish(artifact)
    provider, calls = stub_provider()
    for _ in range(2):
        restored = repo.get_task(task.id)
        assert bound_synthesis(restored, PublicationPlanner().build(restored)).input_truncated
        assert not restored.review_synthesis["input_truncated"]
        # This fixture has no publishable findings in its changed range.
        assert await freeze_review_synthesis(restored, repo, provider) is None
    assert calls == []


async def test_eligible_legacy_generation_reentry_reuses_claim_without_sending(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan, source_policy_version="review-synthesis-sources-v2")
    artifact = ReviewSynthesisArtifact(
        prompt_version="review-synthesis-prompt-v5", review_id=task.id,
        head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")), status="ready",
        synthesis=ReviewSynthesisDraft.model_validate(draft_for(facts)),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons)
    repo.create_task(task)
    store = SynthesisStore(repo, task.id)
    assert store.claim(artifact)[0]
    store.finish(artifact)
    provider, calls = stub_provider()
    restored = repo.get_task(task.id)
    again = await freeze_review_synthesis(restored, repo, provider)
    assert again.source_policy_version == "review-synthesis-sources-v2"
    assert again.fact_bundle_hash == artifact.fact_bundle_hash
    assert calls == []


async def test_missing_observation_falls_back_without_sending(persistence):
    repo, _, _ = persistence
    task = parsed_task(count=1, content="x" * 6200)
    # Both sides must be unrepresentable, including the deleted line.
    for line in task.changed_files[0].hunks[0].lines:
        line.content = "x" * 6200
    repo.create_task(task)
    provider, calls = stub_provider()
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.failure_code == "insufficient_input"
    assert artifact.source_policy_version == SOURCE_POLICY_VERSION and calls == []


def test_prepared_cache_does_not_cross_diff_policy(monkeypatch):
    from app.services.preview import preview_input_cache as cache

    task = synthesis_task()
    task.pr = PullRequestInfo.model_validate(PR_INFO)
    monkeypatch.setattr(cache, "parser_fingerprint", lambda: "fixture")
    configuration = {"call_resolution_version": "fixture"}
    current = cache.input_cache_key(task.pr, configuration)
    monkeypatch.setattr(cache, "DIFF_CONTEXT_LINES", 80)
    assert cache.input_cache_key(task.pr, configuration) != current
    monkeypatch.setattr(cache, "DIFF_CONTEXT_LINES", 3)
    monkeypatch.setattr(cache, "DIFF_POLICY_VERSION", "merge-base-v1")
    assert cache.input_cache_key(task.pr, configuration) != current


def test_changed_hunk_identity_prevents_incremental_goal_reuse():
    from app.review.runtime_goals import plan_runtime_goals
    from app.services.review.incremental_review import reusable_units

    previous = _previous([_unit("ru-a", ["a.py"])], [_result("ru-a")])
    wide = parsed_task(context=80).changed_files
    compact = parsed_task(context=3).changed_files
    for file in [*wide, *compact]:
        file.file_path = "a.py"
    previous.changed_files = wide
    goals = plan_runtime_goals(wide, ReviewPlan(planner_version="v", review_units=previous.review_units))
    previous.task_runtime_report["initial_scope_coverage"] = [
        {**goal.model_dump(mode="json"), "status": "examined", "result_id": "saved-" + goal.goal_id}
        for goal in goals]
    assert reusable_units(previous, wide, {"unrelated.py"}, previous.review_units)
    assert reusable_units(previous, compact, {"unrelated.py"}, previous.review_units) == []
