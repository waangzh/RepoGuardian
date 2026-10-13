from app.models.review import (
    ContextSnippet,
    UnitContractDependency,
    UnitReviewRecord,
)
from app.services.review_input_context import build_context_evidence, make_evidence
from app.services.unit_review_summary import build_record_input, validate_record
from test_unit_review_summary import _file


def test_derived_prior_diff_evidence_can_verify_cross_batch_dependency() -> None:
    files = [_file()]
    original = make_evidence(
        "a.py", "diff", 1, 1, "prior implementation", "head", "base"
    )
    context = ContextSnippet(
        file="a.py",
        start_line=1,
        end_line=1,
        content="prior implementation",
        relevance="contract",
        source="batch_diff_support",
        derived_from_evidence_id=original["id"],
    )
    chunks, refs = build_context_evidence(
        [context.model_dump(mode="json")], "head", "base"
    )
    record_input = build_record_input(files, chunks, None, "head", "base")
    record_input["prior_evidence_bindings"] = [
        {key: value for key, value in original.items() if key != "content"}
    ]
    evidence_id = refs[0]["id"]
    summary = validate_record(
        UnitReviewRecord(
            change_summary="核验跨批次契约",
            target_checks=[],
            contract_dependencies=[
                UnitContractDependency(
                    file_path="a.py",
                    symbol="weighted_posterior",
                    assumption="实现与测试使用同一边界契约",
                    status="verified",
                    evidence_ids=[evidence_id],
                )
            ],
        ),
        record_input,
        {"a.py"},
    )

    assert summary.status == "reported", summary.reason
    assert summary.record.contract_dependencies[0].status == "verified"
