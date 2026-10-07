"""证据归档与活动工作集选择；替换只改变模型可见性，不删除正文。"""
from __future__ import annotations


from app.models.review import ContextSnippet, UnitEvidenceReference, UnitEvidenceStore, UnitStoredEvidence
from app.review.tool_scope import is_sensitive_repository_change
from app.services.review_input_context import build_context_evidence, EVIDENCE_INPUT_VERSION


class ContextAdmission(dict):
    """准入事务的临时结果；仅字典元数据进入日志，正文显式写入 graph store。"""
    def __init__(self, metadata, store, active):
        super().__init__(metadata)
        self.store = store
        self.active = active


def evidence_snapshot(head, base):
    return {"head_sha": head, "base_sha": base, "input_version": EVIDENCE_INPUT_VERSION}


def restore_store(value, unit_id, snapshot, readable):
    store = UnitEvidenceStore(review_unit_id=unit_id, snapshot=snapshot)
    if value is None:
        return store
    try:
        old = UnitEvidenceStore.model_validate(value)
    except ValueError:
        store.validation_warnings = ["invalid_evidence_store_schema"]
        return store
    if old.review_unit_id != unit_id or old.snapshot != snapshot:
        store.validation_warnings = ["evidence_store_snapshot_mismatch"]
        return store
    store.revision = old.revision
    store.validation_warnings = list(old.validation_warnings)
    store.request_groups = {key: list(identities) for key, identities in old.request_groups.items()}
    for identity, item in old.entries.items():
        try:
            chunks, references = build_context_evidence([item.snippet.model_dump(mode="json")],
                snapshot["head_sha"], snapshot["base_sha"])
            if (item.snippet.file not in readable or is_sensitive_repository_change(item.snippet.file)
                    or len(chunks) != 1 or references[0]["id"] != identity
                    or item.reference.model_dump(mode="json") != {key: value for key, value in references[0].items() if key != "content"}):
                raise ValueError("stored_evidence_binding_mismatch")
            store.entries[identity] = item.model_copy(deep=True)
        except ValueError:
            store.validation_warnings.append("invalid_stored_evidence:" + identity)
    return store


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


def evidence_priorities(state, store):
    pinned, resolved = set(), set()
    summary = state.get("last_valid_review_summary") or state.get("review_summary") or state.get("parent_state", {}).get("batch_memory_summary")
    record = (summary.record or summary.last_valid_record) if summary else None
    trusted = {item.id: item for item in summary.evidence} if summary else {}
    if record:
        for check in [*record.hypothesis_checks, *record.contract_dependencies, *record.unresolved_questions]:
            status = getattr(check, "status", "unresolved")
            valid = {identity for identity in check.evidence_ids if identity in store.entries
                     and trusted.get(identity) == store.entries[identity].reference}
            (pinned if status in {"unresolved", "conflicting"} else resolved).update(valid)
    ledger = state.get("coverage_ledger")
    if ledger and ledger.snapshot == store.snapshot:
        for check in [*ledger.hypotheses.values(), *ledger.questions.values(), *ledger.dependencies.values()]:
            if check.status in {"pending", "unresolved", "conflicting"}:
                pinned.update(identity for identity in check.evidence_ids if identity in store.entries)
    issues = [*state.get("issues", []), *state.get("pending_issues", []),
              *state.get("parent_state", {}).get("batch_review_issues", [])]
    for issue in issues:
        for anchor in [issue.primary_evidence, *issue.supporting_evidence]:
            if anchor.existing_code:
                pinned.update(identity for identity, item in store.entries.items()
                    if item.snippet.file == anchor.file_path and (anchor.existing_code in item.snippet.content
                        or any(line.strip() and line in item.snippet.content.splitlines() for line in anchor.existing_code.splitlines())))
    unit = state["unit"]
    tiers = {}
    for identity, item in store.entries.items():
        snippet = item.snippet
        if identity in pinned:
            tier = 0
        elif identity in resolved or snippet.relevance in {"exploratory", "resolved"}:
            tier = 3
        elif (snippet.relevance in {"caller", "callee", "test", "contract", "direct_dependency"}
              or snippet.file in unit.primary_files or snippet.symbol in unit.changed_symbols):
            tier = 1
        elif snippet.file in unit.related_files or snippet.relevance in {"direct", "related"}:
            tier = 2
        else:
            tier = 3
        tiers[identity] = tier
    return tiers, pinned


def archived_request_chunks(store, request_key, active_ids):
    chunks = store_chunks(store)
    return [chunks[identity] for identity, item in sorted(store.entries.items(), key=lambda pair: pair[1].discovered_order)
            if identity not in active_ids and request_key in item.request_fingerprints]


def merge_stores(stores):
    result = None
    for value in stores:
        if value is None:
            continue
        if result is None:
            result = value.model_copy(deep=True)
        elif result.review_unit_id == value.review_unit_id and result.snapshot == value.snapshot:
            result.entries.update(value.entries)
            result.revision = max(result.revision, value.revision)
            result.validation_warnings = list(dict.fromkeys([*result.validation_warnings, *value.validation_warnings]))
            result.request_groups.update(value.request_groups)
    return result




def store_catalog(store, active_ids):
    if store is None:
        return {}
    inactive = sorted(((identity, item) for identity, item in store.entries.items() if identity not in active_ids),
                      key=lambda pair: -pair[1].discovered_order)
    return {"stored_count": len(store.entries), "inactive_count": len(inactive), "trust": "metadata_not_citable_evidence",
        "entries": [item.reference.model_dump(mode="json") for _, item in inactive[:12]], "has_more": len(inactive) > 12}
