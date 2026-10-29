from types import SimpleNamespace

import pytest

from app.review.context import AgentContext, ReviewContext
from app.review.models import ReviewLimits
from app.review.nodes.preparation import plan as execution_plan
from app.review.planning import _Unit, _merge, plan_review


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


def test_candidate_import_surfaces_unmodified_consumer_as_impacted_hint():
    changes = [change("models/order.py")]
    graph = {"edges": [edge("services/checkout.py", "models/order.py",
                            resolution_status="candidate",
                            relationship_level="import_candidate", confidence=0.8)]}
    plan = plan_review(changes, depth="standard", repository_graph=graph)
    assert plan.units[0].changed_paths == ["models/order.py"]
    assert plan.units[0].impacted_paths == ["services/checkout.py"]
    assert "services/checkout.py" in plan.assignments[0].objective


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
    # Packing stops once the budget fits instead of merging every legal pair.
    assert len(plan.units) == 21
    assert not plan.limitations
    assert not plan.uncovered
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


def test_rough_input_estimate_does_not_pre_drop_units():
    changes = [change("pkg/a.py"), change("other/b.py")]
    plan = plan_review(changes, depth="standard",
                       limits=ReviewLimits(total_model_calls=64, total_input_tokens=2500))
    assert plan.units and plan.assignments
    assert not plan.uncovered
    assert not any("累计输入预算不足" in item for item in plan.limitations)
    assert all(unit.estimated_input_tokens >= 1 for unit in plan.units)


def test_selected_files_survive_tiny_input_budget():
    changes = [change("pkg/a.py", content="x" * 500), change("other/b.py")]
    plan = plan_review(changes, depth="standard", selected_paths={"pkg/a.py"},
                       limits=ReviewLimits(total_model_calls=64, total_input_tokens=2500))
    assert [unit.changed_paths for unit in plan.units] == [["pkg/a.py"]]
    assert not plan.uncovered


def test_large_diff_is_not_pre_dropped_by_rough_input_estimate():
    lines = [{"kind": "added", "new_line_no": index + 1, "content": "x" * 100}
             for index in range(900)]
    changes = [{"file_path": "pkg/big.py", "hunks": [{
        "old_start": 1, "old_length": 900, "new_start": 1, "new_length": 900,
        "lines": lines}]}]
    plan = plan_review(changes, depth="quick",
                       limits=ReviewLimits(total_model_calls=64, total_input_tokens=100_000))
    assert plan.units
    assert not plan.uncovered
    # The serialized field remains a byte proxy, not a token count.
    assert plan.units[0].estimated_input_tokens > 80_000


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
@pytest.mark.parametrize("selected", [None, frozenset({"one/alpha.py", "three/charlie.py"})])
async def test_execution_regrouping_preserves_preview_admission(depth, selected):
    changes = [change("one/alpha.py", content="x" * 500),
               change("two/bravo.py", content="x"), change("three/charlie.py", content="x")]
    limits = ReviewLimits(total_model_calls=100, total_input_tokens=2500)
    preview = plan_review(changes, depth=depth, limits=limits, selected_paths=selected)
    admitted = {path for unit in preview.units for path in unit.changed_paths}
    assert not preview.uncovered
    events = []

    async def load():
        return AgentContext(evidence=None, changed_files=changes,
            repository_graph={"edges": [edge("two/bravo.py", "three/charlie.py")]})

    async def progress(event):
        events.append(event)

    context = ReviewContext(load=load, model=None, limits=limits, depth=depth,
        selected_paths=selected, focus=(), progress=progress)
    result = await execution_plan({}, SimpleNamespace(context=context))

    assert {path for unit in result["review_units"] for path in unit["changed_paths"]} == admitted
    assert {path for assignment in result["assignments"] for path in assignment["focus_paths"]} == admitted
    assert result["goals"] == [goal.model_dump() for goal in preview.goals]
    assert set(preview.limitations) <= set(result["coverage_limitations"])
    assert events[0]["limitations"] == result["coverage_limitations"]


