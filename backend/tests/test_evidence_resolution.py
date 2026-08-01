from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.evidence import DiffIndex, EvidenceResolver
from app.models.review import (
    ChangedFile,
    EvidenceAnchor,
    ReviewIssue,
    ReviewUnit,
    ReviewUnitComplexity,
)
from app.tools.diff_parser import DiffParser


def _diff(
    path: str = "app.py",
    *,
    old_path: str | None = None,
    hunk: str = "@@ -1,2 +1,2 @@\n keep\n-old\n+new",
    extra: str = "",
) -> str:
    old = old_path or path
    return (
        f"diff --git a/{old} b/{path}\n"
        f"{extra}"
        f"--- a/{old}\n+++ b/{path}\n{hunk}\n"
    )


def _unit(primary: list[str] | None = None, related: list[str] | None = None) -> ReviewUnit:
    return ReviewUnit(
        id="unit-1",
        primary_files=primary or ["app.py"],
        related_files=related or [],
        estimated_tokens=0,
        complexity=ReviewUnitComplexity.small,
        fingerprint="fingerprint",
        grouping_reason="test",
    )


def _issue(anchor: dict, *, supporting: list[dict] | None = None) -> ReviewIssue:
    return ReviewIssue(
        id="issue-1",
        review_unit_id="unit-1",
        title="问题",
        category="correctness",
        severity="high",
        confidence=0.9,
        affected_behavior="行为会发生错误。",
        failure_scenario="输入触发错误结果。",
        recommendation="修复该逻辑。",
        primary_evidence=anchor,
        supporting_evidence=supporting or [],
    )


def _resolver(diff_text: str, files: dict[tuple[str, str], str], symbols=None) -> EvidenceResolver:
    parsed = DiffParser().parse(diff_text)
    return EvidenceResolver(
        DiffIndex(parsed),
        file_loader=lambda path, side: files.get((path, side)),
        symbol_index=symbols or [],
        repository_files={path for path, _ in files},
    )


def test_head_diff_new_side_exact_match_is_inline() -> None:
    resolver = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n"})
    result = resolver.resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "new"}), _unit()
    )
    assert result.primary_evidence.resolved_start_line == 2
    assert result.primary_evidence.resolution_method == "diff_exact"
    assert result.primary_evidence.resolved_side == "head"
    assert result.placement == "inline"


def test_base_diff_deleted_side_exact_match_is_inline() -> None:
    resolver = _resolver(_diff(), {("app.py", "base"): "keep\nold\n"})
    result = resolver.resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "old", "expected_side": "base"}),
        _unit(),
    )
    assert result.primary_evidence.resolved_start_line == 2
    assert result.primary_evidence.resolved_side == "base"
    assert result.placement == "inline"


def test_crlf_is_matched_only_by_normalized_diff_pass() -> None:
    text = _diff(hunk="@@ -1 +1 @@\r\n-old\r\n+new\r\n")
    result = _resolver(text, {("app.py", "head"): "new\r\n"}).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "new"}), _unit()
    )
    assert result.primary_evidence.resolution_method == "diff_normalized"


def test_leading_and_trailing_whitespace_is_normalized() -> None:
    result = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n"}).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "   new   "}), _unit()
    )
    assert result.primary_evidence.resolution_method == "diff_normalized"


def test_multiple_matches_are_not_auto_selected() -> None:
    text = _diff(hunk="@@ -1,2 +1,2 @@\n-old\n+same\n-old2\n+same")
    result = _resolver(text, {("app.py", "head"): "same\nsame\n"}).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "same"}), _unit()
    )
    assert result.primary_evidence.resolution_method == "unresolved"
    assert result.primary_evidence.match_count == 2
    assert result.placement == "needs_human"


def test_missing_code_is_unresolved_and_suppressed() -> None:
    result = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n"}).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "missing"}), _unit()
    )
    assert result.primary_evidence.resolution_method == "unresolved"
    assert result.unresolved_reason == "code_not_found"
    assert result.placement == "suppressed"


def test_suggested_code_cannot_masquerade_as_existing_evidence() -> None:
    issue = _issue({"file_path": "app.py", "existing_code": "new"})
    issue = issue.model_copy(update={"recommendation": "new"})
    result = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n"}).resolve_issue(
        issue, _unit()
    )
    assert result.placement == "suppressed"
    assert result.unresolved_reason == "existing_code_matches_recommendation"


def test_specified_hunk_has_priority_over_other_hunks() -> None:
    text = _diff(
        hunk=(
            "@@ -1 +1 @@\n-old\n+same\n"
            "@@ -10 +10 @@\n-old2\n+same"
        )
    )
    parsed = DiffParser().parse(text)
    target = parsed[0].hunks[1].hunk_id
    resolver = EvidenceResolver(
        DiffIndex(parsed),
        file_loader=lambda path, side: "same\n" + "x\n" * 8 + "same\n",
        repository_files={"app.py"},
    )
    result = resolver.resolve_issue(
        _issue({
            "file_path": "app.py",
            "existing_code": "same",
            "expected_hunk_id": target,
        }),
        _unit(),
    )
    assert result.primary_evidence.resolved_start_line == 10
    assert result.primary_evidence.candidate_locations[0].hunk_id == target


