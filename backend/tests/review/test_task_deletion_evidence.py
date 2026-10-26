"""删除类变更的 Base 侧证据、悬空引用与范围合法性回归。"""

from __future__ import annotations

import json
import subprocess

from langchain_core.messages import AIMessage

from app.llm.providers import OpenAICompatibleProvider
from app.graph.nodes.task_review_runtime import plan_runtime_goals, task_review_runtime_node
from app.models.review import ChangedFile
from app.services.review.planning.review_planner import DeterministicReviewPlanner
from app.services.review.review_task_derivation import dangling_deleted_references
from app.tools.diff_parser import DiffParser


def _unit(plan):
    return next(unit for unit in plan.review_units if "gone.py" in unit.primary_files)


def _repo_with_deleted_module(tmp_path):
    """Base 有 pkg/gone.py 与调用方；Head 删除该文件但调用方仍 import。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "gone.py").write_text("def check(user):\n    return user.is_admin\n",
                                          encoding="utf-8")
    (root / "caller.py").write_text("from pkg.gone import check\n\n\ndef run(user):\n    return check(user)\n",
                                    encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (root / "pkg" / "gone.py").unlink()
    git("add", "-A")
    git("commit", "-m", "head")
    head = git("rev-parse", "HEAD")
    diff = subprocess.run(["git", "-C", str(root), "diff", "--unified=80", base, head],
                          capture_output=True, check=True, encoding="utf-8").stdout
    return root, base, head, DiffParser().parse(diff)


def _state(root, base, head, changed, plan, provider=None):
    configuration = {"provider": "openai-compatible", "model": "fixture",
        "endpoint": "https://example.test/v1", "readable_scope_version": "repository-tracked-v1",
        "policy_version": "test-v1", "limits": {"max_tokens": 100_000, "max_calls": 20},
        "concurrency": 2, "deadline_seconds": 30, "request_attempts": 1, "request_timeout_seconds": 60,
        "request_profile": {"context_window": 32_000}, "max_lines_per_read": 240,
        "max_search_results": 12}
    state = {"task_id": "run", "engine": "task_review_v1", "task_runtime_configuration": configuration,
        "repo_path": str(root), "head_sha": head, "base_sha": base,
        "pr_url": "https://github.com/o/r/pull/1",
        "changed_files": [value.model_dump(mode="json") for value in changed],
        "review_plan": plan.model_dump(mode="json"),
        "review_units": [value.model_dump(mode="json") for value in plan.review_units],
        "file_index": [{"path": "caller.py", "language": "python",
                        "imports": ["pkg.gone"],
                        "import_refs": [{"module": "pkg.gone", "kind": "import", "line": 1,
                                         "confidence": 1.0, "parser_id": "python",
                                         "imported_name": "check", "local_name": "check"}]}],
        "_provider": provider}
    return state


def test_pure_deletion_hunk_never_produces_reversed_range():
    """-U0 风格纯删除 hunk：不再构造 [N,N-1]，也不崩溃。"""
    diff = """diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -10,2 +9,0 @@
-if not user.is_admin:
-    raise PermissionError()
"""
    changed = DiffParser().parse(diff)
    plan = DeterministicReviewPlanner().plan(changed, base_sha="a" * 40, head_sha="b" * 40, model="fixture")
    goals = plan_runtime_goals(changed, plan)
    assert goals, "纯删除目标必须仍进入分母"
    for goal in goals:
        assert goal.end_line >= goal.start_line, "不得构造反向区间"
        if goal.target_ref == "auth.py":
            assert goal.target_kind == "base_source_range" and goal.base_side
            assert (goal.start_line, goal.end_line) == (10, 11)


def test_deleted_file_goal_binds_base_side_range():
    """整文件删除：目标绑定 Base 行号，而不是 unsupported 的 file_change。"""
    diff = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index 1111111..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
"""
    changed = DiffParser().parse(diff)
    plan = DeterministicReviewPlanner().plan(changed, base_sha="a" * 40, head_sha="b" * 40, model="fixture")
    goals = plan_runtime_goals(changed, plan)
    goal = next(value for value in goals if value.target_ref == "gone.py")
    assert goal.unsupported_reason is None
    assert goal.target_kind == "base_source_range" and goal.base_side
    assert (goal.start_line, goal.end_line) == (1, 2)


