from app.review.findings import finding_digest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.publication_renderer import render_summary


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
