from collections import Counter

from app.models.review import ReviewTask


class ReportService:
    def generate(self, task: ReviewTask) -> str:
        pr = task.pr
        lines: list[str] = ["# RepoGuardian 代码审查报告", ""]

        if pr:
            lines.extend(
                [
                    "## 1. PR 概览",
                    "",
                    f"- 仓库：{pr.owner}/{pr.repo}",
                    f"- PR：#{pr.number}",
                    f"- 标题：{pr.title}",
                    f"- 链接：{pr.html_url}",
                    f"- Base：{pr.base.ref} ({pr.base.sha[:8]})",
                    f"- Head：{pr.head.ref} ({pr.head.sha[:8]})",
                    f"- 模型：{task.model or '默认配置'}",
                    "",
                ]
            )

        total_additions = sum(file.additions for file in task.changed_files)
        total_deletions = sum(file.deletions for file in task.changed_files)
        lines.extend(
            [
                "## 2. 变更概览",
                "",
                f"- 变更文件数：{len(task.changed_files)}",
                f"- 新增行数：{total_additions}",
                f"- 删除行数：{total_deletions}",
                "",
            ]
        )
        if task.changed_files:
            lines.extend(["| 文件 | 类型 | 新增 | 删除 |", "|---|---|---:|---:|"])
            for file in task.changed_files:
                lines.append(
                    f"| `{file.file_path}` | {file.change_type} | {file.additions} | {file.deletions} |"
                )
            lines.append("")

        lines.extend(["## 3. 总体结论", ""])
        if task.issues:
            severity_counts = Counter(issue.severity.value for issue in task.issues)
            lines.append(
                "本次审查发现 "
                f"{len(task.issues)} 个问题："
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
            lines.append("无。")
            lines.append("")
        for index, issue in enumerate(task.issues, start=1):
            location = issue.file_path
            if issue.line_no is not None:
                location = f"{location}:{issue.line_no}"
            lines.extend(
                [
                    f"### 4.{index} {issue.title}",
                    "",
                    f"- 位置：`{location}`",
                    f"- 风险等级：{issue.severity.value}",
                    f"- 类型：{issue.category.value}",
                    f"- 置信度：{issue.confidence:.2f}",
                    "",
                    "问题说明：",
                    issue.description,
                    "",
                    "修复建议：",
                    issue.suggestion,
                    "",
                ]
            )

        lines.extend(
            [
                "## 5. 任务信息",
                "",
                f"- 任务 ID：`{task.id}`",
                f"- 状态：{task.status.value}",
                f"- 创建时间：{task.created_at.isoformat()}",
                f"- 更新时间：{task.updated_at.isoformat()}",
                "",
            ]
        )
        return "\n".join(lines)

