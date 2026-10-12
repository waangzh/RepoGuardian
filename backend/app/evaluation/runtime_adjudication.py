"""离线质量标注验收；源码引用核对与语义质量判断分别保留。

评分者显式提供只读 source_reader；标注不能选择本机路径或命令。
本模块不调用模型、不访问参考答案，不将源码匹配解释为缺陷证明。
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
import re
from typing import Callable

from app.evaluation.runtime_baseline import quality_summary
from app.review.tool_scope import is_sensitive_repository_path


SourceReader = Callable[[str, str, str], str | None]


def _validate_evidence(item: dict, packet: dict, source_reader: SourceReader) -> None:
    if not isinstance(item, dict):
        raise ValueError("evidence_must_be_object")
    path, side, sha = item.get("path"), item.get("side"), item.get("snapshot_sha")
    if (not isinstance(path, str) or not path or "\\" in path or "\x00" in path
            or ":" in path or PurePosixPath(path).is_absolute()
            or PurePosixPath(path).as_posix() != path
            or any(part in {".", ".."} for part in PurePosixPath(path).parts)
            or is_sensitive_repository_path(path)):
        raise ValueError("evidence_path_rejected")
    if (not isinstance(side, str) or side not in {"head", "base"} or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or sha != packet["snapshot"].get(side + "_sha")):
        raise ValueError("evidence_snapshot_mismatch")
    start, end, code = item.get("start_line"), item.get("end_line"), item.get("code")
    if (not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or start < 1 or end < start or not isinstance(code, str) or not code.strip()):
        raise ValueError("evidence_range_rejected")
    content = source_reader(packet["snapshot"]["repository"], sha, path)
    if content is None:
        raise ValueError("evidence_source_unavailable")
    lines = content.splitlines()
    if end > len(lines) or "\n".join(lines[start - 1:end]) != "\n".join(code.splitlines()):
        raise ValueError("evidence_text_mismatch")


def adjudicate_annotations(
    packets: list[dict], annotations: list[dict], *, source_reader: SourceReader,
    reviewer_id: str, blind: bool,
) -> dict:
    """逐项验收，无关拒绝不丢合法标注；评审者身份由调用方提供。

    重复标注全部拒绝，不后写覆盖。duplicate 依赖的有效缺陷标注被拒绝时，
    该重复项也拒绝。blind 是评分流程元数据，不能由模型标注自我授予。
    """
    if not isinstance(reviewer_id, str) or not reviewer_id.strip() or not isinstance(blind, bool):
        raise ValueError("explicit_reviewer_metadata_required")
    if not isinstance(packets, list) or not isinstance(annotations, list):
        raise ValueError("adjudication_requires_arrays")
    by_id = {packet["blind_id"]: packet for packet in packets}
    if len(by_id) != len(packets):
        raise ValueError("duplicate_packet_identity")
    counts = Counter(item.get("blind_id") for item in annotations
                     if isinstance(item, dict) and isinstance(item.get("blind_id"), str))
    accepted, rejected = [], []
    allowed = {"valid_defect", "false_positive", "duplicate", "cannot_determine"}
    for index, item in enumerate(annotations):
        identity = item.get("blind_id") if isinstance(item, dict) else None
        try:
            if not isinstance(identity, str) or identity not in by_id:
                raise ValueError("unknown_quality_identity")
            if counts[identity] != 1:
                raise ValueError("conflicting_quality_identity")
            if set(item) - {"blind_id", "label", "reason", "code_evidence", "duplicate_of"}:
                raise ValueError("unexpected_annotation_field")
            if (not isinstance(item.get("label"), str) or item["label"] not in allowed
                    or not isinstance(item.get("reason"), str)
                    or not item["reason"].strip()):
                raise ValueError("independent_label_and_reason_required")
            evidence = item.get("code_evidence", [])
            if not isinstance(evidence, list):
                raise ValueError("evidence_must_be_array")
            if item["label"] in {"valid_defect", "false_positive"} and not evidence:
                raise ValueError("semantic_label_requires_code_evidence")
            for reference in evidence:
                _validate_evidence(reference, by_id[identity], source_reader)
            if item["label"] != "duplicate" and item.get("duplicate_of"):
                raise ValueError("unexpected_duplicate_binding")
            if item["label"] == "duplicate" and not isinstance(item.get("duplicate_of"), str):
                raise ValueError("duplicate_requires_identity")
            accepted.append({**item, "item_index": index})
        except ValueError as exc:
            rejected.append({"item_index": index, "blind_id": identity, "reason": str(exc)})
    valid = {item["blind_id"] for item in accepted if item["label"] == "valid_defect"}
    independent = []
    for item in accepted:
        target = item.get("duplicate_of")
        if item["label"] == "duplicate" and (target not in valid or target == item["blind_id"]):
            rejected.append({"item_index": item["item_index"], "blind_id": item["blind_id"],
                             "reason": "duplicate_requires_accepted_valid_defect"})
        else:
            independent.append(item)
    summary = quality_summary(packets, independent)
    return {
        "version": "runtime-adjudication-v1", "reviewer_id": reviewer_id, "blind": blind,
        "accepted": independent, "rejected": sorted(rejected, key=lambda item: item["item_index"]),
        "quality": summary,
        "adjudication_status": ("complete" if blind and not summary["not_reviewed"]
                                and not rejected else "pending"),
        "evidence_verification": "snapshot_text_only_not_semantic_truth",
        "recall_evaluation": "not_performed",
    }
