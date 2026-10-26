import hashlib
import json

import pytest

from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.services.review.evidence.review_symbol_context import build_definition_hints
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol


def spec(version="task-review-prompt-v8", readable=("app/service.py", "app/builder.py")):
    return ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="stub", model_name="model",
        policy_version="p", max_tokens=100_000, max_calls=5, prompt_version=version,
        readable_scope_files=readable)


def work_item(run_spec=None, **updates):
    run_spec = run_spec or spec()
    value = {"work_item_id": "w1", "kind": "inspect", "goal_ids": ["g1"],
             "scope_files": ["app/service.py"], "readable_scope_hash": run_spec.readable_scope_hash}
    value.update(updates)
    return ReviewWorkItem(**value)


def catalog_with_change(*, goal_id="g1", path="app/service.py", content="result = build(payload)",
                        change_lines=None):
    content = content
    return {"e1": {
        "file_path": path, "start_line": 1, "end_line": len(content.splitlines()),
        "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "head_sha": "a" * 40, "base_sha": "b" * 40, "side": "head",
        "canonical_change": {"goal_id": goal_id, "hunk_id": "h1", "hunk_hash": "x",
            "old_start": 1, "old_length": 1, "new_start": 1, "new_length": 1,
            "lines": change_lines if change_lines is not None else [
                {"kind": "added", "content": "result = build(payload)", "new_line_no": 1,
                 "old_line_no": None}]}}}


def symbol(name, file="app/builder.py", start=10, end=20, signature=None, docstring=None,
           kind="function", symbol_id=None):
    value = {"file": file, "symbol": name, "type": kind, "start_line": start, "end_line": end,
             "signature": signature or f"def {name}()", "docstring": docstring}
    if symbol_id:
        value["symbol_id"] = symbol_id
    return value


def hints(**kwargs):
    options = {"goals": [ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect",
                                    target_ref="app/service.py", start_line=1, end_line=1)],
               "active_evidence_ids": {"e1"}, "catalog": catalog_with_change(),
               "symbol_index": [symbol("build")], "repository_graph": {},
               "readable_files": {"app/service.py", "app/builder.py"}}
    options.update(kwargs)
    return build_definition_hints(**options)


def test_hint_includes_signature_and_docstring_for_referenced_definition():
    result = hints(symbol_index=[symbol("build", signature="def build(payload: dict) -> Result",
                                        docstring="Build a result from a payload.")])
    assert len(result) == 1
    assert result[0]["file_path"] == "app/builder.py"
    assert result[0]["signature"] == "def build(payload: dict) -> Result"
    assert result[0]["docstring"] == "Build a result from a payload."
    assert result[0]["relation"] == "unique_name"
    assert result[0]["source"] == "static_symbol_index"


def test_hint_never_exposes_definitions_outside_readable_or_sensitive_scope():
    outside = hints(symbol_index=[symbol("build", file="vendor/other.py")])
    assert outside == []
    sensitive = hints(readable_files={"app/service.py", ".env"}, repository_graph={},
                      symbol_index=[symbol("build", file=".env")])
    assert sensitive == []


def test_goal_definition_and_already_visible_source_are_not_reinjected():
    overlapping = hints(symbol_index=[symbol("build", file="app/service.py", start=1, end=1)])
    assert overlapping == []


def test_ambiguous_definitions_are_skipped_instead_of_guessed():
    ambiguous = hints(symbol_index=[symbol("build", file="app/a.py"), symbol("build", file="app/b.py")],
                      readable_files={"app/service.py", "app/a.py", "app/b.py"})
    assert ambiguous == []


def test_single_same_file_definition_wins_over_ambiguous_global_names():
    result = hints(symbol_index=[symbol("build", file="app/service.py", start=30, end=40),
                                 symbol("build", file="app/a.py")],
                   readable_files={"app/service.py", "app/a.py"})
    assert len(result) == 1 and result[0]["file_path"] == "app/service.py"
    assert result[0]["relation"] == "same_file"


