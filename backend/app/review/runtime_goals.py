"""初始审查目标的确定性规划；复用与派生任务不能改写这一分母。"""

from app.core.fingerprints import stable_hash
from app.models.review_runtime import ReviewGoal
from app.review.hunk_identity import hunk_identity


def plan_runtime_goals(changed, plan):
    """初始分母来自完整规范 diff；复用、分页和派生任务不能修改它。"""
    goals = []
    owned = set()
    for unit in plan.review_units:
        for file in changed:
            if file.file_path not in unit.primary_files:
                continue
            owned.add(file.file_path)
            selected = [(index, hunk) for index, hunk in enumerate(file.hunks)
                        if not unit.diff_hunk_ids or hunk_identity(
                            file.file_path, index, hunk.model_dump(mode="json")) in unit.diff_hunk_ids]
            if file.is_binary or not selected:
                goals.append(ReviewGoal(goal_id="file-" + stable_hash([unit.id, file.model_dump(mode="json")])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path, target_kind="file_change",
                    unsupported_reason="non_head_text_change"))
                continue
            if file.change_type == "deleted":
                # 整文件删除与文件清空只有 Base 侧文本；unidiff 会同样判为 deleted。
                # 范围按删除前行号，不构造 Head 零长度区间。
                for index, hunk in selected:
                    identity = hunk_identity(file.file_path, index, hunk.model_dump(mode="json"))
                    goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                        unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                        target_kind="base_source_range", start_line=max(1, hunk.old_start),
                        end_line=max(1, hunk.old_start + hunk.old_length - 1)))
                continue
            for index, hunk in selected:
                identity = hunk_identity(file.file_path, index, hunk.model_dump(mode="json"))
                if hunk.new_length == 0 or hunk.new_start < 1:
                    # 纯删除 hunk 没有 Head 行；改在 Base 侧核对被删原文。
                    goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                        unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                        target_kind="base_source_range", start_line=max(1, hunk.old_start),
                        end_line=max(1, hunk.old_start + hunk.old_length - 1)))
                    continue
                goals.append(ReviewGoal(goal_id="goal-" + stable_hash([unit.id, identity])[:32],
                    unit_id=unit.id, kind="inspect", target_ref=file.file_path,
                    start_line=hunk.new_start, end_line=hunk.new_start + hunk.new_length - 1))
    # 排除文件仍可见，不能变成零分母的完整结果。
    for file in changed:
        if file.file_path not in owned:
            goals.append(ReviewGoal(goal_id="excluded-" + stable_hash(file.model_dump(mode="json"))[:32],
                unit_id="excluded", kind="inspect", target_ref=file.file_path, target_kind="file_change",
                unsupported_reason="excluded_or_unsupported_change"))
    return goals
