import pytest

from app.review.findings import finding_severity
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_publication import validated_findings
from app.services.review.review_findings import finding_views
from tests.review.test_finding_aggregation import EquivalentReviewModel
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]


@pytest.mark.parametrize("different_attributions", [False, True])
async def test_aggregated_issue_revalidates_all_members_and_preserves_triggers(
    pipeline, different_attributions,
):
    service, repository, _, _, _ = pipeline
    service._provider.agent_model = lambda *args, **kwargs: EquivalentReviewModel(
        different_attributions=different_attributions,
    )
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    task = repository.get_task(task.id)
    report = task.review_report
    assert len(report.findings) == 1 and len(report.candidates) == 2
    accepted, withheld = validated_findings(task, repository._artifacts)
    assert len(accepted) == 1 and not withheld
    assert finding_severity(accepted[0]) == "high"
    views = finding_views(task, {}, repository._artifacts)
    assert len(views) == 1 and views[0].review_status == "confirmed" and views[0].severity == "high"
    assert views[0].attribution == "introduced"
    assert len(views[0].source_ids) == 1
    assert "event one" in views[0].message and "event two" in views[0].message
    plan = PublicationPlanner(repository._artifacts).build(task)
    drafts = [*plan.inline_drafts, *plan.summary_findings]
    assert len(drafts) == 1 and drafts[0].severity == "high"
    assert drafts[0].attribution == "introduced"
    assert "event one" in drafts[0].failure_scenario and "event two" in drafts[0].failure_scenario
    assert plan.outcome.confirmed_count == plan.outcome.publishable_count == 1
    assert len(plan.digest.merge_decision.blocking_finding_keys) == 1

    for mutation in ("missing_member", "different_subject", "missing_reciprocal", "duplicate_member"):
        changed = task.model_copy(deep=True)
        target = changed.review_report.findings[0]
        member = target.related_candidate_ids[0]
        if mutation == "missing_member":
            target.related_candidate_ids = ["invented"]
        elif mutation == "different_subject":
            next(item for item in changed.review_report.candidates if item.id == member).title = "tampered"
        elif mutation == "missing_reciprocal":
            next(item for item in changed.review_report.verification_results if item.id == member).output[
                "equivalent_candidate_ids"] = []
        else:
            target.related_candidate_ids.append(member)
        accepted, withheld = validated_findings(changed, repository._artifacts)
        assert not accepted and withheld, mutation

    missing = task.model_copy(deep=True)
    missing.review_report.findings = []
    accepted, withheld = validated_findings(missing, repository._artifacts)
    assert not accepted and withheld

    extra = task.model_copy(deep=True)
    extra.review_report.findings[0].related_candidate_ids = []
    accepted, withheld = validated_findings(extra, repository._artifacts)
    assert not accepted and withheld
