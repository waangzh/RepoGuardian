"""冻结作者目标的确定性分段；原文只是待核验声明。"""

import re


def author_targets(context: str | None) -> list[str]:
    if not context:
        return []
    # 保留原文，最多 24 项；长描述尾部合并，不能静默丢失目标。
    parts = [part.strip() for part in re.split(r"\n+|(?<=[。！？])", context) if part.strip()]
    return list(dict.fromkeys(parts[:23] + (["\n".join(parts[23:])] if len(parts) > 23 else [])))
