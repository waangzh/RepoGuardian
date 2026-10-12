from datetime import datetime, timezone
import json
import subprocess
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, END
from app.agents.providers import OpenAICompatibleProvider
from app.graph.nodes.task_review_runtime import task_review_runtime_node
from app.graph.nodes.report import report_node
from app.graph.state import ReviewState, ReviewRunContext
from app.services.review_run_budget import ReviewRunBudget, ReviewRepositoryRunBudgetStore, activate_run_budget
from app.services.review_planner import DeterministicReviewPlanner
from app.tools.diff_parser import DiffParser
from test_phase6a_persistence import persistence as persistence
from app.services.task_queue import DatabaseTaskQueue

from app.graph.review_graph import build_review_graph
from app.models.review import ReviewTask, TaskStatus
from app.models.review_runtime import TaskRuntimeReport
from app.services.report_service import ReportService
from app.services.review_manifest import build_review_manifest
from app.services.review_rebuild import rebuild_task_from_state


def runtime_report(**updates):
    value = TaskRuntimeReport(
        execution_status="completed", termination_reason="plan_exhausted", scope_complete=True,
        initial_scope_coverage=[{"goal_id": "g1", "status": "examined"}],
        findings=[{"status": "unresolved", "candidate": {"claim": "候选命题"}, "reason": "证据不足"}],
        usage_integrity="complete", sent_calls=2, settled_tokens=30,
        unknown_attempts=0, work_items=[{"work_item_id": "w1", "unit_id": "u1", "state": "completed"}],
    )
    return value.model_copy(update=updates)


def test_task_runtime_graph_has_single_runtime_node_and_no_legacy_coordination():
    nodes = set(build_review_graph(phase=3).compile().get_graph().nodes)
    assert nodes == {"__start__", "intake", "repo_prepare", "diff_parse", "repo_index",
                     "project_detection", "review_plan", "task_review_runtime", "report", "complete", "__end__"}
    assert "cross_unit_coordination" not in nodes and "issue_verifier" not in nodes


def test_task_runtime_report_and_manifest_keep_new_coverage_version():
    report = runtime_report()
    state = {"task_id": "run", "engine": "task_review_v1", "pr_url": "https://github.com/o/r/pull/1",
             "status": "completed_with_warnings", "phase": "completed", "model": "fixture",
             "pr_info": {}, "changed_files": [], "review_units": [], "review_unit_results": [],
             "review_issues": [], "issue_metrics": {}, "review_coverage": {
                 "coverage_version": "task-goals-v1", "review_complete": True,
                 "changed_files": 0, "eligible_files": 1, "reviewed_files": 1, "coverage_rate": 1,
                 "completed_units": 1, "total_units": 1, "unit_coverage_rate": 1,
             }, "task_runtime_report": report.model_dump(mode="json"), "model_usages": [],
             "excluded_files": [], "warnings": [], "created_at": datetime.now(timezone.utc).isoformat()}
    rebuilt = rebuild_task_from_state(state)
    assert rebuilt.engine == "task_review_v1"
    assert rebuilt.task_runtime_report["coverage_version"] == "task-goals-v1"
    manifest = build_review_manifest(state, datetime.now(timezone.utc))
    assert manifest.engine == "task_review_v1" and manifest.coverage_version == "task-goals-v1"


def test_task_runtime_report_does_not_become_legacy_clean_report():
    task = ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
                      status=TaskStatus.completed_with_warnings,
                      task_runtime_report=runtime_report().model_dump(mode="json"))
    markdown = ReportService().generate(task)
    assert "task-goals-v1" in markdown and "证据不足" in markdown
    assert "协调状态" not in markdown


def test_old_task_without_runtime_fields_stays_legacy():
    task = rebuild_task_from_state({"task_id": "old", "pr_url": "https://github.com/o/r/pull/1",
        "status": "completed", "phase": "completed", "review_coverage": {}})
    assert task.engine == "legacy_units" and task.task_runtime_report is None


