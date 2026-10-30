from app.models.review.diff import ChangedFile
from app.review.domain import Options, Snapshot
from app.review.planner import members, plan_review
from app.tools.diff_parser import DiffParser


def snapshot():
    return Snapshot(review_id="run", repository="o/r", repository_id=1, pr_number=1,
                    declared_base_sha="a" * 40, base_sha="a" * 40, head_sha="b" * 40)


def test_small_pr_packs_without_parser_and_freezes_all_members():
    changes = DiffParser().parse(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "diff --git a/test_a.py b/test_a.py\n--- a/test_a.py\n+++ b/test_a.py\n"
        "@@ -1 +1 @@\n-assert x == 1\n+assert x == 2\n")
    plan = plan_review(changes, snapshot(), Options())
    assert len(plan.units) == 1
    assert set(plan.units[0].required_hunk_ids) == set(members(changes))
    assert plan == plan_review(changes, snapshot(), Options())


def test_structural_rename_and_deletion_have_required_members():
    changes = [ChangedFile(file_path="new.py", old_file_path="old.py",
                           change_type="renamed", additions=0, deletions=0),
               ChangedFile(file_path="empty.py", change_type="deleted", additions=0, deletions=0)]
    plan = plan_review(changes, snapshot(), Options())
    assert sum(len(unit.required_hunk_ids) for unit in plan.units) == 2
    assert all(key.startswith("structural-") for key in members(changes))


def test_sensitive_scope_is_excluded_and_cannot_be_selected():
    import pytest
    changes = [ChangedFile(file_path=".env", change_type="modified", additions=1, deletions=1)]
    assert not plan_review(changes, snapshot(), Options()).units
    with pytest.raises(ValueError, match="selected_files_not_eligible"):
        plan_review(changes, snapshot(), Options(paths=[".env"]))


def test_comments_do_not_become_operation_risk():
    changes = DiffParser().parse(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-# old\n+# subprocess.run(sql)\n")
    assert plan_review(changes, snapshot(), Options()).units[0].risk == "low"
