from app.graph.nodes.review import _build_enhanced_diff
from app.models.review import ChangedFile
from app.review.language_rules import build_language_context, render_language_rule_context
from app.services.review_planner import DeterministicReviewPlanner


def test_language_rules_are_matched_per_file_in_mixed_unit() -> None:
    context = build_language_context(
        ["frontend/src/user.ts", "backend/app/user.py", "README.md"],
        project_meta={
            "language": "typescript",
            "languages": ["typescript", "python"],
            "framework": "vue",
            "is_mixed_language": True,
        },
    )

    assert context["languages"] == ["typescript", "python"]
    assert context["is_mixed_language_repository"] is True
    assert [item["id"] for item in context["rule_packs"]] == [
        "review.language.typescript",
        "review.language.python",
    ]
    rendered = render_language_rule_context(context)
    assert "Detected framework: vue" in rendered
    assert "不安全断言" in rendered


def test_enhanced_diff_uses_dynamic_fence_and_injects_typescript_rules() -> None:
    enhanced = _build_enhanced_diff({
        "changed_files": [{"file_path": "src/user.ts"}],
        "file_index": [{"path": "src/user.ts", "language": "typescript"}],
        "project_meta": {
            "language": "typescript",
            "languages": ["typescript"],
            "framework": "vue",
        },
        "context_snippets": [{
            "file": "src/user.ts",
            "start_line": 1,
            "end_line": 2,
            "relevance": "direct",
            "content": "export const user = undefined",
        }],
        "diff_text": "diff --git a/src/user.ts b/src/user.ts",
    })

    assert "review.language.typescript" in enhanced
    assert "```typescript" in enhanced
    assert "```python" not in enhanced


def test_planner_records_language_rule_id_on_review_unit() -> None:
    plan = DeterministicReviewPlanner().plan(
        [ChangedFile(
            file_path="src/user.ts",
            change_type="modified",
            additions=1,
            deletions=0,
        )],
        base_sha="base",
        head_sha="head",
        file_index=[{"path": "src/user.ts", "language": "typescript"}],
    )

    assert "review.language.typescript" in plan.review_units[0].rule_ids
    assert "review.language.typescript" in plan.matched_rules
