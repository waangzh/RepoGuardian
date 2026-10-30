"""Deterministic facts first, followed by one bounded, separately saved overview."""

import asyncio
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.review.domain import Overview, OverviewOutput, Report, utcnow
from app.review.findings import aggregate
from app.review.planner import identity, tokens
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped


def build_report(run, work_items, candidates, usage):
    reasons = []
    reviews = [item for item in work_items if item.kind == "review"]
    for item in reviews:
        if item.status != "completed":
            reasons.append(f"{item.unit_id}:{item.error_code or item.status}")
        if item.material_delivery != "complete":
            reasons.append(f"{item.unit_id}:material_incomplete")
        if item.result and item.result.get("unresolved_reason"):
            reasons.append(f"{item.unit_id}:unresolved_investigation")
    if any(candidate.verification is None for candidate in candidates):
        reasons.append("unverified_candidates")
    status = "partial" if reasons else "completed"
    if reviews and all(item.status == "failed" for item in reviews) and not candidates:
        status = "failed"
    if not reviews:
        reasons.append("scope_empty")
    findings = aggregate(candidates)
    facts = {"findings": findings, "candidates": candidates, "units": work_items,
             "excluded": run.plan.excluded, "status": status, "reasons": reasons, "usage": usage}
    return Report(version=identity(facts), **facts)


def overview_material(report, provider):
    """Archive bytes are source, not reviewer narrative. Keep the input bounded."""
    evidence, visible = [], {}
    for item in report.units:
        for source_id in item.visible_source_ids:
            if source_id in visible:
                continue
            try:
                ref, content = provider.evidence.read_source(source_id)
            except (ValueError, OSError):
                continue
            packet = {"source_ref": f"S{len(visible) + 1}", "source": ref.model_dump(), "content": content}
            if tokens(evidence + [packet]) > 3500:
                continue
            evidence.append(packet)
            visible[source_id] = packet
    payload = {
        "snapshot": provider.run.snapshot.model_dump(),
        "changes": [{"path": change.file_path, "change_type": change.change_type,
                     "additions": change.additions, "deletions": change.deletions}
                    for change in provider.run.changed_files][:100],
        "source_materials": evidence,
        "findings": [{"id": item.id, "author_comment": item.author_comment} for item in report.findings][:100],
        "decision_boundaries": [
            {"claim": item.proposal.claim, "verdict": item.verification.verdict if item.verification else "unverified"}
            for item in report.candidates if item.finding is None][:100],
        "scope_reasons": report.reasons[:100],
    }
    return payload, {packet["source_ref"]: packet["source"] for packet in evidence}


async def generate_overview(report, provider, budget, model):
    previous = provider.run.report
    if not provider.run.plan.units:
        return Overview(status="skipped_empty")
    if utcnow() >= provider.run.hard_deadline:
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    packet, refs = await asyncio.to_thread(overview_material, report, provider)
    if (previous and previous.overview.input_hash == identity(packet)
            and previous.overview.status in {"ready", "failed"}):
        return previous.overview
    overview = Overview(status="running", input_hash=identity(packet))
    report.overview = overview
    await asyncio.to_thread(provider.store.save_report, provider.run.id, report, provider.lease)
    ticket = None
    try:
        prompt = (Path(__file__).parent / "prompts" / "overview.md").read_text(encoding="utf-8")
        messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(packet, ensure_ascii=False))]
        estimate = tokens([prompt, packet, OverviewOutput.model_json_schema()]) + 512
        ticket = await budget.reserve(None, estimate, overview=True)
        structured = model.with_structured_output(OverviewOutput, include_raw=True)
        seconds = max(0, (provider.run.hard_deadline - utcnow()).total_seconds())
        async with asyncio.timeout(seconds):
            response = await structured.ainvoke(messages)
        raw = response.get("raw")
        await budget.settle(ticket, getattr(raw, "usage_metadata", None))
        output = response["parsed"]
        if not isinstance(output, OverviewOutput):
            raise ValueError("overview_schema")
        if any(handle not in refs for paragraph in output.change_overview for handle in paragraph.source_refs):
            raise ValueError("overview_unknown_source")
        if any(key not in {finding.id for finding in report.findings} for key in output.finding_order):
            raise ValueError("overview_unknown_finding")
        overview.status, overview.output = "ready", output
        from app.review.domain import SourceRef
        overview.sources = [SourceRef.model_validate(ref) for ref in refs.values()]
    except (LeaseLost, ReviewStopped, asyncio.CancelledError):
        raise
    except Exception as exc:
        if ticket:
            await budget.settle(ticket, None)
        overview.status = "skipped_deadline" if utcnow() >= provider.run.hard_deadline else "failed"
        overview.error_code = str(exc) if isinstance(exc, BudgetExceeded) else "overview_unavailable"
    return overview
