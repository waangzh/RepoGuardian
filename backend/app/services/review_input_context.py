"""确定性 PR 背景、完整源码块和可恢复的 Unit 工作记忆。"""

import hashlib
import json
from typing import Any

from app.models.review import UnitEvidenceReference, UnitReviewSummary
from app.services.context_assembler import assemble_context
from app.services.fingerprints import stable_hash

EVIDENCE_INPUT_VERSION = "unit-evidence-chunks-v3"
PR_INTENT_VERSION = "pr-author-intent-v1"
SCOPE_PROJECTION_VERSION = "unit-scope-navigation-v1"


def build_scope_projection(scope: Any) -> dict[str, Any]:
    """有界导航视图；服务端仍持有完整权限集合，视图不授予任何权限。"""
    raw = scope.model_dump(mode="python") if hasattr(scope, "model_dump") else (scope or {})
    commentable = sorted(set(raw.get("commentable_files") or []))
    seeds = sorted(set(raw.get("seed_files") or commentable))
    readable = sorted(set(raw.get("readable_files") or []))
    limits = {key: raw.get(key) for key in ("max_lines_per_read", "max_search_results", "max_context_chars")}
    projection = {
        "schema_version": SCOPE_PROJECTION_VERSION, "review_unit_id": raw.get("review_unit_id"),
        "commentable_files": commentable, "seed_files": seeds,
        "commentable_files_count": len(commentable), "seed_files_count": len(seeds),
        "readable_files_count": len(readable), "readable_files_hash": stable_hash(readable),
        "repository_discovery_enabled": bool(raw.get("repository_discovery_enabled", False)),
        "limits": limits, "navigation_is_exhaustive": False,
    }
    projection["id"] = "scope-" + stable_hash([projection, raw.get("repository_root")])[:24]
    return projection


def question_identity(question: Any) -> str:
    raw = question.model_dump(mode="json") if hasattr(question, "model_dump") else question
    return raw.get("id") or "question-" + stable_hash([
        raw["question"], sorted(set(raw.get("affected_files") or []))])[:24]


def build_retrieval_catalog(state: dict) -> dict:
    """按当前目标/已检索文件排序导航目录；目录只提供发现线索。"""
    unit, plan = state.get("review_unit") or {}, state.get("unit_plan") or {}
    primary = set(unit.get("primary_files") or [])
    primary.update(item.get("file_path") for item in state.get("changed_files") or [])
    relevant = set(unit.get("related_files") or [])
    relevant.update(item.get("file") for item in state.get("context_snippets") or [])
    symbols = set(unit.get("changed_symbols") or [])
    symbols.update(item.get("symbol") for item in state.get("context_snippets") or [] if item.get("symbol"))
    for hypothesis in plan.get("risk_hypotheses") or []:
        relevant.update(hypothesis.get("affected_files") or [])
        symbols.update(hypothesis.get("affected_symbols") or [])
    for history in (state.get("retrieval_history") or [])[-2:]:
        retrieval = history.get("plan") or {}
        if not isinstance(retrieval, dict):
            # 低层只读动作保存的是请求 fingerprint；并非 ContextRetrievalPlan。
            continue
        relevant.update(retrieval.get("target_files") or [])
        symbols.update(retrieval.get("target_symbols") or [])

    def rank(path):
        return 0 if path in relevant else 1 if path in primary else 2

    files = sorted({item["path"] for item in state.get("file_index") or [] if item.get("path")},
                   key=lambda path: (rank(path), path))
    entries = [{key: item.get(key) for key in ("file", "symbol", "type")}
               for item in state.get("symbol_index") or []]
    entries.sort(key=lambda item: (0 if item["symbol"] in symbols else 1,
                                  rank(item["file"]), str(item["file"]), str(item["symbol"])))
    return {"files": files, "symbols": entries}


