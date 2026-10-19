"""冻结作者目标的确定性分段；原文只是待核验声明。"""

import re

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE = re.compile(r"^\s*\|")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_BOLD_LABEL = re.compile(r"^\s*\*\*[^*]+\*\*\s*[：:]?\s*$")
_SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]?")
# 这些标题通常描述测试、备注或参考信息，不是作者要求实现的目标。
_NON_GOAL_HEADING = re.compile(
    r"^(?:背景|动机|上下文|测试|测试计划|测试说明|验证|验证方式|如何验证|备注|注意|注意事项|"
    r"参考|参考资料|链接|相关链接|截图|图片|附录|checklist|notes?|testing|tests?|screenshots?|"
    r"references?|related|context|motivation)\b",
    re.IGNORECASE,
)
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
            skip_section = bool(_NON_GOAL_HEADING.match(title))
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
        targets.extend(part.strip() for part in _SENTENCE.findall(item) if part.strip())
    return targets


def author_targets(context: str | None) -> list[str]:
    if not context:
        return []
    # 保留原文，最多 24 项；长描述尾部合并，不能静默丢失目标。
    parts = [part for block in _blocks(context) for part in _split_block(block)]
    return list(dict.fromkeys(parts[:23] + (["\n".join(parts[23:])] if len(parts) > 23 else [])))
