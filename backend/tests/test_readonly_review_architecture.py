from datetime import datetime, timezone

from app.graph.review_graph import build_review_graph
from app.models.review import ReviewUnit, ReviewUnitComplexity, ReviewUnitResult, ReviewUnitStatus
from app.services.review_manifest import build_review_manifest


def test_production_review_graph_contains_only_static_readonly_critical_path() -> None:
    nodes = set(build_review_graph(phase=2).compile().get_graph().nodes)

    assert {"review_plan", "review_units", "resolve_evidence", "issue_policy",
            "issue_verifier", "issue_deduplication", "report", "complete"} <= nodes
    assert nodes.isdisjoint({
        "verification", "repair_graph", "static_analysis", "test",
        "generate_patch", "apply_patch", "run_tests",
    })


def test_review_manifest_reports_partial_unit_failure_without_losing_coverage() -> None:
    completed = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    units = [
        ReviewUnit(
            id="ok", primary_files=["a.py"], estimated_tokens=100,
            complexity=ReviewUnitComplexity.small, fingerprint="ok", grouping_reason="single",
        ),
        ReviewUnit(
            id="failed", primary_files=["b.py"], estimated_tokens=100,
            complexity=ReviewUnitComplexity.small, fingerprint="failed", grouping_reason="single",
        ),
    ]
    state = {
        "task_id": "review-1",
        "created_at": "2026-08-28T02:59:00+00:00",
        "review_plan": {
            "planner_version": "test",
            "changed_files": [
                {"file_path": "a.py", "change_type": "modified", "additions": 1,
                 "deletions": 0, "included": True},
                {"file_path": "b.py", "change_type": "modified", "additions": 1,
                 "deletions": 0, "included": True},
                {"file_path": "asset.bin", "change_type": "modified", "additions": 0,
                 "deletions": 0, "included": False, "excluded_reason": "binary_file"},
            ],
        },
        "review_units": [item.model_dump(mode="json") for item in units],
        "review_unit_results": [
            ReviewUnitResult(
                review_unit_id="ok", status=ReviewUnitStatus.completed
            ).model_dump(mode="json"),
            ReviewUnitResult(
                review_unit_id="failed", status=ReviewUnitStatus.failed, error="provider down"
            ).model_dump(mode="json"),
        ],
        "warnings": ["1 个 Unit 失败"],
    }

    manifest = build_review_manifest(state, completed)

    assert manifest.coverage.changed_files == 3
    assert manifest.coverage.eligible_files == 2
    assert manifest.coverage.reviewed_files == 1
    assert manifest.coverage.failed_files == 1
    assert manifest.coverage.skipped_files == 1
    assert manifest.coverage.coverage_rate == 0.5
    assert manifest.duration_ms == 60_000
    assert manifest.warnings == ["1 个 Unit 失败"]
