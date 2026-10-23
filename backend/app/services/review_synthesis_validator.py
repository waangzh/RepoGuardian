"""校验摘要结构、来源与白名单；不通过词句匹配推断自然语言语义。"""

import unicodedata

from app.models.review_synthesis import ReviewSynthesisDraft, ReviewSynthesisOutput


def validate_review_synthesis(facts, value):
    if isinstance(value, ReviewSynthesisOutput):
        value = value.model_dump(mode="json")
    draft = ReviewSynthesisDraft.model_validate(value)
    if draft.language != facts.language:
        raise ValueError("synthesis_language_mismatch")
    sources = {source.id: source for source in facts.sources}
    keys = [finding.finding_key for finding in draft.finding_summaries]
    if len(set(keys)) != len(keys) or not set(keys) <= set(facts.finding_keys):
        raise ValueError("synthesis_finding_not_allowed")

    def validate_text(text):
        if any(unicodedata.category(char).startswith("C") for char in text):
            raise ValueError("synthesis_control_character_not_allowed")

    def cited(item, kinds):
        validate_text(item.text)
        if any(ref not in sources or sources[ref].kind not in kinds for ref in item.source_refs):
            raise ValueError("synthesis_reference_not_allowed")

    observed = {"diff_observation", "verified_observation", "test_source_observation"}
    purpose = draft.pr_purpose
    if purpose.source_kind == "unknown":
        if purpose.text or purpose.source_refs:
            raise ValueError("synthesis_unknown_purpose_has_claim")
    else:
        cited(purpose, {"author_claim"} if purpose.source_kind == "author_claim" else observed)
    if not draft.change_summary.text.strip():
        raise ValueError("synthesis_change_summary_missing")
    cited(draft.change_summary, observed)
    for item in draft.key_changes:
        cited(item, observed)
    for item in draft.test_changes:
        cited(item, {"test_source_observation"})
    cited(draft.review_summary, {"verified_finding", "review_boundary"})
    for item in draft.finding_summaries:
        validate_text(item.text)
        # 问题核心主张始终展示权威原文；编辑性摘要不进入 Finding、严重度或去重身份。
    return draft
