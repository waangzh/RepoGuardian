"""可选补丁验证节点，只调用服务端选择的 ValidationBackend。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import (
    PatchResult,
    PatchStatus,
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ReviewMode,
    ReviewPhase,
    ValidationResult,
    ValidationStatus,
)
from app.services.patch_presentation import build_patch_presentation
from app.validation.selector import ValidationBackendSelector


async def optional_validation_node(state: ReviewState) -> ReviewState:
    """仅在显式验证模式且存在候选 Patch 时调用后端。"""
    if ReviewMode(state.get("mode", ReviewMode.review)) != ReviewMode.review_suggest_and_validate:
        return ReviewState()

    patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    candidates = [patch for patch in patches if patch.status == PatchStatus.unverified]
    if not candidates:
        return ReviewState()

    selector = state.get("_validation_backend_selector") or ValidationBackendSelector()
    requested_backend = state.get("validation_backend") or "none"
    backend = selector.select(
        str(getattr(requested_backend, "value", requested_backend)),
        ReviewMode.review_suggest_and_validate,
    )
    repository = _repository_snapshot(state)
    results = list(state.get("validation_results") or [])
    warnings = list(state.get("warnings") or [])

    try:
        capabilities = await backend.capabilities(repository)
        unavailable_reason = capabilities.unavailable_reason
    except Exception as exc:
        capabilities = None
        unavailable_reason = f"validation capabilities failed: {type(exc).__name__}: {exc}"

    for patch in candidates:
        request = PatchValidationRequest(
            task_id=state.get("task_id") or "unknown-task",
            patch_id=patch.id,
            repository_id=_repository_id(state),
            base_sha=state.get("base_sha") or "unknown-base",
            head_sha=patch.head_sha,
            patch_sha=patch.patch_sha or "missing-patch-sha",
            validation_profile=str(state.get("validation_profile") or "unit"),
            repository_clone_url=_repository_clone_url(state),
            repository_fetch_ref=_repository_fetch_ref(state),
            patch_content=patch.unified_diff,
            is_fork=_repository_is_fork(state),
        )
        try:
            if capabilities is None:
                raise RuntimeError(unavailable_reason)
            raw_result = await backend.validate(request)
            result = PatchValidationResult.model_validate(raw_result)
        except Exception as exc:
            result = PatchValidationResult(
                backend=backend.name,
                status=ValidationStatus.infrastructure_error,
                head_sha=request.head_sha,
                patch_sha=request.patch_sha,
                checks=[],
                resolved_failures=[],
                new_failures=[],
                trusted=False,
            )
            warnings.append(f"验证后端 {backend.name} 调用失败：{type(exc).__name__}: {exc}")

        identifiers_match = (
            result.backend == backend.name
            and result.head_sha == request.head_sha
            and result.patch_sha == request.patch_sha
        )
        if not identifiers_match:
            result = result.model_copy(update={
                "backend": backend.name,
                "head_sha": request.head_sha,
                "patch_sha": request.patch_sha,
                "trusted": False,
            })
        if capabilities is not None and not capabilities.available and result.status in {
            ValidationStatus.passed, ValidationStatus.failed
        }:
            result = result.model_copy(update={
                "status": ValidationStatus.inconclusive,
                "trusted": False,
            })
        if not result.trusted and result.status in {
            ValidationStatus.passed, ValidationStatus.failed
        }:
            result = result.model_copy(
                update={"status": ValidationStatus.inconclusive, "trusted": False}
            )

        _apply_result_to_patch(patch, result)
        patch.validation_backend = result.backend
        patch.validation_result_id = result.id
        patch.presentation = build_patch_presentation(patch)
        results.append(ValidationResult.model_validate({
            **result.model_dump(mode="json"),
            "patch_id": patch.id,
        }).model_dump(mode="json"))

        if result.status not in {ValidationStatus.passed, ValidationStatus.unsupported}:
            warnings.append(f"Patch {patch.id[:8]} 验证结论：{result.status.value}")
        if unavailable_reason and backend.name != "none":
            warnings.append(f"验证后端 {backend.name} 不可用：{unavailable_reason}")

    return ReviewState(
        phase=ReviewPhase.validation,
        status="validating",
        patches=[patch.model_dump(mode="json") for patch in patches],
        validation_results=results,
        warnings=list(dict.fromkeys(warnings)),
        step_progress=append_step(
            state,
            "optional_validation",
            "completed",
            f"验证后端 {backend.name} 已记录 {len(candidates)} 个补丁结果",
        ),
    )


def _apply_result_to_patch(patch: PatchResult, result: PatchValidationResult) -> None:
    if result.status == ValidationStatus.passed and result.trusted:
        patch.status = PatchStatus.verified
    elif result.status == ValidationStatus.failed and result.trusted:
        patch.status = PatchStatus.validation_failed
    elif result.status == ValidationStatus.unsupported:
        patch.status = PatchStatus.unverified
    else:
        patch.status = PatchStatus.validation_inconclusive


def _repository_snapshot(state: ReviewState) -> RepositorySnapshot:
    metadata = state.get("project_meta") or {}
    return RepositorySnapshot(
        language=metadata.get("language", "unknown"),
        framework=metadata.get("framework"),
        test_framework=metadata.get("test_framework"),
        total_files=metadata.get("total_files", len(state.get("file_index") or [])),
    )


def _repository_id(state: ReviewState) -> str:
    pr = state.get("pr_info") or {}
    owner = pr.get("owner")
    repo = pr.get("repo")
    return f"{owner}/{repo}" if owner and repo else str(state.get("pr_url") or "unknown-repository")


def _repository_clone_url(state: ReviewState) -> str:
    pr = state.get("pr_info") or {}
    return str(pr.get("clone_url") or (pr.get("head") or {}).get("repo_clone_url") or "")


def _repository_fetch_ref(state: ReviewState) -> str | None:
    head = (state.get("pr_info") or {}).get("head") or {}
    return str(head.get("ref")) if head.get("ref") else None


def _repository_is_fork(state: ReviewState) -> bool:
    pr = state.get("pr_info") or {}
    base_clone = str(pr.get("clone_url") or "").lower()
    head_clone = str(((pr.get("head") or {}).get("repo_clone_url")) or "").lower()
    return bool(base_clone and head_clone and base_clone != head_clone)
