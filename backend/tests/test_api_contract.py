import re
from pathlib import Path

from app.main import app
from app.models.review import ReviewMode, ReviewPreviewResponse, ReviewTask, ReviewUnitResult


FRONTEND_TYPES = Path(__file__).parents[2] / "frontend" / "src" / "types" / "review.ts"


def _interface_body(source: str, name: str) -> str:
    match = re.search(rf"export interface {name} \{{(?P<body>[\s\S]*?)\n\}}", source)
    assert match is not None, f"frontend interface {name} is missing"
    return match.group("body")


def test_frontend_response_interfaces_cover_backend_contract() -> None:
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    for model in (ReviewPreviewResponse, ReviewUnitResult, ReviewTask):
        body = _interface_body(source, model.__name__)
        missing = [name for name in model.model_fields if re.search(rf"\b{name}\??\s*:", body) is None]
        assert not missing, f"{model.__name__} missing frontend fields: {missing}"


def test_openapi_exposes_preview_and_retry_contracts() -> None:
    schema = app.openapi()
    assert "/api/reviews/preview" in schema["paths"]
    assert "/api/reviews/{task_id}/units/{unit_id}/retry" in schema["paths"]
    assert "/api/validation-requests/{request_id}/claim" in schema["paths"]
    assert "/api/validation-requests/{request_id}/result" in schema["paths"]
    assert "/api/validation-requests/{request_id}/cancel" in schema["paths"]
    assert "/api/runners/register" in schema["paths"]
    preview = schema["components"]["schemas"]["ReviewPreviewResponse"]
    assert {
        "mode", "changed_file_count", "included_file_count", "changed_files",
        "review_units", "patch_generation_enabled", "validation_backend",
    } <= set(preview["properties"])


def test_review_mode_enum_is_kept_in_frontend_contract() -> None:
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    declaration = re.search(r"export type ReviewMode = (?P<body>[^;]+);", source)
    assert declaration is not None
    frontend_values = set(re.findall(r'"([^"]+)"', declaration.group("body")))
    assert frontend_values == {item.value for item in ReviewMode}
