import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.graph.nodes.patch import patch_node
from app.graph.nodes.repair_policy import repair_check_candidates_node, repair_policy_node
from app.models.review import (
    ExecutionBudget,
    PatchEligibilityDecision,
    PatchProposal,
    PatchGenerationResponse,
    PatchStatus,
    ReviewIssue,
    ReviewTask,
)
from app.services.patch_eligibility import PatchEligibilityPolicy
from app.services.patch_presentation import UNVERIFIED_PATCH_WARNING, build_patch_presentation
from app.services.review_rebuild import rebuild_patches
from app.tools.git_tool import GitTool
from app.tools.patch_tool import PatchTool


HEAD = "head-sha"
GOOD_DIFF = """diff --git a/module.py b/module.py
--- a/module.py
+++ b/module.py
@@ -1 +1 @@
-VALUE = 'head'
+VALUE = 'patched'
"""


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 'head'\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.test", "-c", "user.name=Test", "commit", "-m", "head"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _issue(**updates: object) -> ReviewIssue:
    payload = {
        "id": "issue-1",
        "review_unit_id": "unit-1",
        "title": "修复确定性错误",
        "category": "correctness",
        "severity": "medium",
        "confidence": 0.95,
        "affected_behavior": "输入会返回错误结果。",
        "failure_scenario": "确定输入触发确定错误。",
        "recommendation": "替换错误表达式。",
        "primary_evidence": {
            "file_path": "module.py",
            "existing_code": "VALUE = 'head'",
            "resolved_start_line": 1,
            "resolved_end_line": 1,
            "resolution_method": "diff_exact",
            "match_count": 1,
            "anchor_hash": "anchor",
            "resolved_side": "head",
        },
        "auto_fix_eligible": True,
        "status": "confirmed",
    }
    payload.update(updates)
    return ReviewIssue.model_validate(payload)


def _decision(**updates: object) -> PatchEligibilityDecision:
    payload = {
        "issue_id": "issue-1",
        "eligible": True,
        "reasons": ["eligible"],
        "allowed_files": ["module.py"],
        "max_files": 1,
        "max_changed_lines": 10,
    }
    payload.update(updates)
    return PatchEligibilityDecision.model_validate(payload)


def _patch(diff: str = GOOD_DIFF, **updates: object) -> PatchProposal:
    payload = {
        "issue_ids": ["issue-1"],
        "title": "修复 VALUE",
        "rationale": "证据唯一且替换范围有限。",
        "unified_diff": diff,
        "touched_files": ["module.py"],
        "risk": "low",
        "assumptions": [],
        "status": "suggested",
        "head_sha": HEAD,
    }
    payload.update(updates)
    return PatchProposal.model_validate(payload)


def test_non_confirmed_and_unresolved_issues_are_ineligible() -> None:
    policy = PatchEligibilityPolicy(confidence_threshold=0.8)
    candidate = policy.evaluate(_issue(status="candidate"))
    unresolved = policy.evaluate(_issue(primary_evidence={
        "file_path": "module.py",
        "existing_code": "VALUE = 'head'",
    }))

    assert candidate.eligible is False
    assert unresolved.eligible is False
    assert any("confirmed" in reason for reason in candidate.reasons)
    assert any("未唯一定位" in reason for reason in unresolved.reasons)


def test_high_fix_risk_is_ineligible() -> None:
    decision = PatchEligibilityPolicy().evaluate(_issue(fix_risk="high"))

    assert decision.eligible is False
    assert any("风险过高" in reason for reason in decision.reasons)