@pytest.fixture
def production_input(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (root / "a.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (root / "a.py").write_text("def answer():\n    return 2\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-m", "head")
    head = git("rev-parse", "HEAD")
    changed = DiffParser().parse(git("diff", "--unified=3", base, head))
    plan = DeterministicReviewPlanner().plan(changed, base_sha=base, head_sha=head, model="fixture")
    configuration = {"provider": "openai-compatible", "model": "fixture", "endpoint": "https://example.test/v1",
        "readable_scope_version": "repository-tracked-v1",
        "policy_version": "test-v1", "limits": {"max_tokens": 100_000, "max_calls": 5},
        "concurrency": 2, "deadline_seconds": 10, "request_attempts": 1, "request_timeout_seconds": 60,
        "request_profile": {"context_window": 32_000}, "max_lines_per_read": 240, "max_search_results": 12}
    state = {"task_id": "run", "engine": "task_review_v1", "task_runtime_configuration": configuration,
        "repo_path": str(root), "head_sha": head, "base_sha": base, "pr_url": "https://github.com/o/r/pull/1",
        "changed_files": [value.model_dump(mode="json") for value in changed], "review_plan": plan.model_dump(mode="json"),
        "review_units": [value.model_dump(mode="json") for value in plan.review_units],
        "file_index": [{"path": "a.py"}], "created_at": datetime.now(timezone.utc).isoformat()}
    calls = []
    provider = OpenAICompatibleProvider("test-key", configuration["endpoint"], "fixture", request_attempts=1)
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            calls.append(payload)
            assert any("return 2" in value["content"] for value in payload["active_evidence"])
            assert not any(value["content"] == "" for value in payload["active_evidence"])
            raw = {"kind": "submit", "examinations": [{"target_id": value["goal_id"], "disposition": "examined",
                "evidence_ids": payload["active_evidence_ids"], "reason": "核对固定 hunk"} for value in payload["goals"]]}
            return AIMessage(content=json.dumps(raw), usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    return state, provider, calls


async def test_production_node_reuses_budget_and_database_restores_without_resend(production_input, persistence):
    state, provider, calls = production_input
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
        task_runtime_configuration=state["task_runtime_configuration"]))
    store = ReviewRepositoryRunBudgetStore(repository)
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5, store=store)
    graph = StateGraph(ReviewState, context_schema=ReviewRunContext)
    graph.add_node("task_review_runtime", task_review_runtime_node)
    graph.add_node("report", report_node)
    graph.set_entry_point("task_review_runtime")
    graph.add_edge("task_review_runtime", "report")
    graph.add_edge("report", END)
    compiled = graph.compile()
    context = {"coordination_repository": repository, "task_runtime_provider": provider}
    with activate_run_budget(budget):
        result = await compiled.ainvoke(state, context=context)
    assert result["status"] == "completed" and result["review_coverage"]["coverage_version"] == "task-goals-v1"
    assert result["run_manifest"]["coverage"]["review_complete"]
    assert budget.settled_actual == 15 and budget.calls_consumed == 1 and len(calls) == 1
    restored = await ReviewRunBudget.load_or_create("run", max_tokens=0, max_calls=0, store=store, recover_running=False)
    with activate_run_budget(restored):
        repeated = await compiled.ainvoke(state, context=context)
    assert len(calls) == 1 and restored.settled_actual == 15
    assert repeated["run_manifest"]["model_calls"] == 1
    assert repeated["run_manifest"]["total_tokens"] == 15
    assert len(repeated["model_usages"]) == 1
    assert "prompt_version" not in repository.load_task_review_runtime("run")["spec"]


async def test_production_spec_ignores_hot_default_changes(production_input, monkeypatch):
    from app.core.config import settings
    state, provider, calls = production_input
    state["_provider"] = provider
    monkeypatch.setattr(settings, "repoguardian_model", "other-model")
    monkeypatch.setattr(settings, "repoguardian_run_max_tokens", 0)
    result = await task_review_runtime_node(state)
    assert calls[0]["task"]["kind"] == "inspect"
    assert result["task_runtime_report"]["scope_complete"]
    assert result["run_budget"]["limits"]["max_tokens"] == 100_000
    change = calls[0]["canonical_changes"][0]
    assert {line["content"] for line in change["lines"] if line["kind"] == "deleted"} == {"    return 1"}
    assert {line["content"] for line in change["lines"] if line["kind"] == "added"} == {"    return 2"}
    assert all("canonical_change" not in value for value in calls[0]["active_evidence"])


