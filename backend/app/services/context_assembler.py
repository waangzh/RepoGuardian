"""在对象层选择完整可选项；核心输入超限时拒绝，而非截断 JSON。"""

from copy import deepcopy
import hashlib
import json
from typing import Any, Callable

from app.services.model_request_budgeter import ModelContextBudget


class RequiredInputTooLarge(ValueError):
    pass


def assemble_context(payload: dict[str, Any], required: set[str], limit: int, *,
                     priorities: dict[str, int] | None = None,
                     field_limits: dict[str, int] | None = None,
                     item_priority: Callable[[str, Any], tuple] | None = None,
                     context_budget: ModelContextBudget | None = None) -> str:
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
    if context_budget is not None:
        result["input_manifest"]["context_budget"] = context_budget.describe()

    def dump() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def fits() -> bool:
        serialized = dump()
        return len(serialized) <= limit and (context_budget is None or context_budget.fits(serialized))

    if not fits():
        raise RequiredInputTooLarge(
            f"required_input_too_large: limit_chars={limit}, required_chars={len(dump())}, "
            f"context_budget={context_budget.describe() if context_budget else None}"
        )
    if priorities is not None or field_limits is not None or item_priority is not None:
        # 全部类型的完整对象共同排序；目录不能靠字段顺序抢走行动证据。
        candidates = []
        for order, (path, value) in enumerate(optional):
            label = ".".join(path)
            priority = (priorities or {}).get(label, (priorities or {}).get(path[0], 5))
            values = list(enumerate(value)) if isinstance(value, list) else [(None, value)]
            for index, item in values:
                rank = item_priority(label, item) if item_priority else (priority,)
                candidates.append((rank, order, index, path, item))
        candidates.sort(key=lambda candidate: (candidate[0], candidate[1], candidate[2] or 0))
        for _, _, index, path, item in candidates:
            parent = result
            for key in path[:-1]:
                parent = parent.setdefault(key, {})
            label = ".".join(path)
            if index is not None:
                parent.setdefault(path[-1], []).append(deepcopy(item))
            else:
                parent[path[-1]] = deepcopy(item)
            omitted[label] -= 1
            within_fields = True
            for field, field_limit in (field_limits or {}).items():
                node = result
                for key in field.split("."):
                    node = node.get(key, {}) if isinstance(node, dict) else {}
                within_fields = within_fields and len(json.dumps(node, ensure_ascii=False, separators=(",", ":"))) <= field_limit
            if within_fields and fits():
                continue
            omitted[label] += 1
            if index is not None:
                parent[path[-1]].pop()
                if not parent[path[-1]]:
                    parent.pop(path[-1])
            else:
                parent.pop(path[-1], None)
            for depth in range(len(path) - 1, 0, -1):
                ancestor = result
                for key in path[:depth - 1]:
                    ancestor = ancestor[key]
                if ancestor.get(path[depth - 1]) == {}:
                    ancestor.pop(path[depth - 1])
                else:
                    break
        return dump()
    for path, value in optional:
        parent = result
        for key in path[:-1]:
            parent = parent.setdefault(key, {})
        label = ".".join(path)
        total = omitted[label]
        parent[path[-1]] = deepcopy(value)
        omitted[label] = 0
        if fits():
            continue
        if isinstance(value, list):
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                parent[path[-1]] = value[:middle]
                omitted[label] = total - middle
                if fits():
                    low = middle
                else:
                    high = middle - 1
            parent[path[-1]] = deepcopy(value[:low])
            omitted[label] = total - low
        else:
            del parent[path[-1]]
            omitted[label] = total
        # Even an empty optional collection has serialization overhead.
        if not fits():
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
    if not fits():
        raise RequiredInputTooLarge("required_input_too_large: optional_metadata_overflow")
    return serialized