async def test_execution_keeps_confirmed_groups_if_index_regrouping_would_drop_files():
    changes = [change(f"pkg/auth{index}.py") for index in range(8)]
    limits = ReviewLimits(total_model_calls=6)
    graph = {"edges": [edge(f"pkg/auth{left}.py", f"pkg/auth{right}.py")
                        for left, right in [(0, 4), (4, 6), (1, 5), (5, 7), (2, 3)]]}
    preview = plan_review(changes, depth="standard", limits=limits)
    assert not preview.uncovered
    indexed = plan_review(changes, depth="standard", limits=limits, repository_graph=graph)
    assert indexed.uncovered

    async def load():
        return AgentContext(evidence=None, changed_files=changes, repository_graph=graph)

    context = ReviewContext(load=load, model=None, limits=limits, focus=())
    result = await execution_plan({}, SimpleNamespace(context=context))
    assert result["review_units"] == [unit.model_dump() for unit in preview.units]
    assert result["assignments"] == [assignment.model_dump() for assignment in preview.assignments]
    assert result["coverage_limitations"] == preview.limitations


def test_metadata_only_change_is_reported_not_dispatched():
    metadata = {"file_path": "README.md", "hunks": [{"old_start": 0, "old_length": 0,
        "new_start": 0, "new_length": 0, "lines": []}]}
    plan = plan_review([metadata], depth="standard")
    assert not plan.units and not plan.assignments
    assert plan.goals[0].status == "blocked"
    assert plan.uncovered == {"README.md": "no_text_source_range"}
    assert any("无文本变更范围" in item for item in plan.limitations)


def test_pure_rename_dispatches_structural_reference_check_without_line_evidence():
    changes = [{"file_path": "pkg/billing.py", "old_file_path": "pkg/payment.py",
                "change_type": "renamed", "hunks": []}]
    plan = plan_review(changes, depth="standard")
    assert len(plan.units) == len(plan.assignments) == 1
    unit = plan.units[0]
    assert unit.changed_paths == ["pkg/billing.py"]
    assert unit.changed_hunk_ids == []
    assert "structural_change" in unit.risk_reasons
    structural = [goal for goal in plan.goals if goal.kind == "structural"]
    assert len(structural) == 1
    assert structural[0].side == "head" and structural[0].path == "pkg/billing.py"
    assert structural[0].start_line is None and structural[0].end_line is None
    assert "pkg/payment.py" in (structural[0].description or "")
    assert structural[0].id in unit.goal_ids
    assert structural[0].id in plan.assignments[0].goal_ids
    assert "结构性变更" in plan.assignments[0].objective
    assert not any(goal.status == "blocked" for goal in plan.goals)
    assert plan.uncovered == {}


def test_impacted_paths_exclude_sensitive_repository_paths():
    changes = [change("app/config.py")]
    graph = {"edges": [edge("app/config.py", ".env"),
                       edge("app/config.py", "app/other.py")]}
    plan = plan_review(changes, depth="standard", repository_graph=graph)
    assert plan.units[0].impacted_paths == ["app/other.py"]


def test_deleted_module_dispatches_structural_reference_check_without_fake_range():
    deleted = {"file_path": "pkg/payment.py", "change_type": "deleted",
               "hunks": [{"old_start": 1, "old_length": 1, "new_start": 0, "new_length": 0,
                          "lines": [{"kind": "deleted", "old_line_no": 1,
                                     "content": "def charge(): ..."}]}]}
    plan = plan_review([deleted], depth="standard")
    structural = [goal for goal in plan.goals if goal.kind == "structural"]
    assert len(structural) == 1
    assert structural[0].path == "pkg/payment.py" and structural[0].side == "base"
    assert structural[0].start_line is None and structural[0].end_line is None
    assert structural[0].id in plan.units[0].goal_ids
    assert any(goal.kind == "source" for goal in plan.goals)


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


def test_risk_ignores_context_deleted_and_common_identifiers():
    changes = [{"file_path": "pkg/block.py", "hunks": [{
        "old_start": 1, "old_length": 3, "new_start": 1, "new_length": 3,
        "lines": [
            {"kind": "context", "content": "async def handler(session):  # author"},
            {"kind": "deleted", "content": "lock = threading.Lock()"},
            {"kind": "added", "content": "value = 1"}]}]}]
    plan = plan_review(changes, depth="standard")
    assert plan.units[0].risk_tier == "low"


