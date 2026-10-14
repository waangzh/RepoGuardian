import asyncio
import json

import pytest

from app.services.review_planner import DeterministicReviewPlanner
from app.services.unit_coverage import build_diff_manifest
from app.tools.context_files import ScopedContextTool, ScopedContextToolError
from test_batch_contract_handoff import _scope
from test_large_unit_worksets import large_file, review_unit


def test_raw_and_manifest_hunk_ids_select_the_same_authorized_body():
    first = large_file(count=2, width=8)
    second = first.hunks[0].model_copy(update={"new_start": 20})
    target = first.model_copy(update={"hunks": [first.hunks[0], second]})
    manifest = build_diff_manifest(review_unit(), [target], "head", "base")
    tool = ScopedContextTool()
    scope = _scope({"a.py"}, {"a.py"})
    outputs = [asyncio.run(tool.file_read_diff(scope=scope, file_path="a.py", changed_files=[target],
        hunk_ids=[identity])) for identity in [manifest.hunks[1].hunk_id, manifest.hunks[1].id]]
    assert outputs[0] == outputs[1]
    assert outputs[0]["start_line"] == 20 and "@@ -1,0 +20,2 @@" in outputs[0]["content"]
    raw = DeterministicReviewPlanner.hunk_id("a.py", 1, second.model_dump(mode="json"))
    with pytest.raises(ScopedContextToolError, match="unknown hunk_id") as error:
        asyncio.run(tool.file_read_diff(scope=scope, file_path="a.py", changed_files=[target], hunk_ids=["stale"]))
    feedback = json.loads(str(error.value).split("; ", 1)[1])
    assert raw in feedback["accepted_hunk_ids"] and feedback["unknown_hunk_ids"] == ["stale"]
    with pytest.raises(ScopedContextToolError, match="readable scope"):
        asyncio.run(tool.file_read_diff(scope=_scope(set(), set()), file_path="a.py", changed_files=[target]))


def test_other_file_manifest_identity_is_rejected():
    target = large_file(count=2, width=8)
    foreign = target.model_copy(update={"file_path": "b.py"})
    identity = build_diff_manifest(review_unit(), [foreign], "head", "base").hunks[0].id
    with pytest.raises(ScopedContextToolError, match="unknown hunk_id"):
        asyncio.run(ScopedContextTool().file_read_diff(scope=_scope({"a.py"}, {"a.py", "b.py"}),
            file_path="a.py", changed_files=[target], hunk_ids=[identity]))
