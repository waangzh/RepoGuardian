"""冻结作者目标的确定性分段；原文只是待核验声明。"""

import re

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE = re.compile(r"^\s*\|")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_BOLD_LABEL = re.compile(r"^\s*\*\*[^*]+\*\*\s*[：:]?\s*$")
_SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]?")
# 英文句号不参与中英文通用切分（避免小数、缩写）；仅在句号后接空白与大写/代码时切分。
_EN_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*_(\[])")
_WHITESPACE = re.compile(r"\s+")
# 这些标题通常描述测试、备注或参考信息，不是作者要求实现的目标。
_NON_GOAL_HEADING = re.compile(
    r"^(?:背景(?:信息|说明|与动机)?|动机|上下文|现状(?:与问题)?|问题(?:描述|背景|与现状)?|"
    r"原因(?:分析)?|测试(?:计划|说明|方式|用例)?|验证(?:方式|说明|步骤)?|如何验证|"
    r"备注|注意(?:事项)?|参考(?:资料)?|相关链接|链接|截图|图片|附录|"
    r"其他(?:说明|信息)?|补充(?:说明|信息)?|额外说明|变更背景)"
    r"(?:$|[\s:：\-—（(])",
    re.IGNORECASE,
)
_EN_NON_GOAL_HEADING = re.compile(
    r"^(?:checklist|notes?|testing|tests?|test plan|screenshots?|references?|related|context|motivation)\b",
    re.IGNORECASE,
)


def _is_non_goal_heading(title: str) -> bool:
    return bool(_NON_GOAL_HEADING.match(title) or _EN_NON_GOAL_HEADING.match(title))
_IMAGE_ONLY = re.compile(r"^\s*(?:!\[[^\]]*\]\([^)]*\)|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\))\s*$")


def _blocks(context: str):
    """按空行和 Markdown 结构切块；标题只用于识别非目标章节。"""
    lines = re.sub(r"<!--.*?-->", "", context, flags=re.DOTALL).splitlines()
    in_fence = False
    skip_section = False
    block: list[str] = []
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            if block:
                yield block
                block = []
            continue
        if in_fence:
            continue
        heading = _HEADING.match(line)
        if heading:
            if block:
                yield block
                block = []
            title = heading.group(2).strip().strip("*_ ")
            skip_section = _is_non_goal_heading(title)
            continue
        if skip_section or _TABLE.match(line) or _RULE.match(line) or _IMAGE_ONLY.match(line):
            continue
        if _BOLD_LABEL.match(line):
            continue
        if not line.strip():
            if block:
                yield block
                block = []
            continue
        block.append(line)
    if block:
        yield block


def _split_block(block: list[str]) -> list[str]:
    """项目符号逐条成为目标；普通段落按句末标点切分，保留原文。"""
    items: list[str] = []
    current: list[str] = []
    for line in block:
        bullet = _BULLET.match(line)
        if bullet:
            if current:
                items.append(" ".join(current).strip())
                current = []
            current.append(bullet.group(1).strip())
        else:
            current.append(line.strip())
    if current:
        items.append(" ".join(current).strip())
    targets: list[str] = []
    for item in items:
        for chunk in _SENTENCE.findall(item):
            targets.extend(part.strip() for part in _EN_SENTENCE.split(chunk) if part.strip())
    return targets


def normalized_target(value: str) -> str:
    """Markdown 换行与空白不影响目标引用；其他字符保持原样。"""
    return _WHITESPACE.sub(" ", value).strip()


def matched_author_targets(quote: str | None, targets: list[str]) -> list[str]:
    """确定性解析引用可归属的目标；执行准入与报告投影必须共用这一规则。

    优先级：规范化后精确相等，其次是长度达标的完整子串，最后是相邻目标的
    连续拼接。返回全部匹配的原始目标（按 targets 顺序、按规范化文本去重）；
    多个结果表示引用存在歧义，调用方不能自行挑选其中一个。
    """
    candidate = normalized_target(quote or "")
    if not candidate:
        return []
    normalized = [normalized_target(target) for target in targets]
    exact = [index for index, target in enumerate(normalized) if candidate == target]
    if exact:
        return _distinct_targets(targets, normalized, exact)
    contained = [index for index, target in enumerate(normalized)
                 if len(candidate) >= 24 and candidate in target]
    if contained:
        return _distinct_targets(targets, normalized, contained)
    # 模型也可能原样引用相邻的完整句子（例如连续两句）；只接受连续拼接。
    joined: list[int] = []
    for start in range(len(normalized)):
        parts = [normalized[start]]
        for end in range(start + 1, len(normalized)):
            parts.append(normalized[end])
            # 中文句子之间通常没有空格，英文需要；两种拼接都视为连续原文。
            spaced = normalized_target(" ".join(parts))
            concatenated = "".join(parts)
            if spaced == candidate or concatenated == candidate:
                joined.extend(range(start, end + 1))
                break
            # 后续拼接只会更长；用无分隔长度判断下界，避免误丢中文短句组合。
            if len(concatenated) > len(candidate):
                break
    return _distinct_targets(targets, normalized, joined)


def _distinct_targets(targets: list[str], normalized: list[str], indices: list[int]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for index in sorted(set(indices)):
        if normalized[index] in seen:
            continue
        seen.add(normalized[index])
        result.append(targets[index])
    return result


def matches_author_target(quote: str | None, targets: list[str]) -> bool:
    """模型可引用完整目标或其中的完整句子；歧义引用仍算合法引用。"""
    return bool(matched_author_targets(quote, targets))


def author_targets(context: str | None) -> list[str]:
    if not context:
        return []
    # 保留原文，最多 24 项；长描述尾部合并，不能静默丢失目标。
    parts = [part for block in _blocks(context) for part in _split_block(block)]
    # 空白差异不产生重复目标，避免报告条目与引用解析的身份不一致。
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = normalized_target(part)
        if key not in seen:
            seen.add(key)
            unique.append(part)
    return unique[:23] + (["\n".join(unique[23:])] if len(unique) > 23 else [])
