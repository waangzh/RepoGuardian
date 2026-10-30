import subprocess
from datetime import timedelta

from app.review.contracts import ReviewRequest
from app.review.domain import utcnow
from app.services.review.incremental_webhook import IncrementalWebhookService, verify_signature
from tests.review.test_api import service_pipeline as service_pipeline


def commit(github, path, content):
    target = github.source_repo / path
    target.write_text(content, encoding="utf-8")
    for args in (("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.test",
                                "commit", "-m", "next")):
        subprocess.run(["git", "-C", str(github.source_repo), *args], check=True, capture_output=True)
    github.pr.head.sha = subprocess.run(["git", "-C", str(github.source_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True).stdout.decode().strip()


async def test_document_update_reuses_delivered_source_and_rechecks_history(service_pipeline):
    service, github, _ = service_pipeline
    first = await service.create(ReviewRequest(pr_url=github.pr.html_url), "first")
    assert await service._worker.run_once()
    first = service.store.get(first.id)
    commit(github, "README.md", "# Ratio API\n\nUse a nonzero divisor.\n")
    second = await service.create(ReviewRequest(pr_url=github.pr.html_url), "next")
    assert second.plan.incremental_from == first.id
    assert any(unit.reused_from for unit in second.plan.units)
    assert second.plan.history[0]["needs_verification"]
    assert await service._worker.run_once()
    second = service.store.get(second.id)
    assert second.status == "completed", second.report
    reused = next(work for work in second.report.units if work.reused_from)
    assert reused.status == "completed" and reused.attempt == 0
    assert len(second.report.findings) == 1
    candidate = second.report.candidates[0]
    assert candidate.previous_candidate_id == first.report.candidates[0].id
    assert candidate.verification is not None
    assert {source.review_id for source in candidate.verification_sources} == {second.id}
    assert second.report.overview.status == "ready"
    from app.services.publication.publication_planner import PublicationPlanner
    plan = PublicationPlanner(service._artifacts).build(second)
    assert not plan.inline_drafts  # Rechecked historical claim never invents a new line anchor.
    assert len(plan.summary_findings) == 1
    assert plan.historical_findings[0]["rechecked"]


async def test_source_changes_and_explicit_rereview_get_full_pass(service_pipeline):
    service, github, _ = service_pipeline
    first = await service.create(ReviewRequest(pr_url=github.pr.html_url), "first")
    assert await service._worker.run_once()
    same = await service.create(ReviewRequest(pr_url=github.pr.html_url), "explicit-rereview")
    assert same.id != first.id
    assert same.plan.incremental_reason == "explicit_same_snapshot_review"
    assert not any(unit.reused_from for unit in same.plan.units)
    commit(github, "a.py", "def ratio(x):\n    return 20 / x\n")
    changed = await service.create(ReviewRequest(pr_url=github.pr.html_url), "changed")
    assert changed.plan.incremental_reason == "source_change_requires_full_review"
    assert not any(unit.reused_from for unit in changed.plan.units)
    assert service.store.get(same.id).status == "cancelled"
    assert service.store.get(same.id).error_code == "review_superseded"


async def test_baseline_change_does_not_reuse_old_scope(service_pipeline):
    service, github, _ = service_pipeline
    await service.create(ReviewRequest(pr_url=github.pr.html_url), "first")
    assert await service._worker.run_once()
    github.pr.base.sha = github.pr.head.sha
    commit(github, "README.md", "new baseline\n")
    second = await service.create(ReviewRequest(pr_url=github.pr.html_url), "base-changed")
    assert second.plan.incremental_reason == "baseline_or_configuration_changed"
    assert not any(unit.reused_from for unit in second.plan.units)


def payload(github):
    return {"action": "synchronize", "repository": {"id": 1, "full_name": "o/r"},
        "pull_request": {"number": 1, "state": "open", "merged": False, "draft": False,
            "html_url": github.pr.html_url, "base": {"sha": github.pr.base.sha, "repo": {"id": 1}},
            "head": {"sha": github.pr.head.sha, "repo": {"id": 1}}}}


async def test_webhook_delivery_and_head_idempotency_with_rate_limit(service_pipeline, monkeypatch):
    from app.core.config import settings
    service, github, _ = service_pipeline
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_enabled", True)
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_repositories", "o/r")
    monkeypatch.setattr(settings, "repoguardian_incremental_review_max_runs_per_hour", 1)
    webhook = IncrementalWebhookService(service, service.store)
    first = await webhook.handle(payload=payload(github), delivery_id="delivery-1")
    repeated = await webhook.handle(payload=payload(github), delivery_id="delivery-1")
    new_delivery = await webhook.handle(payload=payload(github), delivery_id="delivery-2")
    assert first.status == repeated.status == new_delivery.status == "completed"
    assert first.task_id == repeated.task_id == new_delivery.task_id
    commit(github, "README.md", "next head\n")
    limited = await webhook.handle(payload=payload(github), delivery_id="delivery-3")
    assert limited.reason == "review_frequency_limited"
    assert limited.status == "skipped"
    assert service.store.webhook_count("o/r", 1) == 1
    assert service.store.get(first.task_id).status == "cancelled"


def test_webhook_signature_remains_required(monkeypatch):
    import hashlib
    import hmac
    from app.core.config import settings
    monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", "fixture-secret")
    body = b'{"action":"synchronize"}'
    signature = "sha256=" + hmac.new(b"fixture-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature)
    assert not verify_signature(body + b" ", signature)


async def test_retry_does_not_refresh_expired_deadline(service_pipeline):
    from app.models.orm import ReviewTaskOrm
    service, github, _ = service_pipeline
    first = await service.create(ReviewRequest(pr_url=github.pr.html_url), "first")
    assert await service._worker.run_once()
    with service.store.sessions.begin() as session:
        row = session.get(ReviewTaskOrm, first.id)
        data = dict(row.task_snapshot)
        data["hard_deadline"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.task_snapshot = data
    import pytest
    with pytest.raises(ValueError, match="retry_unavailable"):
        await service.retry(first.id, overview=True)
