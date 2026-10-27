"""Deterministic bounded batching is not semantic finding deduplication."""

from collections import Counter

from app.review.evidence import digest
from app.review.models import VerificationResult


def verification_batches(candidates):
    batches = []
    for candidate in sorted(candidates, key=lambda item: (item["affected_path"], item["line"], item["id"])):
        current = batches[-1] if batches else []
        if (not current or len(current) == 3
                or candidate["affected_path"] != current[0]["affected_path"]
                or candidate["line"] - current[0]["line"] > 40):
            batches.append([])
        batches[-1].append(candidate)
    return [{"id": (batch[0]["id"] if len(batch) == 1 else
                     digest("verify-batch:" + ":".join(sorted(item["id"] for item in batch)))),
             "candidates": batch} for batch in batches]


def split_batch_result(result, candidates):
    expected = {item["id"] for item in candidates}
    decisions = result["output"].get("decisions", []) if result["status"] == "completed" else []
    counts = Counter(item.get("candidate_id") for item in decisions)
    extra = set(counts) - expected
    output = []
    for candidate in candidates:
        item = {**result, "id": candidate["id"], "execution_id": result["id"]}
        valid = result["status"] == "completed" and not extra and counts[candidate["id"]] == 1
        if valid:
            raw = next(value for value in decisions if value["candidate_id"] == candidate["id"])
            try:
                decision = VerificationResult.model_validate(raw)
                item["output"] = decision.model_dump(exclude={"candidate_id"})
                item["investigation_limitations"] = decision.limitations
            except ValueError:
                valid = False
        if not valid:
            item.update(status="failed", output={}, investigation_limitations=[],
                        error=result["error"] or "BatchVerificationIncomplete")
        output.append(item)
    return output
