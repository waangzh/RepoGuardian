"""审查内容的基准与原始 PR base 身份分别处理。"""


def review_diff_base(state: dict) -> str:
    effective = state.get("effective_base_sha")
    if state.get("diff_policy") == "merge_base" and not effective:
        raise ValueError("merge_base review requires effective_base_sha")
    return effective or state.get("base_sha") or ""
