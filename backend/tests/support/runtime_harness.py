"""审查运行时测试支撑：模型响应桩、生产输入与切片构建。"""

from app.llm.providers import OpenAICompatibleProvider
from app.models.review import ReviewToolScope
from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.models.review_runtime import TaskRuntimeReport
from app.services.review.planning.review_planner import DeterministicReviewPlanner
from app.services.review.review_run_budget import ReviewRunBudget
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.review_task_derivation import TaskReviewDerivation
from app.services.review.runtime.review_task_executor import ReviewTaskExecutor
from app.services.review.runtime.review_task_step import InspectTaskStep
from app.tools.diff_parser import DiffParser
from datetime import datetime, timezone
from langchain_core.messages import AIMessage
from tests.support.runtime_v1_fixtures import evidence_catalog
import json
import pytest
import subprocess


def runtime_report(**updates):
    value = TaskRuntimeReport(
        execution_status="completed", termination_reason="plan_exhausted", scope_complete=True,
        initial_scope_coverage=[{"goal_id": "g1", "status": "examined"}],
        findings=[{"status": "unresolved", "candidate": {"claim": "候选命题"}, "reason": "证据不足"}],
        usage_integrity="complete", sent_calls=2, settled_tokens=30,
        unknown_attempts=0, work_items=[{"work_item_id": "w1", "unit_id": "u1", "state": "completed"}],
    )
    return value.model_copy(update=updates)


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


def candidate(*, claim="空返回值破坏调用方契约", scenario="服务返回空值时调用方直接读取字段", requests=None):
    return {"claim": claim, "primary_anchor": "t1", "evidence_ids": ["e1"],
            "affected_behavior": "调用方字段读取失败", "failure_scenario": scenario,
            "recommendation": "在读取返回值前处理空值", "evidence_requests": requests or []}


def inspect_submit(candidates=(), leads=()):
    return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
        "evidence_ids": ["e1"], "reason": "核对固定源码"}], "candidates": list(candidates), "leads": list(leads)}


def verification(payload, decision="supported"):
    assert payload["task"]["kind"] == "verify"
    assert "confidence" not in payload["candidate"]
    assert not {"question_updates", "obligation_updates", "review_record"} & set(payload)
    result = {"kind": "submit", "verification": {"candidate_id": payload["candidate"]["candidate_id"],
        "decision": decision, "evidence_ids": payload["active_evidence_ids"], "reason": "独立核对主张及必要反证"}}
    if payload.get("publication_contract") == "claim-publication-v1":
        result["verification"].update(subject_hash=payload["verification_subject_hash"],
            publication_action="retain" if decision == "supported" else "none")
    return result


def investigation(payload, answer="refuted", candidates=(), leads=()):
    assert payload["task"]["kind"] == "investigate" and payload["proposition"]
    return {"kind": "submit", "investigation": {"answer": answer,
        "evidence_ids": payload["active_evidence_ids"], "limitations": [], "reason": "核对具体关系"},
        "candidates": list(candidates), "leads": list(leads)}


def configured(tmp_path, responses, **kwargs):
    step, messages = make_slice(tmp_path, responses)
    step.executor.work_item.unit_id = "u1"
    runtime = TaskReviewRuntime([step.executor.work_item])
    steps = {"w1": step}
    derivation = TaskReviewDerivation(runtime, steps, **kwargs)
    return runtime, steps, derivation, messages


def make_slice(tmp_path, responses, *, max_calls=5):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("return value\n", encoding="utf-8")
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="openai-compatible", model_name="fixture",
        policy_version="p1", max_tokens=100_000, max_calls=max_calls)
    goal = ReviewGoal(goal_id="t1", unit_id="u1", kind="inspect", target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind="inspect", goal_ids=["t1"], scope_files=["src/a.py"])
    executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"},
        evidence_catalog=evidence_catalog("e1"), request_id="initial")
    provider = OpenAICompatibleProvider("stub-key", "https://example.test/v1", "fixture",
                                       request_attempts=1)
    messages = []

    class SDKChat:
        async def ainvoke(self, payload):
            messages.append(payload)
            response = responses.pop(0)
            if callable(response):
                response = response(json.loads(payload[1].content.split("\n")[0]))
            return AIMessage(content=response if isinstance(response, str) else json.dumps(response),
                             usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    class SnapshotGit:
        _git = "git"

        def _run(self, argv):
            return "a" * 40

        def get_file_content_at_revision(self, *args):
            return "return value\n"

    provider._build_chat_model = lambda *args: SDKChat()
    scope = ReviewToolScope(review_unit_id="w1", commentable_files={"src/a.py"},
        readable_files={"src/a.py"}, repository_root=str(tmp_path),
        max_lines_per_read=240, max_search_results=12)
    budget = ReviewRunBudget("run", max_tokens=spec.max_tokens, max_calls=max_calls)
    step = InspectTaskStep(executor, provider, budget, scope, git_tool=SnapshotGit())
    return step, messages
