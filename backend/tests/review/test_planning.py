from app.review.planning import plan_review


def change(path, length=1):
    return {"file_path": path, "hunks": [{"old_start": 1, "old_length": length,
        "new_start": 1, "new_length": length, "lines": []}]}


def edge(left, right, kind="imports", **extra):
    return {"source_kind": "file", "source": left, "target_kind": "file", "target": right,
            "type": kind, "confidence": 0.99, **extra}


def test_bounded_related_changes_keep_goals_and_stable_assignment_identity():
    changes = [change(path) for path in ("pay/service.py", "pay/api.py", "pay/models.py",
                                        "pay/tests/test_callback.py", "unrelated.py")]
    graph = {"edges": [edge("pay/api.py", "pay/models.py"),
        edge("pay/tests/test_callback.py", "pay/service.py"),
        edge("pay/service.py", "pay/models.py", kind="calls", source_kind="symbol",
            source="service", target="model", source_file="pay/service.py",
            target_file="pay/models.py", resolution_status="resolved") ]}
    symbols = [{"file": "pay/service.py", "symbol_id": "service", "start_line": 1, "end_line": 2}]
    plain = plan_review(changes, depth="standard")
    planned = plan_review(changes, depth="standard", repository_graph=graph, symbols=symbols)
    assert [item.id for item in plain[0]] == [item.id for item in planned[0]]
    behavior = [item for item in planned[1] if item.reviewer == "behavior"]
    assert sorted(len(item.focus_paths) for item in behavior) == [1, 4]
    assert len(set(goal for item in behavior for goal in item.goal_ids)) == len(planned[0])
    assert planned == plan_review(list(reversed(changes)), depth="standard",
        repository_graph={"edges": list(reversed(graph["edges"]))}, symbols=symbols)


def test_unresolved_calls_and_unrelated_imports_do_not_merge():
    changes = [change("a.py"), change("b.py")]
    for item in (edge("a.py", "b.py"), edge("a.py", "b.py", kind="calls",
                    source_file="a.py", target_file="b.py", resolution_status="unresolved")):
        _, assignments, _ = plan_review(changes, depth="standard", repository_graph={"edges": [item]})
        assert len(assignments) == 4 and all(len(item.focus_paths) == 1 for item in assignments)


def test_selected_scope_sensitive_binary_and_group_volume_boundaries():
    changes = [change("api/a.py", 500), change("models/a.py"), change("tests/test_a.py")]
    graph = {"edges": [edge("api/a.py", "models/a.py"), edge("tests/test_a.py", "models/a.py")]}
    _, assignments, _ = plan_review(changes, depth="standard", repository_graph=graph)
    assert all(len(item.focus_paths) <= 2 for item in assignments)
    assert all("api/a.py" not in item.focus_paths or len(item.focus_paths) == 1 for item in assignments)
    _, selected, excluded = plan_review(changes + [change(".env"),
        {**change("image.bin"), "is_binary": True}], depth="standard", repository_graph=graph,
        selected_paths={"models/a.py"})
    assert len(selected) == 2 and all(item.focus_paths == ["models/a.py"] for item in selected)
    assert set(excluded) == {"api/a.py", "tests/test_a.py", ".env", "image.bin"}


def test_assignment_identity_includes_goal_scope_and_objective():
    changes = [change("a.py")]
    baseline = plan_review(changes, depth="standard", focus=("correctness",))[1]
    changed_focus = plan_review(changes, depth="standard", focus=("security",))[1]
    changed_range = plan_review([change("a.py", 2)], depth="standard",
                                focus=("correctness",))[1]
    assert all(a.id != b.id for a, b in zip(baseline, changed_focus))
    assert all(a.id != b.id for a, b in zip(baseline, changed_range))


def test_weak_test_pair_and_unmodified_symbol_call_are_only_hints():
    changes = [change("a.py"), change("tests/test_a.py")]
    for item in (edge("tests/test_a.py", "a.py", kind="test_of", confidence=0.8),
                 edge("tests/test_a.py", "a.py", kind="calls", source_file="tests/test_a.py",
                      target_file="a.py", source="later", target="a", resolution_status="resolved")):
        _, assignments, _ = plan_review(changes, depth="standard", repository_graph={"edges": [item]},
            symbols=[{"file": "tests/test_a.py", "symbol_id": "later", "start_line": 30, "end_line": 40}])
        assert len(assignments) == 4
