"""校验摘要结构、来源与白名单；不通过词句匹配推断自然语言语义。"""

from copy import deepcopy
from hashlib import sha256
import unicodedata

from app.models.review_synthesis import (
    ReviewSynthesisDraft, ReviewSynthesisOutput, SynthesisReferenceFailure,
)


OBSERVED_SOURCE_KINDS = frozenset({
    "diff_observation", "verified_observation", "test_source_observation",
})
PURPOSE_SOURCE_KINDS = {
    "author_claim": frozenset({"author_claim"}),
    "code_inferred": OBSERVED_SOURCE_KINDS,
    "unknown": frozenset(),
}


class SynthesisReferenceError(ValueError):
    def __init__(self, diagnostic):
        super().__init__("synthesis_reference_not_allowed")
        self.diagnostic = diagnostic


def synthesis_output_schema(facts):
    """提示词和服务端校验复用同一来源策略，不修改历史输出模型。"""
    schema = ReviewSynthesisOutput.model_json_schema()

    def restrict_refs(definition, kinds):
        refs = definition["properties"]["source_refs"]
        allowed = sorted({source.id for source in facts.sources if source.kind in kinds})
        if allowed:
            refs["items"]["enum"] = allowed
        else:
            refs["maxItems"] = 0

    restrict_refs(schema["$defs"]["CitedText"], OBSERVED_SOURCE_KINDS)
    purpose = schema["$defs"]["SynthesisPurpose"]
    variants = []
    for kind, allowed_kinds in PURPOSE_SOURCE_KINDS.items():
        if kind != "unknown" and not any(source.kind in allowed_kinds for source in facts.sources):
            continue
        variant = deepcopy(purpose)
        variant["properties"]["source_kind"] = {"type": "string", "const": kind}
        variant.setdefault("required", []).append("source_kind")
        restrict_refs(variant, allowed_kinds)
        if kind == "unknown":
            variant["properties"]["text"]["const"] = ""
        variants.append(variant)
    schema["$defs"]["SynthesisPurpose"] = {"title": "SynthesisPurpose", "anyOf": variants}
    return schema


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

    def cited(item, kinds, field_path):
        validate_text(item.text)
        for index, ref in enumerate(item.source_refs):
            source = sources.get(ref)
            if source is None or source.kind not in kinds:
                raise SynthesisReferenceError(SynthesisReferenceFailure(
                    field_path=f"{field_path}.source_refs[{index}]",
                    reason="unknown_source" if source is None else "source_kind_not_allowed",
                    source_ref=source.id if source else None,
                    source_ref_hash=sha256(ref.encode("utf-8", errors="replace")).hexdigest(),
                    actual_kind=source.kind if source else None,
                    allowed_kinds=sorted(kinds),
                ))

    purpose = draft.pr_purpose
    if purpose.source_kind == "unknown":
        if purpose.text or purpose.source_refs:
            raise ValueError("synthesis_unknown_purpose_has_claim")
    else:
        cited(purpose, PURPOSE_SOURCE_KINDS[purpose.source_kind], "pr_purpose")
    if not draft.change_summary.text.strip():
        raise ValueError("synthesis_change_summary_missing")
    cited(draft.change_summary, OBSERVED_SOURCE_KINDS, "change_summary")
    for index, item in enumerate(draft.key_changes):
        cited(item, OBSERVED_SOURCE_KINDS, f"key_changes[{index}]")
    for index, item in enumerate(draft.test_changes):
        cited(item, {"test_source_observation"}, f"test_changes[{index}]")
    # review_summary 仅为旧工件保留读取；新输出不再生成，也不进入交付。
    validate_text(draft.review_summary.text)
    for item in draft.finding_summaries:
        validate_text(item.text)
        # 问题核心主张始终展示权威原文；编辑性摘要不进入 Finding、严重度或去重身份。
    return draft
