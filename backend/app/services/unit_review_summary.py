"""确定性 Unit 检查记录输入：证据目录、范围核验与快照绑定。"""

import json
from typing import Any

from app.models.review import ChangedFile
from app.services.review_input_context import build_context_evidence, make_evidence, EVIDENCE_INPUT_VERSION
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL
from app.services.file_change_evidence import file_change_reference, set_file_change_impact_scope


def build_record_input(
    files: list[ChangedFile], context: list[dict[str, Any]],
    plan: Any, head_sha: str, base_sha: str = "",
) -> dict[str, Any]:
    targets = list(dict.fromkeys(
        [*(plan.review_objectives if plan else []), *(plan.coverage_targets if plan else [])]
    )) or [f"审查 {item.file_path} 的变更" for item in files]
    evidence: list[dict[str, Any]] = []

    for item in files:
        for hunk in item.hunks:
            content = json.dumps(
                hunk.model_dump(), ensure_ascii=False, sort_keys=True,
            )
            evidence.append(make_evidence(item.file_path, "diff", hunk.new_start,
                hunk.new_start + max(0, hunk.new_length - 1), content, head_sha, base_sha))
    chunks, context_evidence = build_context_evidence(context, head_sha, base_sha)
    evidence.extend(reference for file in files
                    if (reference := file_change_reference(file, head_sha, base_sha)) is not None)
    evidence.extend(context_evidence)
    result = {
        "targets": targets,
        "input_protocol": CANONICAL_UNIT_INPUT_PROTOCOL,
        "hypotheses": [item.model_dump(mode="json") for item in plan.risk_hypotheses] if plan else [],
        "evidence": evidence,
        "readonly_context": chunks,
        "snapshot": {"head_sha": head_sha, "base_sha": base_sha, "input_version": EVIDENCE_INPUT_VERSION},
        "context_coverage": {"source_snippets": len(context), "included_chunks": len(chunks),
                             "rendered_diff_snippets": sum(item.get("source") == "file_read_diff" for item in context)},
    }
    set_file_change_impact_scope(result)
    return result
