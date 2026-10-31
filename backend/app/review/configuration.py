"""Freeze user options within server resource policy, without role quotas."""

from app.core.config import settings
from app.review.domain import Configuration, Limits, Options

DEPTHS = {"quick": "快速初筛", "standard": "标准审查", "deep": "深度核验"}


def maxima():
    return {
        "model_calls": settings.repoguardian_agent_max_calls,
        "tool_calls": settings.repoguardian_agent_max_tool_calls,
        "concurrency": settings.repoguardian_review_concurrency,
        "timeout_seconds": int(settings.repoguardian_review_timeout_seconds),
        "output_tokens": settings.repoguardian_agent_output_tokens,
        "total_model_calls": settings.repoguardian_review_max_model_calls,
        "total_input_tokens": settings.repoguardian_review_max_input_tokens,
        "total_output_tokens": settings.repoguardian_review_max_output_tokens,
        "context_tokens": settings.repoguardian_model_context_tokens,
    }


def freeze_configuration(request, provider):
    if request.depth not in DEPTHS:
        raise ValueError("unknown_review_depth")
    if request.include_pr_discussions and not settings.repoguardian_pr_discussions_enabled:
        raise ValueError("discussion_disabled")
    maximum = maxima()
    if any(key not in maximum or (maximum[key] is not None and value > maximum[key])
           for key, value in request.limits.items()):
        raise ValueError("review_limit_exceeds_server_policy")
    limits = Limits.model_validate({**maximum, **request.limits})
    options = Options(depth=request.depth, paths=request.paths, focus=request.focus, limits=limits,
        include_pr_discussions=(settings.repoguardian_pr_discussions_enabled
            if request.include_pr_discussions is None else request.include_pr_discussions))
    return Configuration(provider=provider._provider_name, endpoint=provider._base_url,
                         model=request.model or provider._default_model, options=options)


def capabilities():
    return {
        "depth_presets": [{"id": key, "label": value, "limits": maxima()}
                          for key, value in DEPTHS.items()],
        "focus_categories": ["correctness", "security", "performance", "compatibility"],
        "scope_selection_supported": True,
        "context_boundary": "固定提交的普通源码；不会执行目标仓库代码",
        "limits": {"defaults": maxima(), "maxima": maxima()},
        "pr_discussions_available": settings.repoguardian_pr_discussions_enabled,
        "pr_discussions_default": settings.repoguardian_pr_discussions_enabled,
    }
