"""对外摘要的句子边界；原始证据和调查文本保持不变。"""

import re


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
