"""只做本地冻结记录校验，不读取仓库、索引或调用规划器。"""
from pydantic import ValidationError

from app.core.config import settings
from app.models.preview_confirmation import PreviewConfirmation
from app.models.review import ReviewPlan, ReviewPreviewResponse
from app.services.fingerprints import stable_hash
from app.services.review_input_context import build_pr_intent
from app.services.review_plan import DEPTHS, configure_runtime, maxima, normalize_plan, preview_hash


def pr_fingerprint(pr):
    return stable_hash({"owner": pr.owner, "repo": pr.repo, "number": pr.number,
        "clone_url": pr.clone_url, "base": pr.base.model_dump(mode="json"),
        "head": pr.head.model_dump(mode="json"), "intent": build_pr_intent(pr)["intent_hash"]})


def request_fingerprint(request):
    value = request.model_dump(mode="json")
    return stable_hash({key: value[key] for key in (
        "engine", "mode", "validation_backend", "validation_profile")})


def policy_fingerprint():
    from app.languages.isolation import parser_fingerprint
    from app.services.review_planner import PLANNER_VERSION
    from app.services.preview_input_cache import INDEX_CACHE_VERSION
    from app.tools.git_tool import DIFF_POLICY_VERSION, DIFF_CONTEXT_LINES
    names = ("repoguardian_config_version", "repoguardian_rule_version", "repoguardian_prompt_version",
        "repoguardian_tool_schema_version", "repoguardian_review_unit_small_max_changed_lines",
        "repoguardian_review_unit_large_min_changed_lines", "repoguardian_review_unit_max_lines_per_read",
        "repoguardian_review_unit_max_search_results", "repoguardian_model_pricing_json",
        "repoguardian_incremental_review_enabled")
    return stable_hash({"settings": {key: getattr(settings, key) for key in names},
        "planner": PLANNER_VERSION, "index": INDEX_CACHE_VERSION, "parser": parser_fingerprint(),
        "diff": DIFF_POLICY_VERSION, "context": DIFF_CONTEXT_LINES, "depths": DEPTHS, "maxima": maxima()})


def configuration_fingerprint(configuration):
    """增量绑定不属于用户方案；冻结与复核都忽略它，避免把同一方案误判为变更。"""
    return stable_hash({key: value for key, value in (configuration or {}).items()
                        if key not in {"incremental", "incremental_units"}})


def freeze_confirmation(pr, configuration, request, response, *, planning_policy):
    return PreviewConfirmation(pr_fingerprint=pr_fingerprint(pr),
        configuration_fingerprint=configuration_fingerprint(configuration), policy_fingerprint=planning_policy,
        response_fingerprint=stable_hash(response.model_dump(mode="json")),
        request_fingerprint=request_fingerprint(request)).model_dump(mode="json")


def validate_confirmation(record, request):
    """返回核验一致的冻结配置及待复核PR身份；不重建旧协议或失效产物。"""
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    try:
        binding = PreviewConfirmation.model_validate(record.get("confirmation"))
    except (ValidationError, TypeError):
        raise ValueError("review_plan_repreview_required") from None
    try:
        response = ReviewPreviewResponse.model_validate(record["response"])
        saved = record["configuration"]
        plan = ReviewPlan.model_validate(saved["initial_plan"])
        model = saved["model"]
        if not isinstance(model, str) or not model or response.snapshot is None or response.effective_plan is None:
            raise ValueError("invalid_saved_plan")
    except (ValidationError, KeyError, TypeError, ValueError):
        raise ValueError("review_plan_stale") from None
    try:
        current = task_runtime_configuration(model)
    except (ValueError, TypeError, KeyError):
        raise ValueError("review_plan_configuration_changed") from None
    if (binding.configuration_fingerprint != configuration_fingerprint(current)
            or binding.policy_fingerprint != policy_fingerprint()):
        raise ValueError("review_plan_configuration_changed")
    try:
        options = normalize_plan(response.effective_plan, model)
    except ValueError:
        raise ValueError("review_plan_configuration_changed") from None
    plan_hash = preview_hash(options, response.snapshot.model_dump(mode="json"), current, plan)
    expected = configure_runtime(current, options, response.snapshot.model_dump(mode="json"), plan_hash)
    expected.update(initial_plan=plan.model_dump(mode="json"), initial_plan_hash=stable_hash(plan.model_dump(mode="json")))
    # 增量绑定在预览时冻结；它不属于用户方案，只在保存配置中存在。
    incremental = {key: saved[key] for key in ("incremental", "incremental_units") if key in saved}
    applied = response.incremental is not None and response.incremental.status == "applied"
    if incremental or applied:
        from app.services.incremental_review import reused_unit_snapshots, validate_binding
        try:
            frozen_binding = validate_binding(saved, head_sha=response.snapshot.head_sha,
                                             effective_base_sha=response.snapshot.effective_base_sha)
            reused_unit_snapshots(saved)
            if (not applied or frozen_binding is None
                    or response.incremental.binding_hash != stable_hash(frozen_binding.model_dump(mode="json"))):
                raise ValueError("incremental_preview_mismatch")
        except (ValueError, TypeError, KeyError):
            raise ValueError("review_plan_stale") from None
    comparable = {key: value for key, value in saved.items()
                  if key not in {"incremental", "incremental_units"}}
    if (binding.response_fingerprint != stable_hash(response.model_dump(mode="json"))
            or binding.request_fingerprint != request_fingerprint(request)
            or plan_hash != response.plan_hash or comparable != expected):
        raise ValueError("review_plan_stale")
    return {**expected, **incremental}, binding.pr_fingerprint
