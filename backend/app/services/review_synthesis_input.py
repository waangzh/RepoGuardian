"""只读取内存中的已冻结审查资料，不抓取源码、讨论或远端检查。"""

import json

from app.models.review_synthesis import ReviewSynthesisInput, SynthesisSource
from app.review.tool_scope import (
    ReviewPathPolicyError, _validate_repository_relative_path, is_sensitive_repository_change,
)
from app.services.fingerprints import stable_hash


def build_synthesis_input(task, plan):
    policy = (task.task_runtime_configuration or {}).get("output_policy") or {}
    limit = min(60_000, max(2000, int(policy.get("max_input_chars", 24_000))))
    facts = ReviewSynthesisInput(review_id=task.id, head_sha=plan.snapshot.head_sha or "",
        effective_base_sha=plan.snapshot.effective_base_sha or "")

    def add(identity, kind, text):
        text = text.strip()
        if not text:
            return False
        source = SynthesisSource(id=identity, kind=kind, text=text[:6000])
        candidate = facts.model_copy(update={"sources": [*facts.sources, source]})
        # 保留统计/白名单的编码空间；实际请求另走 Provider 窗口与 token 准入。
        if len(candidate.sources) > 190 or len(json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False)) > limit - 512:
            facts.input_truncated = True
            return False
        facts.sources.append(source)
        if len(text) > 6000:
            facts.input_truncated = True
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
        facts.input_truncated |= len(author) > 4000
    excluded = {file.path for file in plan.digest.excluded_files}
    for file in sorted(task.changed_files, key=lambda item: item.file_path):
        try:
            _validate_repository_relative_path(file.file_path)
            if file.old_file_path:
                _validate_repository_relative_path(file.old_file_path)
        except ReviewPathPolicyError:
            facts.omitted_files += 1
            facts.omitted_hunks += len(file.hunks)
            continue
        if (file.file_path in excluded or file.is_binary
                or is_sensitive_repository_change(file.file_path, file.old_file_path)):
            facts.omitted_files += 1
            facts.omitted_hunks += len(file.hunks)
            continue
        test = any(part in {"test", "tests", "__tests__"} for part in file.file_path.split("/"))
        test |= any(part in file.file_path.rsplit("/", 1)[-1] for part in (".test.", ".spec.", "test_", "_test."))
        kind = "test_source_observation" if test else "diff_observation"
        included = False
        for index, hunk in enumerate(file.hunks):
            raw = hunk.model_dump(mode="json")
            identity = "diff:" + stable_hash([facts.head_sha, facts.effective_base_sha, file.file_path, index, raw])
            text = json.dumps({"path": file.file_path, "change_type": file.change_type,
                "hunk": raw}, ensure_ascii=False)
            # 不保留半个 hunk 的引用；无法完整提供就省略该 hunk。
            if len(text) > 6000 or not add(identity, kind, text):
                facts.omitted_hunks += 1
            else:
                included = True
        if not included:
            facts.omitted_files += 1
    understanding = plan.digest.change_understanding
    if understanding and understanding.implementation_changes:
        add("observation:implementation", "verified_observation", understanding.implementation_changes)
    facts.input_truncated |= bool(facts.omitted_files or facts.omitted_hunks)
    # finding_keys 自身也计入硬性长度，过大的事实包直接降级而不发送。
    return facts