def test_risk_detects_sensitive_operations_in_added_lines():
    cases = [
        ("digest = hmac.compare_digest(a, b)", "sensitive_operation:crypto"),
        ("payload = pickle.loads(blob)", "sensitive_operation:deserialization"),
        ("subprocess.run(cmd)", "sensitive_operation:command_execution"),
        ("price = Decimal(raw)", "sensitive_operation:money"),
        ("target = os.path.join(root, name)", "sensitive_operation:path"),
    ]
    for content, reason in cases:
        plan = plan_review([change("pkg/mod.py", content=content)], depth="standard")
        assert plan.units[0].risk_tier == "high"
        assert reason in plan.units[0].risk_reasons


def test_risk_detects_sensitive_paths_and_public_api_changes():
    plan = plan_review([change("app/payment/charge.py", content="value = 1")],
                       depth="standard")
    assert plan.units[0].risk_tier == "high"
    assert "sensitive_path:money" in plan.units[0].risk_reasons
    symbols = [{"file": "app/api/orders.py", "symbol_id": "orders",
                "start_line": 1, "end_line": 1}]
    plan = plan_review([change("app/api/orders.py", content="value = 1")],
                       depth="standard", symbols=symbols)
    assert plan.units[0].risk_tier == "high"
    assert "public_api_change" in plan.units[0].risk_reasons


@pytest.mark.parametrize(("path", "content"), [
    ("pkg/mod.py", "# TODO: replace pickle with JSON"),
    ("pkg/mod.py", '"""Use subprocess for this example."""'),
    ("pkg/mod.py", 'r"""Use subprocess for this example."""'),
    ("pkg/mod.py", 'message = "COMMIT after reading the guide"'),
    ("pkg/mod.ts", "// TODO: replace eval(value)"),
    ("pkg/mod.ts", "/* Avoid execute(query) here. */"),
    ("pkg/mod.ts", 'const message = "wait() for a response";'),
    ("pkg/mod.vue", "<!-- Avoid execute(query) here. -->"),
    ("pkg/mod.sql", "-- BEGIN the example with COMMIT"),
    ("docs/guide.md", "BEGIN by reading the guide; COMMIT your changes."),
    ("docs/guide.md", "Example: subprocess.run(command)"),
])
def test_non_code_text_does_not_trigger_sensitive_operations(path, content):
    plan = plan_review([change(path, content=content)], depth="standard")
    assert plan.units[0].risk_tier == "low"
    assert plan.units[0].risk_reasons == []
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer"]


@pytest.mark.parametrize(("path", "opening", "closing"), [
    ("pkg/mod.py", '    """Example:', '    """'),
    ("pkg/mod.ts", "    /* Example:", "    */"),
])
def test_added_text_inside_context_comment_does_not_trigger_operations(path, opening, closing):
    changes = [{"file_path": path, "hunks": [{
        "old_start": 10, "old_length": 2, "new_start": 10, "new_length": 3,
        "lines": [
            {"kind": "context", "content": opening},
            {"kind": "added", "content": "    subprocess.run(command)"},
            {"kind": "context", "content": closing}]}]}]
    plan = plan_review(changes, depth="standard")
    assert plan.units[0].risk_tier == "low"
    assert plan.units[0].risk_reasons == []


def test_comment_filter_preserves_real_operations_and_ignores_deleted_quote_state():
    changes = [{"file_path": "pkg/mod.py", "hunks": [{
        "old_start": 1, "old_length": 2, "new_start": 1, "new_length": 1,
        "lines": [
            {"kind": "deleted", "content": '"""old subprocess example'},
            {"kind": "added", "content": 'subprocess.run(["echo", "# value"])  # example'},
            {"kind": "deleted", "content": '"""'}]}]}]
    plan = plan_review(changes, depth="standard")
    assert plan.units[0].risk_tier == "high"
    assert "sensitive_operation:command_execution" in plan.units[0].risk_reasons


@pytest.mark.parametrize(("path", "content", "reason"), [
    ("pkg/mod.py", 'value = f"{eval(raw)}"', "sensitive_operation:command_execution"),
    ("pkg/mod.ts", "const value = `${eval(raw)}`;", "sensitive_operation:command_execution"),
    ("pkg/mod.sql", "BEGIN;", "sensitive_operation:sql"),
    ("pkg/mod.py", 'cursor.execute("SELECT 1")  # query', "sensitive_operation:sql"),
])
def test_comment_filter_preserves_sensitive_code_and_interpolated_expressions(path, content, reason):
    plan = plan_review([change(path, content=content)], depth="standard")
    assert plan.units[0].risk_tier == "high"
    assert reason in plan.units[0].risk_reasons


