from app.review.findings import finding_digest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_publication import validated_findings


def test_normalized_confirmed_finding_is_rebuilt_from_candidate_and_evidence(normalized_run):
    run, artifacts = normalized_run
    accepted, withheld = validated_findings(run, artifacts)
    assert [item.id for item in accepted] == ["finding-candidate"]
    assert withheld == []
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.report_version
    assert [item.finding_key for item in plan.inline_drafts] == ["finding-candidate"]
    assert plan.outcome.publishable_count == 1


def test_tampered_finding_is_withheld_instead_of_published(normalized_run):
    run, artifacts = normalized_run
    run.report.findings[0].impact = "forged"
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == []
    assert withheld == [("candidate", "finding_fact_mismatch")]


def test_report_regeneration_changes_plan_version_and_hash(normalized_run):
    run, artifacts = normalized_run
    first = PublicationPlanner(artifacts).build(run)
    run.report.reasons.append("regenerated")
    from app.review.planner import identity
    run.report.version = identity(run.report.model_dump())
    second = PublicationPlanner(artifacts).build(run)
    assert first.report_version != second.report_version
    assert first.model_dump(mode="json") != second.model_dump(mode="json")


def test_publisher_rejects_confirmed_finding_with_failed_admission(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    candidate.admission_reason = "missing_confirmed_fields"

    accepted, withheld = validated_findings(run, artifacts)

    assert accepted == []
    assert withheld == [("candidate", "candidate_admission_invalid")]


def test_publisher_rechecks_confirmed_fields_and_attribution_sources(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    finding = run.report.findings[0]

    candidate.verification = candidate.verification.model_copy(update={"reason": ""})
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == []
    assert withheld == [("candidate", "verification_admission_invalid")]

    candidate.verification = candidate.verification.model_copy(update={
        "reason": "Head-only evidence on a modified file",
        "source_refs": [candidate.verification_sources[1].id],
    })
    candidate.verification_sources = [candidate.verification_sources[1]]
    finding.sources = list(candidate.verification_sources)
    finding.admission_digest = finding_digest(candidate, run.snapshot)
    accepted, withheld = validated_findings(run, artifacts)

    assert accepted == []
    assert withheld == [("candidate", "verification_admission_invalid")]


def test_publisher_uses_server_resolved_sources_not_model_context_handles(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    finding = run.report.findings[0]
    candidate.proposal = candidate.proposal.model_copy(update={
        "source_refs": ["reviewer-head-handle"],
    })
    candidate.verification = candidate.verification.model_copy(update={
        "source_refs": ["verifier-base-handle", "verifier-head-handle"],
    })
    finding.admission_digest = finding_digest(candidate, run.snapshot)

    accepted, withheld = validated_findings(run, artifacts)

    assert [item.id for item in accepted] == [finding.id]
    assert withheld == []
