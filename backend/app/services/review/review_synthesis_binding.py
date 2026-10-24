"""摘要工件的读取与绑定校验；不发起模型请求，也不依赖发布投影。"""

from pydantic import ValidationError

from app.models.review_synthesis import ReviewSynthesisArtifact
from app.core.fingerprints import stable_hash
from app.services.review.review_synthesis_input import SOURCE_POLICY_VERSION, build_synthesis_input
from app.services.review.review_synthesis_validator import validate_review_synthesis


def bound_synthesis(task, plan):
    if not task.review_synthesis:
        return None
    try:
        artifact = ReviewSynthesisArtifact.model_validate(task.review_synthesis)
        facts = build_synthesis_input(task, plan, source_policy_version=artifact.source_policy_version)
        facts_hash = stable_hash(facts.model_dump(mode="json"))
        legacy_facts = facts.model_dump(mode="json", exclude={"truncation_reasons"})
        legacy_facts["schema_version"] = "review-synthesis-input-v1"
        if (artifact.review_id != task.id or artifact.head_sha != plan.snapshot.head_sha
                or artifact.effective_base_sha != plan.snapshot.effective_base_sha
                or (artifact.fact_bundle_hash != facts_hash and not (
                    artifact.source_policy_version == "review-synthesis-sources-v1"
                    and artifact.fact_bundle_hash == stable_hash(legacy_facts)))):
            return None
        if artifact.fact_bundle_hash != facts_hash:
            # 兼容旧工件哈希；覆盖范围由当前同快照事实包重新派生，不改写冻结工件。
            artifact = artifact.model_copy(update={
                "truncation_reasons": facts.truncation_reasons,
                "omitted_files": facts.omitted_files,
                "omitted_hunks": facts.omitted_hunks,
            })
        elif (artifact.input_truncated != facts.input_truncated
              or artifact.truncation_reasons != facts.truncation_reasons
              or artifact.omitted_files != facts.omitted_files
              or artifact.omitted_hunks != facts.omitted_hunks):
            return None
        if artifact.status == "ready":
            validate_review_synthesis(facts, artifact.synthesis)
        if (artifact.source_policy_version != SOURCE_POLICY_VERSION
                and "diff_hunk_truncated" in facts.truncation_reasons
                and not artifact.input_truncated):
            # 原规则校验通过后仅修正展示提示；冻结记录及其哈希保持原样。
            artifact = artifact.model_copy(update={"input_truncated": True})
        return artifact
    except (ValueError, ValidationError):
        return None