def test_full_file_exact_fallback_routes_non_diff_line_to_summary() -> None:
    result = _resolver(_diff(), {("app.py", "head"): "keep\nnew\noutside\n"}).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "outside"}), _unit()
    )
    assert result.primary_evidence.resolution_method == "file_exact"
    assert result.primary_evidence.resolved_start_line == 3
    assert result.placement == "summary"


def test_symbol_range_disambiguates_full_file_matches() -> None:
    files = {("app.py", "head"): "duplicate\nother\nduplicate\n"}
    symbols = [{"file": "app.py", "symbol": "target", "start_line": 3, "end_line": 3}]
    result = _resolver(_diff(), files, symbols).resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "duplicate", "symbol": "target"}),
        _unit(),
    )
    assert result.primary_evidence.resolution_method == "symbol_assisted"
    assert result.primary_evidence.resolved_start_line == 3


def test_renamed_file_supports_old_base_and_new_head_paths() -> None:
    text = _diff(
        "new.py",
        old_path="old.py",
        extra="similarity index 90%\nrename from old.py\nrename to new.py\n",
    )
    resolver = _resolver(
        text,
        {("old.py", "base"): "keep\nold\n", ("new.py", "head"): "keep\nnew\n"},
    )
    base = resolver.resolve_anchor(EvidenceAnchor(
        file_path="old.py", existing_code="old", expected_side="base"
    ))
    head = resolver.resolve_anchor(EvidenceAnchor(file_path="new.py", existing_code="new"))
    assert base.resolved_start_line == head.resolved_start_line == 2
    assert {base.resolved_side, head.resolved_side} == {"base", "head"}


def test_deleted_file_can_resolve_base_but_head_request_reports_side_conflict() -> None:
    text = (
        "diff --git a/app.py b/app.py\n"
        "deleted file mode 100644\n--- a/app.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old\n"
    )
    resolver = _resolver(text, {("app.py", "base"): "old\n"})
    base = resolver.resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "old", "expected_side": "base"}),
        _unit(),
    )
    conflict = resolver.resolve_issue(
        _issue({"file_path": "app.py", "existing_code": "old", "expected_side": "head"}),
        _unit(),
    )
    assert base.primary_evidence.resolved_side == "base"
    assert conflict.unresolved_reason == "side_conflict"
    assert conflict.placement == "needs_human"


def test_primary_evidence_cannot_target_related_only_file() -> None:
    resolver = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n", ("related.py", "head"): "proof\n"})
    result = resolver.resolve_issue(
        _issue({"file_path": "related.py", "existing_code": "proof"}),
        _unit(related=["related.py"]),
    )
    assert result.placement == "suppressed"
    assert result.unresolved_reason == "primary_evidence_not_in_primary_files"


def test_supporting_evidence_may_target_related_file() -> None:
    resolver = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n", ("related.py", "head"): "proof\n"})
    result = resolver.resolve_issue(
        _issue(
            {"file_path": "app.py", "existing_code": "new"},
            supporting=[{"file_path": "related.py", "existing_code": "proof"}],
        ),
        _unit(related=["related.py"]),
    )
    assert result.placement == "inline"
    assert result.supporting_evidence[0].resolved_start_line == 1


def test_anchor_hash_is_stable_and_side_sensitive() -> None:
    resolver = _resolver(
        _diff(hunk="@@ -1 +1 @@\n-same\n+same"),
        {("app.py", "head"): "same\n", ("app.py", "base"): "same\n"},
    )
    head_anchor = EvidenceAnchor(file_path="app.py", existing_code="same", expected_side="head")
    first = resolver.resolve_anchor(head_anchor)
    second = resolver.resolve_anchor(head_anchor)
    base = resolver.resolve_anchor(EvidenceAnchor(
        file_path="app.py", existing_code="same", expected_side="base"
    ))
    assert first.anchor_hash == second.anchor_hash
    assert first.anchor_hash != base.anchor_hash


def test_server_overwrites_model_like_resolved_lines() -> None:
    anchor = EvidenceAnchor(
        file_path="app.py",
        existing_code="new",
        resolved_start_line=999,
        resolved_end_line=999,
        resolution_method="file_exact",
        match_count=1,
        anchor_hash="model-value",
    )
    resolved = _resolver(_diff(), {("app.py", "head"): "keep\nnew\n"}).resolve_anchor(anchor)
    assert resolved.resolved_start_line == 2
    assert resolved.anchor_hash != "model-value"


def test_illegal_path_is_rejected_by_strict_model() -> None:
    with pytest.raises(ValidationError):
        EvidenceAnchor(file_path="../secret.py", existing_code="secret")


def test_binary_file_cannot_build_text_anchor() -> None:
    index = DiffIndex([ChangedFile(
        file_path="image.png",
        change_type="modified",
        additions=0,
        deletions=0,
        is_binary=True,
    )])
    resolver = EvidenceResolver(
        index,
        file_loader=lambda path, side: "not really text",
        repository_files={"image.png"},
    )
    result = resolver.resolve_anchor(EvidenceAnchor(file_path="image.png", existing_code="text"))
    assert result.resolution_method == "unresolved"
    assert result.unresolved_reason == "binary_file"
