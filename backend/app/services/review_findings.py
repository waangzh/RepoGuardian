"""人工处理与机器证据分离；人工操作不更改 runtime、报告或发布资格。"""
from app.models.review_plan import FindingView


def finding_views(task, dispositions):
    records = (task.task_runtime_report or {}).get("findings", [])
    items = []
    for index, record in enumerate(records):
        candidate = record.get("candidate") or {}
        issue = record.get("issue") or {}
        identity = record.get("key") or candidate.get("candidate_id") or f"finding-{index}"
        state = record.get("status") or "unknown"
        evidence = ("verified" if state in {"confirmed", "published"} and issue else
                    "insufficient" if "insufficient" in (record.get("reason") or "") else
                    "unresolved" if state in {"unresolved", "candidate"} else "unknown")
        saved = dispositions.get(identity) or {}
        items.append(FindingView(id=identity, status=state, severity=issue.get("severity") or candidate.get("severity"),
            evidence_status=evidence,
            human_confirmation_reason=(issue.get("unresolved_reason") or "requires_human_confirmation")
                if issue.get("requires_human_confirmation") else None,
            anchor=issue.get("primary_evidence"), limitations=[record["reason"]] if record.get("reason") else [],
            source_ids=record.get("source_candidate_ids") or [],
            disposition=saved.get("current"), updated_at=saved.get("updated_at")))
    return items
