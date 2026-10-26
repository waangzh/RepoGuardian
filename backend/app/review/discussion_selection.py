"""讨论上下文容量裁剪：只保留能装进真实请求的评论，不裁剪源证据。"""

import json

from app.llm.model_request_budgeter import ModelContextBudget, estimate_tokens


def select_discussions(items, paths, model, profile, base_request):
    """Fit against the actual request; never trim source evidence to admit a comment."""
    selected, serialized = [], []
    capacity = ModelContextBudget(base_request, profile)
    tokens = max(0, capacity.input_limit // 10)

    def serialize(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def escaped(text):
        # 与 envelope 中 prompt 的 JSON 转义一致；字节计数口径下可逐项相加。
        return json.dumps(text, ensure_ascii=False)[1:-1]

    def fallback(item):
        from copy import deepcopy
        item = deepcopy(item)
        item["summary_omitted"] = item.get("summary") is not None
        item["summary"] = None
        for comment in item["comments"]:
            comment["body"] = comment["body"][:128]
            comment["omitted_chars"] = comment["total_chars"] - len(comment["body"])
        item["omitted_comment_count"] = len(item.get("omitted_comment_ids", []))
        item["omitted_comment_ids"] = []
        return item

    # 每项只序列化/分词一次，累计 token 由增量相加得出；不做整表重复编码。
    # 初始 1 与每项 +1 分别计入 "[" 与逗号，使字节计数口径下与整表编码一致。
    # 分词器会合并相邻文本的 token 边界，累加值可能有几个 token 的偏差；
    # 只有落在阈值附近时才整表复核，超限较多时直接按累加值处理，保持线性。
    limit = capacity.input_limit
    fixed_tokens = estimate_tokens(base_request.envelope(), model)[0]
    wrapper_tokens = estimate_tokens(escaped('\n{"pr_discussions": []}'), model)[0]
    selected_tokens = 1
    envelope_tokens = 0
    slack = 8
    for item in sorted(items, key=lambda value: (value.get("path") not in paths, value["id"])):
        if item.get("path") and item["path"] not in paths:
            continue
        text = serialize(item)
        item_tokens = selected_tokens + estimate_tokens(text, model)[0] + 1
        if item_tokens > tokens:
            if item_tokens <= tokens + slack:
                item_tokens = estimate_tokens(json.dumps([*selected, item], ensure_ascii=False,
                                                         separators=(",", ":")), model)[0]
            if item_tokens > tokens:
                item = fallback(item)
                text = serialize(item)
                item_tokens = selected_tokens + estimate_tokens(text, model)[0] + 1
                if item_tokens <= tokens + slack:
                    item_tokens = estimate_tokens(json.dumps([*selected, item], ensure_ascii=False,
                                                             separators=(",", ":")), model)[0]
                if item_tokens > tokens:
                    continue
        # 与 capacity.fits 相同的请求容量判断；只在超限时跳过，不改变降级语义。
        item_envelope = fixed_tokens + wrapper_tokens + envelope_tokens + \
            estimate_tokens(escaped(text), model)[0] + (2 if selected else 0)
        if item_envelope > limit:
            serialized_input = "\n" + json.dumps({"pr_discussions": [*selected, item]}, ensure_ascii=False)
            if not capacity.fits(serialized_input):
                continue
            item_envelope = estimate_tokens(escaped(serialized_input), model)[0]
        selected.append(item)
        serialized.append(text)
        selected_tokens = item_tokens
        envelope_tokens = item_envelope - fixed_tokens - wrapper_tokens
    # 最终仍以真实请求 envelope 兜底；分词边界导致估算偏差时从末尾回退。
    while selected:
        serialized_input = "\n" + json.dumps({"pr_discussions": selected}, ensure_ascii=False)
        if capacity.fits(serialized_input):
            break
        selected.pop()
        serialized.pop()
    return selected
