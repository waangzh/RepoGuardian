from app.services.publication.publication_planner import PublicationPlanner, summary_projection
from app.services.publication.publication_renderer import render_review_comment, render_summary, render_user_report


def test_thin_publisher_formatting_uses_only_approved_facts(normalized_run):
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    texts = [render_summary(plan), render_review_comment(plan), render_user_report(run, plan)]
    for text in texts:
        assert "Value regresses" in text
        assert "Goal" not in text
        assert "Assignment" not in text
    summary = summary_projection(plan)
    assert summary.report_version == plan.report_version
    assert summary.content_eligible


def test_partial_run_with_unresolved_candidate_is_not_clean(normalized_run):
    run, artifacts = normalized_run
    run.status = "partial"
    candidate = run.report.candidates[0]
    candidate.verification = candidate.verification.model_copy(update={
        "verdict": "insufficient", "severity": None,
        "author_comment": None, "attribution": "uncertain",
    })
    candidate.finding = None
    run.report.findings = []

    plan = PublicationPlanner(artifacts).build(run)
    summary = render_summary(plan)

    assert plan.outcome.kind == "inconclusive"
    assert plan.outcome.unresolved_count == 1
    assert "结论不完整" in summary
    assert "存在 1 个未决问题" in summary
    assert plan.outcome.kind != "clean"


def test_refuted_candidate_is_not_unresolved(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    candidate.verification = candidate.verification.model_copy(update={
        "verdict": "refuted", "severity": None,
        "author_comment": None, "attribution": "pre_existing",
    })
    candidate.finding = None
    run.report.findings = []

    plan = PublicationPlanner(artifacts).build(run)

    assert plan.outcome.unresolved_count == 0
    assert plan.outcome.kind == "clean"