@pytest.mark.parametrize("version", ["task-review-prompt-v2", "task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5"])
async def test_prompt_version_frozen_in_configuration_and_checkpoint(production_input, persistence, version):
    state, provider, calls = production_input
    state["task_runtime_configuration"]["prompt_version"] = version
    state["_provider"] = provider
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
        task_runtime_configuration=state["task_runtime_configuration"]))
    state["_coordination_repository"] = repository
    await task_review_runtime_node(state)
    assert calls[0]["prompt_version"] == version
    assert repository.load_task_review_runtime("run")["spec"]["prompt_version"] == version
    state["task_runtime_configuration"]["prompt_version"] = "task-review-prompt-v1"
    with pytest.raises(ValueError, match="frozen manifest mismatch"):
        await task_review_runtime_node(state)
    assert len(calls) == 1


@pytest.mark.parametrize("version", ["task-review-prompt-v4", "task-review-prompt-v5"])
async def test_v4_large_goal_uses_small_anchors_for_independent_verification(production_input, version):
    from pathlib import Path
    state, provider, _ = production_input
    root = state["repo_path"]
    body = "def first(values):\n    return values[0]\n" + "# implementation context " * 3 + "\n"
    body += ("# " + "context " * 12 + "\n") * 175
    Path(root, "a.py").write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-m", "large-goal"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()
    diff = subprocess.run(["git", "-C", root, "diff", "--unified=80", state["base_sha"], head],
                          check=True, capture_output=True, encoding="utf-8").stdout
    changed = DiffParser().parse(diff)
    state.update(head_sha=head, changed_files=[value.model_dump(mode="json") for value in changed],
        review_plan=DeterministicReviewPlanner().plan(changed, base_sha=state["base_sha"], head_sha=head,
            model="fixture").model_dump(mode="json"), _provider=provider)
    state["task_runtime_configuration"]["prompt_version"] = version
    state["task_runtime_configuration"]["limits"]["max_calls"] = 20
    state["task_runtime_configuration"]["deadline_seconds"] = 60
    requests = []
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            requests.append(payload)
            assert payload["prompt_version"] == version
            assert all(len(value["content"]) <= 4_000 for value in payload["active_evidence"])
            if payload["task"]["kind"] == "inspect":
                anchor = next((value["id"] for value in payload["active_evidence"]
                    if "return values[0]" in value["content"]), None)
                raw = {"kind": "submit", "examinations": [{"target_id": value["goal_id"],
                    "disposition": "examined", "evidence_ids": payload["active_evidence_ids"], "reason": "检查完整范围"}
                    for value in payload["goals"]], "candidates": [{"claim": "空列表访问首项抛错",
                    "primary_anchor": payload["goals"][0]["goal_id"], "evidence_ids": [anchor],
                    "affected_behavior": "空列表调用失败", "failure_scenario": "传入空列表时 values[0] 抛出 IndexError",
                    "recommendation": "访问前检查列表非空"}] if anchor else []}
            else:
                assert payload["task"]["kind"] == "verify"
                assert set(payload["active_evidence_ids"]) == set(payload["required_evidence_ids"])
                raw = {"kind": "submit", "verification": {"candidate_id": payload["candidate"]["candidate_id"],
                    "decision": "supported", "evidence_ids": payload["required_evidence_ids"],
                    "reason": "固定原文证实首项访问没有空列表保护"}}
            return AIMessage(content=json.dumps(raw), usage_metadata={"input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    result = await task_review_runtime_node(state)
    report = result["task_runtime_report"]
    assert report["scope_complete"] and report["findings"][0]["status"] == "confirmed", report
    kinds = [request["task"]["kind"] for request in requests]
    assert kinds.count("inspect") > 1 and kinds.count("verify") == 1
    assert report["sent_calls"] == len(requests) and report["settled_tokens"] == 15 * len(requests)


async def test_frozen_repository_scope_reads_caller_beyond_first_forty(production_input):
    state, provider, _ = production_input
    root = state["repo_path"]
    for index in range(55):
        from pathlib import Path
        Path(root, f"caller{index:02}.py").write_text("answer()\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-m", "callers"], check=True, capture_output=True)
    state["head_sha"] = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
        capture_output=True, encoding="utf-8").stdout.strip()
    requests = []
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            requests.append(payload)
            assert payload["readable_scope"]["file_count"] == 56
            assert len(payload["scope_files"]) <= 40
            if len(requests) == 1:
                raw = {"kind": "read", "tool": "file_read", "purpose": "核对远端调用方",
                       "arguments": {"file_path": "caller54.py", "start_line": 1, "end_line": 1}}
            else:
                assert any(value["file_path"] == "caller54.py" for value in payload["active_evidence"])
                raw = {"kind": "submit", "examinations": [{"target_id": value["goal_id"],
                    "disposition": "examined", "evidence_ids": payload["active_evidence_ids"],
                    "reason": "核对调用方"} for value in payload["goals"]]}
            return AIMessage(content=json.dumps(raw), usage_metadata={"input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    result = await task_review_runtime_node(state)
    assert result["task_runtime_report"]["scope_complete"] and len(requests) == 2


def test_executor_rejects_tampered_frozen_readable_scope():
    from app.models.review_runtime import ReviewRunSpec, ReviewGoal, ReviewWorkItem
    from app.services.review_task_executor import ReviewTaskExecutor
    spec = ReviewRunSpec(run_id="scope", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="diff", model_provider="fixture", model_name="fixture", policy_version="test",
        max_tokens=10, max_calls=1, readable_scope_files=("a.py", "caller.py", ".env"))
    goal = ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="a.py")
    item = ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"], scope_files=["a.py"],
                         readable_scope_hash=spec.readable_scope_hash)
    executor = ReviewTaskExecutor(spec, item, [goal], active_evidence_ids=set(), request_id="r", evidence_catalog={})
    for path in ("outside.py", ".env"):
        result = executor.parse_and_apply({"kind": "read", "tool": "file_read", "purpose": "read",
            "arguments": {"file_path": path, "start_line": 1, "end_line": 1}})
        assert result.response is None
    changed = spec.model_copy(update={"readable_scope_files": (*spec.readable_scope_files, "extra.py")})
    with pytest.raises(ValueError, match="frozen_readable_scope_hash_mismatch"):
        ReviewTaskExecutor(changed, item, [goal], active_evidence_ids=set(), request_id="r", evidence_catalog={})


