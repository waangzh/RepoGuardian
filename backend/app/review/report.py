"""Deterministic facts first, followed by one bounded, separately saved overview."""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage, SystemMessage

from app.review.domain import Overview, OverviewOutput, Report, utcnow
from app.review.findings import aggregate
from app.review.planner import identity, tokens
from app.review.planner import members
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped

logger = logging.getLogger("RepoGuardian.Review.Overview")


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
    checked_history = {candidate.previous_candidate_id for candidate in candidates
                       if candidate.verification is not None}
    if any(item["candidate_id"] not in checked_history for item in run.plan.history):
        reasons.append("unverified_historical_candidates")
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
    payload = {
        "snapshot": provider.run.snapshot.model_dump(), "changes": [], "source_materials": evidence,
        "findings": [], "decision_boundaries": [], "scope_reasons": report.reasons[:30],
        "omitted": {"changes": 0, "findings": 0, "decision_boundaries": 0, "sources": 0},
    }
    for change in provider.run.changed_files:
        item = {"path": change.file_path, "change_type": change.change_type,
                "additions": change.additions, "deletions": change.deletions}
        if tokens(payload) + tokens(item) > 1700:
            payload["omitted"]["changes"] += 1
        else:
            payload["changes"].append(item)
    for finding in report.findings:
        item = {"id": finding.id, "author_comment": finding.author_comment[:800]}
        if tokens(payload) + tokens(item) > 2300:
            payload["omitted"]["findings"] += 1
        else:
            payload["findings"].append(item)
    for candidate in report.candidates:
        if candidate.finding is not None:
            continue
        item = {"claim": candidate.proposal.claim[:300],
                "verdict": candidate.verification.verdict if candidate.verification else "unverified"}
        if tokens(payload) + tokens(item) > 2800:
            payload["omitted"]["decision_boundaries"] += 1
        else:
            payload["decision_boundaries"].append(item)
    for item in report.units:
        for source_id in item.visible_source_ids:
            if source_id in visible:
                continue
            try:
                ref, content = provider.evidence.read_source(source_id)
            except (ValueError, OSError):
                continue
            packet = {"source_ref": f"S{len(visible) + 1}", "source": ref.model_dump(), "content": content}
            if tokens(payload) + tokens(packet) > 4500:
                payload["omitted"]["sources"] += 1
                continue
            evidence.append(packet)
            visible[source_id] = packet
    # Incremental reuse and cutoff may leave no new reviewer request. The overview
    # can still receive bounded current source directly, without claiming review.
    if not evidence:
        included_members = {key for unit in provider.run.plan.units for key in unit.required_hunk_ids}
        for member in members(provider.run.changed_files).values():
            if member["id"] not in included_members:
                continue
            for side, key, path in (("head", "new_line_no", member["path"]),
                                    ("base", "old_line_no", member["old_path"])):
                lines = [line[key] for line in member["lines"] if line[key] is not None]
                if not lines or path not in provider.evidence.allowed_paths:
                    continue
                try:
                    result = provider.evidence._read(path, lines[0], lines[-1], side)
                except (ValueError, OSError):
                    continue
                source_id = result["evidence"]["id"]
                if source_id in visible:
                    continue
                packet = {"source_ref": f"S{len(visible) + 1}", "source": result["evidence"],
                          "content": result["content"]}
                if tokens(payload) + tokens(packet) > 4500:
                    payload["omitted"]["sources"] += 1
                    continue
                evidence.append(packet)
                visible[source_id] = packet
    return payload, {packet["source_ref"]: packet["source"] for packet in evidence}


async def generate_overview(report, provider, budget, model):
    started = perf_counter()
    run_id = provider.run.id
    previous = provider.run.report
    if not provider.run.plan.units:
        logger.info("总览跳过 review_id=%s reason=scope_empty", run_id)
        return Overview(status="skipped_empty")
    if utcnow() >= provider.run.hard_deadline:
        logger.warning("总览跳过 review_id=%s reason=hard_deadline", run_id)
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    packet, refs = await asyncio.to_thread(overview_material, report, provider)
    if (previous and previous.overview.input_hash == identity(packet)
            and previous.overview.status in {"ready", "failed"}):
        logger.info("总览复用 review_id=%s status=%s", run_id, previous.overview.status)
        return previous.overview
    overview = Overview(status="running", input_hash=identity(packet), omitted=packet["omitted"])
    report.overview = overview
    await asyncio.to_thread(provider.store.save_report, provider.run.id, report, provider.lease)
    ticket = None
    try:
        prompt = (Path(__file__).parent / "prompts" / "overview.md").read_text(encoding="utf-8")
        messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(packet, ensure_ascii=False))]
        estimate = tokens([prompt, packet, OverviewOutput.model_json_schema()]) + 512
        ticket = await budget.reserve(None, estimate, overview=True)
        logger.info("总览模型请求开始 review_id=%s ticket=%s estimated_input_tokens=%d",
                    run_id, ticket, estimate)
        structured = model.bind_tools([OverviewOutput], tool_choice="OverviewOutput")
        seconds = max(0, (provider.run.hard_deadline - utcnow()).total_seconds())
        async with asyncio.timeout(seconds):
            raw = await structured.ainvoke(messages)
        usage = getattr(raw, "usage_metadata", None)
        await budget.settle(ticket, usage)
        logger.info("总览模型请求返回 review_id=%s ticket=%s input_tokens=%s output_tokens=%s elapsed_ms=%.0f",
                    run_id, ticket, usage["input_tokens"] if usage is not None else "unknown",
                    usage["output_tokens"] if usage is not None else "unknown",
                    (perf_counter() - started) * 1000)
        if len(raw.tool_calls) != 1 or raw.tool_calls[0]["name"] != "OverviewOutput":
            raise ValueError("overview_schema")
        output = OverviewOutput.model_validate(raw.tool_calls[0]["args"])
        if any(handle not in refs for paragraph in output.change_overview for handle in paragraph.source_refs):
            raise ValueError("overview_unknown_source")
        if any(key not in {finding.id for finding in report.findings} for key in output.finding_order):
            raise ValueError("overview_unknown_finding")
        overview.status, overview.output = "ready", output
        from app.review.domain import SourceRef
        overview.sources = [SourceRef.model_validate(ref) for ref in refs.values()]
    except (LeaseLost, ReviewStopped, asyncio.CancelledError) as exc:
        logger.info("总览停止 review_id=%s reason=%s", run_id, type(exc).__name__)
        raise
    except Exception as exc:
        if ticket:
            await budget.settle(ticket, None)
        overview.status = "skipped_deadline" if utcnow() >= provider.run.hard_deadline else "failed"
        overview.error_code = str(exc) if isinstance(exc, BudgetExceeded) else "overview_unavailable"
        logger.warning("总览未完成 review_id=%s status=%s code=%s error_type=%s elapsed_ms=%.0f",
                       run_id, overview.status, overview.error_code, type(exc).__name__,
                       (perf_counter() - started) * 1000)
    else:
        logger.info("总览生成完成 review_id=%s paragraphs=%d omitted_sources=%d",
                    run_id, len(overview.output.change_overview), overview.omitted.get("sources", 0))
    return overview