@pytest.mark.parametrize("inside_symbol", [False, True])
def test_public_api_requires_code_changes_instead_of_context_or_comments(inside_symbol):
    lines = [
        {"kind": "deleted", "content": "# old title"},
        {"kind": "added", "content": "# new title"},
        {"kind": "context", "content": ""},
        {"kind": "context", "content": "def orders():"},
        {"kind": "context", "content": "    return []"},
    ]
    if inside_symbol:
        lines = [
            {"kind": "context", "content": "def orders():"},
            {"kind": "deleted", "content": "    # old title"},
            {"kind": "added", "content": "    # new title"},
            {"kind": "context", "content": "    return []"},
        ]
    changes = [{"file_path": "app/api/orders.py", "hunks": [{
        "old_start": 1, "old_length": 3 if inside_symbol else 4,
        "new_start": 1, "new_length": 3 if inside_symbol else 4, "lines": lines}]}]
    symbols = [{"file": "app/api/orders.py", "symbol_id": "orders",
                "start_line": 1 if inside_symbol else 3, "end_line": 3 if inside_symbol else 4}]
    plan = plan_review(changes, depth="standard", symbols=symbols)
    assert plan.units[0].risk_tier == "low"
    assert plan.units[0].changed_symbols == []
    assert "public_api_change" not in plan.units[0].risk_reasons
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer"]


@pytest.mark.parametrize("kind", ["added", "deleted"])
def test_public_api_still_detects_changed_code_inside_a_symbol(kind):
    changes = [{"file_path": "app/api/orders.py", "hunks": [{
        "old_start": 10, "old_length": 2 if kind == "added" else 3,
        "new_start": 10, "new_length": 3 if kind == "added" else 2,
        "lines": [
            {"kind": "context", "content": "def orders():"},
            {"kind": kind, "content": "    validate()"},
            {"kind": "context", "content": "    return []"}]}]}]
    symbols = [{"file": "app/api/orders.py", "symbol_id": "orders",
                "start_line": 10, "end_line": 12 if kind == "added" else 11}]
    plan = plan_review(changes, depth="standard", symbols=symbols)
    assert plan.units[0].risk_tier == "high"
    assert plan.units[0].changed_symbols == ["orders"]
    assert "public_api_change" in plan.units[0].risk_reasons


def test_deleted_code_before_public_symbol_does_not_mark_the_context_symbol_changed():
    changes = [{"file_path": "app/api/orders.py", "hunks": [{
        "old_start": 1, "old_length": 3, "new_start": 1, "new_length": 2,
        "lines": [
            {"kind": "deleted", "content": "module_flag = 1"},
            {"kind": "context", "content": "def orders():"},
            {"kind": "context", "content": "    return []"}]}]}]
    symbols = [{"file": "app/api/orders.py", "symbol_id": "orders",
                "start_line": 1, "end_line": 2}]
    plan = plan_review(changes, depth="standard", symbols=symbols)
    assert plan.units[0].risk_tier == "low"
    assert plan.units[0].changed_symbols == []
    assert "public_api_change" not in plan.units[0].risk_reasons


def test_public_api_raw_docstring_changes_do_not_mark_the_symbol_changed():
    changes = [{"file_path": "app/api/orders.py", "hunks": [{
        "old_start": 1, "old_length": 3, "new_start": 1, "new_length": 3,
        "lines": [
            {"kind": "context", "content": "def orders():"},
            {"kind": "deleted", "content": '    r"""Old documentation."""'},
            {"kind": "added", "content": '    r"""Replace pickle with JSON."""'},
            {"kind": "context", "content": "    return []"}]}]}]
    symbols = [{"file": "app/api/orders.py", "symbol_id": "orders",
                "start_line": 1, "end_line": 3}]
    plan = plan_review(changes, depth="standard", symbols=symbols)
    assert plan.units[0].risk_tier == "low"
    assert plan.units[0].changed_symbols == []
    assert plan.units[0].risk_reasons == []


def test_pure_sensitive_rename_stays_medium_when_grouped_with_benign_text_changes():
    changes = [
        {"file_path": "pkg/auth.py", "old_file_path": "pkg/access.py",
         "change_type": "renamed", "hunks": []},
        change("pkg/util.py", content="value = 1"),
    ]
    plan = plan_review(changes, depth="standard")
    assert len(plan.units) == 1
    assert plan.units[0].changed_paths == ["pkg/auth.py", "pkg/util.py"]
    assert plan.units[0].risk_tier == "medium"
    assert plan.units[0].risk_reasons == ["structural_change"]
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer"]


