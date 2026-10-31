from app.models.review.diff import ChangedFile, DiffHunk, DiffLine
from app.review.domain import Limits, Options, Snapshot
from app.review.planner import MAX_UNIT_PATHS, members, plan_review, unit_capacity
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


def changed(*paths):
    return [ChangedFile(file_path=path, change_type="modified", additions=1, deletions=0)
            for path in paths]


def test_related_changes_share_a_unit_and_input_order_does_not_matter():
    changes = changed("a.py", "b.py", "c.py", "d.py", "e.py")
    relations = [(0, "a.py", "e.py")]
    plan = plan_review(changes, snapshot(), Options(), relations)
    assert any({"a.py", "e.py"} <= set(unit.paths) for unit in plan.units)
    assert plan == plan_review(list(reversed(changes)), snapshot(), Options(),
                               list(reversed(relations)))


def test_relation_components_stay_within_path_limit():
    paths = [f"{name}.py" for name in "abcdef"]
    relations = [(0, left, right) for left, right in zip(paths, paths[1:])]
    plan = plan_review(changed(*paths), snapshot(), Options(), relations)
    assert all(len(unit.paths) <= 10 for unit in plan.units)
    assert any(set(paths) <= set(unit.paths) for unit in plan.units)


def test_file_members_stay_together_when_capacity_allows():
    hunks = [
        DiffHunk(old_start=1, old_length=1, new_start=1, new_length=1, hunk_id="h1",
                 lines=[DiffLine(kind="added", content="x = 1", new_line_no=1)]),
        DiffHunk(old_start=10, old_length=1, new_start=10, new_length=1, hunk_id="h2",
                 lines=[DiffLine(kind="added", content="y = 2", new_line_no=10)]),
    ]
    changes = [ChangedFile(file_path="a.py", change_type="modified", additions=2,
                           deletions=0, hunks=hunks)]
    plan = plan_review(changes, snapshot(), Options())
    assert len(plan.units) == 1
    assert set(plan.units[0].required_hunk_ids) == set(members(changes))


def test_excluded_paths_never_join_relation_groups():
    changes = [
        ChangedFile(file_path=".env", change_type="modified", additions=1, deletions=0),
        *changed("a.py", "b.py"),
    ]
    relations = [(0, ".env", "a.py"), (0, "a.py", "b.py")]
    plan = plan_review(changes, snapshot(), Options(paths=["a.py"]), relations)
    assert [unit.paths for unit in plan.units] == [["a.py"]]


def test_excluded_members_are_not_tokenized(monkeypatch):
    import app.review.planner as planner

    changes = [
        *changed("selected.py", "bundle.js", ".env"),
        ChangedFile(file_path="image.png", change_type="modified", additions=0,
                    deletions=0, is_binary=True),
    ]
    costed = []
    original = planner.tokens

    def cost(member):
        costed.append(member["path"])
        return original(member)

    monkeypatch.setattr(planner, "tokens", cost)
    plan = plan_review(changes, snapshot(), Options(paths=["selected.py"]))
    assert [unit.paths for unit in plan.units] == [["selected.py"]]
    assert costed == ["selected.py"]


def test_large_file_keeps_all_members_in_one_unit(monkeypatch):
    import app.review.planner as planner

    hunks = [DiffHunk(old_start=index * 10 + 1, old_length=1,
                     new_start=index * 10 + 1, new_length=1, hunk_id=f"h{index}",
                     lines=[DiffLine(kind="added", content="x = 1", new_line_no=index * 10 + 1)])
             for index in range(3)]
    changes = [ChangedFile(file_path="large.py", change_type="modified", additions=3,
                           deletions=0, hunks=hunks)]
    monkeypatch.setattr(planner, "tokens", lambda member: 12_000)
    options = Options(limits=Limits(context_tokens=64_000))
    plan = plan_review(changes, snapshot(), options)
    assert len(plan.units) == 1
    assert set(plan.units[0].required_hunk_ids) == set(members(changes))
    assert plan.units[0].estimated_input_tokens > unit_capacity(options.limits)


def test_group_size_and_capacity_split_at_file_boundaries(monkeypatch):
    import app.review.planner as planner

    paths = [f"file{index:02}.py" for index in range(12)]
    changes = changed(*paths)
    plan = plan_review(changes, snapshot(), Options(), file_groups=[paths])
    assert all(len(unit.paths) <= MAX_UNIT_PATHS for unit in plan.units)
    assert sorted(path for unit in plan.units for path in unit.paths) == paths
    monkeypatch.setattr(planner, "tokens", lambda member: 8_000)
    options = Options(limits=Limits(context_tokens=64_000))
    plan = plan_review(changes[:3], snapshot(), options, file_groups=[paths[:3]])
    assert all(len(unit.paths) == 1 for unit in plan.units)


def test_small_changes_use_deterministic_size_rule():
    changes = changed("a.py", "b.py", "c.py")
    assert len(plan_review(changes, snapshot(), Options()).units) == 1
    changes[0].additions = 198
    assert len(plan_review(changes, snapshot(), Options()).units) == 3
