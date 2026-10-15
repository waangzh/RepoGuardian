"""四文件目录形态回归；模拟规划/核验，不调用真实模型或目标仓库代码。"""

import json
from copy import deepcopy

import pytest

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import (
    ChangedFile, ContextSnippet, CrossUnitCoordinationPlan, CrossUnitCoordinationProposal, CrossUnitRiskAssessment, ReviewUnit,
    ReviewUnitResult, UnitReviewPlan, UnitReviewRecord, UnitReviewResponse,
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
    value["cross_unit_risk"] = CrossUnitRiskAssessment.model_validate(value["cross_unit_risk"]).model_dump(mode="json")
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


def verified_document_state():
    value = document_state()
    paths = [".gitignore", "AGENTS.md", "CLAUDE.md"]
    plan = UnitReviewPlan(change_summary="限定模式与文档关系核验", review_objectives=["限定核验"],
        initial_action={"action": "report_issue", "reason": "检查证据"}, risk_hypotheses=[{
            "id": "ignore-docs", "category": "correctness", "priority": "low", "affected_files": paths,
            "description": "本地文件名忽略模式是否同时忽略普通文档路径并破坏引用",
            "completion_criteria": "核对完整忽略规则与两份正文，仅判断这些明确路径的匹配和引用"}])
    raw = value["review_unit_results"][0]
    raw["plan"] = plan.model_dump(mode="json")
    raw["context_snippets"] = [snippet for item in value["review_unit_results"][:3]
        for snippet in item["context_snippets"]]
    value["review_units"][0]["related_files"] = paths[1:]
    record_input = build_record_input([ChangedFile.model_validate(value["changed_files"][0])],
        raw["context_snippets"], plan, "h", "b")
    raw["review_summary"] = validate_record(UnitReviewRecord(change_summary="限定问题已反证",
        hypothesis_checks=[{"hypothesis_id": "ignore-docs", "status": "refuted",
            "reason": "local 模式不匹配普通文档路径；正文引用存在的文档",
            "evidence_ids": [item["id"] for item in record_input["evidence"] if item["source"] == "context"]}]),
        record_input, set(paths)).model_dump(mode="json")
    return value


def skip_plan(payload):
    return CrossUnitCoordinationPlan(decision="skip", reason="当前跨文件反证覆盖限定模式与引用疑点，无新疑点",
        evidence_ids=[item["id"] for item in payload["evidence"]])


@pytest.mark.parametrize("failure", [None, "no_scope", "no_plan", "local_only", "split_same_file", "supported", "invalid",
    "snapshot", "partial", "missing_evidence", "unresolved", "origin", "outside_scope", "required", "legacy"])
def test_empty_relationship_skip_requires_current_bounded_cross_file_counterevidence(failure):
    value = verified_document_state()
    raw = value["review_unit_results"][0]
    if failure == "no_scope":
        value = document_state()
    elif failure == "no_plan":
        raw["plan"] = None
    elif failure in {"local_only", "split_same_file"}:
        raw["plan"]["risk_hypotheses"][0]["affected_files"] = [".gitignore"]
        if failure == "split_same_file":
            value["review_units"].append({**value["review_units"][0], "id": "split-ignore"})
            value["review_unit_results"].append({**raw, "review_unit_id": "split-ignore"})
    elif failure == "supported":
        raw["review_summary"]["record"]["hypothesis_checks"][0]["status"] = "supported"
    elif failure == "invalid":
        raw["review_summary"]["latest_attempt_status"] = "invalid"
    elif failure == "snapshot":
        value["head_sha"] = "other-head"
    elif failure == "partial":
        value["cross_unit_risk"]["index_status"] = "partial"
    elif failure == "outside_scope":
        value["cross_unit_risk"]["reasons"][0]["files"].append("doc/releases/changelog-dev.md")
    elif failure == "required":
        value["cross_unit_risk"]["decision"] = "required"
    payload = CrossUnitCoordinationService.catalog(value)
    proposed = skip_plan(payload)
    if failure == "missing_evidence":
        proposed.evidence_ids = [item["id"] for item in payload["evidence"] if item["file_path"] != "CLAUDE.md"]
    elif failure == "unresolved":
        proposed.unresolved_questions = ["外部工具加载语义尚未核验"]
    elif failure == "origin":
        payload["origin_obligations"] = [{"id": "parent"}]
    elif failure == "legacy":
        for item in payload["summaries"]:
            item["summary"].pop("strict_complete", None)
    if failure:
        with pytest.raises(ValueError):
            CrossUnitCoordinationService.validate_plan(proposed, payload)
    else:
        assert len(payload["verified_scopes"]) == 1
        assert all(item["summary"]["strict_complete"] for item in payload["summaries"])
        CrossUnitCoordinationService.validate_plan(proposed, payload)


def test_split_catalog_preserves_only_complete_verified_scopes():
    from app.services.coordination_catalog import catalog_batches
    payload = CrossUnitCoordinationService.catalog(verified_document_state())

    def split(item):
        if len(item["units"]) > 2:
            raise ValueError("fixture split")

    batches = catalog_batches(payload, split)
    assert len(batches) > 1
    for _, batch in batches:
        assert batch["verified_scopes"] == []  # 三文件反证不能被截成两文件结论。


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["skip", "followups", "unreachable"])
async def test_empty_graph_can_finish_verified_scope_or_followups_and_preserves_real_unknown(action):
    from test_review_step_flow_repairs import StepProvider
    from app.services.report_service import ReportService
    from app.services.review_rebuild import rebuild_task_from_state

    class Provider(StepProvider):
        async def coordinate_cross_units(self, payload, model):
            if action == "skip":
                return skip_plan(payload)
            if action == "unreachable":
                return CrossUnitCoordinationPlan(decision="uncertain", reason="外部工具加载顺序不可读取",
                    unresolved_questions=["缺少工具加载配置；目录不含该授权文件，不能判断加载顺序"])
            proposed = document_plan(payload)
            first = proposed.followups[0]
            ignore_question = "本次忽略模式是否匹配 AGENTS.md 或 CLAUDE.md？"
            proposed.unresolved_questions.append(ignore_question)
            proposed.followups.append(first.model_copy(update={"id": "ignore-patterns", "question": ignore_question,
                "unit_ids": ["docs-0", "docs-1", "docs-2"], "primary_files": [".gitignore"],
                "supporting_files": ["AGENTS.md", "CLAUDE.md"],
                "verification_files": [".gitignore", "AGENTS.md", "CLAUDE.md"],
                "counterevidence_goal": "核对完整忽略规则，仅排除指定普通文档路径被这些模式匹配的假设",
                "planning_question_indices": [1]}))
            return proposed

        async def review_step(self, pr, files, diff, model, record_input):
            self.inputs.append(record_input)
            evidence = [item["id"] for item in record_input["evidence"]]
            return UnitReviewResponse(review_record=UnitReviewRecord(change_summary="限定问题有当前正文反证",
                target_checks=[{"target": target, "status": "checked", "reason": "已核对正文", "evidence_ids": evidence}
                    for target in record_input["targets"]],
                hypothesis_checks=[{"hypothesis_id": item["id"], "status": "refuted", "reason": "限定路径关系已核验",
                    "evidence_ids": evidence} for item in record_input["hypotheses"]]))

    value = verified_document_state() if action == "skip" else document_state()
    before = deepcopy(value)
    provider = Provider()
    output = await CrossUnitCoordinationService(provider).run(value)
    assert value == before
    assert output["coordination_plan"]["status"] == ("unresolved" if action == "unreachable" else "completed"), output
    if action == "followups":
        assert len(provider.inputs) == 2
        assert all(item["outcome"] == "refuted" for item in output["followup_results"])
        assert all(item["status"] == "resolved" for item in output["coordination_plan"]["question_states"])
        assert output["coordination_plan"]["unresolved_questions"] == []
        report = ReportService().generate(rebuild_task_from_state({**value, **output}))
        assert "规划问题[已解决]" in report
    else:
        assert provider.inputs == []
    assert await CrossUnitCoordinationService(provider).run({**value, **output}) == {}


def test_git_ignore_matching_does_not_require_local_files_to_exist(tmp_path):
    import subprocess
    content = document_state()["review_unit_results"][0]["context_snippets"][0]["content"]
    (tmp_path / ".gitignore").write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    checked = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--no-index", "-v", "--stdin"],
        input=b"AGENTS.md\nCLAUDE.md\nAGENTS.local.md\nCLAUDE.local.md\n", capture_output=True, check=True)
    matches = {line.split("\t")[1]: line.split("\t")[0] for line in checked.stdout.decode("utf-8").splitlines()}
    assert set(matches) == {"AGENTS.local.md", "CLAUDE.local.md"}
    assert ":36:" in matches["AGENTS.local.md"] and ":35:" in matches["CLAUDE.local.md"]
