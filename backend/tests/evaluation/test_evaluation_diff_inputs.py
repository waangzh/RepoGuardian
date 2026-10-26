import json
import subprocess
from pathlib import Path

import pytest

from app.evaluation.data import freeze_dataset
from app.evaluation.diff_inputs import (
    DiffInputLock, dataset_digest, inspect_diff, unique_merge_base,
)
from app.evaluation.runner import SnapshotGitTool, run_sample
from app.models.review import PullRequestInfo, PullRequestRef
from app.review.diff_base import review_diff_base
from app.tools.git_tool import GitTool, GitToolError
from tests.evaluation.test_evaluation import current_evaluation_provider


@pytest.fixture
def forked_history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, encoding="utf-8").stdout.strip()

    def commit(message):
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "-m", message)
        return git("rev-parse", "HEAD")

    git("init")
    (source / "x.py").write_text('def check():\n    return "root"\n', encoding="utf-8")
    root = commit("root")
    (source / "x.py").write_text('def check():\n    return "base-branch"\n', encoding="utf-8")
    (source / "base_only.py").write_text("base_only = True\n", encoding="utf-8")
    base = commit("base")
    git("checkout", "--detach", root)
    (source / "x.py").write_text('def check():\n    return "head"\n', encoding="utf-8")
    head = commit("head")
    pr = PullRequestInfo(owner="local", repo="case", number=1, title="fixture",
        html_url="https://github.com/local/case/pull/1", clone_url=str(source),
        base=PullRequestRef(ref=base, sha=base, repo_clone_url=str(source)),
        head=PullRequestRef(ref=head, sha=head, repo_clone_url=str(source)))
    record = {"githubPrUrl": pr.html_url, "source_commit": base, "target_commit": head,
              "project_main_language": "Python", "change_line_count": 2, "comments": []}
    positive, negative = tmp_path / "positive.json", tmp_path / "negative.json"
    positive.write_text(json.dumps([record]), encoding="utf-8")
    negative.write_text("[]", encoding="utf-8")
    dataset = freeze_dataset(positive, negative, "a" * 40)
    fact, _ = inspect_diff(GitTool(), source, dataset.samples[0],
                          [{"path": "x.py", "side": "left", "from_line": 2, "to_line": 2}])
    return source, git, root, base, head, pr, dataset, fact


def test_forked_snapshot_uses_locked_merge_base_and_excludes_base_branch(forked_history, tmp_path):
    source, _, root, base, head, pr, dataset, fact = forked_history
    tool = SnapshotGitTool(tmp_path / "clones", prepared_diff=fact)
    repo, diff = tool.clone_and_diff(pr)
    assert fact.effective_base_sha == root != base
    assert '-    return "root"' in diff and '+    return "head"' in diff
    assert "base_only.py" not in diff and "base-branch" not in diff
    assert (repo / "x.py").read_text().splitlines()[1].strip() == 'return "head"'
    assert tool.snapshot["verified_base"] == root
    assert tool.snapshot["declared_base_sha"] == base
    assert tool.snapshot["verified_head"] == head
    assert dataset.samples[0].base_sha == base


async def test_effective_base_reaches_evidence_manifest_and_preserves_pr_identity(forked_history, tmp_path):
    source, _, root, base, _, _, dataset, fact = forked_history

    class LocalGit(SnapshotGitTool):
        def clone_and_diff(self, pr, **kwargs):
            local = pr.model_copy(update={"clone_url": str(source),
                "base": pr.base.model_copy(update={"repo_clone_url": str(source)}),
                "head": pr.head.model_copy(update={"repo_clone_url": str(source)})})
            return super().clone_and_diff(local, **kwargs)

    calls = []
    result = await run_sample(dataset.samples[0], current_evaluation_provider(calls, candidates=False), tmp_path / "workspaces",
        prepared_diff=fact, git_tool=LocalGit(tmp_path / "workspaces", prepared_diff=fact))
    assert result["status"] in {"completed", "completed_with_warnings"}, result
    assert result["sample"]["base_sha"] == base
    assert result["manifest"]["base_sha"] == base
    assert result["diff_input"]["effective_base_sha"] == root
    assert result["engine"] == "task_review_v1" and result["task_runtime_report"]["scope_complete"]
    assert calls and all(call["snapshot"]["base_sha"] == root for call in calls)
    assert any(line["kind"] == "deleted" and 'return "root"' in line["content"]
               for call in calls for change in call["canonical_changes"] for line in change["lines"])
    from evaluation.check_smoke import check_result

    verification = check_result(result, tmp_path, [])
    assert verification["snapshot"] == "passed"
    assert verification["actual_base"] == root and verification["declared_base"] == base


