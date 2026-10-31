import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.models.review.diff import ChangedFile
from app.review.context import AgentContext
from app.review.domain import Plan, SourceRef, Unit


def source(path="a.py", start=1, end=2, side="head"):
    return SourceRef(
        id="a" * 64, review_id="review", commit_sha=("b" if side == "head" else "c") * 40,
        side=side, path=path, start_line=start, end_line=end, content_hash="d" * 64,
    ).model_dump()


def packet(ref, handle="S1"):
    return {"ok": True, "source": ref, "source_ref": handle,
            "content": "SOURCE_BYTES_" * 100}


def test_compaction_is_append_only_and_keeps_the_first_source_body():
    initial = HumanMessage(content=json.dumps({"materials": [
        {"sources": [packet(source(start=1, end=100))]},
    ]}))
    messages = [
        initial,
        ToolMessage(content=json.dumps(packet(source(start=101, end=200), "S2")),
                    tool_call_id="second"),
        ToolMessage(content=json.dumps(packet(source(start=20, end=180), "S3")),
                    tool_call_id="overlap"),
    ]
    first_projection = AgentContext.compact_messages(messages)
    assert first_projection[0].content == initial.content
    assert "SOURCE_BYTES_" in first_projection[1].content
    overlap = json.loads(first_projection[2].content)
    assert overlap["content_omitted"] is True
    assert overlap["source_ref"] == "S3"
    assert overlap["source"]["start_line"] == 20
    assert "SOURCE_BYTES_" not in overlap["content"]
    assert "SOURCE_BYTES_" in messages[2].content
    following = [*messages, ToolMessage(
        content=json.dumps(packet(source(start=20, end=180, side="base"), "S4")),
        tool_call_id="base")]
    second_projection = AgentContext.compact_messages(following)
    assert [item.content for item in second_projection[:3]] == [
        item.content for item in first_projection]
    assert "SOURCE_BYTES_" in second_projection[-1].content


def test_model_prose_cannot_suppress_source_and_partial_overlap_keeps_new_bytes():
    model = AIMessage(content=json.dumps(packet(source(start=1, end=100))))
    official = ToolMessage(content=json.dumps(packet(source(start=1, end=100))),
                           tool_call_id="official")
    extending = ToolMessage(content=json.dumps(packet(source(start=90, end=110), "S2")),
                            tool_call_id="extending")
    projected = AgentContext.compact_messages([model, official, extending])
    assert projected[0].content == model.content
    assert "SOURCE_BYTES_" in projected[1].content
    assert "SOURCE_BYTES_" in projected[2].content


def test_repeated_search_results_keep_scope_and_cursor_control_fields():
    result = {"ok": True, "source": "search_hint", "pagination_id": "scope",
              "has_more": False, "next_cursor": None, "scope_complete": True,
              "matches": [{"path": "a.py", "line": 1, "text": "HINT_TEXT"}]}
    messages = [ToolMessage(content=json.dumps(result), tool_call_id=str(index),
                            name="search_code") for index in range(2)]
    projected = AgentContext.compact_messages(messages)
    assert "HINT_TEXT" in projected[0].content
    duplicate = json.loads(projected[1].content)
    assert duplicate["same_result_as_tool_call"] == "0"
    assert duplicate["scope_complete"] is True
    assert duplicate["next_cursor"] is None
    assert "matches" not in duplicate


@pytest.mark.parametrize("missing", [
    None, "materials", "tests", "consumer", "relations", "unrelated", "large", "verification",
])
def test_small_unit_checkpoint_requires_observed_materials_tests_and_related_consumer(missing):
    unit = Unit(id="unit", paths=["a.py", "tests/test_a.py"], required_hunk_ids=["h1", "h2"],
                risk="low", focus=["correctness"], estimated_input_tokens=100)
    changed = ChangedFile(file_path="a.py", change_type="modified", additions=10, deletions=0)
    context = AgentContext(
        provider=SimpleNamespace(run=SimpleNamespace(
            changed_files=[changed], plan=Plan(units=[unit]))),
        work=SimpleNamespace(unit_id="unit", kind="review"), budget=None)
    context.delivered_hunks = {"h1", "h2"}
    context.source_paths = {"a.py", "tests/test_a.py", "consumer.py"}
    context.related_paths = {"consumer.py"}
    context.investigation_tools = {"search_code"}
    if missing == "materials":
        context.delivered_hunks.remove("h2")
    elif missing == "tests":
        context.source_paths.remove("tests/test_a.py")
    elif missing == "consumer":
        context.source_paths.remove("consumer.py")
    elif missing == "relations":
        context.investigation_tools.clear()
    elif missing == "unrelated":
        context.related_paths.clear()
    elif missing == "large":
        changed.additions = 200
    elif missing == "verification":
        context.work.kind = "verification"
    assert context.small_unit_ready_for_checkpoint() is (missing is None)
