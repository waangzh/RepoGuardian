from copy import deepcopy

from app.models.review import ReviewTask
from app.services.report_service import ReportService
from app.services.review_task_derivation import build_task_risk_catalog
from app.services.task_review_projection import group_relationship_leads, relationship_description, project_task_runtime
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_phase6a_persistence import persistence as persistence
from test_relationship_admission_v5 import v5_graph
from test_task_runtime_p3 import configured
from test_task_runtime_p4 import runtime_report
from app.services.task_review_projection import runtime_failure_summary


def test_failed_report_exports_fixed_summary_and_preserves_partial_results():
    report = runtime_report(execution_status="failed", termination_reason="technical_failure",
        diagnostics=[{"provider_failure": {"category": "model_connection_error", "attempts": 2,
            "error_detail": "proxy-secret", "error_type": "malicious-secret"}}]).model_dump()
    task = ReviewTask(id="run", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1",
                      task_runtime_report=report)
    markdown = ReportService().generate(task)
    assert "model_connection_error" in markdown and "尝试 2 次" in markdown
    assert "候选命题" in markdown
    assert "proxy-secret" not in markdown and "malicious-secret" not in markdown
    assert "technical_failure" in runtime_failure_summary([])
    assert "technical_failure" in runtime_failure_summary([{"provider_failure": {"category": "external-secret"}}])


def leads():
    relation = {"type": "calls", "source_file": "a.py", "target_file": "b.py",
        "source": "a.py::first", "target": "b.py::value", "resolution_status": "unresolved",
        "resolution_basis": "shadowed_or_conditional_binding", "call_sites": [{"line": 7, "expression": "value"}]}
    return [{"lead_id": "lead-first", "proposition": "第一条命题", "reason": "relationship_resolution_unverified",
        "status": "not_scheduled", "related_goal_ids": ["goal"], "relationship": relation},
        {"lead_id": "lead-second", "proposition": "第二条命题", "reason": "relationship_resolution_unverified",
         "status": "not_scheduled", "related_goal_ids": ["goal"],
         "relationship": {**deepcopy(relation), "source": "a.py::second", "call_sites": [{"line": 9, "expression": "value"}]}}]


def test_grouping_preserves_every_relation_and_separates_status_and_reason():
    values = leads()
    before = deepcopy(values)
    groups = group_relationship_leads(values)
    assert len(groups) == 1 and groups[0]["members"] == values
    assert "lead-first" in relationship_description(values[0])
    assert "7: value" in relationship_description(values[0])
    assert "a.py::second" in relationship_description(values[1])
    assert values == before
    assert len(group_relationship_leads([*values, {**values[0], "status": "duplicate"}])) == 2
    assert len(group_relationship_leads([*values, {**values[0], "reason": "budget"}])) == 2
    assert len(group_relationship_leads([{"proposition": "旧描述"}, {"proposition": "旧描述"}])) == 2


def test_markdown_groups_relations_without_losing_ids_locations_or_hint_uncertainty():
    report = runtime_report().model_dump()
    report.update(unscheduled_leads=leads(), relationship_hints=[{
        **leads()[0], "lead_id": "file-hint", "status": "hint", "reason": "relationship_hint_only",
        "relationship": {"type": "imports", "source_file": "a.py", "target_file": "b.py",
                         "source": "a.py", "target": "b.py"}}])
    task = ReviewTask(id="run", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1",
                      task_runtime_report=report)
    markdown = ReportService().generate(task)
    assert "（2 条）" in markdown
    for fragment in ("lead-first", "lead-second", "7: value", "9: value", "a.py::first", "a.py::second",
                     "shadowed_or_conditional_binding", "关联目标 goal", "file-hint", "尚未核验，不计作已完成调查"):
        assert fragment in markdown


async def test_relation_evidence_survives_checkpoint_restore_and_cancel(tmp_path, persistence):
    goals, items, graph = v5_graph()
    graph["edges"][0].update(resolution_status="unresolved", resolution_basis="shadowed_or_conditional_binding")
    graph["edges"].append({"source": "src/a.py", "target": "src/b.py", "type": "imports"})
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v5")
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1"))
    runtime, steps, derivation, _ = configured(tmp_path, [], risk_catalog=risks, risk_planner_version="risk-planner-v5")
    checkpoint = TaskRuntimeCheckpoint(runtime, steps, repository)
    await checkpoint.restore()
    derivation.refresh()
    await checkpoint.persist()
    report = project_task_runtime(runtime, steps, list(derivation.initial_goals.values()), termination_reason="running")
    assert report.unscheduled_leads[0]["relationship"]["source"] == "src/a.py::caller"
    saved = repository.load_task_review_runtime("run")
    # 直接验证恢复所读的内容寻址产物中保留了关系身份，取消事务也读取同一份证据。
    import json
    restored = json.loads(repository._artifacts.read_text(saved["derivation"]["uri"]))
    assert all(value.get("relationship") for value in restored["leads"].values())
    assert repository.cancel_task("run")
    cancelled = repository.get_task("run").task_runtime_report
    assert cancelled["unscheduled_leads"] == report.unscheduled_leads
    assert cancelled["relationship_hints"] == report.relationship_hints
