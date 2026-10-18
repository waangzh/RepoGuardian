"""报告共用的确定性事实投影；作者声明与静态事实不合成为目标实现证明。"""

import re

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
        # 与 Findings 页共用人工处理身份和机器状态；不回写原始运行数据。
        digest.findings = sorted(finding_views(task, dispositions or {}),
            key=lambda item: (item.id, stable_hash(item.model_dump(mode="json"))))
    return digest
