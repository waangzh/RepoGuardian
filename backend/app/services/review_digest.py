"""报告共用的确定性事实投影；作者声明与静态事实不合并成目标实现证明。"""

import re

from pydantic import ValidationError

from app.models.publication import ChangeUnderstanding, DigestGoal, DigestInvestigation, DigestIntent
from app.models.review_runtime import IntentAssessment
from app.services.review_intent import author_targets, matched_author_targets
from app.services.review_validation_digest import validation_digest
from app.services.review_findings import finding_views
from app.services.fingerprints import stable_hash

_EXECUTION_KEYS = (
    "execution_status", "termination_reason", "usage_integrity", "sent_calls", "settled_tokens",
    "unknown_attempts", "total_tokens", "work_items", "diagnostics", "relationship_hints",
    "initial_scope_coverage", "admitted_investigations", "unscheduled_leads", "findings",
    "failure_summary", "coverage_version", "discussion_usage",
)
_SORTED_KEYS = ("initial_scope_coverage", "admitted_investigations", "unscheduled_leads",
                "relationship_hints", "findings")


def execution_record(report) -> dict:
    """技术审计专用的原始运行投影；只由审计文档按需使用，不进入轻量结果接口。"""
    if not isinstance(report, dict):
        return {}
    record = {key: report[key] for key in _EXECUTION_KEYS if key in report}
    # 历史报告可能保存了旧版未脱敏的诊断字段；审计导出统一收敛。
    from app.services.task_review_projection import _safe_diagnostic
    if isinstance(record.get("diagnostics"), list):
        record["diagnostics"] = [_safe_diagnostic(value) for value in record["diagnostics"]
                                 if isinstance(value, dict)]
    for key in _SORTED_KEYS:
        if isinstance(record.get(key), list):
            record[key] = sorted(record[key], key=stable_hash)
    return record


def _first_paragraph(value: str | None, limit: int = 240) -> str:
    """提取作者声明中第一段可读自然语言，去掉标题、代码块与表格。"""
    if not value:
        return ""
    in_fence = False
    for raw in re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith(("#", "|", ">", "![", "---")):
            continue
        if re.fullmatch(r"(?:[-*_]\s*){3,}", line):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        text = " ".join(line.split())
        if text:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def _module_summary(paths: list[str]) -> str:
    """按顶层目录归纳，避免把文件清单当作变更理解。"""
    groups: dict[str, int] = {}
    for path in paths:
        parts = path.split("/")
        group = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "仓库根目录")
        groups[group] = groups.get(group, 0) + 1
    ranked = sorted(groups.items(), key=lambda item: (-item[1], item[0]))[:3]
    return "、".join(f"{name}（{count} 个文件）" for name, count in ranked)


