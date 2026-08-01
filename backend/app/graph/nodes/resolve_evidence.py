"""使用 diff、工作树与 symbol index 确定性解析模型证据。"""

from pathlib import Path
from typing import Any

from app.evidence import DiffIndex, EvidenceResolver
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import (
    ReviewIssue,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
)
from app.tools.git_tool import GitTool


async def resolve_evidence_node(state: ReviewState) -> ReviewState:
    changed = state.get("changed_files") or []
    diff_index = DiffIndex(changed)
    git_tool: Any = state.get("_git_tool") or GitTool()
    repo_path = state.get("repo_path") or ""
    base_sha = state.get("base_sha") or ""

    def load_file(file_path: str, side: str) -> str | None:
        if side == "base":
            reader = getattr(git_tool, "get_file_content_at_revision", None)
            return reader(repo_path, base_sha, file_path) if reader else None
        full_path = Path(repo_path) / file_path
        if not full_path.is_file():
            return None
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None if "\x00" in content else content

    repository_files = {
        str(item.get("path")) for item in state.get("file_index") or [] if item.get("path")
    }
    for item in changed:
        if item.get("file_path"):
            repository_files.add(str(item["file_path"]))
        if item.get("old_file_path"):
            repository_files.add(str(item["old_file_path"]))
    resolver = EvidenceResolver(
        diff_index,
        file_loader=load_file,
        symbol_index=state.get("symbol_index") or [],
        repository_files=repository_files,
    )
    units = [ReviewUnit.model_validate(item) for item in state.get("review_units") or []]
    by_unit = {unit.id: unit for unit in units}
    if not units:
        primary_files = [str(item.get("file_path")) for item in changed if item.get("file_path")]
        fallback = ReviewUnit(
            id="unassigned",
            primary_files=primary_files or ["unknown"],
            related_files=sorted(repository_files - set(primary_files)),
            estimated_tokens=0,
            complexity=ReviewUnitComplexity.small,
            fingerprint="unassigned",
            grouping_reason="legacy_review_path",
        )
        by_unit[fallback.id] = fallback

    resolved: list[ReviewIssue] = []
    for raw in state.get("review_issues") or []:
        issue = ReviewIssue.model_validate(raw)
        unit = by_unit.get(issue.review_unit_id)
        if unit is None:
            unit = next(iter(by_unit.values()))
            issue = issue.model_copy(update={"review_unit_id": unit.id})
        resolved.append(resolver.resolve_issue(issue, unit))

    resolved_by_id = {issue.id: issue for issue in resolved}
    unit_results: list[dict] = []
    for raw in state.get("review_unit_results") or []:
        result = ReviewUnitResult.model_validate(raw)
        unit_results.append(result.model_copy(update={
            "issues": [resolved_by_id.get(issue.id, issue) for issue in result.issues]
        }).model_dump(mode="json"))

    located = sum(1 for issue in resolved if issue.primary_evidence.resolved_start_line)
    return ReviewState(
        status="resolving_evidence",
        review_issues=[issue.model_dump(mode="json") for issue in resolved],
        review_unit_results=unit_results,
        step_progress=append_step(
            state,
            "resolve_evidence",
            "completed",
            f"已确定性定位 {located}/{len(resolved)} 个问题的主要证据",
        ),
    )