def test_binary_and_excluded_files_keep_unsupported_denominator():
    """二进制与无 hunk 文件仍保留在分母，不被伪装成已审查。"""
    changed = [ChangedFile(file_path="image.png", change_type="modified", additions=0,
                           deletions=0, is_binary=True)]
    plan = DeterministicReviewPlanner().plan(
        [ChangedFile(file_path="image.png", change_type="modified", additions=1, deletions=0)],
        base_sha="a" * 40, head_sha="b" * 40, model="fixture")
    goals = plan_runtime_goals(changed, plan)
    assert [goal.unsupported_reason for goal in goals if goal.target_ref == "image.png"]


def test_dangling_deleted_references_finds_head_import():
    """Head 仍导入已删模块：产出可举证引用行，不猜符号语义。"""
    references = dangling_deleted_references(
        ["pkg/gone.py"],
        [{"path": "caller.py", "import_refs": [
            {"module": "pkg.gone", "line": 1, "imported_name": "check", "local_name": "check"}]},
         {"path": "pkg/gone.py", "import_refs": []}])
    assert references == [{"file_path": "caller.py", "references": [{
        "module": "pkg.gone", "line": 1, "imported_name": "check",
        "local_name": "check", "target_paths": ["pkg/gone.py"]}]}]


def test_dangling_deleted_references_ignores_sensitive_and_other_modules():
    """敏感路径与非 .py 路径不参与模块推导；无关导入不误报。"""
    assert dangling_deleted_references([".env"], [{"path": "caller.py", "import_refs": [
        {"module": ".env", "line": 1}]}]) == []
    assert dangling_deleted_references(["README.md"], [{"path": "caller.py", "import_refs": [
        {"module": "README", "line": 1}]}]) == []
    assert dangling_deleted_references(["pkg/gone.py"], [{"path": "caller.py", "import_refs": [
        {"module": "pkg.kept", "line": 1}]}]) == []