def test_comment_only_units_can_pack_within_the_discovery_budget():
    changes = [change(f"pkg{index}/mod{index}.py", content="# Replace pickle with JSON")
               for index in range(20)]
    plan = plan_review(changes, depth="standard", limits=ReviewLimits(total_model_calls=16))
    assert {path for unit in plan.units for path in unit.changed_paths} == {
        item["file_path"] for item in changes}
    assert all(unit.risk_tier == "low" for unit in plan.units)
    assert all(assignment.reviewer == "reviewer" for assignment in plan.assignments)
    assert not plan.uncovered


def test_budget_removes_only_enough_specialists():
    changes = [change(f"pkg{index}/mod{index}.py", content="subprocess.run(cmd)")
               for index in range(11)]
    plan = plan_review(changes, depth="standard", limits=ReviewLimits(total_model_calls=64))
    assert sum(assignment.reviewer == "specialist" for assignment in plan.assignments) == 10
    assert len(plan.limitations) == 1
    assert "专项风险" in plan.limitations[0]
    assert "pkg0/mod0.py" in plan.limitations[0]
    assert "按风险相关性排序移除" in plan.limitations[0]


def test_budget_keeps_contract_on_most_units():
    paths, edges = [], []
    for index in range(6):
        group = [f"m{index}a/x.py", f"m{index}b/x.py", f"m{index}c/x.py"]
        paths += group
        edges += [edge(group[0], group[1]), edge(group[1], group[2])]
    plan = plan_review([change(path, content="subprocess.run(cmd)") for path in paths],
                       depth="deep", limits=ReviewLimits(total_model_calls=64),
                       repository_graph={"edges": edges})
    assert len(plan.units) == 6
    assert sum(assignment.reviewer == "specialist" for assignment in plan.assignments) == 6
    assert sum(assignment.reviewer == "contract" for assignment in plan.assignments) == 5
    assert len(plan.limitations) == 1 and "契约核验" in plan.limitations[0]


def test_budget_packing_merges_low_risk_units_first():
    changes = [{**change("med0/a0.py"), "change_type": "added"},
               {**change("med1/a1.py"), "change_type": "added"},
               change("low0/b0.py"), change("low1/b1.py")]
    plan = plan_review(changes, depth="standard", limits=ReviewLimits(total_model_calls=9))
    assert len(plan.units) == 3
    merged = next(unit for unit in plan.units
                  if unit.changed_paths == ["low0/b0.py", "low1/b1.py"])
    assert merged.risk_tier == "low"
    assert not plan.limitations


def test_merge_refuses_to_drop_independent_roles():
    changes = {"a.py": {"hunks": []}, "b.py": {"hunks": []}}

    def unit(path, roles):
        return _Unit(paths=[path], hunk_ids=[], symbols=[], impacted=[], goals=[],
                     risk="medium", reasons=[], strategy="contextual_review",
                     estimated=2000, roles=list(roles), min_calls=2 * len(roles),
                     max_calls=12 * len(roles))

    assert _merge(unit("a.py", ["reviewer", "contract"]), unit("b.py", ["reviewer"]),
                  changes, "deep") is None
    assert _merge(unit("a.py", ["reviewer"]), unit("b.py", ["reviewer"]),
                  changes, "deep") is not None


def test_assignment_objective_exposes_risk_reasons():
    plan = plan_review([change("pkg/mod.py", content="subprocess.run(cmd)")],
                       depth="standard")
    assert [assignment.reviewer for assignment in plan.assignments] == ["reviewer", "specialist"]
    assert all("风险信号：sensitive_operation:command_execution" in assignment.objective
               for assignment in plan.assignments)


@pytest.mark.parametrize("low_count,total_calls", [(1, 12), (3, 9), (5, 9), (5, 4)])
def test_dispatch_prioritizes_risk_after_packing_or_scope_drops(low_count, total_calls):
    changes = [change("zz/auth.py", content="subprocess.run(cmd)")] + [
        change(f"pkg{index}/mod{index}.py") for index in range(low_count)]
    plan = plan_review(changes, depth="standard",
                       limits=ReviewLimits(total_model_calls=total_calls))
    assert plan.units[0].changed_paths == ["zz/auth.py"]
    assert plan.assignments[0].focus_paths == ["zz/auth.py"]
    tiers = [{"high": 0, "medium": 1, "low": 2}[unit.risk_tier] for unit in plan.units]
    assert tiers == sorted(tiers)


