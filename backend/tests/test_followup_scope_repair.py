import pytest
from pydantic import ValidationError

from app.models.review import ReviewUnit, ReviewUnitResult
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.cross_unit_risk import CrossUnitRiskService
from app.tools.context_files import ScopedContextTool
from test_batch_contract_handoff import _scope
from test_cross_unit_coordination import state, plan, Provider
from test_cross_unit_risk import _state


def third_unit_state():
    value = state()
    file = {**value["changed_files"][0], "file_path": "AGENTS.md"}
    value["changed_files"].append(file)
    value["review_units"].append(ReviewUnit(id="u2", primary_files=["AGENTS.md"], estimated_tokens=10,
        complexity="small", fingerprint="agents", grouping_reason="single").model_dump(mode="json"))
    value["review_unit_results"].append(ReviewUnitResult(review_unit_id="u2").model_dump(mode="json"))
    return value


@pytest.mark.parametrize("explicit", [False, True])
def test_plan_adds_known_third_owner_before_execution(explicit):
    payload = CrossUnitCoordinationService.catalog(third_unit_state())
    proposed = plan(payload)
    if explicit:
        proposed.followups[0].supporting_files = ["AGENTS.md"]
    else:
        proposed.followups[0].counterevidence_goal = "核验 AGENTS.md 中的引用"
    with pytest.raises(ValueError, match="supporting files"):
        CrossUnitCoordinationService.validate_plan(proposed, payload)
    normalized, warnings = CrossUnitCoordinationService.normalize_plan(proposed, payload)
    assert normalized.followups[0].unit_ids == ["u0", "u1", "u2"]
    assert normalized.followups[0].supporting_files == ["AGENTS.md"]
    assert warnings and "AGENTS.md" in CrossUnitCoordinationService._followup_readable(normalized.followups[0], payload)


@pytest.mark.asyncio
async def test_normalized_supporting_file_is_in_actual_followup_scope():
    class NeedsThird(Provider):
        async def coordinate_cross_units(self, payload, model):
            proposed = plan(payload)
            proposed.followups[0].supporting_files = ["AGENTS.md"]
            return proposed
    provider = NeedsThird(candidate=False)
    output = await CrossUnitCoordinationService(provider).run(third_unit_state())
    assert output["coordination_plan"]["followups"][0]["unit_ids"] == ["u0", "u1", "u2"]
    assert "AGENTS.md" in provider.decisions[0]["review_tool_scope"]["readable_files"]


@pytest.mark.parametrize("path", ["outside.py", ".env", "AGENTS.local.md"])
def test_supporting_scope_cannot_be_expanded_by_similar_or_arbitrary_path(path):
    payload = CrossUnitCoordinationService.catalog(third_unit_state())
    proposed = plan(payload)
    proposed.followups[0].supporting_files = [path]
    with pytest.raises(ValueError):
        CrossUnitCoordinationService.normalize_plan(proposed, payload)
    proposed.followups[0].supporting_files = ["../escape"]
    with pytest.raises(ValidationError):
        CrossUnitCoordinationService.normalize_plan(proposed, payload)


@pytest.mark.asyncio
async def test_empty_find_is_only_an_authorized_scope_result():
    scope = _scope({"CLAUDE.md"}, {"CLAUDE.md"}).model_copy(update={"repository_discovery_enabled": False})
    page = await ScopedContextTool().file_find_page(scope=scope, query="AGENTS.md")
    assert page["files"] == [] and page["search_scope"] == "seed_files"
    assert page["absence_is_repository_absence"] is False


@pytest.mark.parametrize("path", ["AGENTS.local.md", "doc/AGENTS.md", "OTHERAGENTS.md"])
def test_similar_path_in_question_does_not_select_root_agents_unit(path):
    payload = CrossUnitCoordinationService.catalog(third_unit_state())
    proposed = plan(payload)
    proposed.followups[0].question = "核对 " + path
    normalized, _ = CrossUnitCoordinationService.normalize_plan(proposed, payload)
    assert normalized.followups[0].unit_ids == ["u0", "u1"]


@pytest.mark.parametrize("failure", ["none", "file", "graph", "count", "symbol"])
def test_index_diagnostics_distinguish_non_symbol_config_and_actual_failures(failure):
    value = _state(paths=(".gitignore", "code.py"), edge=False, changed=False)
    value["file_index"][0]["analysis_level"] = 0
    if failure == "file":
        value["file_index"].pop(0)
    elif failure == "graph":
        value["repository_graph"]["files"].pop(0)
    elif failure == "count":
        value["repository_graph"]["metadata"]["edge_count"] = 9
    elif failure == "symbol":
        value["file_index"][1]["analysis_level"] = 0
    result = CrossUnitRiskService().assess(value)
    assert result.decision == "uncertain"
    assert "symbol_analysis_not_applicable:.gitignore" in result.index_diagnostics
    assert not any(item == "symbol_analysis_insufficient:.gitignore" for item in result.index_diagnostics)
    assert result.index_status == ("available" if failure == "none" else "partial")
    if failure != "none":
        expected = {"file": "file_index_missing:.gitignore", "graph": "graph_file_missing:.gitignore",
            "count": "edge_count_mismatch", "symbol": "symbol_analysis_insufficient:code.py"}[failure]
        assert expected in result.index_diagnostics
