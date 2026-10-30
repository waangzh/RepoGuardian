"""Bounded feedback on proposed structured submissions; never grants coverage."""

import json

from langchain_core.messages import ToolMessage

from app.review.findings import covered_goals
from app.review.models import DiscoveryOutput


def issued_source_ids(messages, material_ids):
    issued = set(material_ids)
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {
                "read_file", "read_base_file", "read_symbol"}:
            continue
        try:
            body = json.loads(message.content)
            if body.get("ok") is True and not body.get("truncated"):
                issued.add(body["evidence"]["id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return issued


def submission_feedback(output, goals, service, issued, identity):
    if not isinstance(output, DiscoveryOutput) or not goals:
        return None
    expected = {goal["id"] for goal in goals}
    claimed = {item.goal_id for item in output.examined}
    coverage = covered_goals(goals, [{
        "id": identity, "status": "completed", "output": output.model_dump(),
        "evidence_ids": issued,
    }], service, [{"id": identity, "goal_ids": list(expected)}])
    pending = [goal for goal in coverage if goal["status"] == "not_examined"
               and (goal["id"] in claimed or not output.limitations)]
    issues = {}
    if pending:
        issues["uncovered_goals"] = pending[:12]
        issues["uncovered_goal_count"] = len(pending)
    if claimed - expected:
        issues["unknown_goal_ids"] = sorted(claimed - expected)[:12]
    if not issues:
        return None
    return "Error: review_submission_needs_correction\n" + json.dumps({
        **issues,
        "instruction": (
            "这是一次提交验收反馈，不代表已完成覆盖。核对目标的文件、BASE/HEAD 侧别和完整范围；"
            "先复用 source_materials 或本分支已读取的同侧证据。确实缺少时，在仍允许工具调用的"
            "原预算内补读并重新提交；BASE 使用 read_base_file，HEAD 使用 read_file。"
            "若已进入仅提交阶段或无法补读，移除不实 examined 声明并明确保留证据缺口，"
            "不得伪造证据或宣称完成。不合格的变更观察会在报告阶段被丢弃，"
            "不要把本次纠错用于观察；集中修复覆盖声明。"
        ),
    }, ensure_ascii=False)
