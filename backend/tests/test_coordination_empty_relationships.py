"""四文件目录形态回归；模拟规划/核验，不调用真实模型或目标仓库代码。"""

import json

import pytest

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import (
    ChangedFile, ContextSnippet, CrossUnitCoordinationProposal, ReviewUnit,
    ReviewUnitResult, UnitReviewRecord,
)
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.unit_review_summary import build_record_input, validate_record
from test_cross_unit_coordination import state


def document_state():
    value = state()
    contents = {
        ".gitignore": "# existing rules\n" * 34 + "CLAUDE.local.md\nAGENTS.local.md\n",
        "AGENTS.md": "项目开发约定\n",
        "CLAUDE.md": "@AGENTS.md\n",
        "doc/releases/changelog-dev.md": "新增开发指引\n",
    }
    units, files, results = [], [], []
    for index, (path, content) in enumerate(contents.items()):
        lines = content.splitlines()
        file = ChangedFile.model_validate({"file_path": path, "change_type": "added",
            "additions": len(lines), "deletions": 0, "hunks": [{"old_start": 0, "old_length": 0,
                "new_start": 1, "new_length": len(lines), "added_lines": [
                    {"line_no": n, "content": line} for n, line in enumerate(lines, 1)]}]})
        unit = ReviewUnit(id=f"docs-{index}", primary_files=[path], estimated_tokens=10,
            complexity="small", fingerprint=path, grouping_reason="single")
        snippet = ContextSnippet(file=path, start_line=1, end_line=len(lines), content=content, relevance="direct")
        record_input = build_record_input([file], [snippet.model_dump(mode="json")], None, "h", "b")
        summary = validate_record(UnitReviewRecord(change_summary=f"核对 {path}"), record_input, {path})
        results.append(ReviewUnitResult(review_unit_id=unit.id, status="completed", terminal_reason="no_issue",
            context_snippets=[snippet], review_summary=summary).model_dump(mode="json"))
        units.append(unit.model_dump(mode="json"))
        files.append(file.model_dump(mode="json"))
    value.update(review_units=units, changed_files=files, review_unit_results=results,
        file_index=[{"path": path, "analysis_level": 0} for path in contents],
        repository_graph={"files": [{"path": path} for path in contents], "edges": [],
            "metadata": {"file_count": 4, "edge_count": 0}},
        cross_unit_risk={"decision": "uncertain", "index_status": "available", "relationships": [],
            "index_diagnostics": ["symbol_analysis_not_applicable:.gitignore"], "reasons": [{
                "code": "relationship_unknown", "unit_ids": ["docs-0"], "files": [".gitignore"],
                "detail": "此类文件不适用代码符号分析，仍需核验配置或文件引用关系；不代表无风险"}]})
    return value


def document_plan(payload):
    prompt = OpenAICompatibleProvider._build_coordination_prompt(payload)
    example = next(json.loads(line) for line in prompt.splitlines()
        if line.startswith('{"decision"') and 'docs-reference' in line)
    owners = {path: unit["id"] for unit in payload["units"] for path in unit["primary_files"]}
    refs = {item["file_path"]: item["id"] for item in payload["evidence"]}
    encoded = json.dumps(example, ensure_ascii=False)
    for old, new in {"CLAUDE所属UnitID": owners["CLAUDE.md"], "AGENTS所属UnitID": owners["AGENTS.md"],
                     "CLAUDE证据ID": refs["CLAUDE.md"], "AGENTS证据ID": refs["AGENTS.md"]}.items():
        encoded = encoded.replace(old, new)
    return CrossUnitCoordinationProposal.model_validate_json(encoded)


def test_empty_graph_and_obligations_allow_new_document_followup_from_complete_example():
    payload = CrossUnitCoordinationService.catalog(document_state())
    assert len(payload["units"]) == 4
    assert not payload["risk"]["relationships"] and not payload["origin_obligations"]
    assert all("content" not in item for item in payload["evidence"])
    proposed = document_plan(payload)
    normalized, _ = CrossUnitCoordinationService.normalize_plan(proposed, payload)
    assert normalized.relationship_ids == []
    assert normalized.followups[0].origin_obligation_ids == []
    assert normalized.question_states[0].followup_ids == ["docs-reference"]
    assert set(normalized.followups[0].verification_files) == {"AGENTS.md", "CLAUDE.md"}


@pytest.mark.parametrize("missing", ["path", "evidence", "unit"])
def test_empty_graph_does_not_relax_scope_or_reference_checks(missing):
    payload = CrossUnitCoordinationService.catalog(document_state())
    proposed = document_plan(payload)
    request = proposed.followups[0]
    if missing == "path":
        request.supporting_files = ["tool/config.md"]
    elif missing == "evidence":
        request.evidence_ids = ["stale"]
    else:
        request.unit_ids = ["docs-2", "unknown"]
    with pytest.raises(ValueError):
        CrossUnitCoordinationService.validate_plan(proposed, payload)


def test_prompt_distinguishes_capabilities_and_narrow_questions():
    prompt = OpenAICompatibleProvider._build_coordination_prompt({})
    assert "不表示没有关系或不能检查" in prompt
    assert "不是补查的阻碍" in prompt
    assert "补查能够读取所选 Unit 可读范围内的正文" in prompt
    assert "不要求该路径实际存在" in prompt
    assert "确有无法取得的文件、证据或权限才保留 uncertain" in prompt
