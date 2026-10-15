import pytest

from app.models.review import AgentAction, ExecutionBudget, ReviewUnit, UnitHypothesisCheck, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from test_review_step_flow_repairs import StepProvider
from test_unit_review_summary import _state


class EvidenceProvider(StepProvider):
    async def review_step(self, pr, files, diff, model, record_input):
        response = await super().review_step(pr, files, diff, model, record_input)
        response.review_record.hypothesis_checks = [UnitHypothesisCheck(
            hypothesis_id=item["id"], status="refuted", reason="仅核验已展示正文",
            evidence_ids=[ref["id"] for ref in record_input["evidence"]]) for item in record_input["hypotheses"]]
        return UnitReviewResponse(review_record=response.review_record, next_need=AgentAction(
            action="file_read", reason="复现只读前 200 行", tool_args={"request": {
                "file_path": "pyproject.toml", "start_line": 1, "end_line": 200}})
            if getattr(self, "first_page", False) and len(self.inputs) == 1 else None)


def followup_state(tmp_path=None, retrievals=4):
    value = _state()
    value.update(cross_unit_followup={"question": "文档和配置是否引用本地文件？",
        "counterevidence_goal": "核验限定正文中的显式引用", "stop_condition": "逐文件核验或保留缺口",
        "supporting_files": [], "verification_files": ["a.py", "pyproject.toml", "setup.py"]},
        _cross_unit_budget=ExecutionBudget(max_model_calls=8, max_diagnosis_attempts=8,
            max_token_usage=200000, max_context_retrievals=retrievals).model_dump(mode="json"))
    if tmp_path:
        value["repo_path"] = str(tmp_path)
        value["file_index"] = [{"path": path} for path in ["a.py", "pyproject.toml", "setup.py"]]
        (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("# config\n" * 212 + "# final setting\n", encoding="utf-8")
        (tmp_path / "setup.py").write_text("setup()\n", encoding="utf-8")
    return value


def followup_unit():
    return ReviewUnit(id="followup-evidence", primary_files=["a.py"],
        related_files=["pyproject.toml", "setup.py"], fingerprint="followup-evidence",
        estimated_tokens=10, complexity="small", grouping_reason="cross_unit_followup")


@pytest.mark.asyncio
async def test_required_evidence_is_read_without_model_dependencies_and_reaches_eof(tmp_path):
    provider = EvidenceProvider()
    provider.first_page = True
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), followup_state(tmp_path))
    assert result.error is None
    assert result.execution_budget.context_retrievals == 3
    assert len(provider.inputs) == 4
    last = provider.inputs[-1]
    assert any(item["file_path"] == "setup.py" for item in last["evidence"])
    assert any("final setting" in item["content"] for item in last["readonly_context"])
    snippets = [item for item in result.context_snippets if item.file == "pyproject.toml"]
    assert all(item.total_lines == 213 for item in snippets)


@pytest.mark.asyncio
async def test_required_evidence_does_not_bypass_retrieval_budget(tmp_path):
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), followup_state(tmp_path, 1))
    assert result.execution_budget.context_retrievals == 1
    assert len(provider.inputs) == 2
    assert not any(item["file_path"] == "setup.py" for item in provider.inputs[-1]["evidence"])
    assert any("followup_evidence_budget_exhausted" in (item.message or "") for item in result.messages)


@pytest.mark.asyncio
async def test_supporting_diff_is_delivered_in_first_input_without_tool_read():
    value = followup_state()
    value["changed_files"].append({**value["changed_files"][0], "file_path": "AGENTS.md"})
    value["cross_unit_followup"].update(supporting_files=["AGENTS.md"], verification_files=["a.py", "AGENTS.md"])
    unit = followup_unit().model_copy(update={"related_files": ["AGENTS.md"]})
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(unit, value)
    assert result.error is None
    assert {item["file_path"] for item in provider.inputs[0]["evidence"]} == {"a.py", "AGENTS.md"}
    assert result.execution_budget.context_retrievals == 0
    assert unit.primary_files == ["a.py"]


@pytest.mark.asyncio
async def test_necessary_file_outside_scope_is_not_read(tmp_path):
    value = followup_state(tmp_path)
    value["cross_unit_followup"]["verification_files"] = ["outside.py"]
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), value)
    assert result.execution_budget.context_retrievals == 0
    assert any("followup_evidence_unavailable:outside.py" in (item.message or "") for item in result.messages)


@pytest.mark.asyncio
async def test_followup_question_is_not_used_as_risk_polarity():
    provider = EvidenceProvider()
    value = followup_state()
    value["cross_unit_followup"].update(question="引用是否正确，还是指向其他路径？", verification_files=["a.py"])
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), value)
    assert result.error is None
    assert result.plan.risk_hypotheses[0].description.startswith("风险命题：限定核验契约未满足")
    assert value["cross_unit_followup"]["question"] not in result.plan.risk_hypotheses[0].description
    assert "supported 表示风险成立" in provider.inputs[0]["hypothesis_protocol"]


