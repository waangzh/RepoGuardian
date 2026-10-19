import re
from pathlib import Path

from app.main import app
from app.models.review import ReviewMode, ReviewPreviewResponse, ReviewTask, ReviewUnitResult
from app.models.review_runtime import TaskRuntimeReport


FRONTEND_TYPES = Path(__file__).parents[2] / "frontend" / "src" / "types" / "review.ts"


def _interface_body(source: str, name: str) -> str:
    match = re.search(rf"export interface {name} \{{(?P<body>[\s\S]*?)\n\}}", source)
    assert match is not None, f"frontend interface {name} is missing"
    return match.group("body")


def test_frontend_response_interfaces_cover_backend_contract() -> None:
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    for model in (ReviewPreviewResponse, ReviewUnitResult, ReviewTask, TaskRuntimeReport):
        body = _interface_body(source, model.__name__)
        missing = [name for name in model.model_fields if re.search(rf"\b{name}\??\s*:", body) is None]
        assert not missing, f"{model.__name__} missing frontend fields: {missing}"


def test_openapi_exposes_preview_and_retry_contracts() -> None:
    schema = app.openapi()
    assert "/api/reviews/preview" in schema["paths"]
    assert "/api/reviews/{task_id}/files/{file_id}/context" in schema["paths"]
    assert "/api/reviews/{task_id}/units/{unit_id}/retry" in schema["paths"]
    assert "/api/validation-requests/{request_id}/claim" in schema["paths"]
    assert "/api/validation-requests/{request_id}/result" in schema["paths"]
    assert "/api/validation-requests/{request_id}/cancel" in schema["paths"]
    assert "/api/runners/register" in schema["paths"]
    preview = schema["components"]["schemas"]["ReviewPreviewResponse"]
    assert {
        "mode", "changed_file_count", "included_file_count", "changed_files",
        "review_units", "planning_model_calls", "estimated_model_calls",
        "max_model_calls", "patch_generation_enabled", "validation_backend",
    } <= set(preview["properties"])


def test_review_mode_enum_is_kept_in_frontend_contract() -> None:
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    declaration = re.search(r"export type ReviewMode = (?P<body>[^;]+);", source)
    assert declaration is not None
    frontend_values = set(re.findall(r'"([^"]+)"', declaration.group("body")))
    assert frontend_values == {item.value for item in ReviewMode}


def test_publication_interfaces_cover_backend_fields() -> None:
    from app.models.publication import (PublicationPlan, PublicationFinding, PublicationAnchor,
        PublicationSnapshot, ReviewOutcome, WithheldFinding, PublicationAttempt, PublicationItem,
        PublicationSelection, PublicationPreflightResult, PublicationAccepted, ReviewDigest, DigestGoal, DigestExcludedFile, ReviewCommentCheck,
        DigestInvestigation, DigestIntent, DigestValidation, MergeDecision, ReviewCommentOptions)
    source = FRONTEND_TYPES.with_name("publication.ts").read_text(encoding="utf-8")
    for model in (PublicationPlan, PublicationFinding, PublicationAnchor, PublicationSnapshot, ReviewOutcome, WithheldFinding,
                  PublicationAttempt, PublicationItem, PublicationSelection, PublicationPreflightResult, PublicationAccepted,
                  ReviewDigest, DigestGoal, DigestExcludedFile, ReviewCommentCheck, DigestInvestigation,
                  DigestIntent, DigestValidation, MergeDecision, ReviewCommentOptions):
        body = _interface_body(source, model.__name__)
        assert all(re.search(rf"\b{name}\??\s*:", body) for name in model.model_fields)


def test_browser_publication_fixtures_reproduce_backend_plan() -> None:
    import json
    from app.services.publication_planner import PublicationPlanner
    path = FRONTEND_TYPES.parents[2] / "tests" / "fixtures" / "publication-plans.json"
    for fixture in json.loads(path.read_text(encoding="utf-8")).values():
        task = ReviewTask.model_validate(fixture["task"])
        assert PublicationPlanner().build(task).model_dump(mode="json") == fixture["plan"]


def test_browser_copy_variants_reproduce_backend_selection():
    import json
    from app.services.publication_planner import PublicationPlanner
    from app.services.publication_renderer import comment_selection, render_review_comment, render_finding_comment

    folder = FRONTEND_TYPES.parents[2] / "tests" / "fixtures"
    fixtures = json.loads((folder / "publication-plans.json").read_text(encoding="utf-8"))
    variants = json.loads((folder / "review-comment-options.json").read_text(encoding="utf-8"))
    for name, fixture in fixtures.items():
        task = ReviewTask.model_validate(fixture["task"])
        base = PublicationPlanner().build(task)
        saved = {item.id: {"current": {"status": "resolved", "reason": "合成验收：人工标记已处理"}}
                 for item in base.digest.findings}
        plan = PublicationPlanner().build(task, dispositions=saved)
        for key, expected in variants[name].items():
            detail, exclude, *identity = key.split(":", 2)
            if identity:
                findings = [*plan.inline_drafts, *plan.summary_findings]
                selected = next(item for item in findings if item.finding_key == identity[0])
                included, omitted, retained = [selected], [item for item in findings if item != selected], []
                markdown = render_finding_comment(plan, selected)
            else:
                included, omitted, retained = comment_selection(plan, exclude_resolved=exclude == "true")
                markdown = render_review_comment(plan, detail=detail, exclude_resolved=exclude == "true")
            assert expected == {"detail": detail,
                "review_comment_markdown": markdown,
                "included_finding_keys": [item.finding_key for item in included],
                "omitted_finding_keys": [item.finding_key for item in omitted], "retained_blocking_keys": retained}


def test_public_finding_schema_and_frontend_fields_are_explicit():
    from app.models.review_plan import FindingLocation, FindingVerification, FindingView
    source = FRONTEND_TYPES.with_name("reviewPlan.ts").read_text(encoding="utf-8")
    for model in (FindingView, FindingLocation, FindingVerification):
        body = _interface_body(source, model.__name__)
        assert all(re.search(rf"\b{name}\??\s*:", body) for name in model.model_fields)
    schema = app.openapi()["components"]["schemas"]
    assert set(schema["FindingView"]["properties"]["review_status"]["enum"]) == {
        "candidate", "confirmed", "unresolved", "dismissed", "unknown"}
    assert schema["FindingLocation"]["properties"]["side"]["enum"] == ["left", "right"]


def test_quick_preview_has_a_separate_non_plan_contract():
    from app.models.quick_preview import QuickPreviewResponse, QuickPreviewFile
    source = FRONTEND_TYPES.with_name("quickPreview.ts").read_text(encoding="utf-8")
    for model, name in ((QuickPreviewResponse, "QuickPreview"), (QuickPreviewFile, "QuickPreviewFile")):
        body = _interface_body(source, name)
        assert all(re.search(rf"\b{field}\??\s*:", body) for field in model.model_fields)
    schema = app.openapi()
    assert "/api/reviews/preview/quick" in schema["paths"]
    properties = schema["components"]["schemas"]["QuickPreviewResponse"]["properties"]
    assert "preview_id" not in properties and "plan_hash" not in properties
