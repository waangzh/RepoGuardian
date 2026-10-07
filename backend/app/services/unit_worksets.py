"""按真实请求准入拆分 Diff；覆盖账本保留原 hunk 的完整行区间。"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Callable

from app.models.review import ChangedFile, ChangedLine, DiffHunk, ReviewUnit
from app.services.review_planner import DeterministicReviewPlanner

UNIT_WORKSET_VERSION = "unit-diff-worksets-v1"
MAX_UNIT_WORKSETS = 32


def _fragment(hunk: DiffHunk, start: int, end: int) -> DiffHunk:
    lines = hunk.lines[start:end]
    old_start = hunk.old_start + sum(line.kind != "added" for line in hunk.lines[:start])
    new_start = hunk.new_start + sum(line.kind != "deleted" for line in hunk.lines[:start])
    return DiffHunk(old_start=old_start, new_start=new_start, hunk_id=hunk.hunk_id,
        old_length=sum(line.kind != "added" for line in lines),
        new_length=sum(line.kind != "deleted" for line in lines), lines=lines,
        added_lines=[ChangedLine(line_no=line.new_line_no, content=line.content)
                     for line in lines if line.kind == "added"],
        removed_lines=[ChangedLine(line_no=line.old_line_no, content=line.content)
                       for line in lines if line.kind == "deleted"])


def build_unit_worksets(unit: ReviewUnit, files: list[ChangedFile],
                       admit: Callable) -> list[tuple[ReviewUnit, list[ChangedFile], dict]]:
    """先按 hunk 二分，再按完整行二分；相邻片段重叠最多三行。"""
    atoms = []
    for file in files:
        if not file.hunks:
            atoms.append((file, None, "file:" + file.file_path, 0, 0))
        for index, hunk in enumerate(file.hunks):
            identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
            atoms.append((file, hunk, identity, 0, len(hunk.lines)))
    pending = deque([atoms])
    output = []
    while pending:
        selected = pending.popleft()
        paths = list(dict.fromkeys(atom[0].file_path for atom in selected))
        batch_files = [next(atom[0] for atom in selected if atom[0].file_path == path).model_copy(update={
            "hunks": [atom[1] for atom in selected if atom[0].file_path == path and atom[1] is not None],
        }) for path in paths]
        ranges = [{"file_path": file.file_path, "hunk_id": identity,
                   "start_offset": start, "end_offset": end,
                   "content_hash": hashlib.sha256(json.dumps(hunk.model_dump(mode="json") if hunk else
                       file.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
                  for file, hunk, identity, start, end in selected]
        digest = hashlib.sha256(json.dumps([UNIT_WORKSET_VERSION, unit.fingerprint, ranges],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        child = unit.model_copy(update={"id": unit.id + ":batch:" + digest[:16],
            "fingerprint": digest, "primary_files": paths, "diff_hunk_ids": [],
            "related_files": [path for path in unit.related_files if path not in paths][:12],
            "context_provenance": [item for item in unit.context_provenance if item.file in paths],
            "changed_symbols": [], "grouping_reason": "bounded_diff_workset"})
        metadata = {"id": child.id, "parent_unit_id": unit.id, "input_hash": digest,
                    "ranges": ranges, "version": UNIT_WORKSET_VERSION}
        admission = admit(child, batch_files, metadata)
        if admission["admitted"]:
            output.append((child, batch_files, {**metadata, "admission": admission}))
            continue
        can_split = len(output) + len(pending) + 2 <= MAX_UNIT_WORKSETS
        if len(selected) > 1 and can_split:
            middle = len(selected) // 2
            pending.appendleft(selected[middle:])
            pending.appendleft(selected[:middle])
            continue
        if len(selected) == 1 and can_split:
            file, hunk, identity, start, end = selected[0]
            if (hunk and len(hunk.lines) > 1
                    and sum(line.kind != "added" for line in hunk.lines) == hunk.old_length
                    and sum(line.kind != "deleted" for line in hunk.lines) == hunk.new_length):
                middle = len(hunk.lines) // 2
                overlap = min(3, len(hunk.lines) // 4)
                right = (file, _fragment(hunk, middle - overlap, len(hunk.lines)), identity,
                         start + middle - overlap, end)
                left = (file, _fragment(hunk, 0, middle + overlap), identity, start, start + middle + overlap)
                pending.appendleft([right])
                pending.appendleft([left])
                continue
        output.append((child, batch_files, {**metadata, "admission": admission,
            "reason": "unit_workset_limit_exceeded" if not can_split else "unsplittable_diff_input"}))
    return output


def restore_unit_workset_files(unit: ReviewUnit, files: list[ChangedFile], batch) -> list[ChangedFile]:
    """从当前原始 Diff 重建批次正文；账本元数据本身不授予证据信任。"""
    if batch.parent_unit_id != unit.id or batch.version != UNIT_WORKSET_VERSION:
        raise ValueError("invalid_unit_workset_identity")
    digest = hashlib.sha256(json.dumps([UNIT_WORKSET_VERSION, unit.fingerprint, batch.ranges],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != batch.input_hash:
        raise ValueError("invalid_unit_workset_hash")
    originals = {file.file_path: file for file in files}
    selected = {}
    for interval in batch.ranges:
        file = originals[interval["file_path"]]
        original = next((hunk for index, hunk in enumerate(file.hunks)
            if DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
            == interval["hunk_id"]), None)
        start, end = interval["start_offset"], interval["end_offset"]
        if original is None:
            if file.hunks or interval["hunk_id"] != "file:" + file.file_path or start or end:
                raise ValueError("invalid_unit_workset_range")
            fragment = None
        elif not 0 <= start <= end <= len(original.lines):
            raise ValueError("invalid_unit_workset_range")
        else:
            fragment = original if start == 0 and end == len(original.lines) else _fragment(original, start, end)
        content = json.dumps(fragment.model_dump(mode="json") if fragment else file.model_dump(mode="json"),
                             ensure_ascii=False, sort_keys=True)
        if hashlib.sha256(content.encode()).hexdigest() != interval["content_hash"]:
            raise ValueError("invalid_unit_workset_content")
        if file.file_path not in selected:
            selected[file.file_path] = file.model_copy(update={"hunks": []})
        if fragment:
            selected[file.file_path].hunks.append(fragment)
    return list(selected.values())