@pytest.mark.asyncio
async def test_missing_citation_gets_one_controlled_recheck():
    class Partial(EvidenceProvider):
        async def review_step(self, *args):
            response = await super().review_step(*args)
            for check in response.review_record.hypothesis_checks:
                check.evidence_ids = [item["id"] for item in self.inputs[-1]["evidence"] if item["file_path"] == "a.py"]
            return response
    value = followup_state()
    value["changed_files"].append({**value["changed_files"][0], "file_path": "AGENTS.md"})
    value["cross_unit_followup"].update(supporting_files=["AGENTS.md"], verification_files=["a.py", "AGENTS.md"])
    provider = Partial()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(
        followup_unit().model_copy(update={"related_files": ["AGENTS.md"]}), value)
    assert len(provider.inputs) == 2
    assert result.execution_budget.context_retrievals == 0


def test_tracking_evidence_uses_head_tree_and_exact_basename(tmp_path):
    import subprocess
    from app.tools.git_tool import GitTool
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AGENTS.local.md").write_text("local", encoding="utf-8")
    (tmp_path / "OTHERAGENTS.local.md").write_text("other", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    head = git("rev-parse", "HEAD")
    (tmp_path / "AGENTS.local.md").write_text("untracked", encoding="utf-8")
    fact = GitTool().tracking_evidence(tmp_path, head, ["AGENTS.local.md", "CLAUDE.local.md"])
    assert fact["matches"] == ["docs/AGENTS.local.md"]
    assert fact["complete"] and fact["head_sha"] == head
    with pytest.raises(ValueError):
        GitTool().tracking_evidence(tmp_path, "HEAD", ["AGENTS.local.md"])


@pytest.mark.parametrize("query", ["../escape", "docs/AGENTS.md", "*.md", ".env"])
def test_tracking_query_rejects_path_glob_and_sensitive_file(query):
    from app.models.review import CrossUnitFollowupRequest
    with pytest.raises(ValueError):
        CrossUnitFollowupRequest.validate_tracking_queries([query])


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_tracking_fact_is_citable_metadata_and_restorable(tmp_path, complete):
    from app.tools.git_tool import GitTool
    from app.services.evidence_store import restore_store
    from app.services.review_input_context import build_context_evidence
    value = followup_state()
    value.update(repo_path=str(tmp_path), head_sha="a" * 40)
    value["cross_unit_followup"].update(verification_files=["a.py"], tracking_queries=["AGENTS.local.md"])
    from unittest.mock import patch
    fact = {"head_sha": value["head_sha"], "queries": ["AGENTS.local.md"], "matches": [],
        "matched_count": 0, "complete": complete, "scope": "committed_head_tree_only"}
    provider = EvidenceProvider()
    with patch.object(GitTool, "tracking_evidence", return_value=fact):
        result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), value)
    assert result.error is None
    refs = [ref for ref in provider.inputs[0]["evidence"] if ref["source"] == "repository_tree"]
    assert len(refs) == 1 and refs[0]["start_line"] == refs[0]["end_line"] == 0
    from app.agents.providers import OpenAICompatibleProvider
    from app.models.review import ChangedFile
    OpenAICompatibleProvider._unit_evidence_payload([ChangedFile.model_validate(value["changed_files"][0])], provider.inputs[0])
    restored = restore_store(result.evidence_store, result.evidence_store.review_unit_id,
        result.evidence_store.snapshot, {"a.py"})
    assert refs[0]["id"] in restored.entries
    from app.models.review import CrossUnitFollowupRequest
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    request = CrossUnitFollowupRequest(id="f", unit_ids=["u", "v"], primary_files=["a.py"],
        verification_files=["a.py"], question="跟踪状态是否影响引用？", counterevidence_goal="核验文件跟踪状态",
        stop_condition="证据充分或保留未决", tracking_queries=["AGENTS.local.md"])
    outcome, reason = CrossUnitCoordinationService.followup_conclusion(request, result, [])
    assert outcome == ("refuted" if complete else "unresolved"), reason
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        build_context_evidence([item.model_dump() for item in result.context_snippets], "b" * 40, "b")


def test_conclusion_explains_missing_citation_polarity_and_tracking():
    from app.models.review import ReviewUnitResult
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    from app.services.unit_review_summary import build_record_input, validate_record
    from test_cross_unit_coordination import state, plan
    value = state()
    request = plan(CrossUnitCoordinationService.catalog(value)).followups[0]
    request.verification_files = ["callee.py", "AGENTS.md"]
    request.tracking_queries = ["AGENTS.local.md"]
    from app.models.review import ChangedFile, UnitReviewRecord
    record_input = build_record_input([ChangedFile.model_validate(value["changed_files"][1])], [], None, "h", "b")
    record_input["hypotheses"] = [{"id": "risk"}]
    evidence = [item["id"] for item in record_input["evidence"]]
    record = UnitReviewRecord(change_summary="引用正确", hypothesis_checks=[{
        "hypothesis_id": "risk", "status": "supported", "reason": "支持正向问句", "evidence_ids": evidence}],
        target_checks=[{"target": target, "status": "checked", "reason": "检查", "evidence_ids": evidence}
            for target in record_input["targets"]])
    summary = validate_record(record, record_input, {"callee.py"})
    outcome, reason = CrossUnitCoordinationService.followup_conclusion(request,
        ReviewUnitResult(review_unit_id="f", status="completed", review_summary=summary), [])
    assert outcome == "unresolved"
    assert "AGENTS.md" in reason and "supported" in reason and "仓库跟踪状态" in reason and "取证额度" in reason
