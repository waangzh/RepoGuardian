"""Deterministic assignments and coverage goals, independent of model protocols."""

import re

from app.review.evidence import digest
from app.review.models import ReviewAssignment, ReviewGoal
from app.review.tool_scope import is_sensitive_repository_change


def plan_review(changed_files: list[dict], *, depth: str, selected_paths=None, focus=()):
    goals, assignments, excluded = [], [], []
    for change in changed_files:
        path = change["file_path"]
        if (is_sensitive_repository_change(path, change.get("old_file_path"))
                or change.get("is_binary") or (selected_paths is not None and path not in selected_paths)):
            excluded.append(path)
            continue
        file_goals = []
        for hunk in change.get("hunks", []):
            for side in ("head", "base"):
                prefix = "new" if side == "head" else "old"
                start, length = hunk[f"{prefix}_start"], hunk[f"{prefix}_length"]
                source_path = (change.get("old_file_path") or path) if side == "base" else path
                for offset in range(0, length, 200):
                    first, last = start + offset, start + min(length, offset + 200) - 1
                    identity = digest(f"{source_path}:{side}:{first}:{last}")
                    file_goals.append(ReviewGoal(id=identity, path=source_path, side=side,
                                                 start_line=first, end_line=last))
        if not file_goals:
            # Metadata/binary changes cannot acquire fictional line evidence.
            file_goals.append(ReviewGoal(id=digest(path + ":metadata"), path=path, side="head",
                start_line=1, end_line=1, status="blocked", reason="no_text_source_range"))
        goals.extend(file_goals)
        roles = ["behavior", "contract"]
        risk = re.search(r"auth|permission|security|migrat|transact|lock|thread|async|pool|session",
                         path + " " + " ".join(line.get("content", "")
                         for hunk in change.get("hunks", []) for line in hunk.get("lines", [])), re.I)
        if risk:
            roles.append("specialist")
        for role in roles:
            assignments.append(ReviewAssignment(
                id=digest(f"{path}:{role}"), reviewer=role,
                goal_ids=[goal.id for goal in file_goals if goal.status != "blocked"],
                focus_paths=[path], objective={
                    "behavior": "检查变更前后的行为差异、异常路径与边界回归",
                    "contract": "检查调用者、消费者、数据结构与跨模块契约",
                    "specialist": "检查本次变更中的权限、并发、事务和资源生命周期风险",
                }[role] + f"；审查深度：{depth}；重点：{', '.join(focus)}",
            ))
    unique_goals = {goal.id: goal for goal in goals}
    return list(unique_goals.values()), assignments, excluded
