"""报告共用的确定性事实投影；作者声明与静态事实不合成为目标实现证明。"""

import re

from pydantic import ValidationError

from app.models.publication import DigestGoal, DigestInvestigation, DigestIntent
from app.models.review_runtime import IntentAssessment
from app.services.review_intent import author_targets
from app.services.review_validation_digest import validation_digest
from app.services.review_findings import finding_views
from app.services.fingerprints import stable_hash


def enrich_review_digest(digest, task, report, dispositions):
    digest.pr_url = task.pr_url
    if task.pr:
        body = re.sub(r"<!--.*?-->", "", task.pr.body or "", flags=re.DOTALL).strip()
        digest.author_goal = body or task.pr.title or None
    for file in sorted(task.changed_files, key=lambda item: item.file_path):
        kind = {"added": "新增", "deleted": "删除", "modified": "修改", "renamed": "重命名"}.get(file.change_type, "变更")
        description = f"{kind} {file.file_path}（+{file.additions} / -{file.deletions}）"
        if file.old_file_path and file.old_file_path != file.file_path:
            description += f"；原路径 {file.old_file_path}"
        if file.is_binary:
            description += "；二进制内容，不能据此推断行为"
        symbols = set()
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.kind not in {"added", "deleted"}:
                    continue
                match = re.match(r"\s*(?:(?:export|async|public|private|static)\s+)*(?:def|class|function|const|let|interface|type)\s+([\w$]+)", line.content)
                if match:
                    symbols.add(match.group(1))
        if symbols:
            description += "；变更声明：" + "、".join(sorted(symbols)[:20])
        digest.actual_changes.append(description)
    if report is not None:
        if report.pr_intent_context_truncated is True:
            digest.author_context_status = "truncated"
            digest.author_context_limitation = ("作者声明超过输入上限，送审内容已截断；未输入的部分尚未核验，"
                "不能据此判断全部作者目标已实现。请补充目标并重新审查。")
        elif report.pr_intent_context:
            digest.author_context_status = "complete" if report.pr_intent_context_truncated is False else "unknown"
            if digest.author_context_status == "unknown":
                digest.author_context_limitation = ("历史审查未记录作者声明是否完整输入，无法确认是否遗漏作者目标；"
                    "请重新审查以确认输入范围。")
        digest.validation_state, digest.validations = validation_digest(task)
        record = report.model_dump(mode="json")
        digest.execution_record = {key: record[key] for key in (
            "execution_status", "termination_reason", "usage_integrity", "sent_calls", "settled_tokens",
            "unknown_attempts", "total_tokens", "work_items", "diagnostics", "relationship_hints",
            "initial_scope_coverage", "admitted_investigations", "unscheduled_leads", "findings", "failure_summary", "coverage_version")}
        for key in ("initial_scope_coverage", "admitted_investigations", "unscheduled_leads", "relationship_hints", "findings"):
            digest.execution_record[key] = sorted(digest.execution_record[key], key=stable_hash)
        if report.pr_intent_context:
            digest.author_goal = report.pr_intent_context
            digest.intent_alignment = ("本轮静态检查已提供冻结的作者声明，具体目标对照及证据缺口见已检查行为的理由。"
                "这些局部检查不构成作者全部目标已实现的独立验收结论。")
            for target in author_targets(report.pr_intent_context):
                entry = DigestIntent(author_target=target)
                for goal in report.initial_scope_coverage:
                    for raw in goal.get("intent_assessments") or []:
                        try:
                            assessment = IntentAssessment.model_validate(raw)
                        except ValidationError:
                            continue
                        if assessment.author_quote != target:
                            continue
                        needs_source = assessment.alignment in {"supported", "contradicted"}
                        bound = bool(task.run_manifest and goal.get("snapshot_head_sha") == task.run_manifest.head_sha
                            and goal.get("snapshot_base_sha") == task.run_manifest.effective_base_sha
                            and goal.get("result_id")
                            and (not needs_source or (goal.get("status") == "examined" and assessment.evidence_ids))
                            and set(assessment.evidence_ids).issubset(goal.get("evidence_ids") or []))
                        entry.observations.append({"goal_id": goal.get("goal_id"), "path": goal.get("target_ref"),
                            "start_line": goal.get("start_line"), "side": "base" if goal.get("target_kind") == "base_source_range" else "head",
                            "actual_behavior": assessment.actual_behavior,
                            "alignment": assessment.alignment if bound else "insufficient",
                            "evidence_ids": assessment.evidence_ids if bound else [],
                            "limitations": assessment.limitations + ([] if bound else ["缺少有效快照或已接受证据绑定，结论不可采信。"])})
                statuses = {value["alignment"] for value in entry.observations}
                entry.status = ("contradicted" if "contradicted" in statuses else "insufficient" if "insufficient" in statuses
                    else "observed" if "supported" in statuses else "not_applicable" if statuses else "not_checked")
                digest.intent_assessments.append(entry)
            if any(item.observations for item in digest.intent_assessments):
                digest.intent_alignment = "以下为逐目标的局部静态观察，未覆盖路径仍需核验；支持记录不构成作者全部目标已实现的独立验收结论。"
        # 与 Findings 页共用人工处理身份和机器状态；不回写原始运行数据。
        digest.findings = sorted(finding_views(task, dispositions or {}),
            key=lambda item: (item.id, stable_hash(item.model_dump(mode="json"))))
        digest.investigations_known = True
        digest.execution_status = report.execution_status
        work_items = {item.get("work_item_id"): item for item in report.work_items
                      if isinstance(item.get("work_item_id"), str)}
        for item in report.admitted_investigations:
            identity = item.get("work_item_id") if isinstance(item.get("work_item_id"), str) else None
            answer = item.get("answer")
            investigation = DigestInvestigation(work_item_id=identity,
                proposition=str(item.get("proposition") or "未记录调查命题"),
                answer=answer if item.get("status") == "answered" and isinstance(answer, str)
                    and answer in {"supported", "refuted"} else "unresolved",
                reason=str(item["reason"]) if item.get("reason") else None,
                limitations=[str(value) for value in item.get("limitations") or []])
            for request in work_items.get(identity, {}).get("evidence_requests") or []:
                try:
                    investigation.requested_ranges.append(DigestGoal(path=request.get("file_path"),
                        start_line=request.get("start_line"), end_line=request.get("end_line")))
                except (AttributeError, ValidationError):
                    investigation.limitations.append("原计划取证范围格式无效，需核对技术记录。")
            digest.investigations.append(investigation)
        digest.unresolved_leads = [str(item.get("proposition") or item.get("lead_id") or "未命名线索") + "：" +
            str(item.get("reason") or "未记录原因") for item in report.unscheduled_leads if item.get("status") != "duplicate"]
    return digest
