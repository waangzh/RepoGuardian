"""Readable report and native graph audit, without additional model judgments."""

import json

from app.services.publication.publication_planner import PublicationPlanner


class ReportService:
    def __init__(self, artifacts=None):
        self.artifacts = artifacts

    def generate(self, task):
        plan = PublicationPlanner(self.artifacts).build(task)
        if task.review_report is None:
            return plan.user_report_markdown
        audit = json.dumps(task.review_report.model_dump(mode="json"), ensure_ascii=True, indent=2)
        audit = audit.replace("`", "\\u0060").replace("<", "\\u003c")
        return (plan.user_report_markdown + "\n\n<details><summary>Graph 执行记录</summary>\n\n```json\n"
                + audit + "\n```\n</details>\n")