@pytest.mark.asyncio
async def test_candidate_unified_diff_parse_path_scope_and_size_policies(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tool = PatchTool()

    malformed = await tool.check_candidate(repo, _patch("not a diff"), _decision(), HEAD)
    traversal = await tool.check_candidate(
        repo,
        _patch("""diff --git a/../module.py b/../module.py
--- a/../module.py
+++ b/../module.py
@@ -1 +1 @@
-old
+new
"""),
        _decision(),
        HEAD,
    )
    unauthorized = await tool.check_candidate(
        repo,
        _patch(touched_files=["module.py"]),
        _decision(allowed_files=["other.py"]),
        HEAD,
    )
    too_large = await tool.check_candidate(
        repo,
        _patch(),
        _decision(max_changed_lines=1),
        HEAD,
    )
    mismatched_files = await tool.check_candidate(
        repo,
        _patch(touched_files=["other.py"]),
        _decision(),
        HEAD,
    )

    assert malformed.apply_check.status == "failed"
    assert "非法" in (traversal.error or "")
    assert "allowed_files" in (unauthorized.error or "")
    assert "变更行数" in (too_large.error or "")
    assert "touched_files" in (mismatched_files.error or "")


@pytest.mark.asyncio
async def test_file_count_and_binary_patches_are_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    multi = _patch(
        GOOD_DIFF + """diff --git a/second.py b/second.py
new file mode 100644
--- /dev/null
+++ b/second.py
@@ -0,0 +1 @@
+SECOND = True
""",
        touched_files=["module.py", "second.py"],
    )
    binary = _patch(
        """diff --git a/module.py b/module.py
GIT binary patch
literal 1
Ac$@<O00001
"""
    )

    file_result = await PatchTool().check_candidate(
        repo,
        multi,
        _decision(allowed_files=["module.py", "second.py"], max_files=1),
        HEAD,
    )
    binary_result = await PatchTool().check_candidate(repo, binary, _decision(), HEAD)

    assert "文件数" in (file_result.error or "")
    assert "二进制" in (binary_result.error or "")


@pytest.mark.asyncio
async def test_git_apply_failure_and_head_mismatch_are_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bad_apply = _patch(GOOD_DIFF.replace("VALUE = 'head'", "VALUE = 'missing'"))

    failed = await PatchTool().check_candidate(repo, bad_apply, _decision(), HEAD)
    stale = await PatchTool().check_candidate(repo, _patch(), _decision(), "new-head")

    assert failed.apply_check.status == "failed"
    assert "patch" in (failed.error or "").lower()
    assert stale.stale is True
    assert stale.status == "abandoned"


@pytest.mark.asyncio
async def test_candidate_check_restores_head_and_does_not_call_executor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    actual_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    class BombExecutor:
        async def execute(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("CommandExecutor must not be called")

    patch = _patch(head_sha=actual_head)
    result = await repair_check_candidates_node({
        "repo_path": str(repo),
        "head_sha": actual_head,
        "_git_tool": GitTool(workdir=tmp_path),
        "_command_executor": BombExecutor(),
        "patch_eligibility": [_decision().model_dump(mode="json")],
        "patches": [patch.model_dump(mode="json")],
        "pending_patch_ids": [patch.id],
    })

    checked = result["patches"][0]
    assert checked["status"] == "unverified"
    assert checked["apply_check"]["status"] == "passed"
    assert checked["apply_check"]["worktree_clean"] is True
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 'head'\n"
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout == ""


@pytest.mark.asyncio
async def test_revision_records_parent_and_attempt_number() -> None:
    parent = _patch(status="validation_failed")

    class RevisionProvider:
        async def generate_patch(self, state: dict, model: str | None) -> list[PatchProposal]:
            return [_patch()]

    result = await patch_node({
        "head_sha": HEAD,
        "active_patch_id": parent.id,
        "patches": [parent.model_dump(mode="json")],
        "execution_budget": ExecutionBudget().model_dump(),
        "_provider": RevisionProvider(),
        "next_action": {
            "action": "revise_patch",
            "reason": "根据确定性检查结果修订",
            "target_issue_ids": ["issue-1"],
        },
    })

    previous, revision = result["patches"]
    assert previous["status"] == "superseded"
    assert revision["revision_of"] == parent.id
    assert revision["attempt_number"] == 2
    assert revision["status"] == "unverified"


def test_head_change_supersedes_unverified_patch() -> None:
    rebuilt = rebuild_patches([_patch().model_dump(mode="json")], "new-head")

    assert rebuilt[0].stale is True
    assert rebuilt[0].status == PatchStatus.superseded


def test_small_replacement_uses_github_suggestion_and_api_warning() -> None:
    patch = _patch(status="unverified")
    patch.presentation = build_patch_presentation(patch)
    task = ReviewTask(id="task", pr_url="https://example.test/pr", patches=[patch])
    payload = task.model_dump(mode="json")

    assert patch.presentation.inline_suggestion is not None
    assert patch.presentation.full_diff is None
    assert payload["patches"][0]["presentation"]["warning"] == UNVERIFIED_PATCH_WARNING
    assert payload["patches"][0]["status"] == "unverified"


def test_complex_patch_uses_full_diff_presentation() -> None:
    patch = _patch(
        GOOD_DIFF + """diff --git a/second.py b/second.py
new file mode 100644
--- /dev/null
+++ b/second.py
@@ -0,0 +1 @@
+SECOND = True
""",
        touched_files=["module.py", "second.py"],
    )
    presentation = build_patch_presentation(patch)

    assert presentation.inline_suggestion is None
    assert presentation.full_diff == patch.unified_diff


def test_patch_generation_response_forbids_unknown_external_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PatchGenerationResponse.model_validate({
            "patches": [],
            "abandons": [],
            "command": "pytest",
        })


@pytest.mark.asyncio
async def test_zero_fixable_issues_finishes_repair_policy_normally() -> None:
    result = await repair_policy_node({
        "mode": "review_and_suggest",
        "generate_patches": True,
        "review_issues": [],
        "head_sha": HEAD,
        "execution_budget": ExecutionBudget().model_dump(),
    })

    assert result["repair_enabled"] is False
    assert result["patch_eligibility"] == []
    assert result["patch_generation_requests"] == []
