import pytest
from types import SimpleNamespace

from app.models.review.diff import ChangedFile
from app.models.review_plan import ReviewPlanOptions
from app.review.findings import aggregate_findings, candidate_identity, finding_severity, verify_finding
from app.review.models import AgentResult, CandidateFinding, ReviewReport, VerificationResult
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_publication import validated_findings
from app.services.review.review_findings import finding_views
from tests.review.test_finding_aggregation import EquivalentReviewModel
from tests.review.test_native_lifecycle import preview_and_create
from tests.review.test_native_graph import CHANGE

pytest_plugins = ["tests.review.test_native_lifecycle"]


@pytest.fixture
async def comparison_task(source_service):
    head = await source_service.read("safe.py", 1, 1)
    base = await source_service.read("safe.py", 1, 1, "base")
    evidence_ids = [head["evidence"]["id"], base["evidence"]["id"]]
    candidates = []
    for index in range(12):
        candidate = CandidateFinding(id="pending", assignment_ids=["reviewer"], title=f"claim {index}",
            trigger="trigger", expected_behavior="expected", actual_behavior="actual", impact="impact",
            recommendation="repair", severity="high", affected_path="safe.py", line=1,
            evidence_ids=evidence_ids, attribution="introduced")
        candidate.id = candidate_identity(candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda item: item.id)
    members = candidates[-2:]
    results, decisions, findings = [], [], []
    for candidate in members:
        peer = next(item.id for item in members if item.id != candidate.id)
        result = AgentResult(id=candidate.id, execution_id="batch", status="completed",
            evidence_ids=evidence_ids, comparison_candidate_ids=[peer], output={
                "decision": "confirmed", "explanation": "same causal failure", "evidence_ids": evidence_ids,
                "attribution": "introduced", "severity": "high", "equivalent_candidate_ids": [peer],
                "equivalence_reason": "same mechanism and repair"})
        decision, finding = verify_finding(candidate, result.model_dump(), source_service, [CHANGE],
                                          [item.model_dump() for item in members])
        results.append(result)
        decisions.append(decision)
        findings.append(finding)
    task = SimpleNamespace(id="review", snapshot=SimpleNamespace(
        head_sha=source_service.snapshot.head_sha, effective_base_sha=source_service.snapshot.base_sha),
        pr=SimpleNamespace(owner="local", repo="repo", number=1),
        configuration=SimpleNamespace(options=ReviewPlanOptions()),
        changed_files=[ChangedFile.model_validate({**CHANGE, "additions": 1, "deletions": 1})],
        review_report=ReviewReport(snapshot=source_service.snapshot, assignments=[], goals=[],
            candidates=members, decisions=decisions, findings=aggregate_findings(findings),
            verification_results=results,
            discovery_results=[AgentResult(id="reviewer", status="completed", evidence_ids=evidence_ids)],
            complete=True, termination="completed"))
    return task, candidates[:-2], source_service.artifacts


@pytest.mark.parametrize("extra_kind", ["gate_rejected", "later_eligible", "quick_excluded"])
async def test_publication_keeps_recorded_scope_when_candidate_pool_changes(comparison_task, extra_kind):
    task, extras, artifacts = comparison_task
    assert validated_findings(task, artifacts) == (task.review_report.findings, [])
    if extra_kind == "quick_excluded":
        task.configuration.options.depth = "quick"
    for candidate in extras:
        if extra_kind == "gate_rejected":
            candidate.evidence_ids = ["unissued"]
        elif extra_kind == "quick_excluded":
            candidate.severity = "medium"
            candidate.id = candidate_identity(candidate)
        task.review_report.candidates.append(candidate)
        task.review_report.decisions.append(VerificationResult(candidate_id=candidate.id,
            decision="insufficient_evidence", explanation="not confirmed", attribution="uncertain"))
    # JSON persistence must retain the execution's scope, not recalculate the cap.
    task.review_report = ReviewReport.model_validate_json(task.review_report.model_dump_json())
    assert validated_findings(task, artifacts) == (task.review_report.findings, [])


async def test_publication_uses_investigator_scope_not_original_batch(comparison_task):
    task, _, artifacts = comparison_task
    original = task.review_report.verification_results[0]
    investigation = original.model_copy(deep=True, update={"execution_id": None})
    original.output = {"decision": "insufficient_evidence", "explanation": "missing premise",
                       "attribution": "uncertain"}
    original.comparison_candidate_ids = []
    task.review_report.verification_results.append(investigation)
    assert validated_findings(task, artifacts) == (task.review_report.findings, [])


@pytest.mark.parametrize("mutation", ["scope_missing", "peer_evidence_invalid"])
async def test_publication_rejects_equivalence_outside_recorded_or_valid_scope(comparison_task, mutation):
    task, _, artifacts = comparison_task
    if mutation == "scope_missing":
        task.review_report.verification_results[0].comparison_candidate_ids = []
    else:
        task.review_report.candidates[1].evidence_ids = ["unissued"]
    accepted, withheld = validated_findings(task, artifacts)
    assert not accepted and withheld


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
