"""拒绝越权字段、未知来源、测试/合并断言和未核验问题；不声称证明自然语言语义。"""

import re
import unicodedata

from app.models.review_synthesis import ReviewSynthesisDraft


_AUTHORITY = re.compile(
    r"\bP[0-3]\b|\b(?:critical|high|medium|low)\s+(?:severity|priority)\b|"
    r"(?:测试|CI|构建|检查).{0,12}(?:通过|成功|失败)|"
    r"(?:tests?|CI|build|checks?).{0,20}(?:pass(?:ed)?|fail(?:ed)?|succeed)|"
    r"(?:安全|可以|允许|建议|应当|禁止|不要|无需).{0,8}合并|"
    r"(?:safe to merge|approve|request.changes|merge decision)|"
    r"(?:全部|所有).{0,8}(?:修复|解决|核验|验证)|"
    r"(?:confirmed|已确认|发现)\s*\d+\s*(?:issues?|findings?|项|个)|"
    r"无(?:缺陷|风险)|没有(?:缺陷|风险)|未发现.{0,8}问题",
    re.I,
)
_UNSAFE = re.compile(r"<|>|https?://|javascript:|data:|@\w|!\[|\]\(|```", re.I)


def validate_review_synthesis(facts, value):
    draft = ReviewSynthesisDraft.model_validate(value)
    if draft.language != facts.language:
        raise ValueError("synthesis_language_mismatch")
    sources = {source.id: source for source in facts.sources}
    keys = [finding.finding_key for finding in draft.finding_summaries]
    if len(set(keys)) != len(keys) or not set(keys) <= set(facts.finding_keys):
        raise ValueError("synthesis_finding_not_allowed")

    def validate_text(text):
        if (_AUTHORITY.search(text) or _UNSAFE.search(text)
                or any(unicodedata.category(char).startswith("C") for char in text)):
            raise ValueError("synthesis_unsafe_assertion")

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
        if re.search(r"found by.*audit|deep.review audit", purpose.text, re.I):
            raise ValueError("synthesis_background_is_not_purpose")
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
