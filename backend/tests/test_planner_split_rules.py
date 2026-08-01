from app.models.review import ChangedFile, DiffHunk
from app.services.review_planner import DeterministicReviewPlanner


def _file(path: str, *, additions: int = 1, hunks: list[DiffHunk] | None = None) -> ChangedFile:
    return ChangedFile(
        file_path=path,
        change_type="modified",
        additions=additions,
        deletions=0,
        hunks=hunks or [],
    )


def test_api_handler_is_grouped_with_direct_model() -> None:
    files = [_file("app/api/users.py"), _file("app/models/user.py")]
    plan = DeterministicReviewPlanner().plan(
        files,
        base_sha="base",
        head_sha="head",
        file_index=[
            {"path": "app/api/users.py", "imports": ["user"]},
            {"path": "app/models/user.py", "imports": []},
        ],
    )

    assert len(plan.review_units) == 1
    assert plan.review_units[0].grouping_reason == "api_with_model"
    assert set(plan.review_units[0].primary_files) == {"app/api/users.py", "app/models/user.py"}


def test_migration_is_grouped_with_corresponding_model() -> None:
    files = [_file("migrations/001_add_user_table.py"), _file("app/models/user.py")]
    plan = DeterministicReviewPlanner().plan(files, base_sha="base", head_sha="head")

    assert len(plan.review_units) == 1
    assert plan.review_units[0].grouping_reason == "migration_with_model"
    assert set(plan.review_units[0].primary_files) == {
        "migrations/001_add_user_table.py",
        "app/models/user.py",
    }


def test_large_file_is_split_by_changed_symbol_before_hunk() -> None:
    hunks = [
        DiffHunk(
            old_start=1,
            old_length=1,
            new_start=1,
            new_length=1,
            added_lines=[{"line_no": 2, "content": "    return 1"}],
        ),
        DiffHunk(
            old_start=20,
            old_length=1,
            new_start=20,
            new_length=1,
            added_lines=[{"line_no": 21, "content": "    return 2"}],
        ),
    ]
    planner = DeterministicReviewPlanner(large_min_changed_lines=2)
    plan = planner.plan(
        [_file("large.py", additions=2, hunks=hunks)],
        base_sha="base",
        head_sha="head",
        symbol_index=[
            {"file": "large.py", "symbol": "first", "start_line": 1, "end_line": 5},
            {"file": "large.py", "symbol": "second", "start_line": 20, "end_line": 25},
        ],
    )

    assert [unit.changed_symbols for unit in plan.review_units] == [["first"], ["second"]]
    assert {unit.grouping_reason for unit in plan.review_units} == {"large_file_symbol_split"}
