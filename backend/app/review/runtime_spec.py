"""审查运行配置与冻结 Spec 的确定性构造；图节点只做状态适配。"""

from __future__ import annotations

from app.core.config import settings
from app.core.fingerprints import stable_hash
from app.llm.model_request_budgeter import ModelRequestProfile
from app.models.review_runtime import ReviewRunSpec
from app.review.diff_base import review_diff_base


def task_runtime_configuration(model=None, *, provider=None):
    provider_name = getattr(provider, "_provider_name", settings.repoguardian_provider)
    selected_model = model or settings.repoguardian_model
    resolver = getattr(provider, "resolve_request_profile", None)
    profile = resolver(selected_model) if callable(resolver) else settings.resolve_model_profile(selected_model)
    return {"provider": provider_name, "model": selected_model,
        "output_policy": settings.review_output_policy(),
        "prompt_version": "task-review-prompt-v8",
        "stability_contract": "review-stability-v2",
        "discussion_contract": "pr-discussion-context-v1" if settings.repoguardian_pr_discussions_enabled else None,
        "publication_contract": "claim-publication-v1",
        "pr_intent_contract": "pr-intent-assessment-v6",
        # 新任务冻结具体关系规划；历史手工配置缺省为 legacy，恢复时不能静默换规划器。
        "risk_planner_version": "risk-planner-v8",
        "call_resolution_version": "import-evidence-v6",
        "readable_scope_version": "repository-tracked-v1",
        "endpoint": getattr(provider, "_base_url", settings.openai_base_url),
        "limits": settings.run_budget_limits(), "policy_version": settings.repoguardian_review_policy_version,
        "concurrency": min(32, max(1, settings.repoguardian_review_unit_concurrency)),
        "deadline_seconds": settings.repoguardian_task_review_timeout_seconds,
        "request_attempts": settings.repoguardian_model_request_attempts,
        "request_timeout_seconds": settings.repoguardian_model_request_timeout_seconds,
        "max_lines_per_read": settings.repoguardian_review_unit_max_lines_per_read,
        "max_search_results": settings.repoguardian_review_unit_max_search_results,
        "request_profile": profile.profile.model_dump(mode="json")}


def frozen_runtime_spec(state, configuration, budget, *, readable_files=()):
    # 仅新配置启用；旧任务恢复不改变其冻结请求。上下文进入 Spec 哈希和同一预算请求包。
    intent_context = None
    intent_truncated = None
    contract = configuration.get("pr_intent_contract")
    if contract in {"pr-intent-context-v1", "pr-intent-assessment-v1", "pr-intent-assessment-v2",
                    "pr-intent-assessment-v3", "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"}:
        pr = state.get("pr_info") or {}
        statement = "\n\n".join(value.strip() for value in (pr.get("title"), pr.get("body"))
                                 if isinstance(value, str) and value.strip())
        if statement:
            if contract in {"pr-intent-assessment-v2", "pr-intent-assessment-v3", "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"}:
                intent_truncated = len(statement) > 4_000
            intent_context = statement[:3_999] + "…" if len(statement) > 4_000 else statement
    discussion_fields = {}
    if configuration.get("discussion_contract") == "pr-discussion-context-v1":
        from app.llm.model_request_budgeter import PreparedModelRequest
        from app.services.discussions.pr_discussions import select_discussions

        metadata = state.get("pr_discussion") or {}
        if not metadata.get("manifest_hash"):
            raise ValueError("discussion frozen manifest missing")
        context_request = PreparedModelRequest(model=configuration["model"], operation="task_review_v1",
            system="", prompt="", output_tokens=4096)
        profile = ModelRequestProfile.model_validate(configuration["request_profile"])
        selected = select_discussions(state.get("_discussion_items") or [],
            set(readable_files), configuration["model"], profile, context_request)
        discussion_fields = {"discussion_manifest_hash": metadata["manifest_hash"], "discussion_context": selected,
                             "discussion_profile": configuration["request_profile"]}
    return ReviewRunSpec(run_id=state["task_id"], snapshot_head_sha=state["head_sha"],
        snapshot_base_sha=review_diff_base(state), diff_manifest_hash=stable_hash({
            "changed_files": state.get("changed_files") or [], "plan": state.get("review_plan") or {}}),
        model_provider=configuration["provider"], model_name=configuration["model"],
        policy_version=configuration["policy_version"], max_tokens=budget.max_tokens, max_calls=budget.max_calls,
        prompt_version=configuration.get("prompt_version", "task-review-prompt-v1"),
        publication_contract=configuration.get("publication_contract", "legacy"),
        stability_contract=configuration.get("stability_contract"),
        readable_scope_files=tuple(sorted(readable_files)),
        **discussion_fields, pr_intent_context=intent_context,
        pr_intent_context_truncated=intent_truncated,
        # 新 v6 配置允许同文件同侧的相交上下文；历史配置保留原冻结验收规则。
        intent_assessment_contract=(("structured-intent-v5" if contract == "pr-intent-assessment-v6"
            else "structured-intent-v4" if contract == "pr-intent-assessment-v5"
            else "structured-intent-v3" if contract == "pr-intent-assessment-v4"
            else "structured-intent-v2" if contract == "pr-intent-assessment-v3"
            else "structured-intent-v1") if intent_context and contract in {
                "pr-intent-assessment-v1", "pr-intent-assessment-v2", "pr-intent-assessment-v3",
                "pr-intent-assessment-v4", "pr-intent-assessment-v5", "pr-intent-assessment-v6"} else None),
        review_strategy={key: value for key, value in (configuration.get("effective_plan") or {}).items()
                         if key in {"depth", "focus"}})
