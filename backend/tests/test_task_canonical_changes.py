from copy import deepcopy

from app.models.review_runtime import ReviewGoal
from app.services.fingerprints import stable_hash
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.task_context_pages import build_context_pages
from runtime_v1_fixtures import evidence_catalog


def test_page_diff_context_preserves_deleted_lines_and_filters_other_pages():
    goal = ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="src/a.py", end_line=3)
    change = {"goal_id": "g", "hunk_id": "h", "hunk_hash": "hash", "old_start": 1, "old_length": 3,
        "new_start": 1, "new_length": 3, "lines": [
            {"kind": "context", "content": "a", "old_line_no": 1, "new_line_no": 1},
            {"kind": "deleted", "content": "old", "old_line_no": 2, "new_line_no": None},
            {"kind": "added", "content": "new", "old_line_no": None, "new_line_no": 2},
            {"kind": "context", "content": "b", "old_line_no": 3, "new_line_no": 3}]}
    catalog = {"e": {"canonical_change": change}}
    all_lines = TaskReviewV1Protocol.change_context([goal], {"e"}, catalog)[0]["lines"]
    fragments = [TaskReviewV1Protocol.change_context([goal.model_copy(update={"start_line": index,
        "end_line": index})], {"e"}, catalog)[0]["lines"] for index in range(1, 4)]
    assert [line for fragment in fragments for line in fragment] == all_lines == change["lines"]
    assert [line["kind"] for line in fragments[1]] == ["deleted", "added"]
    assert TaskReviewV1Protocol.change_context([goal], set(), catalog) == []


def test_context_pages_keep_blank_source_lines_and_canonical_hunk_identity():
    from app.models.review_runtime import ReviewWorkItem
    item = ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"], scope_files=["src/a.py"])
    goal = ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="src/a.py", end_line=3)
    catalog = evidence_catalog("e")
    body = "a\n\n\n"
    import hashlib
    catalog["e"].update(content=body, content_hash=hashlib.sha256(body.encode()).hexdigest(), end_line=3,
        canonical_change={"goal_id": "g", "hunk_id": "h", "lines": [], "hunk_hash": stable_hash([])})
    original = deepcopy(catalog["e"]["canonical_change"])
    pages = build_context_pages(item, [goal], catalog, capacity_chars=1)
    assert sum(value["end_line"] - value["start_line"] + 1 for page in pages for value in page.ranges) == 3
    for page in pages:
        evidence = catalog[page.evidence_ids[0]]
        assert evidence["canonical_change"] == original
        assert len(evidence["content"].splitlines()) == evidence["end_line"] - evidence["start_line"] + 1
