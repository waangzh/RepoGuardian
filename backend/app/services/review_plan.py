"""Preview 与执行共用的方案归一化、范围裁剪和版本绑定。"""
from app.core.config import settings
from app.models.review_plan import ReviewCapabilities, ReviewPlanOptions, ReviewLimits
from app.services.fingerprints import stable_hash
from app.services.model_pricing import estimate_cost_microusd
from datetime import datetime, timezone


DEPTHS = {"quick": ("快速初筛", .25, 64), "standard": ("标准审查", .6, 256), "deep": ("深度核验", 1., 512)}


def remaining_review_seconds(configuration, created_at):
    created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
    # SQLite DateTime 读回可能丢失 tzinfo；持久化时约定均为 UTC。
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return configuration["deadline_seconds"] - (datetime.now(timezone.utc) - created).total_seconds()


def maxima():
    return {"max_model_calls": settings.repoguardian_run_max_calls,
            "max_tokens": settings.repoguardian_run_max_tokens,
            "max_cost_microusd": settings.repoguardian_run_max_cost_microusd,
            "timeout_seconds": settings.repoguardian_task_review_timeout_seconds}


def default_limits(depth):
    limits = maxima()
    scale = DEPTHS[depth][1]
    return {key: (max(1 if key == "timeout_seconds" else 0, int(value * scale))
                  if value is not None else None) for key, value in limits.items()}


def capabilities():
    return ReviewCapabilities(policy_version=settings.repoguardian_review_policy_version,
        depth_presets=[{"id": key, "label": data[0], "limits": default_limits(key)} for key, data in DEPTHS.items()],
        focus_categories=["correctness", "security", "performance", "compatibility"],
        pr_discussions_available=settings.repoguardian_pr_discussions_enabled,
        pr_discussions_default=settings.repoguardian_pr_discussions_enabled,
        limits={"defaults": default_limits("standard"), "maxima": maxima()})


def normalize_plan(options, model):
    options = options or ReviewPlanOptions()
    include = options.include_pr_discussions
    if include is True and not settings.repoguardian_pr_discussions_enabled:
        raise ValueError("PR discussion review is disabled by server policy")
    if include is None:
        include = settings.repoguardian_pr_discussions_enabled
    values = default_limits(options.depth)
    for key, value in options.limits.model_dump().items():
        if value is not None:
            maximum = maxima()[key]
            if maximum is not None and value > maximum:
                raise ValueError(f"{key} exceeds server policy maximum {maximum}")
            values[key] = value
    if values["max_cost_microusd"] is not None and estimate_cost_microusd(
            settings.repoguardian_model_pricing_json, provider=settings.repoguardian_provider,
            model=model, input_tokens=1, output_tokens=1) is None:
        raise ValueError("cost limit requires configured model pricing; price is unknown")
    return options.model_copy(update={"limits": ReviewLimits(**values), "include_pr_discussions": include})


def apply_scope(plan, options):
    plan = plan.model_copy(deep=True)
    # 根据现有规划器的风险标签/规则排序，重点同时影响派发顺序与模型检查指令。
    tags = {"security": ("auth", "security", "permission", "workflow"),
            "performance": ("performance", "cache", "loop", "concurrency"),
            "compatibility": ("dependency", "migration", "interface", "compatibility"),
            "correctness": ("correctness", "error", "logic", "boundary")}
    def priority(unit):
        description = " ".join([*unit.risk_tags, *unit.rule_ids, unit.grouping_reason]).lower()
        return next((index for index, category in enumerate(options.focus)
                     if any(tag in description for tag in tags[category])), len(options.focus))
    plan.review_units.sort(key=priority)
    if options.scope.mode == "all":
        return plan
    from app.models.review import ExcludedReviewFile
    selected = set(options.scope.primary_files)
    eligible = {file.file_path for file in plan.changed_files if file.included}
    if not selected <= eligible:
        raise ValueError("selected files must be eligible changed files: " + ", ".join(sorted(selected - eligible)))
    for file in plan.changed_files:
        if file.included and file.file_path not in selected:
            file.included = False
            file.excluded_reason = "outside_selected_scope"
            plan.excluded_files.append(ExcludedReviewFile(file_path=file.file_path, reason="outside_selected_scope"))
    units = []
    for unit in plan.review_units:
        primary = [path for path in unit.primary_files if path in selected]
        if primary:
            # 保留 hunk 身份，目标生成仅遍历裁剪后的 primary_files。
            units.append(unit.model_copy(update={"primary_files": primary}))
    plan.review_units = units
    plan.warnings.append("自定义范围之外的变更未主审；关联上下文仍遵守原有只读权限。")
    return plan


def configure_runtime(configuration, options, snapshot, plan_hash):
    config = dict(configuration)
    if options.include_pr_discussions is False:
        config["discussion_contract"] = None
    config.update(effective_plan=options.model_dump(mode="json"), snapshot=snapshot,
                  plan_hash=plan_hash, deadline_seconds=options.limits.timeout_seconds,
                  limits={"max_calls": options.limits.max_model_calls,
                          "max_tokens": options.limits.max_tokens,
                          "max_cost_microusd": options.limits.max_cost_microusd})
    return config


def preview_hash(options, snapshot, configuration, plan):
    return stable_hash({"options": options.model_dump(mode="json"), "snapshot": snapshot,
                        "configuration": configuration, "initial_plan": plan.model_dump(mode="json")})