async def test_p4_saved_configuration_keeps_its_original_scope(production_input):
    state, provider, calls = production_input
    state["task_runtime_configuration"].pop("readable_scope_version")
    state["_provider"] = provider
    result = await task_review_runtime_node(state)
    assert calls[0]["readable_scope"]["hash"] is None
    assert result["task_runtime_report"]["readable_scope_hash"] is None
    assert result["task_runtime_report"]["scope_complete"]


async def test_production_source_range_preserves_last_blank_line(production_input):
    from pathlib import Path
    state, provider, calls = production_input
    root = state["repo_path"]
    Path(root, "a.py").write_text("def answer():\n    return 2\n\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-m", "blank-line"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()
    diff = subprocess.run(["git", "-C", root, "diff", "--unified=3", state["base_sha"], head],
                          check=True, capture_output=True, encoding="utf-8").stdout
    changed = DiffParser().parse(diff)
    state.update(head_sha=head, changed_files=[value.model_dump(mode="json") for value in changed],
                 review_plan=DeterministicReviewPlanner().plan(changed, base_sha=state["base_sha"],
                     head_sha=head, model="fixture").model_dump(mode="json"), _provider=provider)
    result = await task_review_runtime_node(state)
    evidence = calls[0]["active_evidence"][0]
    assert len(evidence["content"].splitlines()) == evidence["end_line"] - evidence["start_line"] + 1 == 3
    assert result["task_runtime_report"]["scope_complete"]


def test_excluded_and_deleted_files_remain_in_initial_denominator(production_input):
    from app.graph.nodes.task_review_runtime import plan_runtime_goals
    from app.models.review import ChangedFile, ReviewPlan
    state, _, _ = production_input
    changed = [ChangedFile.model_validate(value) for value in state["changed_files"]]
    changed.append(ChangedFile(file_path="removed.py", change_type="deleted", additions=0, deletions=1))
    goals = plan_runtime_goals(changed, ReviewPlan.model_validate(state["review_plan"]))
    excluded = next(value for value in goals if value.target_ref == "removed.py")
    assert excluded.unsupported_reason and excluded.target_kind == "file_change"


def test_frozen_configuration_cannot_be_replaced(persistence):
    repository = persistence[0]
    task = ReviewTask(id="config", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
        task_runtime_configuration={"model": "frozen"})
    repository.create_task(task)
    task.task_runtime_configuration = {"model": "changed"}
    with pytest.raises(ValueError, match="configuration is frozen"):
        repository.save_task(task)


async def test_service_stream_and_retry_use_saved_snapshot_and_budget(production_input, persistence, monkeypatch):
    from app.services.review_service import ReviewService
    from app.tools.git_tool import GitTool
    from langgraph.checkpoint.memory import InMemorySaver
    import app.graph.review_graph as graph_module
    state, provider, calls = production_input
    repository = persistence[0]
    task = ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
                     task_runtime_configuration=state["task_runtime_configuration"])
    repository.create_task(task)
    service = ReviewService(None, GitTool(), DiffParser(), provider, ReportService(), repository=repository,
                            task_queue=DatabaseTaskQueue(persistence[2]))
    monkeypatch.setattr(service, "_ensure_worker_started", lambda: None)
    saver = InMemorySaver()
    async def checkpointer():
        return saver
    async def no_cleanup(*args):
        pass
    monkeypatch.setattr("app.services.review_service.get_checkpointer", checkpointer)
    monkeypatch.setattr("app.services.review_service._cleanup_repo", no_cleanup)
    prepares = []
    async def prepare(input_state):
        prepares.append(input_state["engine"])
        return {key: value for key, value in state.items() if key not in {"engine", "task_runtime_configuration"}}
    async def no_work(input_state):
        return {}
    monkeypatch.setattr(graph_module, "repo_prepare_node", prepare)
    for name in ("diff_parse_node", "repo_index_node", "project_detection_node", "review_plan_node"):
        monkeypatch.setattr(graph_module, name, no_work)
    original = provider._build_chat_model
    blocked = True
    class SDK:
        async def ainvoke(self, messages):
            result = await original("fixture", 4096).ainvoke(messages)
            raw = json.loads(result.content)
            if blocked:
                for value in raw["examinations"]:
                    value["disposition"] = "blocked"
            return result.model_copy(update={"content": json.dumps(raw)})
    provider._build_chat_model = lambda *args: SDK()
    await service._run_graph("run")
    current = repository.get_task("run")
    assert current.status == TaskStatus.completed_with_warnings
    assert not current.task_runtime_report["scope_complete"]
    unit_id = current.task_runtime_report["work_items"][0]["unit_id"]
    queued = await service.retry_unit("run", unit_id)
    assert queued.requeued_work_item_ids and prepares == ["task_review_v1"]
    blocked = False
    await service._run_graph("run")
    current = repository.get_task("run")
    assert current.status == TaskStatus.completed
    assert current.task_runtime_report["scope_complete"]
    assert current.task_runtime_configuration == state["task_runtime_configuration"]
    assert current.task_runtime_retry_unit_id is None
    assert prepares == ["task_review_v1"] and len(calls) == 2
    assert repository.load_run_budget("run")["settled_actual"] == 30


