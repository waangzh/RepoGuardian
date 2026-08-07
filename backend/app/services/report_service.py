"""Markdown 报告生成器 —— 按实际产出渲染内容优先的审查报告。"""

from collections import Counter
import html
import re

from app.models.review import ReviewTask


_MODE_LABELS = {
    "review": "只读审查",
    "review_and_suggest": "审查并生成修复建议",
    "review_suggest_and_validate": "审查、修复建议与验证",
}
_CONVENTIONAL_TITLE_PREFIX = re.compile(
    r"^(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class ReportService:
    """生成 Markdown 格式的代码审查报告，包含 PR 概览、变更、问题、修复、测试等章节。"""

    def generate(self, task: ReviewTask) -> str:
        """从 ReviewTask 渲染完整 Markdown 报告，仅展示适用或有结果的章节。"""
        lines: list[str] = ["# RepoGuardian 代码审查报告", ""]
        _append_pr_summary(lines, task)
        _append_pr_purpose(lines, task)
        _append_issue_summary(lines, task)
        _append_change_summary(lines, task)
        _append_issue_details(lines, task)
        _append_static_results(lines, task)
        _append_validation_results(lines, task)
        _append_patch_results(lines, task)
        _append_test_results(lines, task)
        _append_limitations(lines, task)
        _append_execution_details(lines, task)
        return "\n".join(lines)


def _append_pr_summary(lines: list[str], task: ReviewTask) -> None:
    """PR 概览：仓库、编号、标题、链接、base/head。"""
    if not task.pr:
        return
    pr = task.pr
    lines.extend([
        "## PR 概览",
        "",
        f"- 仓库：{pr.owner}/{pr.repo}",
        f"- PR：{pr.number}",
        f"- 标题：{pr.title}",
        f"- 链接：{pr.html_url}",
        f"- Base：{pr.base.ref} ({pr.base.sha[:8]})",
        f"- Head：{pr.head.ref} ({pr.head.sha[:8]})",
        f"- 审查模式：{_MODE_LABELS.get(task.mode.value, task.mode.value)}",
        f"- 模型：{task.model or '默认配置'}",
        "",
    ])


def _append_pr_purpose(lines: list[str], task: ReviewTask) -> None:
    """用作者描述和实际变更范围解释 PR 的作用，并明确推断来源。"""
    if not task.pr:
        return
    title_purpose = _plain_text_excerpt(
        _CONVENTIONAL_TITLE_PREFIX.sub("", task.pr.title).strip(), limit=240
    )
    author_purpose = _plain_text_excerpt(task.pr.body or "", limit=600)
    lines.extend(["## PR 作用", ""])
    if author_purpose:
        lines.append(f"**作者意图：** {author_purpose}")
        if title_purpose and title_purpose.casefold() not in author_purpose.casefold():
            lines.extend(["", f"**标题概括：** {title_purpose}"])
    elif title_purpose:
        lines.append(f"**作用概括（根据标题）：** {title_purpose}")
    else:
        lines.append("**作用概括：** PR 未提供可用的标题或正文说明。")
    lines.extend(["", f"**实际改动：** {_change_scope_summary(task)}"])
    if not author_purpose:
        lines.extend([
            "",
            "> PR 未提供正文；作用概括来自标题，实际改动来自 Diff 统计，均未视为作者声明。",
        ])
    lines.append("")


def _append_change_summary(lines: list[str], task: ReviewTask) -> None:
    """变更概览：文件数、增减行数表格。"""
    total_additions = sum(file.additions for file in task.changed_files)
    total_deletions = sum(file.deletions for file in task.changed_files)
    lines.extend([
        "## 变更概览",
        "",
        f"- 变更文件数：{len(task.changed_files)}",
        f"- 新增行数：{total_additions}",
        f"- 删除行数：{total_deletions}",
        "",
    ])
    if task.changed_files:
        lines.extend(["| 文件 | 类型 | 新增 | 删除 |", "|---|---|---:|---:|"])
        for file in task.changed_files:
            lines.append(f"| `{file.file_path}` | {file.change_type} | {file.additions} | {file.deletions} |")
        lines.append("")


def _append_issue_summary(lines: list[str], task: ReviewTask) -> None:
    """优先展示面向读者的审查结论。"""
    lines.extend(["## 审查结论", ""])
    if task.issues:
        severity_counts = Counter(issue.severity.value for issue in task.issues)
        lines.append(
            f"本次审查发现 {len(task.issues)} 个问题："
            f"critical {severity_counts['critical']} 个，"
            f"high {severity_counts['high']} 个，"
            f"medium {severity_counts['medium']} 个，"
            f"low {severity_counts['low']} 个。"
        )
    else:
        lines.append("未发现有明确证据的代码问题。")
    lines.append("")


def _append_issue_details(lines: list[str], task: ReviewTask) -> None:
    """仅在存在确认问题时展示详细问题，避免无意义的空章节。"""
    if not task.issues:
        return
    lines.extend(["## 详细问题", ""])
    for index, issue in enumerate(task.issues, start=1):
        anchor = issue.primary_evidence
        location = anchor.file_path
        if anchor.resolved_start_line is not None:
            location = f"{location}:{anchor.resolved_start_line}"
            if anchor.resolved_end_line != anchor.resolved_start_line:
                location += f"-{anchor.resolved_end_line}"
        lines.extend([
            f"### 问题 {index}：{issue.title}",
            "",
            f"- ID：`{issue.id}`",
            f"- 位置：`{location}`",
            f"- 风险等级：{issue.severity.value}",
            f"- 类型：{issue.category.value}",
            f"- 置信度：{issue.confidence:.2f}",
            f"- 评论位置：{issue.placement.value}",
            f"- 问题状态：{issue.status.value}",
            f"- 可自动修复：{'是' if issue.auto_fix_eligible else '否'}",
            f"- 需要人工确认：{'是' if issue.requires_human_confirmation else '否'}",
            "",
            "问题说明：",
            issue.failure_scenario,
            "",
            "代码证据：",
            anchor.existing_code,
            "",
            "证据位置：",
            (
                f"`{location}` ({anchor.resolution_method.value}, {anchor.resolved_side or '-'})"
                if anchor.resolved_start_line is not None
                else f"未定位：{issue.unresolved_reason or anchor.unresolved_reason or 'unknown'}"
            ),
            "",
            "受影响行为：",
            issue.affected_behavior,
            "",
            "假设：",
            "; ".join(issue.assumptions) or "无",
            "",
            "相关测试：",
            ", ".join(issue.related_tests) or "无",
            "",
            "修复建议：",
            issue.recommendation,
            "",
        ])


def _append_static_results(lines: list[str], task: ReviewTask) -> None:
    """仅在实际运行后展示静态分析结果。"""
    if not task.static_results:
        return
    lines.extend(["## 静态分析结果", ""])
    lines.extend(["| 命令 | 状态 | Exit Code | 耗时 |", "|---|---|---:|---:|"])
    for result in task.static_results:
        lines.append(
            f"| `{result.command}` | {'通过' if result.passed else '失败'} | "
            f"{result.exit_code} | {result.duration:.2f}s |"
        )
    lines.append("")


def _append_validation_results(lines: list[str], task: ReviewTask) -> None:
    """展示 Base、Head 与各补丁的验证快照和差异，保留故障归因。"""
    if not (task.validation or task.validation_snapshots or task.validation_deltas):
        return
    lines.extend(["## 验证结果", ""])
    if task.validation:
        lines.extend(["| 后端 | Patch | 状态 | 可信 | 检查 |", "|---|---|---|---|---|"])
        for result in task.validation:
            checks = "; ".join(
                f"{check.name}: {check.status.value}" for check in result.checks
            )
            lines.append(
                f"| {result.backend} | `{(result.patch_id or '')[:8]}` | "
                f"{result.status.value} | {'是' if result.trusted else '否'} | {checks} |"
            )
        lines.append("")
    if not task.validation_snapshots:
        return

    lines.extend([
        "| 阶段 | SHA | Patch | 状态 | 失败类型 | 命令 |",
        "|---|---|---|---|---|---|",
    ])
    for snapshot in task.validation_snapshots:
        commands = ", ".join(
            f"{result.command} ({'通过' if result.passed else '失败'}, exit {result.exit_code})"
            for result in snapshot.command_results
        ) or "未运行"
        lines.append(
            f"| {snapshot.stage.value} | `{snapshot.sha[:8]}` | "
            f"`{(snapshot.patch_id or '')[:8]}` | {'通过' if snapshot.passed else '失败'} | "
            f"{snapshot.failure_kind.value if snapshot.failure_kind else '-'} | {commands} |"
        )
        if snapshot.failure_detail:
            lines.append(f"> {snapshot.stage.value}：{snapshot.failure_detail}")
    lines.append("")

    if task.validation_deltas:
        lines.extend([
            "| 比较 | Patch | 新增失败 | 已解决失败 | 结论 |",
            "|---|---|---|---|---|",
        ])
        for delta in task.validation_deltas:
            outcome = delta.failure_kind.value if delta.failure_kind else "通过"
            lines.append(
                f"| {delta.from_stage.value} → {delta.to_stage.value} | "
                f"`{(delta.patch_id or '')[:8]}` | {'是' if delta.introduced_failure else '否'} | "
                f"{'是' if delta.resolved_failure else '否'} | {outcome} |"
            )
        lines.append("")


def _append_patch_results(lines: list[str], task: ReviewTask) -> None:
    """仅在生成候选修复后展示结果表格。"""
    if not task.patches:
        return
    lines.extend(["## 候选修复结果", ""])
    lines.extend([
        "| Patch | Issues | 文件 | 风险 | apply-check | 验证状态 | Head | 过期 |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for patch in task.patches:
        status = patch.status.value
        lines.append(
            f"| `{patch.id[:8]}` | `{', '.join(patch.issue_ids)}` | "
            f"{', '.join(patch.touched_files)} | {patch.risk} | {patch.apply_check.status.value} | "
            f"{status} | `{patch.head_sha[:8]}` | {'是' if patch.stale else '否'} |"
        )
        if patch.presentation:
            lines.append(f"> {patch.presentation.warning}")
        elif status in {"unverified", "suggested"}:
            lines.append("> 候选修复，尚未运行项目测试。")
        if patch.error:
            lines.append(f"> apply-check 错误：{patch.error}")
    lines.append("")


def _append_test_results(lines: list[str], task: ReviewTask) -> None:
    """仅在实际运行后展示测试结果。"""
    if not task.test_results:
        return
    lines.extend(["## 测试结果", ""])
    lines.extend(["| 命令 | 状态 | Exit Code | 耗时 |", "|---|---|---:|---:|"])
    for result in task.test_results:
        lines.append(
            f"| `{result.command}` | {'通过' if result.passed else '失败'} | "
            f"{result.exit_code} | {result.duration:.2f}s |"
        )
    lines.append("")


def _append_limitations(lines: list[str], task: ReviewTask) -> None:
    """保留真正影响审查可信度的警告与失败，而不是输出未触发步骤。"""
    limitations = list(task.warnings)
    if task.error:
        limitations.append(task.error)
    for event in task.agent_events:
        if event.status in {"failed", "timed_out", "cancelled"}:
            detail = event.message or event.reason
            limitations.append(f"{event.action}（{event.status}）：{detail}")
    limitations = list(dict.fromkeys(item.strip() for item in limitations if item.strip()))
    if not limitations:
        return
    lines.extend(["## 审查限制与警告", ""])
    lines.extend(f"- {_inline_text(item)}" for item in limitations)
    lines.append("")


def _append_execution_details(lines: list[str], task: ReviewTask) -> None:
    """将诊断信息放入默认折叠的附录，保持主报告聚焦结论。"""
    metrics = task.issue_metrics
    lines.extend([
        "<details>",
        "<summary>执行详情（Agent 决策与任务元数据）</summary>",
        "",
        "### Issue 流水线",
        "",
        (
            f"候选 {metrics.candidate_issue_count}，"
            f"确定性过滤 {metrics.deterministic_drop_count}，"
            f"verifier 丢弃 {metrics.verifier_drop_count}，"
            f"需人工 {metrics.needs_human_count}，"
            f"重复 {metrics.duplicate_count}，"
            f"确认 {metrics.confirmed_count}。"
        ),
        "",
    ])
    if task.agent_events:
        lines.extend([
            "### Agent 决策日志",
            "",
            "| 动作 | 状态 | 理由 | 消息 |",
            "|---|---|---|---|",
        ])
        for event in task.agent_events:
            lines.append(
                f"| {_table_cell(str(event.action))} | {_table_cell(event.status)} | "
                f"{_table_cell(event.reason)} | {_table_cell(event.message or '')} |"
            )
        lines.append("")
    lines.extend([
        "### 任务信息",
        "",
        f"- 任务 ID：`{task.id}`",
        f"- 状态：{task.status.value}",
        f"- 创建时间：{task.created_at.isoformat()}",
        f"- 更新时间：{task.updated_at.isoformat()}",
        "",
        "</details>",
        "",
    ])


def _change_scope_summary(task: ReviewTask) -> str:
    files = task.changed_files
    if not files:
        return "未解析到可展示的文件变更。"
    test_files = [file for file in files if _is_test_file(file.file_path)]
    implementation_count = len(files) - len(test_files)
    scopes: list[str] = []
    if implementation_count:
        scopes.append(f"{implementation_count} 个实现或配置文件")
    if test_files:
        scopes.append(f"{len(test_files)} 个测试文件")
    additions = sum(file.additions for file in files)
    deletions = sum(file.deletions for file in files)
    paths = "、".join(f"`{file.file_path}`" for file in files[:4])
    if len(files) > 4:
        paths += f" 等 {len(files)} 个文件"
    return f"涉及{'、'.join(scopes)}，新增 {additions} 行、删除 {deletions} 行；主要包括 {paths}。"


def _is_test_file(file_path: str) -> bool:
    normalized = file_path.casefold()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized}/"
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    )


def _plain_text_excerpt(value: str, *, limit: int) -> str:
    text = _HTML_COMMENT.sub(" ", value)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+(?:\[[ xX]\]\s*)?", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return _inline_text(text)


def _inline_text(value: str) -> str:
    return html.escape(value, quote=False)


def _table_cell(value: str) -> str:
    return _inline_text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
