"""证据归档与活动工作集选择；替换只改变模型可见性，不删除正文。"""
from __future__ import annotations

from app.models.review import ContextSnippet, UnitEvidenceReference, UnitStoredEvidence
from app.services.review.review_input_context import build_context_evidence, EVIDENCE_INPUT_VERSION


def evidence_snapshot(head, base):
    return {"head_sha": head, "base_sha": base, "input_version": EVIDENCE_INPUT_VERSION}


def put_chunks(store, chunks, request_key=None):
    head, base = store.snapshot["head_sha"], store.snapshot["base_sha"]
    order = max((item.discovered_order for item in store.entries.values()), default=-1) + 1
    observed = []
    for chunk in chunks:
        _, references = build_context_evidence([chunk], head, base)
        reference = UnitEvidenceReference.model_validate({key: value for key, value in references[0].items() if key != "content"})
        identity = reference.id
        observed.append(identity)
        old = store.entries.get(identity)
        snippet = ContextSnippet.model_validate({"relevance": "exploratory", **chunk})
        keys = list(old.request_fingerprints) if old else []
        if request_key and request_key not in keys:
            keys.append(request_key)
        store.entries[identity] = UnitStoredEvidence(reference=reference, snippet=snippet,
            discovered_order=old.discovered_order if old else order, request_fingerprints=keys)
        if old is None:
            order += 1
    if request_key:
        store.request_groups[request_key] = list(dict.fromkeys([*store.request_groups.get(request_key, []), *observed]))


def store_chunks(store):
    return {identity: {**item.snippet.model_dump(mode="json"), "evidence_id": identity,
        "content_hash": item.reference.content_hash, "head_sha": item.reference.head_sha,
        "base_sha": item.reference.base_sha} for identity, item in store.entries.items()}
