import pytest

from app.review.models import ReviewLimits
from app.review.planning import plan_review


def change(path, length=1, content=""):
    lines = [{"kind": "added", "new_line_no": 1, "content": content}] if content else []
    return {"file_path": path, "hunks": [{"old_start": 1, "old_length": length,
        "new_start": 1, "new_length": length, "lines": lines}]}


def edge(left, right, kind="imports", **extra):
    return {"source_kind": "file", "source": left, "target_kind": "file", "target": right,
            "type": kind, "confidence": 0.99, **extra}


def test_logical_change_group_uses_one_composite_reviewer():
    changes = [change("app/models/order.py"), change("app/services/order.py"),
               change("app/api/orders.py")]
    graph = {"edges": [edge("app/services/order.py", "app/models/order.py"),
                       edge("app/api/orders.py", "app/services/order.py")]}
    plan = plan_review(changes, depth="standard", repository_graph=graph)
    assert len(plan.units) == 1
    assert plan.units[0].changed_paths == [
        "app/api/orders.py", "app/models/order.py", "app/services/order.py"]
    assert [item.reviewer for item in plan.assignments] == ["reviewer"]
    assert plan.units[0].strategy == "contextual_review"
    assert plan.units[0].risk_tier == "medium"
    assert "cross_module" in plan.units[0].risk_reasons
    assert len({goal for item in plan.assignments for goal in item.goal_ids}) == len(plan.goals)
    assert plan.assignments[0].unit_id == plan.units[0].id


def test_high_risk_adds_specialist_and_deep_adds_independent_contract():
    changes = [change("app/models/user.py"), change("app/services/auth.py"),
               change("app/api/login.py")]
    graph = {"edges": [edge("app/services/auth.py", "app/models/user.py"),
                       edge("app/api/login.py", "app/services/auth.py")]}
    standard = plan_review(changes, depth="standard", repository_graph=graph)
    assert standard.units[0].risk_tier == "high"
    assert [item.reviewer for item in standard.assignments] == ["reviewer", "specialist"]
    deep = plan_review(changes, depth="deep", repository_graph=graph)
    assert deep.units[0].strategy == "deep_investigation"
    assert [item.reviewer for item in deep.assignments] == ["reviewer", "specialist", "contract"]
    quick = plan_review(changes, depth="quick", repository_graph=graph)
    assert quick.units[0].strategy == "fast_pass"
    assert [item.reviewer for item in quick.assignments] == ["reviewer"]


def test_resolved_imports_merge_related_changes_but_unresolved_calls_do_not():
    changes = [change("left/a.py"), change("right/b.py")]
    merged = plan_review(changes, depth="standard",
                         repository_graph={"edges": [edge("left/a.py", "right/b.py")]})
    assert len(merged.units) == 1 and len(merged.assignments) == 1
    split = plan_review(changes, depth="standard", repository_graph={"edges": [
        edge("left/a.py", "right/b.py", kind="calls", source_file="left/a.py",
             target_file="right/b.py", resolution_status="unresolved")]})
    assert len(split.units) == 2 and len(split.assignments) == 2


def test_same_directory_changes_form_one_unit():
    plan = plan_review([change("pkg/a.py"), change("pkg/b.py")], depth="standard")
    assert len(plan.units) == 1
    assert plan.units[0].changed_paths == ["pkg/a.py", "pkg/b.py"]


def test_bounded_related_changes_keep_goals_and_stable_identity():
    changes = [change(path) for path in ("pay/service.py", "pay/api.py", "pay/models.py",
                                        "pay/tests/test_callback.py", "unrelated.py")]
    graph = {"edges": [edge("pay/api.py", "pay/models.py"),
        edge("pay/tests/test_callback.py", "pay/service.py"),
        edge("pay/service.py", "pay/models.py", kind="calls", source_kind="symbol",
            source="service", target="model", source_file="pay/service.py",
            target_file="pay/models.py", resolution_status="resolved")]}
    symbols = [{"file": "pay/service.py", "symbol_id": "service", "start_line": 1, "end_line": 2}]
    plain = plan_review(changes, depth="standard")
    planned = plan_review(changes, depth="standard", repository_graph=graph, symbols=symbols)
    assert [item.id for item in plain.goals] == [item.id for item in planned.goals]
    assert sorted(len(unit.changed_paths) for unit in planned.units) == [1, 4]
    assert len(planned.assignments) == len(planned.units)
    assert len({goal for item in planned.assignments for goal in item.goal_ids}) == len(planned.goals)
    assert planned == plan_review(list(reversed(changes)), depth="standard",
        repository_graph={"edges": list(reversed(graph["edges"]))}, symbols=symbols)


def test_selected_scope_sensitive_binary_and_group_volume_boundaries():
    changes = [change("api/a.py", 500), change("models/a.py"), change("tests/test_a.py")]
    graph = {"edges": [edge("api/a.py", "models/a.py"), edge("tests/test_a.py", "models/a.py")]}
    plan = plan_review(changes, depth="standard", repository_graph=graph)
    assert all(len(unit.changed_paths) <= 2 for unit in plan.units)
    assert all("api/a.py" not in unit.changed_paths or len(unit.changed_paths) == 1
               for unit in plan.units)
    selected = plan_review(changes + [change(".env"),
        {**change("image.bin"), "is_binary": True}], depth="standard", repository_graph=graph,
        selected_paths={"models/a.py"})
    assert len(selected.units) == 1
    assert selected.units[0].changed_paths == ["models/a.py"]
    assert set(selected.excluded) == {"api/a.py", "tests/test_a.py", ".env", "image.bin"}


