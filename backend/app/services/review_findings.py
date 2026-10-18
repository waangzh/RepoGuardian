"""人工处理与机器证据分离；人工操作不更改 runtime 或发布资格。"""
from pydantic import ValidationError

from app.models.review_plan import FindingLocation, FindingVerification, FindingView
from app.services.review_publication import publication_issue


def finding_location(issue):
    anchor = issue.get("primary_evidence") or {}
    if anchor.get("resolution_status") != "exact":
        return None
    location = issue.get("resolved_location") or {}
    if location and (location.get("file_path"), location.get("start_line"), location.get("end_line"), location.get("side")) != (
            anchor.get("file_path"), anchor.get("resolved_start_line"), anchor.get("resolved_end_line"), anchor.get("resolved_side")):
        return None
    if not location:
        location = {"file_path": anchor.get("file_path"), "start_line": anchor.get("resolved_start_line"),
                    "end_line": anchor.get("resolved_end_line"), "side": anchor.get("resolved_side")}
    try:
        return FindingLocation(path=location.get("file_path"), start_line=location.get("start_line"),
            end_line=location.get("end_line"), side={"head": "right", "base": "left"}.get(location.get("side")))
    except ValidationError:
        return None


def finding_views(task, dispositions):
    records = (task.task_runtime_report or {}).get("findings", [])
    items = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
        issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
        identity = record.get("key") or candidate.get("candidate_id") or f"finding-{index}"
        state = record.get("status") if isinstance(record.get("status"), str) else "unknown"
        verified_issue, _ = publication_issue(task, record) if state == "confirmed" else (None, None)
        evidence = ("verified" if verified_issue else
                    "insufficient" if "insufficient" in (record.get("reason") or "") else
                    "unresolved" if state in {"unresolved", "candidate"} else "unknown")
        saved = dispositions.get(identity) or {}
        items.append(FindingView(id=identity, status=state, severity=issue.get("severity") or candidate.get("severity"),
            review_status={"published": "confirmed", "needs_human": "unresolved"}.get(state,
                state if state in {"confirmed", "candidate", "unresolved", "dismissed"} else "unknown"),
            title=issue.get("title") or candidate.get("claim"),
            message=issue.get("failure_scenario") or candidate.get("failure_scenario") or candidate.get("claim"),
            impact=issue.get("affected_behavior") or candidate.get("affected_behavior"),
            recommendation=issue.get("recommendation") or candidate.get("recommendation"),
            category=issue.get("category") or candidate.get("category"), location=finding_location(issue),
            verification=FindingVerification(evidence_status=evidence),
            evidence_status=evidence,
            human_confirmation_reason=(issue.get("unresolved_reason") or "requires_human_confirmation")
                if issue.get("requires_human_confirmation") else None,
            anchor=issue.get("primary_evidence"), limitations=[record["reason"]] if record.get("reason") else [],
            source_ids=record.get("source_candidate_ids") or [],
            disposition=saved.get("current"), updated_at=saved.get("updated_at")))
    return items
