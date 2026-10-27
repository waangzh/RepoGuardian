"""对外摘要的句子边界；原始证据和调查文本保持不变。"""

import re

# 审查工具来源与审计说明不是作者目标；概览、报告与评论共用同一判定。
_AUDIT_PURPOSE = re.compile(r"found by.*audit|deep.review audit|审计发现", re.I)
_NO_AUTHOR_GOAL = "未记录明确的作者目标。"


def author_goal_text(value: str | None) -> str:
    """返回可展示的作者目标；哨兵与审计来源文本返回空串。"""
    paragraphs = re.split(r"\n\s*\n", value or "")
    kept = []
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?。！？])\s+", paragraph.strip())
        kept.extend(sentence for sentence in sentences if not _AUDIT_PURPOSE.search(sentence))
    text = " ".join(" ".join(kept).split())
    return "" if not text or text == _NO_AUTHOR_GOAL else text


def concise_text(value: str | None, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    sentence = re.split(r"(?<=[。！？!?])|(?<=\.)\s+", text, maxsplit=1)[0]
    if len(sentence) > limit:
        boundaries = list(re.finditer(r"[\s，,；;：:]+", sentence[:limit]))
        if not boundaries:
            return "内容较长，详见详细报告。"
        sentence = sentence[:boundaries[-1].start()]
        # 不留下尚未闭合的行内代码或括号，完整内容仍在报告中。
        if sentence.count("`") % 2 or any(sentence.count(left) != sentence.count(right)
                for left, right in (("(", ")"), ("[", "]"), ("（", "）"))):
            return "内容较长，详见详细报告。"
    return sentence.rstrip("。；; .") + "。"