def test_budget_packs_reviewers_before_removing_specialist():
    changes = [change("auth/guard.py", content="subprocess.run(cmd)"),
               change("left/a.py"), change("right/b.py")]
    plan = plan_review(changes, depth="standard", limits=ReviewLimits(total_model_calls=9))
    assert len(plan.units) == 2
    assert [assignment.reviewer for assignment in plan.assignments
            if assignment.focus_paths == ["auth/guard.py"]] == ["reviewer", "specialist"]
    assert any(unit.changed_paths == ["left/a.py", "right/b.py"] for unit in plan.units)
    assert sum(unit.min_model_calls for unit in plan.units) == 6
    assert not plan.limitations and not plan.uncovered


def test_budget_retries_packing_before_removing_another_contract_role():
    groups = [[f"m{index}{part}/x.py" for part in "abc"] for index in range(2)]
    changes = [{**change(path), "change_type": "added"} for group in groups for path in group]
    changes.append(change("lonely/y.py"))
    edges = [edge(group[0], group[1]) for group in groups]
    edges += [edge(group[1], group[2]) for group in groups]
    plan = plan_review(changes, depth="deep", limits=ReviewLimits(total_model_calls=12),
                       repository_graph={"edges": edges})
    assert len(plan.units) == 2
    contracts = [assignment for assignment in plan.assignments if assignment.reviewer == "contract"]
    assert len(contracts) == 1 and contracts[0].focus_paths == groups[1]
    assert any(set(unit.changed_paths) == {*groups[0], "lonely/y.py"} for unit in plan.units)
    assert len(plan.limitations) == 1 and "契约核验" in plan.limitations[0]
    assert not plan.uncovered


async def test_execution_regrouping_does_not_keep_obsolete_packing_limits():
    changes = [change(f"pkg{index}/mod{index}.py") for index in range(4)]

    async def load():
        return AgentContext(evidence=None, changed_files=changes, repository_graph={"edges": [
            edge("pkg0/mod0.py", "pkg2/mod2.py"), edge("pkg1/mod1.py", "pkg3/mod3.py")]})

    result = await execution_plan({}, SimpleNamespace(context=ReviewContext(
        load=load, model=None, limits=ReviewLimits(total_model_calls=9))))
    assert {tuple(unit["changed_paths"]) for unit in result["review_units"]} == {
        ("pkg0/mod0.py", "pkg2/mod2.py"), ("pkg1/mod1.py", "pkg3/mod3.py")}
    assert not result["coverage_limitations"]


@pytest.mark.parametrize("total_calls,count", [(64, 11), (8, 3)])
async def test_execution_regrouping_reconciles_restored_roles_and_frozen_scope(total_calls, count):
    changes = [change(f"pkg{index}/mod{index}.py", content="subprocess.run(cmd)")
               for index in range(count)]
    limits = ReviewLimits(total_model_calls=total_calls)
    preview = plan_review(changes, depth="standard", limits=limits)
    assert any("专项风险" in limitation for limitation in preview.limitations)

    async def load():
        return AgentContext(evidence=None, changed_files=changes,
            repository_graph={"edges": [edge("pkg0/mod0.py", "pkg1/mod1.py")]})

    result = await execution_plan({}, SimpleNamespace(context=ReviewContext(
        load=load, model=None, limits=limits)))
    assert any(item["reviewer"] == "specialist"
               and "pkg0/mod0.py" in item["focus_paths"] for item in result["assignments"])
    admitted = {path for unit in preview.units for path in unit.changed_paths}
    assert {path for unit in result["review_units"] for path in unit["changed_paths"]} == admitted
    assert result["goals"] == [goal.model_dump() for goal in preview.goals]
    if preview.uncovered:
        assert len(result["coverage_limitations"]) == 1
        assert "整轮模型调用预算不足" in result["coverage_limitations"][0]
        assert all(path in result["coverage_limitations"][0] for path in preview.uncovered)
    else:
        assert not result["coverage_limitations"]
