"""Markdown 报告生成器 —— 将 ReviewTask 聚合根渲染为 9 节结构的审查报告。"""

from collections import Counter

from app.models.review import ReviewTask


class ReportService:
    """生成 Markdown 格式的代码审查报告，包含 PR 概览、变更、问题、修复、测试等章节。"""

    def generate(self, task: ReviewTask) -> str:
        """从 ReviewTask 渲染完整 Markdown 报告（9 个章节）。"""
        lines: list[str] = ["# RepoGuardian 代码审查报告", ""]
        _append_pr_summary(lines, task)
        _append_change_summary(lines, task)
        _append_issue_summary(lines, task)
        _append_static_results(lines, task)
        _append_validation_results(lines, task)
        _append_patch_results(lines, task)
        _append_test_results(lines, task)
        _append_agent_events(lines, task)
        _append_task_info(lines, task)
        return "\n".join(lines)


def _append_pr_summary(lines: list[str], task: ReviewTask) -> None:
    """1. PR 概览：仓库、编号、标题、链接、base/head。"""
    if not task.pr:
        return
    pr = task.pr
    lines.extend([
        "## 1. PR 概览",
        "",
        f"- 仓库：{pr.owner}/{pr.repo}",
        f"- PR：{pr.number}",
        f"- 标题：{pr.title}",
        f"- 链接：{pr.html_url}",
        f"- Base：{pr.base.ref} ({pr.base.sha[:8]})",
        f"- Head：{pr.head.ref} ({pr.head.sha[:8]})",
        f"- 模型：{task.model or '默认配置'}",
        "",
    ])


def _append_change_summary(lines: list[str], task: ReviewTask) -> None:
    """2. 变更概览：文件数、增减行数表格。"""
    total_additions = sum(file.additions for file in task.changed_files)
    total_deletions = sum(file.deletions for file in task.changed_files)
    lines.extend([
        "## 2. 变更概览",
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
    """3-4. 审查结论 + 详细问题列表。"""
    lines.extend(["## 3. 审查结论", ""])
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

    lines.extend(["## 4. 详细问题", ""])
    if not task.issues:
        lines.extend(["无。", ""])
        return
    for index, issue in enumerate(task.issues, start=1):
        location = issue.file_path
        if issue.line_no is not None:
            location = f"{location}:{issue.line_no}"
        lines.extend([
            f"### 4.{index} {issue.title}",
            "",
            f"- ID：`{issue.id}`",
            f"- 位置：`{location}`",
            f"- 风险等级：{issue.severity.value}",
            f"- 类型：{issue.category.value}",
            f"- 置信度：{issue.confidence:.2f}",
            f"- 可自动修复：{'是' if issue.auto_fixable else '否'}",
            "",
            "问题说明：",
            issue.description,
            "",
            "修复建议：",
            issue.suggestion,
            "",
        ])


def _append_static_results(lines: list[str], task: ReviewTask) -> None:
    """5. 静态分析结果表格。"""
    lines.extend(["## 5. 静态分析结果", ""])
    if not task.static_results:
        lines.extend(["未运行。", ""])
        return
    lines.extend(["| 命令 | 状态 | Exit Code | 耗时 |", "|---|---|---:|---:|"])
    for result in task.static_results:
        lines.append(
            f"| `{result.command}` | {'通过' if result.passed else '失败'} | "
            f"{result.exit_code} | {result.duration:.2f}s |"
        )
    lines.append("")


def _append_validation_results(lines: list[str], task: ReviewTask) -> None:
    """展示 Base、Head 与各补丁的验证快照和差异，保留故障归因。"""
    lines.extend(["## 6. 三阶段验证", ""])
    if not task.validation_snapshots:
        lines.extend(["未运行。", ""])
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
    """7. 自动修复结果表格。"""
    lines.extend(["## 7. 自动修复结果", ""])
    if not task.patches:
        lines.extend(["未生成 patch。", ""])
        return
    lines.extend(["| Patch | Issue | 状态 | 错误 |", "|---|---|---|---|"])
    for patch in task.patches:
        lines.append(
            f"| `{patch.id[:8]}` | `{patch.issue_id or ''}` | {patch.status} | {patch.error or ''} |"
        )
    lines.append("")


def _append_test_results(lines: list[str], task: ReviewTask) -> None:
    """8. 测试结果表格。"""
    lines.extend(["## 8. 测试结果", ""])
    if not task.test_results:
        lines.extend(["未运行。", ""])
        return
    lines.extend(["| 命令 | 状态 | Exit Code | 耗时 |", "|---|---|---:|---:|"])
    for result in task.test_results:
        lines.append(
            f"| `{result.command}` | {'通过' if result.passed else '失败'} | "
            f"{result.exit_code} | {result.duration:.2f}s |"
        )
    lines.append("")


def _append_agent_events(lines: list[str], task: ReviewTask) -> None:
    """9. Agent 决策日志表格。"""
    lines.extend(["## 9. Agent 决策日志", ""])
    if not task.agent_events:
        lines.extend(["无。", ""])
        return
    lines.extend(["| 动作 | 状态 | 理由 | 消息 |", "|---|---|---|---|"])
    for event in task.agent_events:
        lines.append(f"| {event.action} | {event.status} | {event.reason} | {event.message or ''} |")
    lines.append("")


def _append_task_info(lines: list[str], task: ReviewTask) -> None:
    """10. 任务元信息。"""
    lines.extend([
        "## 10. 任务信息",
        "",
        f"- 任务 ID：`{task.id}`",
        f"- 状态：{task.status.value}",
        f"- 创建时间：{task.created_at.isoformat()}",
        f"- 更新时间：{task.updated_at.isoformat()}",
        "",
    ])
