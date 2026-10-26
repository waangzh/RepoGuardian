"""Markdown 报告生成器 —— 按实际产出渲染内容优先的审查报告。"""

import html
import re

from app.models.review import ReviewTask


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class ReportService:
    """生成 Markdown 格式的代码审查报告。"""

    def generate(self, task: ReviewTask) -> str:
        """从 ReviewTask 渲染完整 Markdown 报告。"""
        lines: list[str] = ["# RepoGuardian 代码审查报告", ""]
        if not task.task_runtime_report:
            return chr(10).join(lines + ["任务级报告尚未生成，不能据此判断本轮检查完整性。"])
        from app.services.publication.publication_planner import PublicationPlanner
        from app.services.publication.publication_renderer import render_technical_audit
        from app.services.review.review_digest import execution_record
        plan = PublicationPlanner().build(task)
        lines = [render_technical_audit(plan, execution_record(task.task_runtime_report))]
        _append_execution_details(lines, task)
        return chr(10).join(lines)


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
            f"未确认 {metrics.unresolved_count + metrics.needs_human_count}，"
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
