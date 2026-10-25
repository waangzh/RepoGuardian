import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.llm.providers import LLMProvider, OpenAICompatibleProvider
from app.core.config import settings
from app.evaluation.data import DatasetLock, FrozenSample, freeze_dataset, load_references
from app.evaluation.runner import FrozenGitHubTool, SnapshotGitTool, final_audit, run_dataset, run_sample
from app.models.review import ReviewIssue
from app.llm.model_request_budgeter import ModelRequestProfile
from app.review.issue_audit import audit_issue, capture_issue_audit
from tests.support.runtime_harness import production_input as production_input


class ScriptedProvider(LLMProvider):
    """评测编排测试的占位 Provider；不实现任何模型能力。"""

    async def summarize_pr_purpose(self, pr, changed_files, model):
        raise AssertionError("scripted evaluation provider never summarizes")


DIFF = '''diff --git a/sample.py b/sample.py
index 1111111..2222222 100644
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hi"
+    name = "RepoGuardian"
+    return f"hi {name}"
'''


class LocalSnapshotGit(SnapshotGitTool):
    def __init__(self, source, workdir, **kwargs):
        super().__init__(workdir, **kwargs)
        self.source = str(source)

    def clone_and_diff(self, pr, **kwargs):
        local = pr.model_copy(update={"clone_url": self.source,
            "base": pr.base.model_copy(update={"repo_clone_url": self.source}),
            "head": pr.head.model_copy(update={"repo_clone_url": self.source})})
        return super().clone_and_diff(local, **kwargs)


