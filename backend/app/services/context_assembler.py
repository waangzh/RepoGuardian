"""在对象层选择完整可选项；核心输入超限时拒绝，而非截断 JSON。"""

from copy import deepcopy
import hashlib
import json
from typing import Any


class RequiredInputTooLarge(ValueError):
    pass


def assemble_context(payload: dict[str, Any], required: set[str], limit: int) -> str:
    result = {key: deepcopy(value) for key, value in payload.items() if key in required}
    optional: list[tuple[tuple[str, ...], Any]] = []

    def leaves(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict) and value:
            for key, child in value.items():
                leaves(child, (*path, key))
        else:
            optional.append((path, value))

    for key, value in payload.items():
        if key not in required:
            leaves(value, (key,))
    omitted = {".".join(path): len(value) if isinstance(value, list) else 1
               for path, value in optional}
    diff = str(result.get("unit_diff") or "")
    result["input_manifest"] = {
        "version": "structured-unit-input-v1", "required_fields": sorted(required),
        "required_coverage": "complete", "unit_diff_chars": len(diff),
        "unit_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "omitted": omitted,
    }

    def dump() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    if len(dump()) > limit:
        raise RequiredInputTooLarge(
            f"required_input_too_large: limit_chars={limit}, required_chars={len(dump())}"
        )
    for path, value in optional:
        parent = result
        for key in path[:-1]:
            parent = parent.setdefault(key, {})
        label = ".".join(path)
        total = omitted[label]
        parent[path[-1]] = deepcopy(value)
        omitted[label] = 0
        if len(dump()) <= limit:
            continue
        if isinstance(value, list):
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                parent[path[-1]] = value[:middle]
                omitted[label] = total - middle
                if len(dump()) <= limit:
                    low = middle
                else:
                    high = middle - 1
            parent[path[-1]] = deepcopy(value[:low])
            omitted[label] = total - low
        else:
            del parent[path[-1]]
            omitted[label] = total
        # Even an empty optional collection has serialization overhead.
        if len(dump()) > limit:
            parent.pop(path[-1], None)
        # Remove newly-created empty containers too, so optional metadata cannot
        # make a fitting required object fail admission at the exact boundary.
        for depth in range(len(path) - 1, 0, -1):
            ancestor = result
            for key in path[:depth - 1]:
                ancestor = ancestor[key]
            if ancestor.get(path[depth - 1]) == {}:
                del ancestor[path[depth - 1]]
            else:
                break
    serialized = dump()
    if len(serialized) > limit:
        raise RequiredInputTooLarge("required_input_too_large: optional_metadata_overflow")
    return serialized
