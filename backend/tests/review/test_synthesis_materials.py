"""Replay frozen PR materials without running target code or a live model."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.review.diff import ChangedFile
from app.models.publication import (
    ChangeUnderstanding, MergeDecision, PublicationPlan, ReviewDigest, ReviewOutcome,
)
from app.review.evidence import EvidenceArchive, digest
from app.review.models import AgentResult, ReviewReport, ReviewSynthesis
from app.review.synthesis import validate_synthesis
from app.review.synthesis_materials import MATERIAL_BYTES, omitted_changes, synthesis_materials
from app.services.publication.review_presentation import _change_overview, build_presentation, render_presentation
from app.services.review.artifact_store import LocalArtifactStore


@pytest.fixture
def frozen_pr(tmp_path):
    raw = json.loads((Path(__file__).parent / "fixtures/onpar_95_synthesis.json").read_text(encoding="utf-8"))
    report = ReviewReport(snapshot=raw["snapshot"], review_units=raw["review_units"],
        assignments=[], goals=[], discovery_results=[], verification_results=[],
        candidates=[], decisions=[], findings=[], complete=True, termination="completed")
    plan = SimpleNamespace(review_id=report.snapshot.review_id,
        snapshot=SimpleNamespace(head_sha=report.snapshot.head_sha, effective_base_sha=report.snapshot.base_sha),
        outcome=SimpleNamespace(model_dump=lambda **_: {}),
        digest=SimpleNamespace(merge_decision=SimpleNamespace(model_dump=lambda **_: {})))
    archive = EvidenceArchive(report.snapshot, LocalArtifactStore(tmp_path))
    changes = [ChangedFile.model_validate(item) for item in raw["changed_files"]]
    return report, plan, archive, changes, raw


def materials(frozen_pr):
    report, plan, archive, changes, _ = frozen_pr
    return synthesis_materials(report, plan, [], changed_files=changes, evidence=archive)


def test_real_pr_raw_diff_supports_brief_overview_without_observations(frozen_pr):
    _, material = materials(frozen_pr)
    change_facts = {key: value for key, value in material["facts"].items() if value["kind"] == "change"}
    assert len(change_facts) == 2
    implementation = next(key for key, value in change_facts.items() if value["category"] == "implementation")
    tests = next(key for key, value in change_facts.items() if value["category"] == "test_source")
    assert "lo = min(lo, point)" in json.dumps(change_facts[implementation])
    assert "test_the_posterior_point_estimate_lies_inside_its_own_interval" in json.dumps(change_facts[tests])
    assert material["notes"] == [] and len(json.dumps(material, ensure_ascii=False).encode()) < MATERIAL_BYTES
    output = {"change_overview": [
        {"source_ids": [implementation], "text": "调整 `weighted_posterior` 的区间边界，使其包含点估计，且不收窄区间。"},
        {"source_ids": [tests], "text": "补充边界参数化测试和中间值对照断言。"}]}
    assert validate_synthesis(output, material)
    partial = {"change_overview": [output["change_overview"][0]]}
    assert omitted_changes(validate_synthesis(partial, material), material) == [
        {"path": change_facts[tests]["path"], "hunk_index": None, "reason": "editorial_omission"}]
    assert sum(len(item["text"]) for item in output["change_overview"]) < material["target_characters"]
    assert sum(map(len, frozen_pr[-1]["previous_overview"])) > material["target_characters"]
    # Length is an editorial target, not another all-or-nothing semantic gate.
    output["change_overview"][0]["text"] = "调整边界；" * 80
    assert validate_synthesis(output, material)


def test_partial_material_keeps_other_changes_and_records_omitted_hunks(frozen_pr):
    report, plan, archive, changes, _ = frozen_pr
    implementation = changes[0]
    duplicate = implementation.hunks[0].model_copy(deep=True)
    duplicate.lines[0].content = "large source " * 5000
    implementation.hunks.append(duplicate)
    _, material = materials(frozen_pr)
    ids = [key for key, value in material["facts"].items() if value["kind"] == "change"]
    output = validate_synthesis({"change_overview": [{"source_ids": ids, "text": "调整区间并补充边界断言。"}]}, material)
    omissions = omitted_changes(output, material)
    assert {"path": implementation.file_path, "reason": "material_limit", "hunk_index": 1} in omissions
    assert len(material["facts"]) == 2
    assert report.complete and report.termination == "completed"
    first_version, _ = materials(frozen_pr)
    implementation.hunks[-1].lines[0].content += "modified even while omitted"
    assert materials(frozen_pr)[0] != first_version


def test_change_material_version_is_independent_of_usage_and_verdict_projection(frozen_pr):
    before, _ = materials(frozen_pr)
    plan = frozen_pr[1]
    plan.outcome.model_dump = lambda **_: {"runtime_reliability": "unknown", "unknown_calls": 1}
    plan.digest.merge_decision.model_dump = lambda **_: {"advice": "计量未知，请结合运行记录判断"}
    assert materials(frozen_pr)[0] == before


@pytest.mark.parametrize("side", ["head_sha", "effective_base_sha"])
def test_materials_reject_cross_snapshot_pairing(frozen_pr, side):
    setattr(frozen_pr[1].snapshot, side, "0" * 40)
    with pytest.raises(ValueError, match="synthesis_snapshot_mismatch"):
        materials(frozen_pr)


@pytest.mark.parametrize("case", ["sensitive", "sensitive_rename", "symlink", "outside"])
def test_frozen_materials_do_not_bypass_path_or_file_policy(frozen_pr, case):
    report, _, _, changes, _ = frozen_pr
    for change in changes:
        for hunk in change.hunks:
            for line in hunk.lines:
                line.content = "PRIVATE_SOURCE_MUST_NOT_BE_SENT"
    if case == "sensitive":
        for change in changes:
            change.file_path = ".env"
    elif case == "sensitive_rename":
        for change in changes:
            change.old_file_path = ".env"
    elif case == "symlink":
        for change in changes:
            change.new_mode = "120000"
    else:
        for change in changes:
            change.file_path = "../outside.py"
    report.review_units[0].changed_paths = [change.file_path for change in changes]
    _, material = materials(frozen_pr)
    assert "PRIVATE_SOURCE_MUST_NOT_BE_SENT" not in json.dumps(material)
    assert not material["facts"] and material["omitted_changes"]


def test_synthesis_sources_are_verified_original_bytes_and_tampering_changes_version(frozen_pr):
    report, _, archive, _, _ = frozen_pr
    content = "point = weighted_point(usable)\nlo = min(lo, point)\n"
    payload = {"review_id": report.snapshot.review_id, "commit_sha": report.snapshot.head_sha,
        "side": "head", "path": "src/clickllm/prove/stats.py",
        "start_line": 454, "end_line": 455, "content_hash": digest(content), "content": content}
    blob = archive.artifacts.put_text(task_id=report.snapshot.review_id, kind="evidence",
        content=json.dumps(payload, ensure_ascii=False))
    report.discovery_results = [AgentResult(id="reviewer", status="completed", evidence_ids=[blob.sha256])]
    before, material = materials(frozen_pr)
    source = next(item for item in material["facts"].values() if item["kind"] == "source")
    assert source["content"] == content
    assert source["ref"]["side"] == "head"
    # A source-only paragraph cannot turn a current-state claim into a change.
    key = next(key for key, item in material["facts"].items() if item["kind"] == "source")
    with pytest.raises(ValueError, match="synthesis_change_source_missing"):
        validate_synthesis({"change_overview": [{"source_ids": [key], "text": "新增中间值测试。"}]}, material)
    path = archive.artifacts.root / report.snapshot.review_id / f"evidence-{blob.sha256}.txt"
    path.write_text("corrupted", encoding="utf-8")
    after, material = materials(frozen_pr)
    assert before != after and material["omitted"]["sources"] == 1
    assert not any(item["kind"] == "source" for item in material["facts"].values())


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_change_overview_paragraphs_and_code_survive_without_allowing_links_html_or_mentions(newline):
    text = f"修复 `low <= point` 边界。{newline}{newline}补充测试。 [点击](https://evil.test) <script>x</script> @someone"
    presentation = SimpleNamespace(change_summary=text, summary_source="model", test_changes=[], purpose_text=None)
    rendered = _change_overview(presentation)
    assert "`low <= point`" in rendered and "\n\n补充测试" in rendered
    assert r"\[点击\]" in rendered and "https&#58;" in rendered
    assert "<script>" not in rendered and "@someone" not in rendered
    assert "&#64;someone" in rendered


@pytest.mark.parametrize("copy", [False, True])
def test_saved_two_paragraph_overview_survives_complete_comment_projection(frozen_pr, copy):
    report, _, archive, changes, _ = frozen_pr
    verdict = MergeDecision(status="conditional_merge", label="未发现已确认问题", advice="结合 CI 判断。")
    plan = PublicationPlan(review_id=report.snapshot.review_id,
        snapshot={"head_sha": report.snapshot.head_sha, "effective_base_sha": report.snapshot.base_sha},
        outcome=ReviewOutcome(kind="clean", coverage="complete",
            finding_conclusion="no_confirmed_findings", examined_goals=4, total_goals=4),
        digest=ReviewDigest(merge_decision=verdict, change_understanding=ChangeUnderstanding(
            diff_summary="本次变更涉及2个文件。")))
    version, material = synthesis_materials(report, plan, [], changed_files=changes, evidence=archive)
    primary = next(key for key, fact in material["facts"].items()
                   if fact["kind"] == "change" and fact["category"] == "implementation")
    tests = next(key for key, fact in material["facts"].items()
                 if fact["kind"] == "change" and fact["category"] == "test_source")
    first = "调整 `weighted_posterior` 的区间边界，使其包含点估计；向外扩展边界，不收窄原区间。"
    second = "补充边界参数化测试和中间值对照断言。"
    output = validate_synthesis({"change_overview": [
        {"source_ids": [primary], "text": first}, {"source_ids": [tests], "text": second},
    ]}, material)
    report.synthesis = ReviewSynthesis(status="ready", material_version=version, output=output)
    # Exercise persistence -> version revalidation -> overview -> full Markdown.
    reopened = ReviewReport.model_validate_json(report.model_dump_json())
    plan.presentation = build_presentation(plan, report=reopened, observations=[],
        changed_files=changes, evidence=archive)
    assert plan.presentation.synthesis_status == "ready"
    comment = render_presentation(plan, copy=copy)
    assert first + "\n\n" + second in comment
    assert comment.count("已检查范围内未发现已确认问题") == 1
