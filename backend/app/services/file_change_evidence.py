"""固定 Git tree 的非行证据；元数据核验不代替模型的影响检查。"""
from __future__ import annotations

import json
import re

from app.models.review import FileChangeEvidence, FileChangeSide
from app.review.tool_scope import is_sensitive_repository_change
from app.services.fingerprints import stable_hash


def _kinds(base, head):
    regular = {"100644", "100755"}
    if any(side.mode and side.mode not in regular for side in (base, head)):
        return []
    if not base.blob_id:
        return ["empty_added"] if head.blob_id and head.size == 0 else []
    if not head.blob_id:
        return ["empty_deleted"] if base.size == 0 else []
    return (["rename"] if base.path != head.path else []) + (["mode"] if base.mode != head.mode else [])


def validate_file_change_evidence(evidence, head_sha, base_sha, file_path):
    if (evidence.head_sha != head_sha or evidence.base_sha != base_sha
            or evidence.file_path != file_path or evidence.head.path != file_path
            or is_sensitive_repository_change(file_path, evidence.base.path)
            or evidence.change_kinds != _kinds(evidence.base, evidence.head) or not evidence.change_kinds):
        raise ValueError("file_change_evidence_binding_mismatch")
    payload = evidence.model_dump(mode="json", exclude={"id", "content_hash"})
    digest = stable_hash(payload)
    if evidence.content_hash != digest or evidence.id != "file-change-" + digest[:24]:
        raise ValueError("file_change_evidence_hash_mismatch")
    return evidence


def file_change_reference(file, head_sha, base_sha):
    evidence = file.file_change_evidence
    if evidence is None:
        return None
    validate_file_change_evidence(evidence, head_sha, base_sha, file.file_path)
    if evidence.base.path != (file.old_file_path or file.file_path):
        raise ValueError("file_change_evidence_old_path_mismatch")
    if not file.hunks and (file.is_binary or file.additions or file.deletions
            or (evidence.base.blob_id and evidence.head.blob_id
                and evidence.base.blob_id != evidence.head.blob_id)):
        raise ValueError("file_change_evidence_missing_text_diff")
    raw = evidence.model_dump(mode="json")
    return {"id": evidence.id, "file_path": evidence.file_path, "source": "file_change",
        "start_line": 0, "end_line": 0, "head_sha": head_sha, "base_sha": base_sha,
        "content_hash": evidence.content_hash, "file_change": raw,
        "content": json.dumps(raw, ensure_ascii=False, sort_keys=True)}


def set_file_change_impact_scope(record_input, related_files=()):
    """影响检查仅允许当前文件双侧路径与服务端规划的关联文件，不使用全仓库可读集合。"""
    record_input["file_change_impact_scope"] = {item["id"]: sorted({
        item["file_change"]["base"]["path"], item["file_change"]["head"]["path"], *related_files})
        for item in record_input["evidence"] if item["source"] == "file_change"}


def bind_file_change_evidence(files, repo_path, head_sha, base_sha, git_tool):
    """不信任恢复/外部传入的证据；每次执行重新从固定双侧 tree 绑定。"""
    clean = [file.model_copy(update={"file_change_evidence": None}) for file in files]
    if not repo_path or not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha or "")
                                for sha in (head_sha, base_sha)):
        return clean
    if not callable(getattr(git_tool, "get_tree_entries", None)):
        return clean
    eligible = [file for file in clean if not is_sensitive_repository_change(file.file_path, file.old_file_path)]
    paths = sorted({path for file in eligible for path in (file.file_path, file.old_file_path) if path})
    from app.tools.git_tool import GitToolError
    try:
        base_tree = git_tool.get_tree_entries(repo_path, base_sha, paths)
        head_tree = git_tool.get_tree_entries(repo_path, head_sha, paths)
    except (GitToolError, OSError):
        # 无法读取固定快照时只保留未经核验的清单；Coverage Gate 拒绝元数据覆盖。
        return clean
    for file in eligible:
        old = file.old_file_path or file.file_path
        base = FileChangeSide(path=old, **base_tree.get(old, {}))
        head = FileChangeSide(path=file.file_path, **head_tree.get(file.file_path, {}))
        kinds = _kinds(base, head)
        if not kinds or file.is_binary:
            continue
        if ((file.old_mode and base.mode != file.old_mode) or (file.new_mode and head.mode != file.new_mode)):
            continue
        if (file.change_type == "added" and base.blob_id or file.change_type == "deleted" and head.blob_id
                or file.change_type == "renamed" and (not base.blob_id or not head.blob_id
                    or file.file_path in base_tree or old in head_tree)):
            continue
        raw = {"schema_version": "file-change-evidence-v1", "file_path": file.file_path,
            "base_sha": base_sha, "head_sha": head_sha, "base": base.model_dump(mode="json"),
            "head": head.model_dump(mode="json"), "change_kinds": kinds}
        digest = stable_hash(raw)
        file.file_change_evidence = FileChangeEvidence(**raw, content_hash=digest, id="file-change-" + digest[:24])
        try:
            file_change_reference(file, head_sha, base_sha)
        except ValueError:
            file.file_change_evidence = None
    return clean