def test_resolved_call_edge_selects_the_actual_target():
    graph = {"edges": [{"type": "calls", "source": "app/service.py::run",
                        "target": "app/builder.py::build", "source_file": "app/service.py",
                        "target_file": "app/builder.py", "resolution_status": "resolved",
                        "relationship_level": "symbol"}]}
    result = hints(symbol_index=[symbol("build", file="app/builder.py", symbol_id="app/builder.py::build"),
                                 symbol("build", file="app/legacy.py")],
                   repository_graph=graph, readable_files={"app/service.py", "app/builder.py", "app/legacy.py"})
    assert len(result) == 1 and result[0]["file_path"] == "app/builder.py"
    assert result[0]["relation"] == "resolved_call"


def test_definition_hints_are_bounded_and_deterministic():
    names = [f"call{i}" for i in range(20)]
    change = [{"kind": "added", "content": " ".join(f"{name}()" for name in names),
               "new_line_no": 1, "old_line_no": None}]
    index = [symbol(name, file=f"app/m{index}.py") for index, name in enumerate(names)]
    readable = {"app/service.py", *(f"app/m{index}.py" for index in range(20))}
    first = hints(catalog=catalog_with_change(change_lines=change), symbol_index=index,
                  readable_files=readable)
    second = hints(catalog=catalog_with_change(change_lines=change), symbol_index=index,
                   readable_files=readable)
    assert 0 < len(first) <= 8
    assert first == second
    assert [value["file_path"] for value in first] == sorted(value["file_path"] for value in first)


def test_v8_payload_carries_hints_but_older_versions_stay_byte_identical():
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect", target_ref="app/service.py",
                      start_line=1, end_line=1)
    catalog = catalog_with_change()
    index = [symbol("build", docstring="Build a result.")]
    old_spec = spec("task-review-prompt-v7")
    item = work_item(old_spec)
    old = TaskReviewV1Protocol(old_spec).prepare(item, [goal],
        active_evidence_ids={"e1"}, evidence_catalog=catalog, symbol_index=index,
        repository_graph={})
    new_spec = spec()
    new = TaskReviewV1Protocol(new_spec).prepare(work_item(new_spec), [goal],
        active_evidence_ids={"e1"}, evidence_catalog=catalog, symbol_index=index,
        repository_graph={})
    old_payload, new_payload = json.loads(old.prompt), json.loads(new.prompt)
    assert "definition_hints" not in old_payload
    assert new_payload["definition_hints"][0]["symbol"] == "build"
    assert new_payload["definition_hints_source"] == "static_symbol_index"
    assert "NOT citable evidence" in new_payload["definition_hints_trust"]
    assert new_payload["definition_hints"][0]["file_path"] not in new_payload["active_evidence_ids"]


def test_v8_without_symbol_index_does_not_change_the_payload_shape():
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect", target_ref="app/service.py",
                      start_line=1, end_line=1)
    request = TaskReviewV1Protocol(spec()).prepare(work_item(), [goal], active_evidence_ids={"e1"},
        evidence_catalog=catalog_with_change())
    assert "definition_hints" not in json.loads(request.prompt)


@pytest.mark.parametrize("version", [f"task-review-prompt-v{index}" for index in range(1, 8)])
def test_legacy_system_messages_do_not_include_definition_hint_instructions(version):
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="inspect", target_ref="app/service.py",
                      start_line=1, end_line=1)
    run_spec = spec(version)
    request = TaskReviewV1Protocol(run_spec).prepare(
        work_item(run_spec), [goal], active_evidence_ids={"e1"},
        evidence_catalog=catalog_with_change(), symbol_index=[symbol("build")])
    assert "definition_hints" not in request.system
    assert "definition_hints" not in json.loads(request.prompt)


def test_investigate_prompt_does_not_advertise_unavailable_tools():
    goal = ReviewGoal(goal_id="g1", unit_id="u1", kind="investigate", target_ref="app/service.py",
                      start_line=1, end_line=1)
    item = work_item(kind="investigate", proposition="该调用是否仍然成立")
    request = TaskReviewV1Protocol(spec()).prepare(item, [goal], active_evidence_ids={"e1"},
        evidence_catalog=catalog_with_change())
    assert "symbol_info" not in request.system and "symbol_info" not in request.prompt
