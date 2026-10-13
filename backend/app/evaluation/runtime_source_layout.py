"""只按快照和显式源码边界重排证据，不注入语义解释或评审答案。"""

import json
import re

from app.services.fingerprints import stable_hash


def partition_source_case(case):
    partitions = {"base": [], "head": []}
    for item in case["evidence"]:
        side = item["side"]
        if side not in partitions or item["snapshot_sha"] != case["snapshot"][side + "_sha"]:
            raise ValueError("source layout snapshot mismatch")
        lines = item["content"].splitlines(keepends=True)
        if len(lines) != item["end_line"] - item["start_line"] + 1:
            raise ValueError("source layout range mismatch")
        starts = [0]
        # 只处理片段中完整显示的单行 if/elif/else 头；不猜测片段之外的控制流。
        starts.extend(index for index, line in enumerate(lines) if index and
                      re.match(r"^\s*(?:if\s+.+|elif\s+.+|else)\s*:\s*(?:#.*)?$", line.rstrip("\r\n")))
        starts = sorted(set(starts))
        sections = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            sections.append({"start_line": item["start_line"] + start,
                "end_line": item["start_line"] + end - 1,
                "content": "".join(lines[start:end])})
        if "".join(section["content"] for section in sections) != item["content"]:
            raise ValueError("source layout lost source text")
        partitions[side].append({"path": item["path"], "snapshot_sha": item["snapshot_sha"],
            "start_line": item["start_line"], "end_line": item["end_line"], "sections": sections})
    return {"question": case["question"], "snapshot": case["snapshot"], "source_partitions": partitions}


def source_layout_prompt(case):
    return json.dumps(partition_source_case(case), ensure_ascii=False, sort_keys=True)


def layout_inventory(payload):
    """从分区输入恢复原始片段集合，用于证明没有增删源码。"""
    result = []
    for side, items in payload["source_partitions"].items():
        for item in items:
            cursor = item["start_line"]
            for section in item["sections"]:
                if section["start_line"] != cursor or section["end_line"] < cursor:
                    raise ValueError("source layout gap or overlap")
                cursor = section["end_line"] + 1
            if cursor != item["end_line"] + 1:
                raise ValueError("source layout incomplete range")
            result.append({"side": side, "path": item["path"], "snapshot_sha": item["snapshot_sha"],
                "start_line": item["start_line"], "end_line": item["end_line"],
                "content": "".join(section["content"] for section in item["sections"])})
    return sorted(result, key=stable_hash)
