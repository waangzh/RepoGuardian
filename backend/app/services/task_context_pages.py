"""容量分页：保留原目标分母和源码映射，不创建子 Unit 或独立预算。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.services.fingerprints import stable_hash
from app.services.review_input_context import build_context_evidence


@dataclass(frozen=True)
class ContextPage:
    page_id: str
    ranges: tuple[dict, ...]
    evidence_ids: tuple[str, ...]


def build_context_pages(work_item, goals, catalog, *, capacity_chars: int,
                        chunk_evidence: bool = False) -> list[ContextPage]:
    """只由输入容量规划完整行片段；消费余额从不传入此函数。"""
    if capacity_chars < 1:
        raise ValueError("context capacity must be positive")
    atoms = []
    for goal in goals:
        if goal.goal_id not in work_item.goal_ids:
            continue
        if goal.target_kind != "source_range":
            raise ValueError("context page requires source range goal")
        source = next((value for value in catalog.values()
                       if value["file_path"] == goal.target_ref
                       and value["start_line"] <= goal.start_line
                       and value["end_line"] >= goal.end_line), None)
        if source is None:
            pieces = sorted((value for value in catalog.values() if value["file_path"] == goal.target_ref
                and value["start_line"] <= goal.end_line and value["end_line"] >= goal.start_line),
                key=lambda value: value["start_line"])
            by_line = {}
            for piece in pieces:
                if hashlib.sha256(piece["content"].encode()).hexdigest() != piece["content_hash"]:
                    raise ValueError("context page source hash mismatch")
                if len(piece["content"].splitlines()) != piece["end_line"] - piece["start_line"] + 1:
                    raise ValueError("context page source range mismatch")
                for index, text in enumerate(piece["content"].splitlines(), piece["start_line"]):
                    if index in by_line and by_line[index] != text:
                        raise ValueError("conflicting context page source")
                    by_line[index] = text
            if any(index not in by_line for index in range(goal.start_line, goal.end_line + 1)):
                raise ValueError("goal source missing for context planning")
            body = "\n".join(by_line[index] for index in range(goal.start_line, goal.end_line + 1))
            if by_line[goal.end_line] == "":
                body += "\n"
            source = {**pieces[0], "start_line": goal.start_line, "end_line": goal.end_line,
                      "content": body, "content_hash": hashlib.sha256(body.encode()).hexdigest()}
        if hashlib.sha256(source["content"].encode()).hexdigest() != source["content_hash"]:
            raise ValueError("context page source hash mismatch")
        lines = source["content"].splitlines()[
            goal.start_line - source["start_line"]:goal.end_line - source["start_line"] + 1]
        if len(lines) != goal.end_line - goal.start_line + 1:
            raise ValueError("context page source range mismatch")
        chunks = []
        start, current, size = goal.start_line, [], 0
        for line in lines:
            cost = len(line) + (1 if current else 0)
            if len(line) > capacity_chars:
                raise ValueError("unsplittable_source_line")
            if current and size + cost > capacity_chars:
                chunks.append((start, current))
                start += len(current)
                current, size = [], 0
            current.append(line)
            size = len("\n".join(current))
        if current:
            chunks.append((start, current))
        for start, fragment in chunks:
            content = "\n".join(fragment)
            if fragment[-1] == "":
                content += "\n"
            end = start + len(fragment) - 1
            digest = hashlib.sha256(content.encode()).hexdigest()
            binding = {"goal_id": goal.goal_id, "file_path": goal.target_ref,
                       "start_line": start, "end_line": end, "content_hash": digest,
                       "original_start_line": goal.start_line, "original_end_line": goal.end_line,
                       "original_content_hash": source["content_hash"],
                       "hunk_id": source.get("hunk_id")}
            identity = "page-evidence-" + stable_hash([work_item.work_item_id, binding])[:32]
            catalog[identity] = {**source, "content": content, "content_hash": digest,
                                 "start_line": start, "end_line": end}
            identities = (identity,)
            if chunk_evidence:
                chunks, _ = build_context_evidence([{**catalog.pop(identity), "file": goal.target_ref}],
                                                   source["head_sha"], source["base_sha"])
                identities = tuple(chunk["evidence_id"] for chunk in chunks)
                catalog.update({chunk["evidence_id"]: chunk for chunk in chunks})
            atoms.append((binding, identities, len(content)))
    pages, selected, size = [], [], 0
    for atom in atoms:
        if selected and (size + atom[2] > capacity_chars or
                         any(value[0]["goal_id"] == atom[0]["goal_id"] for value in selected)):
            pages.append(_page(work_item, selected))
            selected, size = [], 0
        selected.append(atom)
        size += atom[2]
    if selected:
        pages.append(_page(work_item, selected))
    return pages


def _page(work_item, atoms):
    ranges = tuple(atom[0] for atom in atoms)
    return ContextPage(page_id="page-" + stable_hash([work_item.work_item_id, ranges])[:32],
                       ranges=ranges, evidence_ids=tuple(dict.fromkeys(identity for atom in atoms for identity in atom[1])))
