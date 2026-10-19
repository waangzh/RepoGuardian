"""只读取已有验证结果；不启动验证，不把候选补丁等同于当前 Head。"""

from app.models.publication import DigestValidation
from app.models.review import FailureKind


def validation_digest(task):
    records = []
    head = task.run_manifest.head_sha if task.run_manifest else None
    base = task.run_manifest.effective_base_sha if task.run_manifest else None
    for snapshot in task.validation_snapshots:
        stage = snapshot.stage.value
        bound = bool((stage == "head" and snapshot.sha == head and not snapshot.patch_id)
                     or (stage == "base" and snapshot.sha == base and not snapshot.patch_id))
        executed = bool(snapshot.command_results)
        consistent = (snapshot.passed == all(item.passed and item.exit_code == 0 for item in snapshot.command_results)
                      and all(item.passed == (item.exit_code == 0) for item in snapshot.command_results))
        classified = snapshot.failure_kind is None or (
            snapshot.failure_kind == FailureKind.code_regression and not snapshot.passed)
        status = ("passed" if snapshot.passed else "failed") if executed and consistent and classified else "unknown"
        reason = "已有受控验证快照；仅适用于记录的阶段和命令。"
        if snapshot.failure_kind:
            kind = {FailureKind.code_regression: "代码回归", FailureKind.dependency_missing: "依赖缺失",
                FailureKind.test_collection_error: "测试收集错误", FailureKind.timeout: "超时",
                FailureKind.infrastructure: "基础设施故障", FailureKind.unknown: "失败归因未知"}[snapshot.failure_kind]
            reason += f"失败分类：{kind}。"
        records.append(DigestValidation(id=snapshot.id, source="controlled_snapshot", stage=stage,
            head_sha=snapshot.sha, status=status, trusted=bound and executed and consistent,
            snapshot_matches=bound, reason=reason if bound else "快照或阶段不匹配，不能用于当前 Head 结论。",
            checks=[f"{item.command}: {'passed' if item.passed else 'failed'}" for item in snapshot.command_results]))
    patches = {patch.id: patch for patch in task.patches}
    for result in task.validation:
        patch = patches.get(result.patch_id)
        bound = bool(head and result.head_sha == head and patch and patch.head_sha == head
                     and result.patch_sha == patch.patch_sha and patch.validation_result_id == result.id)
        provenance = (result.backend == "user_runner" and result.trust_source == "user_runner") or (
            result.backend == "project_ci" and (result.trust_source or "").startswith("project_ci:"))
        trusted = result.trusted and bool(provenance) and bound
        records.append(DigestValidation(id=result.id, source=result.backend, stage="patched",
            head_sha=result.head_sha, patch_sha=result.patch_sha, status=result.status.value,
            trusted=trusted, snapshot_matches=bound,
            reason="已有独立补丁验证结果，不代表未经补丁修改的 PR Head 已通过验证。" if trusted else
                "结果未通过可信来源或当前补丁身份绑定，不能据此判断当前代码通过。",
            checks=[f"{item.name}: {item.status.value}" for item in result.checks], completed_at=result.completed_at))
    current_head = [item for item in records if item.stage == "head" and item.trusted and item.snapshot_matches]
    if any(item.status == "failed" for item in current_head):
        state = "failed"
    elif current_head and all(item.status == "passed" for item in current_head):
        state = "passed"
    elif any(item.trusted and item.stage == "patched" for item in records):
        state = "independent_results"
    else:
        state = "unknown" if records else "not_executed"
    return state, records