def test_unit_and_assignment_identity_include_scope_and_objective():
    baseline = plan_review([change("a.py")], depth="standard", focus=("correctness",))
    changed_focus = plan_review([change("a.py")], depth="standard", focus=("security",))
    changed_range = plan_review([change("a.py", 2)], depth="standard", focus=("correctness",))
    assert all(a.id != b.id for a, b in zip(baseline.assignments, changed_focus.assignments))
    assert all(a.id != b.id for a, b in zip(baseline.assignments, changed_range.assignments))
    assert baseline.units[0].id != changed_range.units[0].id


def test_weak_test_pair_and_unmodified_symbol_call_are_only_hints():
    changes = [change("pkg/a.py"), change("tests/test_a.py")]
    for item in (edge("tests/test_a.py", "pkg/a.py", kind="test_of", confidence=0.8),
                 edge("tests/test_a.py", "pkg/a.py", kind="calls", source_file="tests/test_a.py",
                      target_file="pkg/a.py", source="later", target="a",
                      resolution_status="resolved")):
        plan = plan_review(changes, depth="standard", repository_graph={"edges": [item]},
            symbols=[{"file": "tests/test_a.py", "symbol_id": "later",
                      "start_line": 30, "end_line": 40}])
        assert len(plan.units) == 2 and len(plan.assignments) == 2


def test_large_pr_packs_into_budget_and_reports_uncovered_scope():
    changes = [change(f"pkg{index}/mod{index}.py") for index in range(33)]
    paths = {item["file_path"] for item in changes}
    plan = plan_review(changes, depth="standard",
                       limits=ReviewLimits(total_model_calls=64))
    covered = {path for unit in plan.units for path in unit.changed_paths}
    assert covered == paths
    assert sum(unit.min_model_calls for unit in plan.units) <= 64
    assert not plan.limitations
    assert len(plan.assignments) == len(plan.units)

    tight = plan_review(changes, depth="standard",
                        limits=ReviewLimits(total_model_calls=8))
    covered = {path for unit in tight.units for path in unit.changed_paths}
    assert covered < paths
    assert tight.limitations
    dropped = paths - covered
    assert set(tight.uncovered) == dropped
    assert all(path in " ".join(tight.limitations) for path in dropped)


def test_added_files_pack_medium_units_within_budget():
    changes = [{**change(f"pkg{index}/mod{index}.py"), "change_type": "added"}
               for index in range(20)]
    plan = plan_review(changes, depth="standard",
                       limits=ReviewLimits(total_model_calls=16))
    covered = {path for unit in plan.units for path in unit.changed_paths}
    assert covered == {item["file_path"] for item in changes}
    assert all(unit.risk_tier == "medium" for unit in plan.units)
    assert not plan.uncovered


def test_quick_covers_more_small_units_than_standard_under_the_same_budget():
    changes = [change(f"pkg{index}/mod{index}.py") for index in range(20)]
    quick = plan_review(changes, depth="quick", limits=ReviewLimits(total_model_calls=8))
    standard = plan_review(changes, depth="standard", limits=ReviewLimits(total_model_calls=8))
    def covered(plan):
        return sum(len(unit.changed_paths) for unit in plan.units)

    assert covered(quick) == 20
    assert covered(standard) < covered(quick)
    assert all(unit.strategy == "fast_pass" for unit in quick.units)


def test_input_budget_limits_units_and_is_reported():
    changes = [change("pkg/a.py"), change("other/b.py")]
    plan = plan_review(changes, depth="standard",
                       limits=ReviewLimits(total_model_calls=64, total_input_tokens=2500))
    assert not plan.units and not plan.assignments
    assert any("累计输入预算不足" in item for item in plan.limitations)
    assert set(plan.uncovered.values()) == {"input_budget_insufficient"}


def test_metadata_only_change_is_reported_not_dispatched():
    metadata = {"file_path": "README.md", "hunks": [{"old_start": 0, "old_length": 0,
        "new_start": 0, "new_length": 0, "lines": []}]}
    plan = plan_review([metadata], depth="standard")
    assert not plan.units and not plan.assignments
    assert plan.goals[0].status == "blocked"
    assert plan.uncovered == {"README.md": "no_text_source_range"}
    assert any("无文本变更范围" in item for item in plan.limitations)


@pytest.mark.parametrize("prefix", ["app/", "backend/app/", "backend/src/app/", "frontend/src/"])
def test_source_container_prefixes_preserve_cross_module_risk_and_deep_contract(prefix):
    paths = [prefix + path for path in ("models/user.py", "services/auth.py", "api/login.py")]
    plan = plan_review([change(path) for path in paths], depth="deep",
        repository_graph={"edges": [edge(paths[0], path) for path in paths[1:]]})
    assert len(plan.units) == 1
    assert "cross_module" in plan.units[0].risk_reasons
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer", "specialist", "contract"]


def test_matching_module_names_in_separate_packages_remain_cross_module():
    paths = ["backend/app/api/user.py", "frontend/src/api/user.ts", "frontend/src/api/user.vue"]
    plan = plan_review([change(path) for path in paths], depth="deep",
        repository_graph={"edges": [edge(paths[0], path) for path in paths[1:]]})
    assert "cross_module" in plan.units[0].risk_reasons
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer", "contract"]