async def test_preparation_context_comes_from_verified_tool(forked_history, tmp_path):
    from app.graph.nodes.repo_prepare import repo_prepare_node
    from unittest.mock import patch

    _, _, root, base, _, pr, _, fact = forked_history
    class GitHub:
        async def fetch_pr(self, _):
            return pr
    tool = SnapshotGitTool(tmp_path / "clones", prepared_diff=fact)
    # 直接节点调用也必须传播有效基准，不能依赖 Runner 预置状态。
    with patch("app.graph.nodes.repo_prepare.get_stream_writer", return_value=lambda _: None):
        state = await repo_prepare_node({"pr_url": pr.html_url, "_github_tool": GitHub(), "_git_tool": tool})
    assert state["base_sha"] == base and state["pr_info"]["base"]["sha"] == base
    assert state["effective_base_sha"] == root and state["diff_policy"] == "merge_base"


def test_input_binding_and_diff_hash_fail_closed(forked_history, tmp_path):
    _, _, _, _, _, pr, dataset, fact = forked_history
    lock = DiffInputLock(dataset_lock_sha256=dataset_digest(dataset), samples=[fact])
    lock.validate_dataset(dataset)
    with pytest.raises(ValueError, match="parent lock"):
        lock.model_copy(update={"dataset_lock_sha256": "0" * 64}).validate_dataset(dataset)
    for changed, message in [({"diff_sha256": "0" * 64}, "hash_mismatch"),
                             ({"effective_base_sha": "1" * 40}, "merge_base_mismatch")]:
        tool = SnapshotGitTool(tmp_path / "bad-clones", prepared_diff=fact.model_copy(update=changed))
        with pytest.raises(GitToolError, match=message):
            tool.clone_and_diff(pr)
    assert not list((tmp_path / "bad-clones").iterdir())


def test_reference_mismatch_quarantines_without_fetch(forked_history, tmp_path):
    source, _, _, _, _, pr, dataset, _ = forked_history
    fact, audit = inspect_diff(GitTool(), source, dataset.samples[0],
                              [{"path": "missing.py", "side": "right", "from_line": 1, "to_line": 1}])
    assert fact.alignment_status == "quarantined"
    assert audit["failures"][0]["reason"] == "reference_file_not_changed"
    with pytest.raises(GitToolError, match="not_ready"):
        SnapshotGitTool(tmp_path / "never-created", prepared_diff=fact).clone_and_diff(pr)
    assert not (tmp_path / "never-created").exists()


def test_multiple_merge_bases_rejected(forked_history):
    _, git, root, base, head, _, _, _ = forked_history
    tree = git("rev-parse", root + "^{tree}")
    def merge_commit(*parents):
        args = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return git(*args, "-m", "criss-cross")
    left, right = merge_commit(base, head), merge_commit(head, base)
    source = forked_history[0]
    with pytest.raises(GitToolError, match="unique_merge_base"):
        unique_merge_base(GitTool(), source, left, right)


def test_no_common_ancestor_and_missing_effective_base_rejected(forked_history):
    source, git, root, base, head, _, _, _ = forked_history
    tree = git("rev-parse", root + "^{tree}")
    unrelated = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit-tree", tree, "-m", "unrelated")
    with pytest.raises(GitToolError):
        unique_merge_base(GitTool(), source, unrelated, head)
    with pytest.raises(ValueError, match="effective_base_sha"):
        review_diff_base({"diff_policy": "merge_base", "base_sha": base})
    assert review_diff_base({"base_sha": base}) == base


def test_v2_lock_has_no_reference_answers():
    path = Path(__file__).parents[2] / "evaluation" / "aacr.diff-v2.json"
    lock = DiffInputLock.model_validate_json(path.read_bytes())
    assert len(lock.samples) == 5
    assert all(s.alignment_status == "ready" for s in lock.samples)
    assert "reference_index" not in lock.model_dump_json()
