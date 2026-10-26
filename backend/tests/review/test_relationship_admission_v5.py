from copy import deepcopy

import pytest

from app.services.review.review_task_derivation import build_task_risk_catalog
from app.services.review.task_review_projection import project_task_runtime
from app.graph.nodes.report import _final_status
from app.graph.nodes.repo_index import repo_index_node
from tests.review.test_task_risk_planner import relationship_fixture
from tests.support.runtime_harness import configured
from tests.infrastructure.test_python_call_binding import indexed_graph


def v5_graph():
    goals, items, graph = relationship_fixture()
    graph["metadata"] = {"call_resolution_version": "import-evidence-v4"}
    graph["edges"][0].update(resolution_status="resolved", resolution_basis="python_lexical_import",
                             call_sites=[{"line": 12, "expression": "callee"}])
    return goals, items, graph


def test_v5_file_relationships_are_unverified_hints_and_calls_keep_required_evidence():
    goals, items, graph = v5_graph()
    for kind in ("imports", "test_of", "configures"):
        graph["edges"].append({"source": "src/a.py", "target": "src/b.py", "type": kind})
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v5")
    calls = [risk for risk in risks if not risk.get("hint")]
    hints = [risk for risk in risks if risk.get("hint")]
    assert len(calls) == 1 and len(hints) == 3
    assert calls[0]["evidence_requests"] and calls[0]["not_scheduled_reason"] is None
    assert calls[0]["relationship"]["call_sites"][0]["line"] == 12
    assert all(not risk["evidence_requests"] for risk in hints)
    assert all("尚未核验" in risk["proposition"] for risk in hints)


def test_v5_unproven_call_still_blocks_clean_completion(tmp_path):
    goals, items, graph = v5_graph()
    graph["edges"][0].update(resolution_status="unresolved", resolution_basis="shadowed_or_conditional_binding")
    risk = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v5")[0]
    assert risk["not_scheduled_reason"] == "relationship_resolution_unverified"
    runtime, steps, derivation, _ = configured(tmp_path, [], risk_planner_version="risk-planner-v5",
                                              risk_catalog=[risk])
    derivation.refresh()
    report = project_task_runtime(runtime, steps, list(derivation.initial_goals.values()),
                                  termination_reason="plan_exhausted")
    assert len(report.unscheduled_leads) == 1 and not report.relationship_hints
    assert _final_status({"engine": "task_review_v1", "task_runtime_report": report.model_dump()}) == "completed_with_warnings"


def test_v5_hints_do_not_become_fake_investigations_or_unscheduled_obligations(tmp_path):
    goals, items, graph = v5_graph()
    graph["edges"] = [{"source": "src/a.py", "target": "src/b.py", "type": "imports"}]
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v5")
    runtime, steps, derivation, messages = configured(tmp_path, [], risk_planner_version="risk-planner-v5",
                                                      risk_catalog=risks)
    derivation.refresh()
    report = project_task_runtime(runtime, steps, list(derivation.initial_goals.values()), termination_reason="running")
    assert len(report.relationship_hints) == 1
    assert not report.unscheduled_leads and not report.admitted_investigations
    assert not messages and len(steps) == 1


def test_old_catalog_is_unchanged_and_new_planner_rejects_old_graph():
    goals, items, graph = v5_graph()
    graph["edges"][0]["resolution_basis"] = "qualified_import_module"
    old = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v4")
    without_version = deepcopy(graph)
    without_version.pop("metadata")
    assert old == build_task_risk_catalog(goals, items, without_version, planner_version="risk-planner-v4")
    with pytest.raises(ValueError, match="graph version mismatch"):
        build_task_risk_catalog(goals, items, without_version, planner_version="risk-planner-v5")


async def test_index_node_uses_frozen_resolution_version(tmp_path):
    (tmp_path / "caller.py").write_text("from stats import measure\ndef run():\n    return measure([])\n", encoding="utf-8")
    (tmp_path / "stats.py").write_text("def measure(values):\n    return values\n", encoding="utf-8")
    base = {"repo_path": str(tmp_path)}
    old = await repo_index_node(base)
    new = await repo_index_node({**base, "task_runtime_configuration": {
        "call_resolution_version": "import-evidence-v4"}})
    for result, version, status in ((old, "import-evidence-v3", "unresolved"),
                                     (new, "import-evidence-v4", "resolved")):
        assert result["repository_graph"]["metadata"]["call_resolution_version"] == version
        edge = next(edge for edge in result["repository_graph"]["edges"] if edge["type"] == "calls")
        assert edge["resolution_status"] == status


def test_ambiguous_modules_are_visible_unresolved_edges(tmp_path):
    graph, _ = indexed_graph(tmp_path, "from pkg.stats import measure\ndef run():\n    return measure([])\n",
        extra={"pkg/stats.py": "def measure(values):\n    return None\n"})
    edges = [edge for edge in graph["edges"] if edge["type"] == "calls"]
    assert len(edges) == 2 and all(edge["resolution_status"] == "unresolved" for edge in edges)
    assert all(edge["relationship_level"] == "symbol" for edge in edges)


def test_name_only_match_stays_unverified_hint_instead_of_investigation(tmp_path):
    from app.models.review_runtime import ReviewGoal, ReviewWorkItem
    graph, _ = indexed_graph(tmp_path, "import external_sdk\ndef run():\n    return external_sdk.measure([])\n")
    edge = next(edge for edge in graph["edges"] if edge["type"] == "calls")
    assert edge["resolution_status"] == "unresolved" and edge["relationship_level"] == "search_hint"
    goal = ReviewGoal(goal_id="changed", unit_id="u", kind="inspect", target_ref="src/pkg/stats.py", start_line=1, end_line=2)
    item = ReviewWorkItem(work_item_id="work", kind="inspect", goal_ids=[goal.goal_id])
    risks = build_task_risk_catalog([goal], [item], graph, planner_version="risk-planner-v5")
    assert risks and all(risk.get("hint") for risk in risks)
