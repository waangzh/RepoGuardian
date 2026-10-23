"""只读取内存中的已冻结审查资料，不抓取源码、讨论或远端检查。"""

import json

from app.models.review_synthesis import ReviewSynthesisInput, SynthesisSource
from app.review.tool_scope import (
    ReviewPathPolicyError, _validate_repository_relative_path, is_sensitive_repository_change,
)
from app.services.fingerprints import stable_hash


HUNK_TEXT_LIMIT = 6000


def _hunk_text(hunk, path, change_type, lines):
    raw = hunk.model_copy(update={"lines": lines}).model_dump(mode="json")
    return json.dumps({"path": path, "change_type": change_type, "hunk": raw}, ensure_ascii=False)


def _context_radius_lines(lines, radius):
    """保留改动行及其 radius 范围内的上下文。"""
    keep = set()
    for index, line in enumerate(lines):
        if line.kind != "context":
            keep.update(range(max(0, index - radius), min(len(lines), index + radius + 1)))
    return [line for index, line in enumerate(lines) if index in keep]


def _trimmed_hunk_text(hunk, path, change_type):
    """超长 hunk 先减少上下文，再按改动行前缀截断；返回 (text, truncated)。

    整段丢弃会让模型没有任何可引用的 diff，摘要必然失败。
    """
    full = _hunk_text(hunk, path, change_type, hunk.lines)
    if len(full) <= HUNK_TEXT_LIMIT:
        return full, False
    for radius in (3, 1, 0):
        lines = _context_radius_lines(hunk.lines, radius)
        text = _hunk_text(hunk, path, change_type, lines)
        if len(text) <= HUNK_TEXT_LIMIT:
            return text, True
    changed = [line for line in hunk.lines if line.kind != "context"]
    low, high = 0, len(changed)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_hunk_text(hunk, path, change_type, changed[:middle])) <= HUNK_TEXT_LIMIT:
            low = middle
        else:
            high = middle - 1
    return (_hunk_text(hunk, path, change_type, changed[:low]) if low else ""), True


def build_synthesis_input(task, plan):
    policy = (task.task_runtime_configuration or {}).get("output_policy") or {}
    limit = min(60_000, max(2000, int(policy.get("max_input_chars", 24_000))))
    facts = ReviewSynthesisInput(review_id=task.id, head_sha=plan.snapshot.head_sha or "",
        effective_base_sha=plan.snapshot.effective_base_sha or "")
    truncation_reasons = set()

    def mark_truncated(reason):
        facts.input_truncated = True
        truncation_reasons.add(reason)

    def add(identity, kind, text):
        text = text.strip()
        if not text:
            return False
        source = SynthesisSource(id=identity, kind=kind, text=text[:6000])
        candidate = facts.model_copy(update={"sources": [*facts.sources, source]})
        # 保留统计/白名单的编码空间；实际请求另走 Provider 窗口与 token 准入。
        if len(candidate.sources) > 190:
            mark_truncated("source_count_limited")
            return False
        if len(json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False)) > limit - 512:
            mark_truncated("input_size_limited")
            return False
        facts.sources.append(source)
        if len(text) > 6000:
            mark_truncated("diff_hunk_truncated" if kind in {
                "diff_observation", "test_source_observation"} else "source_text_truncated")
        return True

    for finding in [*plan.inline_drafts, *plan.summary_findings]:
        text = json.dumps({"claim": finding.raw_title or finding.title,
            "affected_behavior": finding.affected_behavior, "failure_scenario": finding.failure_scenario,
            "recommendation": finding.recommendation}, ensure_ascii=False)
        if len(facts.finding_keys) < 100 and add("finding:" + finding.finding_key, "verified_finding", text):
            facts.finding_keys.append(finding.finding_key)
    add("boundary:review", "review_boundary",
        "只概括给定的已核验问题；候选与未决不属于已确认问题。"
        "不描述测试运行结果、严重度、问题数量或合并建议。")
    if task.pr:
        author = task.pr.title + "\n" + (task.pr.body or "")
        add("pr-description:1", "author_claim", author[:4000])
        if len(author) > 4000:
            mark_truncated("author_context_truncated")
    excluded = {file.path for file in plan.digest.excluded_files}
    for file in sorted(task.changed_files, key=lambda item: item.file_path):
        try:
            _validate_repository_relative_path(file.file_path)
            if file.old_file_path:
                _validate_repository_relative_path(file.old_file_path)
        except ReviewPathPolicyError:
            facts.omitted_files += 1
            facts.omitted_hunks += len(file.hunks)
            truncation_reasons.add("invalid_path_excluded")
            continue
        if file.file_path in excluded:
            facts.omitted_files += 1
            facts.omitted_hunks += len(file.hunks)
            truncation_reasons.add("review_scope_excluded")
            continue
        if (file.is_binary or is_sensitive_repository_change(file.file_path, file.old_file_path)):
            facts.omitted_files += 1
            facts.omitted_hunks += len(file.hunks)
            truncation_reasons.add("sensitive_or_binary_excluded")
            continue
        test = any(part in {"test", "tests", "__tests__"} for part in file.file_path.split("/"))
        test |= any(part in file.file_path.rsplit("/", 1)[-1] for part in (".test.", ".spec.", "test_", "_test."))
        kind = "test_source_observation" if test else "diff_observation"
        included = False
        for index, hunk in enumerate(file.hunks):
            raw = hunk.model_dump(mode="json")
            identity = "diff:" + stable_hash([facts.head_sha, facts.effective_base_sha, file.file_path, index, raw])
            text, trimmed = _trimmed_hunk_text(hunk, file.file_path, file.change_type)
            # 超长 hunk 裁剪后仍无可用改动行时才省略。
            if not text or not add(identity, kind, text):
                facts.omitted_hunks += 1
                truncation_reasons.add("diff_hunk_omitted")
            else:
                included = True
                if trimmed:
                    truncation_reasons.add("diff_hunk_truncated")
        if not included:
            facts.omitted_files += 1
            if not file.hunks:
                truncation_reasons.add("diff_metadata_only")
    understanding = plan.digest.change_understanding
    if understanding and understanding.implementation_changes:
        add("observation:implementation", "verified_observation", understanding.implementation_changes)
    facts.input_truncated |= bool(facts.omitted_files or facts.omitted_hunks)
    facts.truncation_reasons = sorted(truncation_reasons)
    # finding_keys 自身也计入硬性长度，过大的事实包直接降级而不发送。
    return facts
