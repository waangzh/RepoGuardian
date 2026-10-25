from copy import deepcopy

import pytest

from app.evaluation.data import sha256
from evaluation import check_smoke


@pytest.fixture
def smoke_result(tmp_path, monkeypatch):
    repository = tmp_path / "workspaces" / "sample"
    repository.mkdir(parents=True)
    for name in ("safe.py", "other.py", ".env"):
        (repository / name).write_text("fixture = True\n", encoding="utf-8")

    class GitStub:
        def _run(self, command):
            if "rev-parse" in command:
                return ("a" if command[-1] == "HEAD" else "b") * 40
            return ""

    monkeypatch.setattr(check_smoke, "GitTool", GitStub)
    return {
        "snapshot": {
            "repo_path": str(repository), "verified_base": "b" * 40,
            "diff_sha256": sha256(b""),
        },
        "sample": {"head_sha": "a" * 40, "base_sha": "b" * 40},
        "confirmed": [], "needs_human": [], "issue_audit": [],
        "model_requests": [], "status": "completed",
    }


@pytest.mark.parametrize("event", [
    {"tool": "file_read", "status": "completed", "results": ["safe.py"]},
    {"tool": "file_read", "status": "completed", "results": [{"file": "safe.py"}]},
    {"operation": "file_read", "arguments": {"file_path": "safe.py"},
     "observation": {"status": "completed", "evidence_ids": ["evidence"]}},
    {"operation": "file_read_diff", "arguments": {"file_path": "safe.py"},
     "observation": {"status": "completed", "result": {"content": "fixture"}}},
    {"operation": "file_find", "arguments": {"pattern": "*.py"},
     "observation": {"status": "completed", "result": {"paths": ["safe.py"]}}},
    {"operation": "code_search", "results": [{"file": "safe.py"}],
     "observation": {"status": "completed", "evidence_ids": ["evidence"]}},
])
def test_smoke_checks_legacy_and_runtime_tool_paths(smoke_result, tmp_path, event):
    event = {**event, "scope": {"readable_files": ["safe.py"]}}
    result = check_smoke.check_result({**smoke_result, "tool_reads": [event]}, tmp_path, [])
    assert result["snapshot"] == "passed"
    assert result["tool_paths"] == ["safe.py"]
    assert result["tool_path_failures"] == result["scope_failures"] == []
    assert result["tool_audit_gaps"] == []


@pytest.mark.parametrize("path", ["../outside.py", ".env"])
@pytest.mark.parametrize("runtime", [False, True], ids=["legacy", "runtime"])
def test_smoke_rejects_unsafe_tool_paths(smoke_result, tmp_path, path, runtime):
    event = (
        {"operation": "file_read", "arguments": {"file_path": path},
         "observation": {"status": "completed"}}
        if runtime else {"tool": "file_read", "status": "completed", "results": [path]}
    )
    event["scope"] = {"readable_files": [path]}
    result = check_smoke.check_result({**smoke_result, "tool_reads": [event]}, tmp_path, [])
    assert result["tool_path_failures"] == [path]


@pytest.mark.parametrize("runtime", [False, True], ids=["legacy", "runtime"])
def test_smoke_preserves_read_scope_rejections(smoke_result, tmp_path, runtime):
    event = (
        {"operation": "file_read", "arguments": {"file_path": "other.py"},
         "observation": {"status": "completed"}}
        if runtime else {"tool": "file_read", "status": "completed", "results": ["other.py"]}
    )
    event["scope"] = {"readable_files": ["safe.py"]}
    result = check_smoke.check_result({**smoke_result, "tool_reads": [event]}, tmp_path, [])
    assert result["scope_failures"] == ["other.py"]
    assert result["tool_path_failures"] == []


@pytest.mark.parametrize(("event", "reason"), [
    ({"operation": "file_read", "arguments": {"file_path": "safe.py"},
      "observation": {"status": "completed"}}, "readable_scope_not_recorded"),
    ({"operation": "code_search",
      "observation": {"status": "completed", "evidence_ids": ["evidence"]}},
     "result_paths_not_recorded"),
    ({"tool": "file_read", "status": "completed"}, "result_paths_not_recorded"),
    ({"operation": "file_read", "observation": {}}, "tool_status_not_recorded"),
])
def test_smoke_keeps_missing_tool_metadata_unverified(smoke_result, tmp_path, event, reason):
    original = deepcopy(event)
    result = check_smoke.check_result({**smoke_result, "tool_reads": [event]}, tmp_path, [])
    assert any(gap["reason"] == reason for gap in result["tool_audit_gaps"])
    assert any("无法完整核验" in finding for finding in result["findings"])
    assert event == original


@pytest.mark.parametrize("status", ["rejected", "blocked", "failed", "cancelled"])
def test_smoke_does_not_treat_failed_reads_as_completed(smoke_result, tmp_path, status):
    event = {"operation": "file_read", "arguments": {"file_path": "../outside.py"},
             "observation": {"status": status}}
    result = check_smoke.check_result({**smoke_result, "tool_reads": [event]}, tmp_path, [])
    assert result["tool_paths"] == result["tool_path_failures"] == []
    assert result["tool_audit_gaps"] == []


def test_smoke_does_not_invent_paths_for_empty_searches_or_discussions(smoke_result, tmp_path):
    events = [
        {"operation": "code_search", "observation": {"status": "completed", "evidence_ids": []}},
        {"operation": "discussion_read", "observation": {"status": "completed"}},
    ]
    result = check_smoke.check_result({**smoke_result, "tool_reads": events}, tmp_path, [])
    assert result["tool_paths"] == result["tool_audit_gaps"] == []
