"""Freeze current user choices and bounded native agent limits."""

from app.core.config import settings
from app.models.review_plan import ReviewPlanOptions
from app.review.contracts import ReviewConfiguration
from app.review.models import ReviewLimits

DEPTHS = {"quick": ("快速初筛", 12), "standard": ("标准审查", 24), "deep": ("深度核验", 48)}


def maxima():
    return {
        "model_calls": settings.repoguardian_agent_max_calls,
        "tool_calls": settings.repoguardian_agent_max_tool_calls,
        "concurrency": settings.repoguardian_review_concurrency,
        "timeout_seconds": int(settings.repoguardian_review_timeout_seconds),
        "output_tokens": settings.repoguardian_agent_output_tokens,
        "total_model_calls": settings.repoguardian_review_max_model_calls,
        "total_input_tokens": settings.repoguardian_review_max_input_tokens,
    }


def default_limits(depth):
    return {**maxima(), "model_calls": min(DEPTHS[depth][1], settings.repoguardian_agent_max_calls)}


def capabilities():
    return {
        "depth_presets": [{"id": key, "label": value[0], "limits": default_limits(key)}
                          for key, value in DEPTHS.items()],
        "focus_categories": ["correctness", "security", "performance", "compatibility"],
        "scope_selection_supported": True,
        "context_boundary": "固定提交中的普通文件；拒绝敏感路径和符号链接逃逸",
        "limits": {"defaults": default_limits("standard"), "maxima": maxima()},
        "pr_discussions_available": settings.repoguardian_pr_discussions_enabled,
        "pr_discussions_default": settings.repoguardian_pr_discussions_enabled,
    }


def freeze_configuration(request, provider):
    options = request.plan
    include = options.include_pr_discussions
    if include is True and not settings.repoguardian_pr_discussions_enabled:
        raise ValueError("discussion_disabled")
    if include is None:
        include = settings.repoguardian_pr_discussions_enabled
    limits = default_limits(options.depth)
    for key, value in options.limits.model_dump().items():
        if value is not None:
            if value > maxima()[key]:
                raise ValueError("review_limit_exceeds_server_policy")
            limits[key] = value
    limits = ReviewLimits.model_validate(limits)
    options = ReviewPlanOptions.model_validate({
        **options.model_dump(), "include_pr_discussions": include,
        "limits": limits.model_dump(),
    })
    return ReviewConfiguration(
        model=request.model or provider._default_model, provider=provider._provider_name,
        endpoint=provider._base_url, options=options, limits=limits,
    )