def current_evaluation_provider(calls, *, delay=0, candidates=True):
    provider = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture", request_attempts=1,
        request_profile=ModelRequestProfile(context_window=32_000))

    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.split("\n")[0])
            calls.append(payload)
            if delay:
                await asyncio.sleep(delay)
            if payload["task"]["kind"] == "verify":
                raw = {"kind": "submit", "verification": {
                    "candidate_id": payload["candidate"]["candidate_id"], "decision": "supported",
                    "evidence_ids": payload["active_evidence_ids"], "reason": "SDK 替身验证交付链",
                    "subject_hash": payload["verification_subject_hash"], "publication_action": "retain"}}
            else:
                raw = {"kind": "submit", "examinations": [{"target_id": goal["goal_id"],
                    "disposition": "examined", "evidence_ids": payload["active_evidence_ids"],
                    "reason": "核对固定变更范围"} for goal in payload["goals"]], "candidates": [], "leads": []}
                if candidates:
                    # 人工构造的候选仅验证协议与发布链，不作为真实缺陷判断。
                    raw["candidates"] = [{"claim": "受控测试候选", "primary_anchor": payload["goals"][0]["goal_id"],
                        "evidence_ids": payload["active_evidence_ids"], "affected_behavior": "返回值兼容性",
                        "failure_scenario": "调用方要求原返回值时出现差异", "recommendation": "核对调用方契约"}]
            return AIMessage(content=json.dumps(raw), usage_metadata={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    provider._build_chat_model = lambda *args: SDK()
    return provider


def raw_record():
    return {"githubPrUrl": "https://github.com/local/sample/pull/1",
            "source_commit": "1" * 40, "target_commit": "2" * 40,
            "project_main_language": "Python", "change_line_count": 3,
            "comments": [{"note": "SECRET_REFERENCE", "path": "sample.py", "side": "left",
                "from_line": 3, "to_line": 2, "category": "Code Defect", "context": "Repo Level",
                "is_ai_comment": True, "source_model": "source-model"}]}


def frozen(tmp_path):
    positive, negative = tmp_path / "positive.json", tmp_path / "negative.json"
    positive.write_text(json.dumps([raw_record()]), encoding="utf-8")
    negative.write_text("[]", encoding="utf-8")
    return freeze_dataset(positive, negative, "a" * 40), positive, negative


def test_freeze_and_reference_isolation(tmp_path):
    lock, positive, _ = frozen(tmp_path)
    assert "SECRET_REFERENCE" not in lock.model_dump_json()
    ref = load_references(lock, positive)[lock.samples[0].instance_id][0]
    assert (ref["from_line"], ref["to_line"]) == (2, 3)
    assert ref["context"] == "Repo Level" and ref["source_model"] == "source-model"
    assert load_references(lock, positive, profile="product_core")
    positive.write_bytes(positive.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        load_references(lock, positive)


@pytest.mark.parametrize("field,value", [("repository", "../sample"),
    ("base_sha", "main"), ("pr_url", "https://evil.test/local/sample/pull/1"),
    ("head_sha", "-" * 40)])
def test_snapshot_rejects_unsafe_identity(field, value):
    from app.evaluation.data import sample_from_record

    raw = sample_from_record(raw_record()).model_dump()
    raw[field] = value
    with pytest.raises(ValueError):
        FrozenSample.model_validate(raw)


async def test_frozen_metadata_has_no_live_body_or_labels(tmp_path):
    lock, _, _ = frozen(tmp_path)
    pr = await FrozenGitHubTool(lock.samples[0]).fetch_pr(lock.samples[0].pr_url)
    assert pr.body is None and "SECRET_REFERENCE" not in pr.model_dump_json()
    assert pr.base.sha == "1" * 40 and pr.head.ref == "2" * 40


async def test_real_production_graph_and_lifecycle(tmp_path, monkeypatch, production_input):
    lock, _, _ = frozen(tmp_path)
    state, _, _ = production_input
    sample = lock.samples[0].model_copy(update={"base_sha": state["base_sha"], "head_sha": state["head_sha"]})
    monkeypatch.setattr(settings, "repoguardian_model", "fixture")
    calls = []
    result = await run_sample(sample, current_evaluation_provider(calls), tmp_path / "workspaces",
        git_tool=LocalSnapshotGit(state["repo_path"], tmp_path / "workspaces"))
    assert result["status"] == "completed", result
    assert result["engine"] == result["manifest"]["engine"] == "task_review_v1"
    assert result["manifest"]["head_sha"] == sample.head_sha
    assert result["coverage"]["completed_units"] >= 1
    assert [call["task"]["kind"] for call in calls] == ["inspect", "verify"]
    assert {call["operation"] for call in result["model_calls"]} == {"task_review_step"}
    assert result["task_runtime_configuration"]["publication_contract"] == "claim-publication-v1"
    assert result["task_runtime_configuration"]["risk_planner_version"] == "risk-planner-v8"
    assert result["confirmed"] and not result["needs_human"]
    assert result["confirmed"][0]["side"] == "right"
    assert result["scoring_profiles"]["product_core"] == result["confirmed"]
    assert result["usage_summary"]["actual_total_tokens"] == result["run_budget"]["settled_actual"] == 30
    assert "SECRET_REFERENCE" not in json.dumps(result)


async def test_prepare_failure_and_timeout_preserve_results(tmp_path, production_input):
    lock, _, _ = frozen(tmp_path)
    class BrokenGit:
        def clone_and_diff(self, *args, **kwargs):
            raise RuntimeError("do not export arbitrary exception secrets")
    result = await run_sample(lock.samples[0], ScriptedProvider(), tmp_path,
                              git_tool=BrokenGit())
    assert result["status"] == "prepare_failed" and result["error_type"] == "RuntimeError"
    assert result["confirmed"] == [] and result["coverage"] is None
    state, _, _ = production_input
    sample = lock.samples[0].model_copy(update={"base_sha": state["base_sha"], "head_sha": state["head_sha"]})
    result = await run_sample(sample, current_evaluation_provider([], delay=10), tmp_path,
        timeout_seconds=0.5, git_tool=LocalSnapshotGit(state["repo_path"], tmp_path / "target"))
    assert result["status"] == "timed_out"


def test_audit_context_and_final_drops():
    issue = ReviewIssue(review_unit_id="u", severity="low", category="correctness",
        title="bad", failure_scenario="failure", affected_behavior="behavior",
        recommendation="fix", confidence=0.9,
        primary_evidence={"file_path": "x.py", "existing_code": "x", "expected_side": "head"})
    events = []
    with capture_issue_audit(events.append):
        audit_issue("generated", issue)
        audit_issue("unit_filter", issue, reason="primary_file_out_of_scope")
    audit_issue("generated", issue)
    assert len(events) == 2
    final = final_audit(events, {}, "completed")[-1]
    assert final["status"] == "dismissed" and final["reason"] == "primary_file_out_of_scope"


def test_snapshot_git_fetch_never_falls_back(tmp_path):
    from app.tools.git_tool import GitToolError
    tool = SnapshotGitTool(tmp_path)
    commands = []
    def fail(command, **kwargs):
        commands.append(command)
        raise GitToolError("missing commit")
    tool._run = fail
    with pytest.raises(GitToolError):
        tool._fetch_ref(tmp_path, "origin", "1" * 40, "main", phase="fetch_base",
                        phase_label="fetch", progress_callback=None, cancel_event=None)
    assert len(commands) == 1 and commands[0][-1] == "1" * 40


def test_audit_identity_is_scoped_by_unit_and_final_gap_is_explicit():
    events = [{"stage": "identity", "review_unit_id": unit, "issue_id": "old",
               "canonical_id": canonical, "status": "candidate", "reason": "identity"}
              for unit, canonical in [("u1", "new1"), ("u2", "new2")]]
    state = {"review_issues": [{"id": "new1", "review_unit_id": "u1", "status": "confirmed"},
                               {"id": "new2", "review_unit_id": "u2", "status": "needs_human"}]}
    final = final_audit(events, state, "completed")
    aliases = [x for x in final if x["stage"] == "final" and x["issue_id"] == "old"]
    assert [x["canonical_id"] for x in aliases] == ["new1", "new2"]
    assert [x["canonical_status"] for x in aliases] == [None, None]
    assert all(x["status"] == "not_published" and x["reason"] == "canonical_lifecycle_not_observed" for x in aliases)
    assert sum(x["stage"] == "audit_gap" for x in final) == 2


def test_snapshot_git_real_history(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    (repo / "x.py").write_text("x = 1\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "x.py").write_text("x = 2\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "head")
    head = git("rev-parse", "HEAD")
    from app.models.review import PullRequestInfo, PullRequestRef
    pr = PullRequestInfo(owner="local", repo="sample", number=1, title="fixture",
        html_url="https://github.com/local/sample/pull/1", clone_url=str(repo),
        base=PullRequestRef(ref="main", sha=base, repo_clone_url=str(repo)),
        head=PullRequestRef(ref="main", sha=head, repo_clone_url=str(repo)))
    tool = SnapshotGitTool(tmp_path / "clones")
    path, diff = tool.clone_and_diff(pr)
    assert (path / "x.py").read_text().strip() == "x = 2"
    assert "+x = 2" in diff and "-x = 1" in diff
    assert tool.snapshot["verified_head"] == head
    assert tool.snapshot["verified_base"] == base
    assert git("rev-parse", "HEAD") == head


async def test_run_manifest_records_all_failures_and_refuses_overwrite(tmp_path, monkeypatch):
    lock, _, _ = frozen(tmp_path)
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(lock.model_dump_json())
    with pytest.raises(ValueError, match="未知"):
        await run_dataset(lock_path, tmp_path / "unused", ScriptedProvider(), sample_ids=["unknown"])
    output = tmp_path / "results"
    # 不依赖网络：用空的模型配置错误模拟图入口准备失败。
    from unittest.mock import patch
    async def failed(sample, *args, **kwargs):
        return {"status": "prepare_failed", "sample": sample.model_dump()}
    with patch("app.evaluation.runner.run_sample", failed):
        summary = await run_dataset(lock_path, output, ScriptedProvider())
    assert summary["samples"][0]["status"] == "prepare_failed"
    assert summary["engine"] == summary["config"]["repoguardian_review_engine"] == "task_review_v1"
    assert summary["task_runtime_configuration"]["publication_contract"] == "claim-publication-v1"
    assert (output / "0000.json").exists()
    with pytest.raises(FileExistsError):
        await run_dataset(lock_path, output, ScriptedProvider())


async def test_dataset_freezes_runtime_configuration_across_samples(tmp_path, monkeypatch):
    lock, _, _ = frozen(tmp_path)
    second = lock.samples[0].model_copy(update={"instance_id": "local__sample@3333333"})
    lock.samples.append(second)
    lock.files["positive_samples.json"].records = 2
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(lock.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "repoguardian_model", "fixture")
    monkeypatch.setattr(settings, "repoguardian_run_max_calls", 5)
    seen = []

    async def run(sample, provider, workdir, **kwargs):
        seen.append(json.loads(json.dumps(kwargs["runtime_configuration"])))
        monkeypatch.setattr(settings, "repoguardian_model", "changed-default")
        monkeypatch.setattr(settings, "repoguardian_run_max_calls", 99)
        return {"status": "completed"}

    monkeypatch.setattr("app.evaluation.runner.run_sample", run)
    summary = await run_dataset(lock_path, tmp_path / "results", ScriptedProvider())
    assert len(seen) == 2 and seen[0] == seen[1] == summary["task_runtime_configuration"]
    assert seen[0]["model"] == "fixture" and seen[0]["limits"]["max_calls"] == 5


async def test_missing_current_runtime_report_cannot_fall_back_to_legacy_success(tmp_path, monkeypatch):
    lock, _, _ = frozen(tmp_path)

    class Graph:
        def compile(self):
            return self

        async def astream(self, initial, **kwargs):
            yield {"report": {"status": "completed", "review_units": [], "review_unit_results": [],
                              "review_coverage": {"review_complete": True}}}

    monkeypatch.setattr("app.evaluation.runner.build_review_graph", lambda **kwargs: Graph())
    result = await run_sample(lock.samples[0], ScriptedProvider(), tmp_path, git_tool=object())
    assert result["engine"] == "task_review_v1" and result["status"] == "failed"


def test_cli_rejects_quality_gate_without_experiment_plan(tmp_path, monkeypatch, capsys):
    from app.evaluation.__main__ import main

    monkeypatch.setattr("app.evaluation.isolation.protect_reviewer_process", lambda: None)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid experiment arguments must fail before provider construction")

    monkeypatch.setattr("app.llm.providers.build_provider", forbidden)
    with pytest.raises(SystemExit) as exc:
        main(["_review", "--lock", str(tmp_path / "lock.json"), "--output", str(tmp_path / "output"),
              "--quality-gate", str(tmp_path / "gate.json")])
    assert exc.value.code == 2
    assert "实验组和门必须绑定冻结清单" in capsys.readouterr().err


def test_frozen_local_dataset():
    path = Path(__file__).parents[1] / "evaluation" / "aacr.lock.json"
    if not path.exists():
        pytest.skip("本地冻结数据集未随代码快照交付；纯 fixture 回归仍执行")
    lock = DatasetLock.model_validate_json(path.read_bytes())
    assert len(lock.samples) == 196
    assert lock.files["positive_samples.json"].comments == 1506
    assert "comments" not in lock.samples[0].model_dump()


async def test_wire_request_audit_keeps_retries_and_payload(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.llm.providers import OpenAICompatibleProvider
    from app.review.run_audit import capture_run_audit

    class Chat:
        calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return AIMessage(content='{}', usage_metadata={
                "input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
    provider = OpenAICompatibleProvider("do-not-record-this-key", "https://example.test/v1", "test",
                                        retry_backoff_seconds=0)
    monkeypatch.setattr(provider, "_build_chat_model", lambda *args: Chat())
    events = []
    with capture_run_audit(model_sink=events.append, tool_sink=lambda _: None):
        result = await provider._request_json_content("user payload", None, "review", "system payload", 100)
    assert [x["status"] for x in events] == ["started", "failed", "started", "completed"]
    assert events[0]["prompt"] == "user payload" and events[0]["system"] == "system payload"
    assert result.usage.actual_total_tokens == 12
    assert result.usage.response_metadata["request_attempts"] == 2
    assert "do-not-record-this-key" not in json.dumps(events)
