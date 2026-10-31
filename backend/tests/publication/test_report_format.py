from app.review.findings import finding_digest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.publication_renderer import overview_markdown, render_summary


def test_summary_fallback_keeps_the_verifier_comment(normalized_run):
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    finding = plan.inline_drafts[0].model_copy(update={"placement": "summary"})
    plan.inline_drafts, plan.summary_findings = [], [finding]
    assert finding.body_markdown in render_summary(plan)


def test_exact_duplicate_does_not_become_a_false_withheld_gap(normalized_run):
    run, artifacts = normalized_run
    duplicate = run.report.candidates[0].model_copy(deep=True)
    duplicate.id = "another-candidate"
    duplicate.finding.id = "finding-another-candidate"
    duplicate.finding.candidate_id = duplicate.id
    duplicate.finding.admission_digest = finding_digest(duplicate, run.snapshot)
    run.report.candidates.append(duplicate)
    plan = PublicationPlanner(artifacts).build(run)
    assert len(plan.inline_drafts) == 1
    assert plan.withheld_findings == []


def test_withheld_confirmed_result_cannot_produce_clean_conclusion(normalized_run):
    run, artifacts = normalized_run
    run.report.findings[0].admission_digest = "0" * 64
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.withheld_findings
    assert plan.outcome.kind == "inconclusive"


def test_overview_preserves_inline_code_but_escapes_other_markdown():
    rendered = overview_markdown(
        "调整 `weighted_posterior`，避免 <script>alert(1)</script> 和 *伪加粗*。")
    assert rendered == (
        r"调整 `weighted_posterior`，避免 &lt;script&gt;alert\(1\)&lt;/script&gt; "
        r"和 \*伪加粗\*。"
    )


def test_overview_escapes_mentions_links_fences_and_markdown_block_markers():
    text = "@reviewer [link](javascript:alert(1)) ![img](https://example.test)\n- list\n1. item\n```html\n<img>\n```"
    rendered = overview_markdown(text)
    assert "@reviewer" not in rendered
    assert "＠reviewer" in rendered
    assert r"\[link\]\(javascript\:alert\(1\)\)" in rendered
    assert r"\!\[img\]" in rendered
    assert "\n\\- list\n1\\. item" in rendered
    assert "```" not in rendered
    assert "<img>" not in rendered


def test_omission_notice_reaches_comment_and_exact_ranges_remain_in_report(normalized_run):
    from app.review.domain import OverviewOmission
    run, artifacts = normalized_run
    run.report.overview.omitted = {"changes": 1, "sources": 2}
    run.report.overview.omitted_materials = [OverviewOmission(kind="change", unit_id="unit",
        member_id="hunk", path="safe.py", side="head", start_line=1, end_line=3,
        reason="material_capacity")]
    plan = PublicationPlanner(artifacts).build(run)
    assert "总览输入有材料省略" in plan.review_comment_markdown
    assert "safe.py HEAD 1–3：材料容量不足" in plan.user_report_markdown
    assert plan.overview_omitted == {"changes": 1, "sources": 2}
