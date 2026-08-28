from datetime import datetime, timezone

import pytest

from app.graph.review_graph import build_review_graph
from app.models.review import (
    AgentActionName,
    ReviewFileStatus,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitTerminalReason,
)
from app.services.review_manifest import build_review_manifest


def test_production_review_graph_contains_only_static_readonly_critical_path() -> None:
    nodes = set(build_review_graph(phase=2).compile().get_graph().nodes)

    assert {"review_plan", "review_units", "resolve_evidence", "issue_policy",
            "issue_verifier", "issue_deduplication", "report", "complete"} <= nodes
    assert nodes.isdisjoint({
        "verification", "repair_graph", "static_analysis", "test",
        "generate_patch", "apply_patch", "run_tests",
    })


def test_agent_action_type_does_not_expose_execution_capabilities() -> None:
    assert {item.value for item in AgentActionName}.isdisjoint({
        "run_static_analysis",
        "generate_patch",
        "apply_patch",
        "run_tests",
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


def test_review_manifest_marks_split_file_partial_unless_all_units_complete() -> None:
    completed_at = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    units = [
        ReviewUnit(
            id=unit_id,
            primary_files=["big_service.py"],
            estimated_tokens=100,
            complexity=ReviewUnitComplexity.large,
            fingerprint=unit_id,
            grouping_reason="large_file_hunk_split",
        )
        for unit_id in ("foo", "bar", "baz")
    ]
    state = {
        "task_id": "review-split",
        "created_at": completed_at,
        "review_plan": {"changed_files": [{
            "file_path": "big_service.py",
            "change_type": "modified",
            "additions": 30,
            "deletions": 10,
            "included": True,
        }]},
        "review_units": [unit.model_dump(mode="json") for unit in units],
        "review_unit_results": [
            ReviewUnitResult(
                review_unit_id="foo",
                status=ReviewUnitStatus.completed,
                terminal_reason=ReviewUnitTerminalReason.completed,
            ).model_dump(mode="json"),
            ReviewUnitResult(
                review_unit_id="bar",
                status=ReviewUnitStatus.timed_out,
                terminal_reason=ReviewUnitTerminalReason.timed_out,
                error="timeout",
            ).model_dump(mode="json"),
            ReviewUnitResult(
                review_unit_id="baz",
                status=ReviewUnitStatus.failed,
                terminal_reason=ReviewUnitTerminalReason.provider_error,
                error="provider down",
            ).model_dump(mode="json"),
        ],
    }

    coverage = build_review_manifest(state, completed_at).coverage

    assert coverage.files[0].status == ReviewFileStatus.partial
    assert coverage.reviewed_files == 0
    assert coverage.partial_files == 1
    assert coverage.coverage_rate == 0
    assert coverage.completed_units == 1
    assert coverage.total_units == 3
    assert coverage.unit_coverage_rate == pytest.approx(1 / 3)


def test_review_manifest_exposes_budget_exhausted_terminal_reason() -> None:
    completed_at = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    unit = ReviewUnit(
        id="budget",
        primary_files=["service.py"],
        estimated_tokens=100,
        complexity=ReviewUnitComplexity.small,
        fingerprint="budget",
        grouping_reason="single_file",
    )
    state = {
        "task_id": "review-budget",
        "created_at": completed_at,
        "review_plan": {"changed_files": [{
            "file_path": "service.py",
            "change_type": "modified",
            "additions": 1,
            "deletions": 0,
            "included": True,
        }]},
        "review_units": [unit.model_dump(mode="json")],
        "review_unit_results": [ReviewUnitResult(
            review_unit_id="budget",
            status=ReviewUnitStatus.completed,
            terminal_reason=ReviewUnitTerminalReason.model_budget_exhausted,
        ).model_dump(mode="json")],
    }

    coverage = build_review_manifest(state, completed_at).coverage

    assert coverage.files[0].status == ReviewFileStatus.budget_exhausted
    assert coverage.units[0].terminal_reason == ReviewUnitTerminalReason.model_budget_exhausted
    assert coverage.completed_units == 0
    assert coverage.unit_coverage_rate == 0
