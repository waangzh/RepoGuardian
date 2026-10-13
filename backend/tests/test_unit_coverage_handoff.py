from app.models.review import (
    ChangedFile,
    DiffHunk,
    ReviewUnit,
    ReviewUnitComplexity,
    UnitReviewRecord,
)
from app.services.unit_coverage import (
    active_evidence_set,
    build_diff_manifest,
    new_coverage_ledger,
    update_coverage,
)
from app.services.unit_review_summary import build_record_input, validate_record


def test_latest_no_progress_keeps_valid_record_for_next_batch() -> None:
    unit = ReviewUnit(
        id="unit-coverage-handoff",
        primary_files=["src/a.py", "tests/test_a.py"],
        related_files=[],
        complexity=ReviewUnitComplexity.small,
        estimated_tokens=100,
        fingerprint="unit-fingerprint",
        grouping_reason="test",
    )
    changed = [
        ChangedFile(
            file_path="src/a.py",
            change_type="modified",
            additions=1,
            deletions=0,
            hunks=[DiffHunk(old_start=1, old_length=0, new_start=1, new_length=1, content="+value")],
        ),
        ChangedFile(
            file_path="tests/test_a.py",
            change_type="modified",
            additions=1,
            deletions=0,
            hunks=[DiffHunk(old_start=1, old_length=0, new_start=1, new_length=1, content="+assert value")],
        ),
    ]
    manifest = build_diff_manifest(unit, changed, "head", "base")
    active = active_evidence_set(manifest, changed[:1], unit.id)
    request = build_record_input(changed[:1], [], None, "head", "base")
    evidence_id = request["evidence"][0]["id"]
    summary = validate_record(
        UnitReviewRecord(
            change_summary="已核验实现变更",
            target_checks=[{
                "target": "审查 src/a.py 的变更",
                "status": "checked",
                "reason": "已检查",
                "evidence_ids": [evidence_id],
            }],
            unresolved_questions=[{
                "id": "question-tests",
                "question": "尚未核验测试变更",
                "affected_files": ["tests/test_a.py"],
                "evidence_ids": [],
            }],
        ),
        request,
        {"src/a.py", "tests/test_a.py"},
    )
    summary = summary.model_copy(update={
        "latest_attempt_status": "unknown",
        "latest_attempt_reason": "review_step_no_evidence_progress",
        "last_valid_record": summary.record,
    })

    ledger = update_coverage(new_coverage_ledger(manifest), manifest, active, summary)

    assert ledger.targets["审查 src/a.py 的变更"].status == "checked"
    assert any(item.question == "尚未核验测试变更" for item in ledger.questions.values())
