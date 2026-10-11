import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.evaluation.data import DatasetLock, FrozenSample, freeze_dataset, load_references
from app.evaluation.runner import FrozenGitHubTool, SnapshotGitTool, final_audit, run_dataset, run_sample
from app.models.review import ReviewIssue
from app.review.issue_audit import audit_issue, capture_issue_audit
from test_review_pipeline import FakeGitTool, ScriptedProvider


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


async def test_real_production_graph_and_lifecycle(tmp_path, monkeypatch):
    lock, _, _ = frozen(tmp_path)
    # 无 DB/checkpoint 路径不应构造生产持久化仓储。
    def forbidden(*args, **kwargs):
        raise AssertionError("production repository/cache accessed")
    monkeypatch.setattr("app.graph.nodes.review_units.ReviewRepository", forbidden)
    monkeypatch.setattr("app.graph.nodes.cross_unit_coordination.ReviewRepository", forbidden)
    result = await run_sample(lock.samples[0], ScriptedProvider(), tmp_path / "workspaces",
        git_tool=FakeGitTool(tmp_path / "target", DIFF))
    assert result["status"] == "completed", result
    assert result["manifest"]["head_sha"] == lock.samples[0].head_sha
    assert result["coverage"]["completed_units"] >= 1
    stages = {x["stage"] for x in result["issue_audit"]}
    assert {"generated", "unit_filter", "evidence", "policy", "verifier", "deduplication", "final"} <= stages
    assert result["confirmed"] and not result["needs_human"]
    assert result["confirmed"][0]["side"] == "right"
    assert result["scoring_profiles"]["product_core"] == []
    assert result["usage_summary"]["actual_total_tokens"] is None
    assert "SECRET_REFERENCE" not in json.dumps(result)


async def test_prepare_failure_and_timeout_preserve_results(tmp_path):
    lock, _, _ = frozen(tmp_path)
    class BrokenGit:
        def clone_and_diff(self, *args, **kwargs):
            raise RuntimeError("do not export arbitrary exception secrets")
    result = await run_sample(lock.samples[0], ScriptedProvider(), tmp_path,
                              git_tool=BrokenGit())
    assert result["status"] == "prepare_failed" and result["error_type"] == "RuntimeError"
    assert result["confirmed"] == [] and result["coverage"] is None
    class SlowProvider(ScriptedProvider):
        async def decide(self, *args, **kwargs):
            await asyncio.sleep(1)
    result = await run_sample(lock.samples[0], SlowProvider(), tmp_path,
        timeout_seconds=0.2, git_tool=FakeGitTool(tmp_path / "target", DIFF))
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


async def test_run_manifest_records_all_failures_and_refuses_overwrite(tmp_path):
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
    assert (output / "0000.json").exists()
    with pytest.raises(FileExistsError):
        await run_dataset(lock_path, output, ScriptedProvider())


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
    from app.agents.providers import OpenAICompatibleProvider
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
