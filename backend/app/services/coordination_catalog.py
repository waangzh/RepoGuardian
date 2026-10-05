"""确定性的关系批次：完整目录项、关系两端重叠、显式未覆盖范围。"""

import json
from typing import Any, Callable

from app.models.review import CrossUnitCatalogBatch
from app.services.fingerprints import stable_hash

CATALOG_CHAR_LIMIT = 48_000
CATALOG_BATCH_VERSION = "coordination-catalog-batches-v1"
_PRIORITY = {"changed_contract": 0, "conflicting_contract": 0,
             "unresolved_dependency": 1, "dependency_coverage_gap": 1,
             "unresolved_cross_unit_question": 2}


def catalog_batches(catalog: dict[str, Any],
                    admission: Callable[[dict], None] | None = None) -> list[tuple[CrossUnitCatalogBatch, dict]]:
    units = {item["id"]: item for item in catalog["units"]}
    summaries = {item["unit_id"]: item for item in catalog["summaries"]}
    risk = catalog["risk"]
    relations = {item["id"]: item for item in risk["relationships"]}
    reasons = {"risk-" + stable_hash(item)[:24]: item for item in risk["reasons"]}
    index = [[key, summaries.get(key, {}).get("status", "unknown")] for key in sorted(units)]

    def check(payload):
        if len(json.dumps(payload, ensure_ascii=False)) > CATALOG_CHAR_LIMIT:
            raise ValueError("coordination_catalog_exceeds_48000_chars")
        edges = payload["risk"]["relationships"]
        # skip 必须引用本批每条关系及两端证据；不能生成输出 schema 无法表达的批次。
        if len(edges) > 40 or len({path for edge in edges for path in (
            edge["source_file"], edge["target_file"])}) > 40:
            raise ValueError("coordination_batch_exceeds_proposal_reference_limits")
        if admission is not None:
            admission(payload)

    def record(payload, ids, relation_ids, reason_ids):
        encoded = json.dumps(payload, ensure_ascii=False)
        digest = stable_hash(payload)
        entry = CrossUnitCatalogBatch(id="catalog-" + digest[:24], input_hash=digest,
            input_chars=len(encoded), unit_ids=sorted(ids), relationship_ids=sorted(relation_ids),
            risk_reason_ids=sorted(reason_ids))
        try:
            check(payload)
        except ValueError as exc:
            entry = entry.model_copy(update={"status": "skipped", "reason": str(exc)[:1000]})
        return entry, payload

    full = {**catalog, "coverage_index": index, "batch_scope": {
        "version": CATALOG_BATCH_VERSION, "unit_ids": sorted(units),
        "relationship_ids": sorted(relations), "risk_reason_ids": sorted(reasons)}}
    entry, _ = record(full, units, relations, reasons)
    if entry.status == "pending":
        return [(entry, full)]

    # 连通关系优先同批；过大时按完整关系/风险组拆分，两端摘要不会被截断。
    parents = {key: key for key in units}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    atoms = []
    for key, reason in reasons.items():
        ids = set(reason["unit_ids"]) & units.keys()
        linked = {identity for identity, relation in relations.items()
                  if identity in reason["evidence_ids"]}
        ids.update(endpoint for identity in linked for endpoint in (
            relations[identity]["source_unit_id"], relations[identity]["target_unit_id"]) if endpoint in units)
        atoms.append((_PRIORITY.get(reason["code"], 3), ids, linked, {key}))
    for key, relation in relations.items():
        ids = {relation["source_unit_id"], relation["target_unit_id"]} & units.keys()
        atoms.append((2, ids, {key}, set()))
        endpoints = sorted(ids)
        if len(endpoints) == 2:
            parents[root(endpoints[1])] = root(endpoints[0])
    covered = {key for _, ids, _, _ in atoms for key in ids}
    atoms.extend((4, {key}, set(), set()) for key in sorted(units.keys() - covered))
    if not atoms:
        atoms.append((4, set(units), set(), set()))
    atoms.sort(key=lambda atom: (atom[0], min((root(key) for key in atom[1]), default=""),
                                sorted(atom[1]), sorted(atom[2]), sorted(atom[3])))

    def project(ids, relation_ids, reason_ids):
        selected = [units[key] for key in sorted(ids)]
        selected_summaries = [summaries[key] for key in sorted(ids) if key in summaries]
        paths = {path for item in selected for path in [*item["primary_files"], *item["related_files"]]}
        evidence_ids = {key for item in selected_summaries for key in item["summary"]["evidence_ids"]}
        evidence_ids.update(key for identity in reason_ids for key in reasons[identity]["evidence_ids"])
        return {"coverage_index": index, "units": selected, "summaries": selected_summaries,
            "evidence": [item for item in catalog["evidence"]
                         if item["id"] in evidence_ids and item["file_path"] in paths],
            "risk": {**risk, "relationships": [relations[key] for key in sorted(relation_ids)],
                     "reasons": [reasons[key] for key in sorted(reason_ids)]},
            "batch_scope": {"version": CATALOG_BATCH_VERSION, "unit_ids": sorted(ids),
                "relationship_ids": sorted(relation_ids), "risk_reason_ids": sorted(reason_ids)}}

    batches, pending = [], (set(), set(), set())
    for _, ids, relation_ids, reason_ids in atoms:
        combined = tuple(old | new for old, new in zip(pending, (ids, relation_ids, reason_ids)))
        candidate = record(project(*combined), *combined)
        if candidate[0].status == "pending":
            pending = combined
            continue
        if any(pending):
            batches.append(record(project(*pending), *pending))
        candidate = record(project(ids, relation_ids, reason_ids), ids, relation_ids, reason_ids)
        if candidate[0].status == "skipped":
            batches.append(candidate)
            pending = (set(), set(), set())
        else:
            pending = (ids, relation_ids, reason_ids)
    if any(pending):
        batches.append(record(project(*pending), *pending))
    # 重复关系原子只需检查一次，身份来自最终完整输入。
    return list({entry.id: (entry, payload) for entry, payload in batches}.values())