def build_pr_intent(pr: Any) -> dict[str, Any]:
    raw = pr.model_dump(mode="json") if hasattr(pr, "model_dump") else (pr or {})
    title = str(raw.get("title") or "").replace("\r\n", "\n").replace("\r", "\n")
    body = str(raw.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
    limit = 6_000
    prefix, suffix = (body, "") if len(body) <= limit else (body[:4_000], body[-2_000:])
    return {
        "schema_version": PR_INTENT_VERSION, "source": "pr_author", "trust": "unverified",
        "pr_id": f"{raw.get('owner', '')}/{raw.get('repo', '')}#{raw.get('number', '')}",
        "title": title[:1_000], "title_omitted_chars": max(0, len(title) - 1_000),
        "body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "intent_hash": stable_hash([PR_INTENT_VERSION, title, body]),
        "body_excerpt": prefix, "body_tail": suffix, "body_chars": len(body),
        "body_excerpt_range": [0, len(prefix)],
        "body_tail_range": [len(body) - len(suffix), len(body)],
        "omitted_chars": len(body) - len(prefix) - len(suffix), "truncated": bool(suffix),
    }


def complete_line_prefix(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    selected, size = [], 0
    for line in content.splitlines(keepends=True):
        if size + len(line) > max(0, limit):
            break
        selected.append(line)
        size += len(line)
    return "".join(selected)


def make_evidence(path, source, start, end, content, head_sha, base_sha):
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = [EVIDENCE_INPUT_VERSION, base_sha, head_sha, path, source, start, end, digest]
    reference = UnitEvidenceReference(
        id="evidence-" + stable_hash(identity)[:24], file_path=path, source=source,
        start_line=max(0, start), end_line=max(0, start, end), head_sha=head_sha,
        base_sha=base_sha, content_hash=digest,
    )
    return {**reference.model_dump(mode="json"), "content": content}


def build_context_evidence(context, head_sha, base_sha):
    chunks, evidence, seen = [], [], set()
    for snippet in context:
        # Rendered diff has headers, gaps and two sides; canonical hunks already
        # supply it. Never label that representation as contiguous Head source.
        if snippet.get("source") == "file_read_diff":
            continue
        content = str(snippet.get("content") or "")
        if not content:
            continue
        if content.endswith("\n...(truncated)"):
            raise ValueError("legacy_truncated_context_requires_reread")
        start = int(snippet.get("start_line") or 1)
        if start < 1:
            raise ValueError("context_source_line_must_be_positive")
        lines = content.splitlines(keepends=True)
        end = start + len(lines) - 1
        if snippet.get("end_line") is not None and end > int(snippet["end_line"]):
            raise ValueError("context_source_exceeds_declared_range")
        offset = 0
        while offset < len(lines):
            selected, size = [], 0
            while offset + len(selected) < len(lines) and len(selected) < 80:
                line = lines[offset + len(selected)]
                if selected and size + len(line) > 4_000:
                    break
                selected.append(line)
                size += len(line)
            body = "".join(selected)
            ref = make_evidence(str(snippet["file"]), "context", start + offset,
                                start + offset + len(selected) - 1, body, head_sha, base_sha)
            if ref["id"] not in seen:
                seen.add(ref["id"])
                evidence.append(ref)
                chunks.append({**snippet, "content": body, "start_line": ref["start_line"],
                               "end_line": ref["end_line"], "evidence_id": ref["id"],
                               "content_hash": ref["content_hash"], "head_sha": head_sha,
                               "base_sha": base_sha})
            offset += len(selected)
    return chunks, evidence


def build_working_memory(summary: UnitReviewSummary | None, record_input: dict,
                         unit_id: str, latest_attempt: dict | None = None) -> tuple[dict, list]:
    current = {item["id"]: item for item in record_input["evidence"]}
    record = (summary.record or summary.last_valid_record or (
        summary.record_history[-1] if summary.record_history and not summary.latest_attempt_snapshot else None
    )) if summary else None
    invalidated = bool(summary and summary.last_valid_snapshot and summary.last_valid_snapshot != input_snapshot(record_input))
    stale_record = record if invalidated else None
    if invalidated:
        record = None
    trusted = {item.id: item.model_dump(mode="json") for item in summary.evidence} if summary else {}
    missing = set()
    if stale_record:
        missing.update(identity for key in ("target_checks", "hypothesis_checks", "contract_dependencies", "unresolved_questions")
                       for check in getattr(stale_record, key) for identity in check.evidence_ids)
    payload = {
        "schema_version": "unit-working-memory-v2", "unit_id": unit_id,
        "snapshot": record_input.get("snapshot") or {},
        "revision": stable_hash(record.model_dump(mode="json") if record else None),
        "trust": "prior_checks_not_correctness_proof",
        "memory_invalidated": invalidated,
        "latest_attempt": latest_attempt or ({"status": summary.latest_attempt_status or summary.status,
                                              "reason": summary.latest_attempt_reason or summary.reason}
                                             if summary else {"status": "not_executed"}),
    }
    if record is not None:
        for key in ("unresolved_questions", "hypothesis_checks", "contract_dependencies", "target_checks"):
            items = []
            for check in getattr(record, key):
                item = check.model_dump(mode="json")
                if key == "unresolved_questions":
                    item["id"] = question_identity(item)
                unavailable = [identity for identity in item["evidence_ids"] if identity not in current
                               or trusted.get(identity) != {
                                   k: v for k, v in current[identity].items() if k != "content"}]
                missing.update(unavailable)
                if unavailable:
                    item["evidence_ids"] = [i for i in item["evidence_ids"] if i not in unavailable]
                    if "status" in item:
                        item["status"] = "unresolved"
                        item["reason" if "reason" in item else "assumption"] = "历史证据不能在当前快照恢复"
                items.append(item)
            payload[key] = items
    payload["missing_evidence_ids"] = sorted(missing)
    required = {"schema_version", "unit_id", "snapshot", "revision", "trust", "latest_attempt", "memory_invalidated"}
    def rank(label, item):
        if not isinstance(item, dict):
            return (6, 0, 0)
        status = item.get("status", "unresolved")
        tier = {"conflicting": 0, "refuted": 1, "supported": 2, "verified": 2,
                "unresolved": 3, "not_checked": 4, "checked": 5}.get(status, 3)
        goal = (item.get("target") in record_input.get("targets", [])
                or item.get("hypothesis_id") in {h["id"] for h in record_input.get("hypotheses", [])})
        cost = len(json.dumps(item, ensure_ascii=False)) + sum(
            len(current[identity].get("content", "")) for identity in item.get("evidence_ids", []))
        return (tier, -int(goal), cost)

    memory = json.loads(assemble_context(payload, required, 8_000, item_priority=rank))
    references = set()
    for key in ("unresolved_questions", "hypothesis_checks", "contract_dependencies", "target_checks"):
        for item in memory.get(key, []):
            references.update(item["evidence_ids"])
    restored = [current[identity] for identity in sorted(references)]
    return memory, restored


def input_snapshot(record_input: dict) -> dict[str, str]:
    snapshot = dict(record_input.get("snapshot") or {})
    snapshot["pr_intent_hash"] = str((record_input.get("pr_intent") or {}).get("intent_hash") or "")
    snapshot["provider_input_protocol"] = str(record_input.get("input_protocol") or "canonical-evidence-v3")
    snapshot["review_scope_hash"] = stable_hash([record_input.get("targets") or [], record_input.get("hypotheses") or []])
    return snapshot
