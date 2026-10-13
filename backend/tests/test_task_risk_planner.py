"""具体关系规划：绑定变更符号、合法读取和冻结恢复，不把调查答案当质量标签。"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
from langchain_core.messages import AIMessage

from app.graph.nodes.task_review_runtime import task_review_runtime_node, task_runtime_configuration
from app.models.review import ReviewTask
from app.models.review_runtime import ReviewGoal, ReviewWorkItem
from app.services.review_task_derivation import build_task_risk_catalog
from app.services.task_review_projection import project_task_runtime
from app.tools.diff_parser import DiffParser
from test_phase6a_persistence import persistence as persistence
from test_task_runtime_p3 import configured
from test_task_runtime_p4 import production_input as production_input


def relationship_fixture():
    goals = [ReviewGoal(goal_id=identity, unit_id=unit, kind="inspect", target_ref=path,
                        start_line=start, end_line=start + 1)
             for identity, unit, path, start in [
                 ("caller", "u1", "src/a.py", 12), ("unrelated-a", "u1", "src/a.py", 80),
                 ("callee", "u2", "src/b.py", 22), ("unrelated-b", "u2", "src/b.py", 90)]]
    items = [ReviewWorkItem(work_item_id=goal.goal_id + "-work", kind="inspect", goal_ids=[goal.goal_id])
             for goal in goals]
    graph = {"symbols": [
        {"id": "src/a.py::caller", "file": "src/a.py", "start_line": 10, "end_line": 19},
        {"id": "src/b.py::callee", "file": "src/b.py", "start_line": 20, "end_line": 29}],
        "edges": [{"type": "calls", "source": "src/a.py::caller", "target": "src/b.py::callee"}]}
    return goals, items, graph


def test_v2_binds_only_changed_symbols_and_reads_their_definitions():
    goals, items, graph = relationship_fixture()
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v2", max_lines_per_read=6)
    assert len(risks) == 1
    risk = risks[0]
    assert risk["related_goal_ids"] == ["caller", "callee"]
    assert risk["origin_work_item_id"] == "caller-work"
    assert risk["not_scheduled_reason"] is None
    assert risk["evidence_requests"] == [
        {"file_path": "src/a.py", "start_line": 10, "end_line": 15},
        {"file_path": "src/a.py", "start_line": 16, "end_line": 19},
        {"file_path": "src/b.py", "start_line": 20, "end_line": 25},
        {"file_path": "src/b.py", "start_line": 26, "end_line": 29}]
    # 输入遍历顺序不能改变风险身份、来源或读取参数。
    graph["symbols"].reverse()
    assert risks == build_task_risk_catalog(list(reversed(goals)), list(reversed(items)), graph,
                                           planner_version="risk-planner-v2", max_lines_per_read=6)
    assert len(build_task_risk_catalog(goals, items, graph)) == 4  # 原冻结任务保留原目录。


def test_known_relationship_outside_changed_symbols_does_not_pair_unrelated_hunks():
    goals, items, graph = relationship_fixture()
    graph["symbols"][0].update(start_line=30, end_line=40)
    assert not build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v2")


@pytest.mark.parametrize("problem", ["missing", "duplicate", "invalid_range", "wrong_file", "imports", "oversized"])
def test_unbound_or_unreadable_relationship_is_visible_without_requests(problem):
    goals, items, graph = relationship_fixture()
    if problem == "missing":
        graph["symbols"].clear()
    elif problem == "duplicate":
        graph["symbols"].append(deepcopy(graph["symbols"][0]))
    elif problem == "invalid_range":
        graph["symbols"][0]["start_line"] = 0
    elif problem == "wrong_file":
        graph["symbols"][0]["file"] = "outside.py"
    elif problem == "imports":
        graph["edges"][0].update(type="imports", source="src/a.py", target="src/b.py")
    else:
        graph["symbols"][0]["end_line"] = 10_000
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v2")
    assert risks and all(risk["not_scheduled_reason"] for risk in risks)
    assert all(not risk["evidence_requests"] for risk in risks)


def test_v2_unbound_risk_projects_as_unscheduled_without_creating_work(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [], risk_planner_version="risk-planner-v2",
        risk_catalog=[{"id": "unbound", "origin_work_item_id": "w1", "proposition": "缺少具体符号",
                       "related_goal_ids": ["t1"], "evidence_requests": [],
                       "not_scheduled_reason": "relationship_not_bound_to_symbols"}])
    derivation.refresh()
    report = project_task_runtime(runtime, steps, list(derivation.initial_goals.values()), termination_reason="running")
    assert report.unscheduled_leads[0]["reason"] == "relationship_not_bound_to_symbols"
    assert report.unscheduled_leads[0]["related_goal_ids"] == ["t1"]
    assert len(steps) == 1 and not messages


def test_v2_investigation_uses_bound_evidence_instead_of_unrelated_file_archive(tmp_path):
    runtime, steps, derivation, _ = configured(tmp_path, [], risk_planner_version="risk-planner-v2")
    root = steps["w1"].executor
    path = root.goals["t1"].target_ref
    for index in range(25):
        root.evidence_catalog[f"unrelated-{index}"] = {**root.evidence_catalog["e1"],
            "start_line": 50 + index, "end_line": 50 + index}
    derivation.risk_catalog = [{"id": "bound", "origin_work_item_id": "w1", "proposition": "核对具体调用",
        "related_goal_ids": ["t1"], "evidence_requests": [{"file_path": path, "start_line": 1, "end_line": 1}]}]
    derivation.refresh()
    assert len(steps) == 2
    item = next(step.executor for key, step in steps.items() if key != "w1")
    assert item.work_item.required_evidence_ids == ["e1"]
    assert item.active_evidence_ids == {"e1"}
    assert derivation.data["leads"]["bound"]["status"] == "scheduled"


@pytest.mark.parametrize("version", ["risk-planner-v2", "risk-planner-v3"])
async def test_risk_planner_version_is_frozen_even_when_catalog_is_empty(production_input, persistence, version):
    state, provider, calls = production_input
    state["task_runtime_configuration"]["risk_planner_version"] = version
    state["_provider"] = provider
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
                                     task_runtime_configuration=state["task_runtime_configuration"]))
    state["_coordination_repository"] = repository
    await task_review_runtime_node(state)
    await task_review_runtime_node(state)
    assert len(calls) == 1
    state["task_runtime_configuration"].pop("risk_planner_version")
    with pytest.raises(ValueError, match="frozen manifest mismatch"):
        await task_review_runtime_node(state)
    assert len(calls) == 1


async def test_unknown_planner_version_rejected_before_any_dispatch(production_input):
    state, provider, calls = production_input
    state["_provider"] = provider
    state["task_runtime_configuration"]["risk_planner_version"] = "unknown"
    with pytest.raises(ValueError, match="unsupported risk planner version"):
        await task_review_runtime_node(state)
    assert not calls


def test_new_task_configuration_freezes_v3():
    assert task_runtime_configuration()["risk_planner_version"] == "risk-planner-v4"


@pytest.mark.parametrize("changed_side", ["caller", "callee", "both"])
def test_v3_investigates_either_changed_endpoint_without_inventing_coverage(changed_side):
    goals, items, graph = relationship_fixture()
    selected = [goal for goal in goals if goal.goal_id in
                ({"caller", "callee"} if changed_side == "both" else {changed_side})]
    risks = build_task_risk_catalog(selected, items, graph, planner_version="risk-planner-v3")
    assert len(risks) == 1 and risks[0]["not_scheduled_reason"] is None
    assert set(risks[0]["related_goal_ids"]) == {goal.goal_id for goal in selected}
    assert {request["file_path"] for request in risks[0]["evidence_requests"]} == {"src/a.py", "src/b.py"}
    assert risks[0]["origin_work_item_id"] in {goal.goal_id + "-work" for goal in selected}
    if changed_side != "both":
        assert not build_task_risk_catalog(selected, items, graph, planner_version="risk-planner-v2")


def test_v3_ignores_relationship_when_neither_changed_symbol_is_related():
    goals, items, graph = relationship_fixture()
    selected = [goal for goal in goals if goal.goal_id.startswith("unrelated")]
    assert not build_task_risk_catalog(selected, items, graph, planner_version="risk-planner-v3")


def test_v3_one_relationship_groups_related_hunks_once_and_supports_same_unit():
    goals, items, graph = relationship_fixture()
    goals = [goal.model_copy(update={"unit_id": "same"}) for goal in goals]
    goals.append(goals[0].model_copy(update={"goal_id": "caller-second", "start_line": 14, "end_line": 15}))
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v3")
    assert len(risks) == 1
    assert risks[0]["related_goal_ids"] == ["callee", "caller", "caller-second"]


@pytest.mark.parametrize("target_path", ["src/outside.py", ".env"])
def test_v3_unchanged_endpoint_cannot_expand_frozen_permissions(tmp_path, target_path):
    runtime, steps, derivation, messages = configured(tmp_path, [], risk_planner_version="risk-planner-v3")
    goal = derivation.initial_goals["t1"]
    graph = {"symbols": [
        {"id": goal.target_ref + "::caller", "file": goal.target_ref, "start_line": 1, "end_line": 1},
        {"id": target_path + "::callee", "file": target_path, "start_line": 1, "end_line": 1}],
        "edges": [{"type": "calls", "source": goal.target_ref + "::caller", "target": target_path + "::callee"}]}
    derivation.risk_catalog = build_task_risk_catalog([goal], [steps["w1"].executor.work_item], graph,
                                                     planner_version="risk-planner-v3")
    assert len(derivation.risk_catalog) == 1
    derivation.refresh()
    report = project_task_runtime(runtime, steps, [goal], termination_reason="running")
    assert len(steps) == 1 and not messages
    assert report.unscheduled_leads[0]["reason"] == "no_legal_evidence_path"


async def test_production_v3_reads_unchanged_caller_and_preserves_coverage_on_restore(production_input, persistence):
    state, provider, _ = production_input
    root = Path(state["repo_path"])
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()
    (root / "a.py").write_text("from b import other\ndef answer():\n    return other().value\n", encoding="utf-8")
    (root / "b.py").write_text("def other():\n    return object()\n", encoding="utf-8")
    git("add", "a.py", "b.py")
    git("commit", "-m", "caller base")
    state["base_sha"] = git("rev-parse", "HEAD")
    (root / "b.py").write_text("def other():\n    return None\n", encoding="utf-8")
    git("add", "b.py")
    git("commit", "-m", "callee head")
    state["head_sha"] = git("rev-parse", "HEAD")
    state["changed_files"] = [value.model_dump(mode="json") for value in
        DiffParser().parse(git("diff", "--unified=3", state["base_sha"], state["head_sha"]))]
    unit = state["review_plan"]["review_units"][0]
    state["review_plan"]["review_units"] = [{**unit, "primary_files": ["b.py"], "related_files": [], "diff_hunk_ids": []}]
    state["repository_graph"] = {"symbols": [
        {"id": "a.py::answer", "file": "a.py", "start_line": 2, "end_line": 3},
        {"id": "b.py::other", "file": "b.py", "start_line": 1, "end_line": 2}],
        "edges": [{"type": "calls", "source": "a.py::answer", "target": "b.py::other"}]}
    state["task_runtime_configuration"].update(risk_planner_version="risk-planner-v3", prompt_version="task-review-prompt-v5")
    calls = []
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            calls.append(payload)
            if payload["task"]["kind"] == "inspect":
                raw = {"kind": "submit", "examinations": [{"target_id": goal["goal_id"],
                    "disposition": "examined", "evidence_ids": payload["active_evidence_ids"],
                    "reason": "检查变更"} for goal in payload["goals"]]}
                assert {goal["target_ref"] for goal in payload["goals"]} == {"b.py"}
            else:
                assert payload["task"]["kind"] == "investigate"
                assert any(value["file_path"] == "a.py" and "other().value" in value["content"]
                           for value in payload["active_evidence"])
                raw = {"kind": "submit", "investigation": {"answer": "refuted",
                    "evidence_ids": payload["active_evidence_ids"], "limitations": [], "reason": "stub 调查结论"}}
            return AIMessage(content=json.dumps(raw), usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
                                     task_runtime_configuration=state["task_runtime_configuration"]))
    state["_coordination_repository"] = repository
    report = (await task_review_runtime_node(state))["task_runtime_report"]
    assert len(calls) == 2 and report["settled_tokens"] == 30
    assert {goal["target_ref"] for goal in report["initial_scope_coverage"]} == {"b.py"}
    assert report["scope_complete"] and report["admitted_investigations"][0]["answer"] == "refuted"
    assert any(observation.get("status") == "completed" for item in report["work_items"]
               for observation in item["tool_observations"])
    restored = (await task_review_runtime_node(state))["task_runtime_report"]
    assert len(calls) == 2 and restored["settled_tokens"] == 30
    assert restored["initial_scope_coverage"] == report["initial_scope_coverage"]


async def test_production_v2_investigation_binds_both_files_and_restores_without_resend(production_input, persistence):
    state, provider, _ = production_input
    root = Path(state["repo_path"])
    (root / "a.py").write_text("from b import other\ndef answer():\n    return other()\n", encoding="utf-8")
    (root / "b.py").write_text("def other():\n    return 3\n", encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()
    git("add", "a.py", "b.py")
    git("commit", "-m", "cross-file fixture")
    state["head_sha"] = git("rev-parse", "HEAD")
    changed = DiffParser().parse(git("diff", "--unified=3", state["base_sha"], state["head_sha"]))
    state["changed_files"] = [value.model_dump(mode="json") for value in changed]
    unit = state["review_plan"]["review_units"][0]
    state["review_plan"]["review_units"] = [
        {**unit, "id": path, "primary_files": [path], "related_files": [], "diff_hunk_ids": []}
        for path in ("a.py", "b.py")]
    state["repository_graph"] = {"symbols": [
        {"id": "a.py::answer", "file": "a.py", "start_line": 2, "end_line": 3},
        {"id": "b.py::other", "file": "b.py", "start_line": 1, "end_line": 2}],
        "edges": [{"type": "calls", "source": "a.py::answer", "target": "b.py::other"}]}
    state["task_runtime_configuration"].update(risk_planner_version="risk-planner-v2", prompt_version="task-review-prompt-v5")
    calls = []
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            calls.append(payload)
            if payload["task"]["kind"] == "inspect":
                raw = {"kind": "submit", "examinations": [{"target_id": goal["goal_id"],
                    "disposition": "examined", "evidence_ids": payload["active_evidence_ids"],
                    "reason": "核对固定变更"} for goal in payload["goals"]]}
            else:
                assert payload["task"]["kind"] == "investigate"
                assert "a.py::answer" in payload["proposition"] and "b.py::other" in payload["proposition"]
                assert {value["file_path"] for value in payload["active_evidence"]} == {"a.py", "b.py"}
                raw = {"kind": "submit", "investigation": {"answer": "supported",
                    "evidence_ids": payload["active_evidence_ids"], "limitations": [], "reason": "仅测试结构化调查协议"}}
            return AIMessage(content=json.dumps(raw), usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
                                     task_runtime_configuration=state["task_runtime_configuration"]))
    state["_coordination_repository"] = repository
    result = await task_review_runtime_node(state)
    report = result["task_runtime_report"]
    assert len(calls) == 3 and report["scope_complete"]
    assert len(report["admitted_investigations"]) == 1
    assert report["admitted_investigations"][0]["status"] == "answered"
    assert report["sent_calls"] == 3 and report["settled_tokens"] == 45
    restored = await task_review_runtime_node(state)
    assert len(calls) == 3 and restored["task_runtime_report"]["settled_tokens"] == 45
