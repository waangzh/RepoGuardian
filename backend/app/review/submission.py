"""Bounded feedback on proposed structured submissions; never grants coverage."""

import json

from langchain_core.messages import ToolMessage

from app.review.findings import covered_goals
from app.review.limitations import validate_resolutions
from app.review.models import BatchVerificationOutput, DiscoveryGap, DiscoveryOutput
from app.review.observations import validate_observation

EXECUTION_BOUNDARIES = (
    "未执行代码", "未运行代码", "未执行测试", "未运行测试", "未执行构建", "未运行构建",
    "未执行脚本", "未运行脚本", "没有执行代码", "没有运行代码", "没有执行测试", "没有运行测试",
    "did not run tests", "no tests were run", "tests were not run", "code was not executed",
)


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


def submission_feedback(output, goals, service, issued, identity, limitations=()):
    issues = {}
    if isinstance(output, DiscoveryOutput) and goals:
        expected = {goal["id"] for goal in goals}
        invalid_gaps = [index for index, gap in enumerate(output.limitations)
                        if isinstance(gap, DiscoveryGap) and not set(gap.goal_ids) <= expected]
        if invalid_gaps:
            issues["invalid_gap_goal_ids"] = invalid_gaps
            issues["allowed_goal_ids"] = sorted(expected)
        claimed = {item.goal_id for item in output.examined}
        coverage = covered_goals(goals, [{
            "id": identity, "status": "completed", "output": output.model_dump(),
            "evidence_ids": issued,
        }], service, [{"id": identity, "goal_ids": list(expected)}])
        pending = [goal for goal in coverage if goal["status"] == "not_examined"
                   and (goal["id"] in claimed or not output.limitations)]
        if pending:
            issues["uncovered_goals"] = pending[:12]
            issues["uncovered_goal_count"] = len(pending)
        if claimed - expected:
            issues["unknown_goal_ids"] = sorted(claimed - expected)[:12]
        paths = {goal["path"] for goal in goals}
        rejected = []
        for index, observation in enumerate(output.observations):
            try:
                validate_observation(observation, service, issued, paths, goals)
            except (ValueError, OSError) as exc:
                rejected.append({"index": index, "code": str(exc) if isinstance(exc, ValueError)
                                 else "observation_source_unavailable"})
        if rejected:
            available = []
            for identity in sorted(issued):
                try:
                    ref = service.resolve(identity)
                    if ref.path in paths:
                        available.append(ref.model_dump())
                except (ValueError, OSError):
                    continue
            issues["invalid_observations"] = rejected
            issues["available_observation_evidence"] = available[:32]
    outputs = output.decisions if isinstance(output, BatchVerificationOutput) else [output]
    invalid = []
    for index, item in enumerate(outputs):
        resolutions = getattr(item, "limitation_resolutions", [])
        try:
            validate_resolutions(resolutions, limitations, getattr(item, "candidate_id", identity), issued, service)
        except (ValueError, OSError):
            invalid.append(index)
    if invalid:
        issues["invalid_limitation_resolutions"] = invalid
    boundaries = [{"output_index": index, "limitation_index": offset}
                  for index, item in enumerate(outputs)
                  for offset, text in enumerate(getattr(item, "limitations", []))
                  if (text.missing_fact if isinstance(text, DiscoveryGap) else text)
                  .lstrip().casefold().startswith(EXECUTION_BOUNDARIES)]
    if boundaries:
        issues["possible_boundary_notes"] = boundaries[:12]
    if not issues:
        return None
    return "Error: review_submission_needs_correction\n" + json.dumps({
        **issues,
        "instruction": (
            "这是一次提交验收反馈，不代表已完成覆盖。核对目标的文件、BASE/HEAD 侧别和完整范围；"
            "先复用 source_materials 或本分支已读取的同侧证据。确实缺少时，在仍允许工具调用的"
            "原预算内补读并重新提交；BASE 使用 read_base_file，HEAD 使用 read_file。"
            "若已进入仅提交阶段或无法补读，移除不实 examined 声明并明确保留证据缺口，"
            "不得伪造证据或宣称完成。possible_boundary_notes 仅提示核对归类："
            "未执行代码、测试或构建是静态审查边界，应放在 boundary_notes；"
            "limitations 只保留影响结论的具体缺失源码或未解决前提。"
            "若同一条同时包含真实证据缺口，必须保留该缺口，不能只为通过验收而删除限制。"
            "limitation_resolutions 只能引用已提供且关联当前候选的限制 ID，附本分支实际读取的源码证据；"
            "不能凭候选被排除就关闭限制。无法证实时移除处理声明，原限制将继续保留。"
            "主审 limitations 逐条说明本分支 goal_ids、missing_fact、dependent_conclusion、"
            "change_relevance 和 next_check；它们是未独立核实的局部说明，不是已确认缺陷。"
            "不要将穷尽全部构造、全仓正确性或未执行测试自动视为必要前提。"
            "invalid_observations 按 index/code 标记未通过的变更观察；优先复用本分支已读的"
            "available_observation_evidence，补齐对应 BASE/HEAD 引用，也可以移除无法支持的观察。"
            "before 要有同一变更范围的 BASE 源码，after 要有 HEAD；不能用其他文件的侧别凑数。"
            "无法支持的比较必须撤回，不得仅删除 before 字段却继续宣称新增或前后不变。"
            "观察只作可选背景，不要求逐文件提交；变更总览由服务端冻结 Diff 独立提供依据。"
        ),
    }, ensure_ascii=False)