async def test_user_cancel_keeps_unknown_usage_and_rejects_old_worker(production_input, persistence):
    import asyncio
    state, provider, _ = production_input
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
        task_runtime_configuration=state["task_runtime_configuration"]))
    entered = asyncio.Event()
    release = asyncio.Event()
    class SDK:
        async def ainvoke(self, messages):
            entered.set()
            await release.wait()
            return AIMessage(content="{}", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    state["_coordination_repository"] = repository
    running = asyncio.create_task(task_review_runtime_node(state))
    await asyncio.wait_for(entered.wait(), 10)
    assert repository.cancel_task("run")
    cancelled = repository.get_task("run")
    assert cancelled.status == TaskStatus.cancelled
    assert cancelled.task_runtime_report["usage_integrity"] == "unknown"
    assert cancelled.task_runtime_report["unknown_attempts"] == 1
    assert cancelled.task_runtime_report["total_tokens"] is None
    saved_budget = repository.load_run_budget("run")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert repository.load_run_budget("run") == saved_budget
    assert repository.get_task("run").status == TaskStatus.cancelled


async def test_evaluation_selects_frozen_new_graph(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.evaluation.data import FrozenSample
    from app.evaluation.runner import run_sample
    from app.core.config import settings
    monkeypatch.setattr(settings, "repoguardian_review_engine", "task_review_v1")
    sample = FrozenSample(instance_id="local__sample@2222222", repository="local/sample",
        pr_url="https://github.com/local/sample/pull/1", base_sha="1" * 40, head_sha="2" * 40,
        language="Python", changed_lines=1)
    provider = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture")
    phases = []
    class Graph:
        async def astream(self, state, **kwargs):
            assert state["engine"] == "task_review_v1" and state["task_runtime_configuration"]
            yield {"report": {"status": "completed", "task_runtime_report": {
                "execution_status": "completed", "scope_complete": True}, "review_units": [],
                "review_unit_results": [], "review_issues": [], "review_coverage": {"review_complete": True}}}
    def build(**kwargs):
        phases.append(kwargs["phase"])
        return SimpleNamespace(compile=lambda: Graph())
    monkeypatch.setattr("app.evaluation.runner.build_review_graph", build)
    result = await run_sample(sample, provider, tmp_path, git_tool=object())
    assert phases == [3] and result["engine"] == "task_review_v1"
    assert result["status"] == "completed" and result["task_runtime_report"]["scope_complete"]


async def test_runtime_retry_does_not_reset_saved_deadline(production_input, persistence):
    import time
    state, provider, calls = production_input
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=state["pr_url"], engine="task_review_v1",
        task_runtime_configuration=state["task_runtime_configuration"]))
    original = provider._build_chat_model
    class SDK:
        async def ainvoke(self, messages):
            result = await original("fixture", 4096).ainvoke(messages)
            raw = json.loads(result.content)
            for item in raw["examinations"]:
                item["disposition"] = "blocked"
            return result.model_copy(update={"content": json.dumps(raw)})
    provider._build_chat_model = lambda *args: SDK()
    state.update(_provider=provider, _coordination_repository=repository)
    first = await task_review_runtime_node(state)
    saved = repository.load_task_review_runtime("run")
    saved["deadline_at"] = time.time() - 1
    revision = saved["revision"]
    saved["revision"] += 1
    repository.save_task_review_runtime("run", saved, revision)
    state["task_runtime_retry_unit_id"] = first["task_runtime_report"]["work_items"][0]["unit_id"]
    repeated = await task_review_runtime_node(state)
    assert len(calls) == 1
    assert repeated["task_runtime_report"]["termination_reason"] == "deadline"
    assert repeated["run_budget"]["settled_actual"] == 15


async def test_evaluation_observer_uses_frozen_profile_in_actual_provider(production_input):
    from app.evaluation.runner import ObservedProvider
    from app.services.model_request_budgeter import ModelRequestProfile
    state, provider, calls = production_input
    provider._profile_override = ModelRequestProfile(context_window=1_000)
    observed = ObservedProvider(provider)
    state["_provider"] = observed
    result = await task_review_runtime_node(state)
    assert result["task_runtime_report"]["scope_complete"]
    assert len(calls) == 1 and len(observed.calls) == 1
    assert provider._profile_override.context_window == 1_000
    assert result["run_budget"]["calls_consumed"] == 1
