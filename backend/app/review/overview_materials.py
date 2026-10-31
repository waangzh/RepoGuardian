"""Change-first, bounded editorial materials over the frozen plan."""

from collections import deque

from app.review.domain import OverviewOmission, SourceRef, utcnow
from app.review.evidence import source_lines
from app.review.planner import members, tokens
from app.review.store import BudgetExceeded

MATERIAL_TOKENS = 4500
_HEADROOM = 128


def _active(provider):
    provider.store.assert_active(provider.run.id, provider.lease)
    if utcnow() >= provider.run.hard_deadline:
        raise BudgetExceeded("hard_deadline")


def _ranges(member):
    for side, key, path in (("base", "old_line_no", member["old_path"]),
                            ("head", "new_line_no", member["path"])):
        numbers = [line[key] for line in member["lines"] if line[key] is not None]
        if numbers:
            yield side, key, path, numbers[0], numbers[-1]


def _change(provider, unit_id, member):
    _active(provider)
    sources = []
    for side, key, path, start, end in _ranges(member):
        _active(provider)
        result = provider.evidence._read(path, start, end, side)
        actual = source_lines(result["content"])
        for line in member["lines"]:
            if line[key] is not None and actual[line[key] - start].rstrip("\n") != line["content"]:
                raise ValueError("diff_source_mismatch")
        sources.append({"source": result["evidence"], "content": result["content"]})
    change = {
        "unit_id": unit_id, "member_id": member["id"],
        "path": member["path"], "old_path": member["old_path"],
        "ranges": {side: [start, end] for side, _, _, start, end in _ranges(member)},
        "patch": "\n".join({"added": "+", "deleted": "-", "context": " "}[line["kind"]]
                          + line["content"] for line in member["lines"]),
    }
    if not member["lines"]:
        change.update(metadata_only=True, change_type=member["change_type"],
                      old_mode=member["old_mode"], new_mode=member["new_mode"])
    return change, sources


def _with_change(payload, change, sources):
    evidence = list(payload["source_materials"])
    handles = {item["source"]["id"]: item["source_ref"] for item in evidence}
    cited = {}
    for item in sources:
        ref = item["source"]
        if ref["id"] not in handles:
            handle = f"S{len(evidence) + 1}"
            handles[ref["id"]] = handle
            evidence.append({**item, "source_ref": handle})
        cited[ref["side"]] = handles[ref["id"]]
    return {**payload, "changes": [*payload["changes"], {**change, "source_refs": cited}],
            "source_materials": evidence}


def overview_material(report, provider, *, max_tokens=MATERIAL_TOKENS):
    """Return bounded model input and full local omission diagnostics separately."""
    max_tokens = min(max_tokens, MATERIAL_TOKENS)
    payload = {
        "snapshot": provider.run.snapshot.model_dump(), "changes": [], "source_materials": [],
        "findings": [], "decision_boundaries": [],
        "target_characters": min(900, max(180, len(provider.run.plan.units) * 120)),
        "omitted": {"changes": 0, "findings": 0, "decision_boundaries": 0, "sources": 0},
    }
    omissions = []
    all_members = members(provider.run.changed_files)

    def omit(unit_id, member, reason):
        payload["omitted"]["changes"] += 1
        ranges = list(_ranges(member))
        payload["omitted"]["sources"] += len(ranges)
        for side, _, path, start, end in ranges:
            omissions.append(OverviewOmission(kind="change", unit_id=unit_id, member_id=member["id"],
                path=path, side=side, start_line=start, end_line=end, reason=reason))
        if not ranges:
            omissions.append(OverviewOmission(kind="change", unit_id=unit_id, member_id=member["id"],
                                             path=member["path"], reason=reason))

    # Leave a small allowance for final decisions without letting them displace
    # change evidence. Omission records themselves never consume model context.
    decisions = [
        ("findings", {"id": finding.id, "author_comment": finding.author_comment[:800]})
        for finding in report.findings
    ] + [
        ("decision_boundaries", {"claim": candidate.proposal.claim[:300],
          "verdict": candidate.verification.verdict if candidate.verification else "unverified"})
        for candidate in report.candidates if candidate.finding is None
    ]
    decision_allowance = min(400, tokens(decisions)) if decisions else 0
    core_limit = max_tokens - _HEADROOM - decision_allowance
    fair_share = max(0, (core_limit - tokens(payload)) // max(1, len(provider.run.plan.units)))
    pending = []
    for unit in provider.run.plan.units:
        deferred, spent = deque(), 0
        for key in unit.required_hunk_ids:
            member = all_members[key]
            try:
                change, sources = _change(provider, unit.id, member)
            except (ValueError, OSError) as exc:
                reason = "diff_source_mismatch" if str(exc) == "diff_source_mismatch" else "source_unavailable"
                omit(unit.id, member, reason)
                continue
            trial = _with_change(payload, change, sources)
            cost = max(0, tokens(trial) - tokens(payload))
            if spent + cost <= fair_share and tokens(trial) <= core_limit:
                payload, spent = trial, spent + cost
            else:
                deferred.append((member, change, sources))
        pending.append((unit.id, deferred))
    # Redistribute unused shares one member per unit at a time.
    while any(queue for _, queue in pending):
        for unit_id, queue in pending:
            if not queue:
                continue
            member, change, sources = queue.popleft()
            trial = _with_change(payload, change, sources)
            if tokens(trial) <= core_limit:
                payload = trial
            else:
                omit(unit_id, member, "material_capacity")
    for section, item in decisions:
        trial = {**payload, section: [*payload[section], item]}
        if tokens(trial) <= max_tokens - _HEADROOM:
            payload = trial
        else:
            payload["omitted"][section] += 1

    # Only adjacent, non-overlapping context can add information here. Broad
    # exploratory reads and unrelated callers must not replace the actual diff.
    selected = [SourceRef.model_validate(item["source"]) for item in payload["source_materials"]]
    selected_ids = {ref.id for ref in selected}
    source_ids = {key for work in report.units for key in work.visible_source_ids} - selected_ids
    background = []
    for key in source_ids:
        _active(provider)
        try:
            ref, content = provider.evidence.read_source(key)
        except (ValueError, OSError):
            continue
        related = [old for old in selected if (old.side, old.path) == (ref.side, ref.path)]
        overlaps = any(ref.start_line <= old.end_line and old.start_line <= ref.end_line for old in related)
        adjacent = any(ref.end_line + 1 == old.start_line or old.end_line + 1 == ref.start_line for old in related)
        if adjacent and not overlaps:
            background.append((ref, content))
    for ref, content in sorted(background, key=lambda item: (
            item[0].path, item[0].side, item[0].start_line, item[0].end_line)):
        if any((ref.path, ref.side) == (old.path, old.side)
               and ref.start_line <= old.end_line and old.start_line <= ref.end_line for old in selected):
            continue
        item = {"source_ref": f"S{len(selected) + 1}", "source": ref.model_dump(), "content": content,
                "background_only": True}
        trial = {**payload, "source_materials": [*payload["source_materials"], item]}
        if tokens(trial) <= max_tokens - _HEADROOM:
            payload = trial
            selected.append(ref)
        else:
            payload["omitted"]["sources"] += 1
            omissions.append(OverviewOmission(kind="source", path=ref.path, side=ref.side,
                start_line=ref.start_line, end_line=ref.end_line, reason="material_capacity"))
    return payload, {item["source_ref"]: item["source"] for item in payload["source_materials"]}, omissions