async def test_cleared_file_only_evidence_is_base_side_and_never_head(tmp_path):
    """清空文件（unidiff 判为 deleted）：只提供 Base 侧证据，且不伪造 Head 覆盖。"""
    root, base, head, changed = _repo_with_deleted_module(tmp_path)
    plan = DeterministicReviewPlanner().plan(changed, base_sha=base, head_sha=head, model="fixture")
    state = _state(root, base, head, changed, plan)
    requests = []

    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            requests.append(payload)
            assert not any(value.get("side", "head") == "base" and value["file_path"] == "caller.py"
                           for value in payload["active_evidence"])
            raw = {"kind": "submit", "examinations": [
                {"target_id": value["goal_id"], "disposition": "examined",
                 "evidence_ids": payload["active_evidence_ids"], "reason": "核对删除范围"}
                for value in payload["goals"]]}
            return AIMessage(content=json.dumps(raw),
                             usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider = OpenAICompatibleProvider("test-key", state["task_runtime_configuration"]["endpoint"],
                                        "fixture", request_attempts=1)
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    result = await task_review_runtime_node(state)
    deleted_goal = next(goal for goal in requests[0]["goals"] if goal["target_ref"] == "pkg/gone.py")
    assert deleted_goal["target_kind"] == "base_source_range"
    base_evidence = [value for value in requests[0]["active_evidence"]
                     if value["file_path"] == "pkg/gone.py"]
    assert base_evidence and all(value["side"] == "base" for value in base_evidence)
    outcomes = {value["target_ref"]: value["status"]
                for value in result["task_runtime_report"]["initial_scope_coverage"]}
    assert outcomes["pkg/gone.py"] == "examined"


def test_deleted_permission_check_hunk_binds_base_range():
    """删除权限条件：纯删除 hunk 在 Base 侧可核对，并保留被删行原文。"""
    diff = """diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -1,1 +0,0 @@
-if not user.is_admin: raise PermissionError()
"""
    changed = DiffParser().parse(diff)
    plan = DeterministicReviewPlanner().plan(changed, base_sha="a" * 40, head_sha="b" * 40, model="fixture")
    goals = plan_runtime_goals(changed, plan)
    goal = next(value for value in goals if value.target_ref == "auth.py")
    assert goal.unsupported_reason is None and goal.base_side
    canonical = next(line for line in changed[0].hunks[0].lines if line.kind == "deleted")
    assert canonical.old_line_no == 1 and "PermissionError" in canonical.content


def test_deleted_required_config_is_not_auto_unsupported():
    """删除必需配置项：配置文件的删除同样进入 Base 侧证据，而不是直接 unsupported。"""
    diff = """diff --git a/config/settings.yaml b/config/settings.yaml
deleted file mode 100644
index 1111111..0000000
--- a/config/settings.yaml
+++ /dev/null
@@ -1,1 +0,0 @@
-required_token: 30
"""
    changed = DiffParser().parse(diff)
    plan = DeterministicReviewPlanner().plan(changed, base_sha="a" * 40, head_sha="b" * 40, model="fixture")
    goals = plan_runtime_goals(changed, plan)
    goal = next(value for value in goals if value.target_ref == "config/settings.yaml")
    assert goal.unsupported_reason is None and goal.target_kind == "base_source_range"


async def test_deleted_module_reference_reaches_finding(tmp_path):
    """删除仍被调用的模块：悬空引用线索进入报告，不再只是 unsupported。"""
    from app.tools.repo_indexer import RepoIndexer
    root, base, head, changed = _repo_with_deleted_module(tmp_path)
    plan = DeterministicReviewPlanner().plan(changed, base_sha=base, head_sha=head, model="fixture")
    state = _state(root, base, head, changed, plan)
    state["task_runtime_configuration"].update(risk_planner_version="risk-planner-v6",
                                               prompt_version="task-review-prompt-v5",
                                               call_resolution_version="import-evidence-v5")
    indexed = await RepoIndexer().execute(repo_path=str(root), call_resolution_version="import-evidence-v5")
    state["file_index"] = indexed["file_index"]
    state["symbol_index"] = indexed["symbol_index"]
    state["repository_graph"] = indexed["repository_graph"]
    requests = []

    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            requests.append(payload)
            if payload["task"]["kind"] == "investigate":
                assert "pkg.gone" in payload["proposition"]
                raw = {"kind": "submit", "investigation": {
                    "answer": "supported", "evidence_ids": payload["active_evidence_ids"],
                    "reason": "调用方仍导入已删除模块 pkg.gone"},
                    "candidates": [{"claim": "caller.py 仍导入已删除的 pkg.gone",
                        "primary_anchor": payload["goals"][0]["goal_id"],
                        "evidence_ids": payload["active_evidence_ids"],
                        "affected_behavior": "运行时导入失败",
                        "failure_scenario": "导入 caller.py 时抛出 ModuleNotFoundError",
                        "recommendation": "删除或改写到替代实现", "category": "correctness",
                        "severity": "high"}]}
            else:
                raw = {"kind": "submit", "examinations": [
                    {"target_id": value["goal_id"], "disposition": "examined",
                     "evidence_ids": payload["active_evidence_ids"], "reason": "核对删除范围"}
                    for value in payload["goals"]]}
            return AIMessage(content=json.dumps(raw),
                             usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider = OpenAICompatibleProvider("test-key", state["task_runtime_configuration"]["endpoint"],
                                        "fixture", request_attempts=1)
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    result = await task_review_runtime_node(state)
    report = result["task_runtime_report"]
    leads = report["admitted_investigations"] + report["unscheduled_leads"] + report["relationship_hints"]
    assert any("pkg.gone" in json.dumps(lead, ensure_ascii=False) for lead in leads), report
    findings = report["findings"]
    assert findings and any("pkg.gone" in record["candidate"]["claim"] for record in findings), report
    assert not any(value["target_ref"] == "pkg/gone.py" and value["status"] == "unsupported"
                   for value in report["initial_scope_coverage"])
