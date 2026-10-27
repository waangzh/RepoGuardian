import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.findings import aggregate_findings, candidate_identity, verify_finding
from app.review.graph import build_review_graph
from app.review.models import CandidateFinding, VerificationOutput, VerificationResult, VerifiedFinding
from tests.review.test_native_graph import CHANGE, ReviewModel, review_context

pytest_plugins = ["tests.review.test_native_agents"]


def finding(identity, peers, *, severity="high", line=1):
    candidate = CandidateFinding(id=identity, assignment_ids=["behavior"], title=identity,
        trigger="different event", expected_behavior="once", actual_behavior="twice",
        impact="duplicate payment", recommendation="add idempotency", severity="high",
        affected_path="safe.py", line=line, evidence_ids=["source"], attribution="introduced")
    verification = VerificationResult(candidate_id=identity, decision="confirmed", explanation="same cause",
        evidence_ids=["source"], attribution="introduced", severity=severity,
        equivalent_candidate_ids=peers,
        equivalence_reason="same missing idempotency guard" if peers else None)
    return VerifiedFinding(id=identity, candidate=candidate, verification=verification,
                           evidence_ids=["source"], inline_line=1)


def test_aggregation_requires_reciprocal_clique_severity_and_location():
    pair = [finding("a", ["b"]), finding("b", ["a"])]
    merged = aggregate_findings(pair)
    assert len(merged) == 1 and merged[0].related_candidate_ids == ["b"]
    assert all(item.related_candidate_ids == [] for item in pair)
    assert aggregate_findings(list(reversed(pair))) == merged
    assert len(aggregate_findings([finding("a", ["b"]), finding("b", [])])) == 2
    assert len(aggregate_findings([finding("a", ["b"]), finding("b", ["a"], severity="medium")])) == 2
    assert len(aggregate_findings([finding("a", ["b"]), finding("b", ["a"], line=40)])) == 2
    chain = aggregate_findings([finding("a", ["b"]), finding("b", ["a", "c"]), finding("c", ["b"])])
    assert len(chain) == 2 and chain[0].related_candidate_ids == ["b"]


def test_equivalence_claim_requires_confirmed_reason_and_severity():
    for kwargs in ({}, {"decision": "refuted", "severity": "high", "equivalence_reason": "same"}):
        fields = {"decision": "confirmed", "explanation": "same", "attribution": "introduced",
                  "equivalent_candidate_ids": ["other"], **kwargs}
        with pytest.raises(ValueError, match="equivalence_requires"):
            VerificationOutput(**fields)


async def test_equivalence_cannot_name_unread_or_unsupplied_candidate(source_service):
    head, base = await source_service.read("safe.py", 1, 1), await source_service.read("safe.py", 1, 1, "base")
    ids = [head["evidence"]["id"], base["evidence"]["id"]]
    candidate = finding("a", []).candidate.model_copy(update={"evidence_ids": ids})
    result = {"status": "completed", "evidence_ids": ids, "output": {
        "decision": "confirmed", "explanation": "independent", "attribution": "introduced",
        "evidence_ids": ids, "severity": "high", "equivalent_candidate_ids": ["b"],
        "equivalence_reason": "same causal defect"}}
    with pytest.raises(ValueError, match="equivalence_subject_not_read"):
        verify_finding(candidate, result, source_service, [CHANGE])
    with pytest.raises(ValueError, match="equivalence_subject_not_read"):
        verify_finding(candidate, result, source_service, [CHANGE],
                       [{"id": "b", "affected_path": "other.py", "line": 1}])
    with pytest.raises(ValueError, match="equivalence_subject_not_read"):
        verify_finding(candidate, result, source_service, [CHANGE],
                       [{"id": "b", "affected_path": "safe.py", "line": 25,
                         "attribution": "introduced"}])


class EquivalentReviewModel(ReviewModel):
    reciprocal: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        message = result.generations[0].message
        for call in message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                proposal = call["args"]["findings"][0]
                proposal["title"] = "view " + payload["assignment"]["reviewer"]
                proposal["trigger"] = "event " + payload["assignment"]["reviewer"]
                proposal["severity"] = "high" if payload["assignment"]["reviewer"] == "behavior" else "low"
            elif call["name"] == "VerificationOutput":
                call["args"]["severity"] = "high"
                peers = payload.get("related_candidates", [])
                if peers and (self.reciprocal or payload["candidate"]["title"].endswith("behavior")):
                    call["args"]["equivalent_candidate_ids"] = [item["id"] for item in peers]
                    call["args"]["equivalence_reason"] = "same VALUE contract regression and repair"
        return result


@pytest.mark.parametrize("reciprocal,count", [(True, 1), (False, 2)])
async def test_graph_keeps_candidate_hashes_and_all_verdicts(source_service, reciprocal, count):
    model = EquivalentReviewModel(reciprocal=reciprocal)
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, model))
    report = state["report"]
    assert len(report["candidates"]) == len(report["decisions"]) == len(report["verification_results"]) == 2
    assert len(report["findings"]) == count
    assert len(model.observed) == 4
    assert report["termination"] == "completed"
    assert all(item["id"] == candidate_identity(CandidateFinding.model_validate(item))
               for item in report["candidates"])


class PaginatedModel(ReviewModel):
    consume_pages: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        responses = [message for message in messages if isinstance(message, ToolMessage)]
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        if not responses:
            calls = [{"name": "find_files", "args": {"limit": 1}, "id": "paths-first"}]
        else:
            bodies = [json.loads(message.content) for message in responses]
            page = bodies[-1]
            if page.get("next_cursor") and self.consume_pages:
                calls = [{"name": "find_files", "args": {"limit": 1, "cursor": page["next_cursor"]},
                          "id": f"paths-{len(responses)}"}]
            elif not any(message.name == "read_file" for message in responses):
                calls = [{"name": name, "args": {"path": "safe.py", "start": 1, "end": 1}, "id": name}
                         for name in ("read_file", "read_base_file")]
            else:
                evidence = [body["evidence"] for body in bodies if "evidence" in body]
                calls = [{"name": "DiscoveryOutput", "id": "done", "args": {"findings": [], "examined": [
                    {"goal_id": goal["id"], "evidence_ids": [item["id"] for item in evidence
                        if item["side"] == goal["side"]]} for goal in payload["goals"]]}}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls,
            usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}))])


@pytest.mark.parametrize("consume,termination", [(True, "completed"), (False, "partial")])
async def test_consumed_pages_are_not_permanent_read_errors(source_service, consume, termination):
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, PaginatedModel(consume_pages=consume)))
    assert state["report"]["complete"]
    assert state["report"]["termination"] == termination
    assert all(bool(item["read_errors"]) is not consume for item in state["report"]["discovery_results"])


class ToolFailureModel(ReviewModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        responses = [message for message in messages if isinstance(message, ToolMessage)]
        if not responses:
            calls = [{"name": "search_code", "args": {"query": "VALUE", "path": "../safe.py"}, "id": "bad"}]
        else:
            body = json.loads(responses[-1].content)
            assert body == {"ok": False, "error": "read_rejected", "error_type": "ReviewPathPolicyError",
                            "truncated": False}
            calls = [{"name": "DiscoveryOutput", "args": {"findings": [], "examined": [],
                      "limitations": ["拒绝了不安全路径"]}, "id": "done"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls,
            usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}))])


async def test_path_validation_failure_is_an_agent_tool_result(source_service):
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, ToolFailureModel()))
    result = state["report"]["discovery_results"][0]
    assert result["status"] == "completed" and result["read_errors"] == 1
