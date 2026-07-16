import subprocess
from pathlib import Path

import pytest

from app.graph.nodes.repair_policy import repair_apply_patch_node, repair_generate_patch_node
from app.graph.nodes.verification import patched_validation_node
from app.models.review import ExecutionBudget, PatchResult, TestRunResult as RunResult, ValidationSnapshot, ValidationStage
from app.projects.python import PythonProjectAdapter
from app.tools.git_tool import GitTool


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 'head'\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.test", "-c", "user.name=Test", "commit", "-m", "head"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def _add_marker_patch(content: str) -> str:
    return f"""diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+{content}
"""


def _invalid_patch() -> str:
    return """diff --git a/module.py b/module.py
--- a/module.py
+++ b/module.py
@@ -1 +1 @@
-VALUE = 'missing'
+VALUE = 'changed'
"""


async def _apply_candidate(state: dict) -> dict:
    result = await repair_apply_patch_node(state)
    return {**state, **result}


@pytest.mark.asyncio
async def test_each_candidate_is_applied_from_a_clean_head_worktree(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    first = PatchResult(diff_content=_add_marker_patch("first"))
    second = PatchResult(diff_content=_add_marker_patch("second"))
    state = {
        "repo_path": str(repo),
        "head_sha": head,
        "_git_tool": GitTool(workdir=tmp_path),
        "patches": [first.model_dump(mode="json"), second.model_dump(mode="json")],
        "pending_patch_ids": [first.id, second.id],
    }

    after_first = await _apply_candidate(state)
    assert after_first["patches"][0]["status"] == "applied"
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "first\n"

    after_second = await _apply_candidate(after_first)
    assert after_second["patches"][1]["status"] == "applied"
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "second\n"


@pytest.mark.asyncio
async def test_failed_apply_is_cleaned_before_the_next_candidate(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    failed = PatchResult(diff_content=_invalid_patch())
    second = PatchResult(diff_content=_add_marker_patch("second"))
    state = {
        "repo_path": str(repo),
        "head_sha": head,
        "_git_tool": GitTool(workdir=tmp_path),
        "patches": [failed.model_dump(mode="json"), second.model_dump(mode="json")],
        "pending_patch_ids": [failed.id, second.id],
    }

    after_failed = await _apply_candidate(state)
    assert after_failed["patches"][0]["status"] == "apply_failed"
    assert not (repo / "marker.txt").exists()
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 'head'\n"

    after_second = await _apply_candidate(after_failed)
    assert after_second["patches"][1]["status"] == "applied"
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "second\n"


@pytest.mark.asyncio
async def test_validation_failure_is_cleaned_before_the_next_candidate(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    first = PatchResult(diff_content=_add_marker_patch("first"))
    second = PatchResult(diff_content=_add_marker_patch("second"))
    profile = PythonProjectAdapter().detect(repo)
    assert profile is not None

    class FailingExecutor:
        async def execute(self, repo_path: str, spec) -> RunResult:  # type: ignore[no-untyped-def]
            return RunResult(
                tool=spec.tool,
                command=spec.command_id.value,
                exit_code=1,
                stdout="FAILED tests/test_example.py::test_failure - AssertionError: regression\n",
                passed=False,
            )

    state = {
        "repo_path": str(repo),
        "head_sha": head,
        "_git_tool": GitTool(workdir=tmp_path),
        "_command_executor": FailingExecutor(),
        "project_profile": profile.model_dump(mode="json"),
        "validation_snapshots": [
            ValidationSnapshot(stage=ValidationStage.head, sha=head, passed=True).model_dump(mode="json")
        ],
        "validation_deltas": [],
        "patches": [first.model_dump(mode="json"), second.model_dump(mode="json")],
        "pending_patch_ids": [first.id, second.id],
    }

    after_first = await _apply_candidate(state)
    after_validation = {
        **after_first,
        **await patched_validation_node(after_first),
    }
    assert after_validation["patches"][0]["status"] == "validation_failed"
    assert not (repo / "marker.txt").exists()

    after_second = await _apply_candidate(after_validation)
    assert after_second["patches"][1]["status"] == "applied"
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "second\n"


@pytest.mark.asyncio
async def test_revision_candidate_does_not_stack_on_failed_version(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    failed_revision = PatchResult(diff_content=_add_marker_patch("failed"), status="validation_failed")
    revision = PatchResult(
        diff_content=_add_marker_patch("revision"),
        revision_of=failed_revision.id,
        attempt_number=2,
    )
    state = {
        "repo_path": str(repo),
        "head_sha": head,
        "_git_tool": GitTool(workdir=tmp_path),
        "patches": [failed_revision.model_dump(mode="json"), revision.model_dump(mode="json")],
        "pending_patch_ids": [revision.id],
    }
    (repo / "marker.txt").write_text("failed\n", encoding="utf-8")

    result = await _apply_candidate(state)

    assert result["patches"][1]["status"] == "applied"
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "revision\n"


class RevisionProvider:
    async def generate_patch(self, state: dict, model: str | None) -> list[PatchResult]:
        return [PatchResult(diff_content=_add_marker_patch("revision"))]


@pytest.mark.asyncio
async def test_revision_records_parent_and_supersedes_the_previous_candidate() -> None:
    failed = PatchResult(diff_content=_add_marker_patch("failed"), status="validation_failed")

    result = await repair_generate_patch_node({
        "active_patch_id": failed.id,
        "patches": [failed.model_dump(mode="json")],
        "review_issues": [{"id": "issue-1", "auto_fixable": True}],
        "execution_budget": ExecutionBudget().model_dump(),
        "_provider": RevisionProvider(),
    })

    previous, revision = result["patches"]
    assert previous["status"] == "superseded"
    assert revision["revision_of"] == failed.id
    assert revision["attempt_number"] == 2


class PassingExecutor:
    async def execute(self, repo_path: str, spec) -> RunResult:  # type: ignore[no-untyped-def]
        return RunResult(
            tool=spec.tool,
            command=spec.command_id.value,
            exit_code=0,
            passed=True,
        )


@pytest.mark.asyncio
async def test_patched_snapshot_and_patch_share_the_same_patch_id(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    patch = PatchResult(diff_content=_add_marker_patch("verified"), status="applied")
    (repo / "marker.txt").write_text("verified\n", encoding="utf-8")
    profile = PythonProjectAdapter().detect(repo)
    assert profile is not None
    head_snapshot = ValidationSnapshot(stage=ValidationStage.head, sha=head, passed=True)

    result = await patched_validation_node({
        "repo_path": str(repo),
        "head_sha": head,
        "_git_tool": GitTool(workdir=tmp_path),
        "_command_executor": PassingExecutor(),
        "project_profile": profile.model_dump(mode="json"),
        "active_patch_id": patch.id,
        "patches": [patch.model_dump(mode="json")],
        "validation_snapshots": [head_snapshot.model_dump(mode="json")],
        "validation_deltas": [],
    })

    snapshot = result["validation_snapshots"][-1]
    assert snapshot["patch_id"] == patch.id
    assert result["validation_deltas"][-1]["patch_id"] == patch.id
    assert result["patches"][0]["validation_snapshot_id"] == snapshot["id"]
    assert result["patches"][0]["status"] == "validation_passed"
    assert not (repo / "marker.txt").exists()
