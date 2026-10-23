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


def synthesis_texts(draft):
    """服务端枚举所有非空编辑字段，检查器不能自行省略字段。"""
    fields = {name: {"text": getattr(draft, name).text,
                     "source_refs": getattr(draft, name).source_refs}
              for name in ("pr_purpose", "change_summary", "review_summary")}
    for name in ("key_changes", "test_changes"):
        for index, item in enumerate(getattr(draft, name, [])):
            fields[f"{name}:{index}"] = {"text": item.text, "source_refs": item.source_refs}
    for item in draft.finding_summaries:
        fields["finding:" + item.finding_key] = {
            "text": item.text, "source_refs": ["finding:" + item.finding_key]}
    return {key: value for key, value in fields.items() if value["text"].strip()}


def validate_synthesis_grounding(facts, draft, value):
    """检查语义评估的完整性与证据绑定；语义判断仍由独立模型完成。"""
    from app.models.review_synthesis import SynthesisGroundingOutput

    result = SynthesisGroundingOutput.model_validate(value)
    fields = synthesis_texts(draft)
    # 下游正文的历史渲染上限为 600；不让已经检查的条件/否定被再次截断。
    if any(len(fields[name]["text"]) > 600 for name in ("change_summary", "review_summary") if name in fields):
        raise ValueError("grounding_text_too_long")
    ids = [item.field_id for item in result.checks]
    if len(ids) != len(set(ids)) or set(ids) != set(fields):
        raise ValueError("grounding_incomplete")
    sources = {source.id: source for source in facts.sources}
    for item in result.checks:
        if item.verdict != "supported":
            raise ValueError("grounding_" + item.verdict)
        if not item.evidence:
            raise ValueError("grounding_evidence_missing")
        for evidence in item.evidence:
            source = sources.get(evidence.source_ref)
            if (source is None or evidence.source_ref not in fields[item.field_id]["source_refs"]
                    or not evidence.quote.strip() or evidence.quote not in source.text):
                raise ValueError("grounding_evidence_mismatch")
        behavior = (item.field_id == "change_summary" or item.field_id.startswith(("key_changes:", "test_changes:"))
                    or (item.field_id == "pr_purpose" and draft.pr_purpose.source_kind == "code_inferred"))
        # 已有模型观察可辅助理解，但不能自己证明自己。
        if behavior and not any(sources[e.source_ref].kind in {
                "diff_observation", "test_source_observation"} for e in item.evidence):
            raise ValueError("grounding_source_insufficient")
        if item.field_id == "review_summary" and not any(
                sources[e.source_ref].kind == "verified_finding" for e in item.evidence):
            raise ValueError("grounding_source_insufficient")
    return result
