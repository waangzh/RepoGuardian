"""Deterministic facts first, followed by one bounded, separately saved overview."""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage, SystemMessage

from app.review.domain import Overview, OverviewOutput, Report, utcnow
from app.review.findings import aggregate
from app.review.overview_materials import overview_material
from app.review.planner import identity, tokens
from app.review.store import OVERVIEW_INPUT, BudgetExceeded, LeaseLost, ReviewStopped

logger = logging.getLogger("RepoGuardian.Review.Overview")
OVERVIEW_POLICY_VERSION = "change-first-prose-v1"


def build_report(run, work_items, candidates, usage):
    reasons = []
    reviews = [item for item in work_items if item.kind == "review"]
    reviewed_units = {item.unit_id for item in reviews}
    if run.plan.grouping_status == "pending":
        reasons.append("grouping_incomplete")
    reasons.extend(f"{unit.id}:not_started" for unit in run.plan.units
                   if unit.id not in reviewed_units)
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
    if not reviews and run.plan.grouping_status != "pending":
        reasons.append("scope_empty")
    findings = aggregate(candidates)
    facts = {"findings": findings, "candidates": candidates, "units": work_items,
             "excluded": run.plan.excluded, "status": status, "reasons": reasons, "usage": usage}
    return Report(version=identity(facts), **facts)


async def generate_overview(report, provider, budget, model):
    started = perf_counter()
    run_id = provider.run.id
    previous = provider.run.report
    if not provider.run.plan.units:
        logger.info("总览跳过 review_id=%.8s reason=scope_empty", run_id)
        return Overview(status="skipped_empty")
    if utcnow() >= provider.run.hard_deadline:
        logger.warning("总览跳过 review_id=%.8s reason=hard_deadline", run_id)
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    prompt = (Path(__file__).parent / "prompts" / "overview.md").read_text(encoding="utf-8")
    schema = OverviewOutput.model_json_schema()
    # Include editorial instructions and schema in the existing 6000-token
    # protection; the material ceiling itself must not grow with the prompt.
    material_limit = OVERVIEW_INPUT - tokens([prompt, schema]) - 512 - 128
    try:
        packet, refs, omissions = await asyncio.to_thread(
            overview_material, report, provider, max_tokens=material_limit)
    except BudgetExceeded:
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    except (ValueError, OSError):
        logger.warning("总览材料不可用 review_id=%.8s", run_id)
        return Overview(status="failed", error_code="overview_material_unavailable")
    input_hash = identity({"policy": OVERVIEW_POLICY_VERSION, "prompt": prompt,
                          "schema": schema, "materials": packet,
                          "omissions": [item.model_dump() for item in omissions]})
    if (previous and previous.overview.input_hash == input_hash
            and previous.overview.status in {"ready", "failed"}):
        logger.info("总览复用 review_id=%.8s status=%s", run_id, previous.overview.status)
        return previous.overview
    overview = Overview(status="running", input_hash=input_hash, omitted=packet["omitted"],
                        omitted_materials=omissions)
    logger.info("总览材料准备完成 review_id=%.8s changes=%d sources=%d material_tokens=%d "
                "omitted_changes=%d omitted_sources=%d policy=%s", run_id, len(packet["changes"]),
                len(refs), tokens(packet), packet["omitted"]["changes"], packet["omitted"]["sources"],
                OVERVIEW_POLICY_VERSION)
    report.overview = overview
    await asyncio.to_thread(provider.store.save_report, provider.run.id, report, provider.lease)
    ticket = None
    try:
        messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(packet, ensure_ascii=False))]
        estimate = tokens([prompt, packet, schema]) + 512
        ticket = await budget.reserve(None, estimate, overview=True)
        logger.info("总览模型请求开始 review_id=%.8s ticket=%.8s estimated_input_tokens=%d",
                    run_id, ticket, estimate)
        structured = model.bind_tools([OverviewOutput], tool_choice="OverviewOutput")
        seconds = max(0, (provider.run.hard_deadline - utcnow()).total_seconds())
        async with asyncio.timeout(seconds):
            raw = await structured.ainvoke(messages)
        usage = getattr(raw, "usage_metadata", None)
        await budget.settle(ticket, usage)
        logger.info("总览模型请求返回 review_id=%.8s ticket=%.8s input_tokens=%s output_tokens=%s elapsed_ms=%.0f",
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
        logger.info("总览停止 review_id=%.8s reason=%s", run_id, type(exc).__name__)
        raise
    except Exception as exc:
        if ticket:
            await budget.settle(ticket, None)
        overview.status = "skipped_deadline" if utcnow() >= provider.run.hard_deadline else "failed"
        overview.error_code = str(exc) if isinstance(exc, BudgetExceeded) else "overview_unavailable"
        logger.warning("总览未完成 review_id=%.8s status=%s code=%s error_type=%s elapsed_ms=%.0f",
                       run_id, overview.status, overview.error_code, type(exc).__name__,
                       (perf_counter() - started) * 1000)
    else:
        logger.info("总览生成完成 review_id=%.8s paragraphs=%d omitted_sources=%d",
                    run_id, len(overview.output.change_overview), overview.omitted.get("sources", 0))
    return overview
