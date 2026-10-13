from copy import deepcopy
import json

import pytest

from app.evaluation.runtime_source_layout import partition_source_case, layout_inventory, source_layout_prompt
from app.services.fingerprints import stable_hash


def case_fixture():
    return {"question": "compare source", "snapshot": {"base_sha": "b" * 40, "head_sha": "a" * 40},
        "evidence": [{"side": "head", "snapshot_sha": "a" * 40, "path": "src/a.py", "start_line": 10, "end_line": 15,
            "content": "def f(flag):\n    if flag:\n        value = external()\n    else:\n        value = other()\n    return value"},
            {"side": "base", "snapshot_sha": "b" * 40, "path": "src/a.py", "start_line": 10, "end_line": 11,
             "content": "def f(flag):\n    return other()"}]}


def test_partition_preserves_every_source_byte_and_provenance_without_semantic_labels():
    case = case_fixture()
    payload = json.loads(source_layout_prompt(case))
    assert payload["question"] == case["question"] and payload["snapshot"] == case["snapshot"]
    assert layout_inventory(payload) == sorted(case["evidence"], key=stable_hash)
    sections = payload["source_partitions"]["head"][0]["sections"]
    assert [item["start_line"] for item in sections] == [10, 11, 13]
    assert all(set(item) == {"start_line", "end_line", "content"} for item in sections)
    assert partition_source_case(deepcopy(case)) == payload


@pytest.mark.parametrize("problem", ["snapshot", "range", "gap"])
def test_invalid_snapshot_range_or_reconstruction_rejected(problem):
    case = case_fixture()
    if problem == "snapshot":
        case["evidence"][0]["snapshot_sha"] = "wrong"
        with pytest.raises(ValueError, match="snapshot"):
            partition_source_case(case)
    elif problem == "range":
        case["evidence"][0]["end_line"] += 1
        with pytest.raises(ValueError, match="range"):
            partition_source_case(case)
    else:
        payload = partition_source_case(case)
        payload["source_partitions"]["head"][0]["sections"][1]["start_line"] += 1
        with pytest.raises(ValueError, match="gap"):
            layout_inventory(payload)