def _change_understanding(digest, task, report) -> ChangeUnderstanding:
    goal = _first_paragraph(digest.author_goal)
    # 正文只保存目标内容；输入完整性与符合性仍由 Digest 的独立字段表达。
    author_goal = goal or "未记录明确的作者目标。"
    observed_goals = [goal for goal in digest.examined_goals if goal.reason and goal.reason.strip()]
    paths = [file.file_path for file in task.changed_files]
    additions = sum(file.additions for file in task.changed_files)
    deletions = sum(file.deletions for file in task.changed_files)
    kinds = {kind: sum(file.change_type == kind for file in task.changed_files)
             for kind in ("added", "deleted", "modified", "renamed")}
    kind_text = "、".join(f"{label} {count}" for label, count in (
        ("新增", kinds["added"]), ("删除", kinds["deleted"]), ("修改", kinds["modified"]), ("重命名", kinds["renamed"])) if count)
    stats = f"共 {len(paths)} 个文件（+{additions} / -{deletions}）" + (f"，其中 {kind_text}" if kind_text else "")
    observed = []
    for item in digest.intent_assessments:
        for observation in item.observations:
            behavior = " ".join(str(observation.get("actual_behavior") or "").split())
            if observation.get("alignment") in {"supported", "contradicted"} and behavior:
                observed.append(behavior)
    if not observed:
        # 没有结构化目标对照时，使用已检查目标的静态理由，仍不推断作者目标已经实现。
        observed = [((goal.path + "：") if goal.path else "") + " ".join(goal.reason.split())
                    for goal in observed_goals]
    observed = list(dict.fromkeys(observed))[:2]
    if observed:
        actual = (f"根据已绑定证据的局部检查，代码行为表现为：{'；'.join(observed)}。"
                  + (f"本次 Diff {stats}。" if paths else ""))
    elif paths:
        symbols = set()
        for file in task.changed_files:
            for hunk in file.hunks:
                for line in hunk.lines:
                    if line.kind not in {"added", "deleted"}:
                        continue
                    match = re.match(
                        r"\s*(?:(?:export|async|public|private|static)\s+)*(?:def|class|function|const|let|interface|type)\s+([\w$]+)",
                        line.content)
                    if match:
                        symbols.add(match.group(1))
        names = "、".join(sorted(symbols)[:8])
        actual = (f"本次 Diff {stats}，主要集中在 {_module_summary(paths)}"
                  + (f"；变更声明包括 {names}。" if names else "。")
                  + " 尚未形成可引用的局部行为观察，以上仅为变更元数据。")
    else:
        actual = "未保存变更明细，无法归纳实际修改。"
    if digest.intent_assessments:
        total = len(digest.intent_assessments)
        supported = sum(item.status == "observed" for item in digest.intent_assessments)
        contradicted = sum(item.status == "contradicted" for item in digest.intent_assessments)
        not_applicable = sum(item.status == "not_applicable" for item in digest.intent_assessments)
        unresolved = total - supported - contradicted - not_applicable
        parts = [f"作者声明片段共 {total} 项"]
        if supported:
            parts.append(f"{supported} 项获得局部静态证据支持")
        if contradicted:
            parts.append(f"{contradicted} 项与代码存在静态证据矛盾")
        if unresolved:
            parts.append(f"{unresolved} 项未逐项核验（辅助信息，不作为合并阻断）")
        if not_applicable:
            parts.append(f"{not_applicable} 项当前检查不适用")
        alignment = "，".join(parts) + "。局部观察不构成作者全部目标已实现的验收结论。"
    elif report is not None and report.pr_intent_context:
        alignment = ("已冻结作者声明，但本轮没有逐目标的结构化符合性记录；"
                     "不能用已检查目标数量推断作者目标已经实现。")
    else:
        alignment = "未保存独立的目标符合性核验记录，不能由覆盖率推断作者目标已经实现。"
    return ChangeUnderstanding(author_goal=author_goal, actual_changes=actual, alignment=alignment)


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
        if report.pr_intent_context:
            digest.author_goal = report.pr_intent_context
            digest.intent_alignment = ("本轮静态检查已提供冻结的作者声明，具体目标对照及证据缺口见已检查行为的理由。"
                "这些局部检查不构成作者全部目标已实现的独立验收结论。")
            targets = author_targets(report.pr_intent_context)
            # 先按目标解析一次引用，避免对每个目标重复计算；歧义引用同时归入多个目标。
            matched_by_target: dict[str, list[tuple[dict, IntentAssessment, bool]]] = {
                target: [] for target in targets}
            for goal in report.initial_scope_coverage:
                for raw in goal.get("intent_assessments") or []:
                    try:
                        assessment = IntentAssessment.model_validate(raw)
                    except ValidationError:
                        continue
                    # 与执行准入共用同一目标解析规则；执行器接受的引用必须能在此归属。
                    matched = matched_author_targets(assessment.author_quote, targets)
                    ambiguous = len(matched) > 1
                    for matched_target in matched:
                        matched_by_target[matched_target].append((goal, assessment, ambiguous))
            for target in targets:
                entry = DigestIntent(author_target=target)
                for goal, assessment, ambiguous in matched_by_target[target]:
                    needs_source = assessment.alignment in {"supported", "contradicted"}
                    bound = bool(not ambiguous and task.run_manifest
                        and goal.get("snapshot_head_sha") == task.run_manifest.head_sha
                        and goal.get("snapshot_base_sha") == task.run_manifest.effective_base_sha
                        and goal.get("result_id")
                        and (not needs_source or (goal.get("status") == "examined" and assessment.evidence_ids))
                        and set(assessment.evidence_ids).issubset(goal.get("evidence_ids") or []))
                    entry.observations.append({"goal_id": goal.get("goal_id"), "path": goal.get("target_ref"),
                        "start_line": goal.get("start_line"), "side": "base" if goal.get("target_kind") == "base_source_range" else "head",
                        "actual_behavior": assessment.actual_behavior,
                        "alignment": assessment.alignment if bound else "insufficient",
                        "evidence_ids": assessment.evidence_ids if bound else [],
                        "limitations": assessment.limitations + ([] if bound else [
                            "引用同时匹配多个作者目标，归属存在歧义，结论不可采信。" if ambiguous
                            else "缺少有效快照或已接受证据绑定，结论不可采信。"])})
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
    digest.change_understanding = _change_understanding(digest, task, report)
    return digest
