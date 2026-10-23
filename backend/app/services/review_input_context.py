"""确定性 PR 背景、权限导航和完整源码证据块。"""

import hashlib
import json
from typing import Any

from app.models.review import UnitEvidenceReference
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


def make_evidence(
    path,
    source,
    start,
    end,
    content,
    head_sha,
    base_sha,
    *,
    derived_from_evidence_id: str | None = None,
):
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = [EVIDENCE_INPUT_VERSION, base_sha, head_sha, path, source, start, end, digest]
    reference = UnitEvidenceReference(
        id="evidence-" + stable_hash(identity)[:24], file_path=path, source=source,
        start_line=max(0, start), end_line=max(0, start, end), head_sha=head_sha,
        base_sha=base_sha, content_hash=digest,
        derived_from_evidence_id=derived_from_evidence_id,
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
        if snippet.get("source") == "repository_tree":
            fact = json.loads(content)
            if fact.get("head_sha") != head_sha or fact.get("scope") != "committed_head_tree_only":
                raise ValueError("repository_tree_snapshot_mismatch")
            ref = make_evidence(str(snippet["file"]), "repository_tree", 0, 0, content, head_sha, base_sha)
            if ref["id"] not in seen:
                seen.add(ref["id"])
                evidence.append(ref)
                chunks.append({**snippet, "start_line": 0, "end_line": 0, "evidence_id": ref["id"],
                    "content_hash": ref["content_hash"], "head_sha": head_sha, "base_sha": base_sha})
            continue
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
            ref = make_evidence(
                str(snippet["file"]),
                "context",
                start + offset,
                start + offset + len(selected) - 1,
                body,
                head_sha,
                base_sha,
                derived_from_evidence_id=snippet.get("derived_from_evidence_id"),
            )
            if ref["id"] not in seen:
                seen.add(ref["id"])
                evidence.append(ref)
                chunks.append({**snippet, "content": body, "start_line": ref["start_line"],
                               "end_line": ref["end_line"], "evidence_id": ref["id"],
                               "content_hash": ref["content_hash"], "head_sha": head_sha,
                               "base_sha": base_sha})
            offset += len(selected)
    return chunks, evidence
