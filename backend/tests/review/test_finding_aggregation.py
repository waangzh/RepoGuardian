import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.findings import aggregate_findings, candidate_identity, verify_finding
from app.review.graph import build_review_graph
from app.review.models import (
    CandidateFinding, CandidateProposal, VerificationOutput, VerificationResult, VerifiedFinding,
)
from tests.review.test_native_graph import CHANGE, ReviewModel, review_context

pytest_plugins = ["tests.review.test_native_agents"]


def finding(identity, peers, *, severity="high", line=1):
    candidate = CandidateFinding(id=identity, assignment_ids=["reviewer"], title=identity,
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
    different_attribution = finding("b", ["a"])
    different_attribution.verification.attribution = "exposed"
    assert len(aggregate_findings([finding("a", ["b"]), different_attribution])) == 2
    chain = aggregate_findings([finding("a", ["b"]), finding("b", ["a", "c"]), finding("c", ["b"])])
    assert len(chain) == 2 and chain[0].related_candidate_ids == ["b"]


def test_equivalence_claim_requires_confirmed_reason_and_severity():
    for kwargs in ({}, {"decision": "refuted", "severity": "high", "equivalence_reason": "same"}):
        fields = {"decision": "confirmed", "explanation": "same", "attribution": "introduced",
                  "equivalent_candidate_ids": ["other"], **kwargs}
        with pytest.raises(ValueError, match="equivalence_requires"):
            VerificationOutput(**fields)


async def test_invalid_equivalence_claims_are_dropped_not_downgraded(source_service):
    head, base = await source_service.read("safe.py", 1, 1), await source_service.read("safe.py", 1, 1, "base")
    ids = [head["evidence"]["id"], base["evidence"]["id"]]
    candidate = finding("a", []).candidate.model_copy(update={"evidence_ids": ids})
    result = {"status": "completed", "evidence_ids": ids, "output": {
        "decision": "confirmed", "explanation": "independent", "attribution": "introduced",
        "evidence_ids": ids, "severity": "high", "equivalent_candidate_ids": ["b"],
        "equivalence_reason": "same causal defect"}}
    for peers in ((), [{"id": "b", "affected_path": "other.py", "line": 1}],
                  [{"id": "b", "affected_path": "safe.py", "line": 25,
                    "attribution": "introduced"}]):
        verification, rebuilt = verify_finding(candidate, result, source_service, [CHANGE], peers)
        assert rebuilt is not None
        assert verification.decision == "confirmed"
        assert verification.equivalent_candidate_ids == []


class _Ref:
    def __init__(self, path, side, start_line, end_line):
        self.path, self.side, self.start_line, self.end_line = path, side, start_line, end_line


class _RefService:
    def __init__(self, refs):
        self.refs = refs

    def resolve(self, identity):
        return self.refs[identity]


DELETED_CHANGE = {"file_path": "removed.py", "change_type": "deleted",
                  "hunks": [{"lines": [{"kind": "deleted", "old_line_no": 1, "content": "OLD = 3"}]}]}


def _candidate(identity="a", *, path="safe.py", line=10):
    return CandidateFinding(id=identity, assignment_ids=["reviewer"], title="title",
        trigger="trigger", expected_behavior="expected", actual_behavior="actual",
        impact="impact", recommendation="recommendation", severity="high",
        affected_path=path, line=line, evidence_ids=["head", "base"], attribution="introduced")


def _result(candidate, *, evidence_ids, peer_ids=()):
    return {"status": "completed", "evidence_ids": list(evidence_ids), "output": {
        "decision": "confirmed", "explanation": "independent", "attribution": "introduced",
        "evidence_ids": [identity for identity in evidence_ids if identity != "peer"],
        "severity": "high", "equivalent_candidate_ids": list(peer_ids),
        "equivalence_reason": "same causal defect" if peer_ids else None}}


def test_out_of_window_equivalence_does_not_downgrade_confirmed_finding():
    candidate = _candidate()
    service = _RefService({
        "head": _Ref("safe.py", "head", 1, 50), "base": _Ref("safe.py", "base", 1, 50),
        "peer": _Ref("safe.py", "head", 1, 50)})
    verification, finding = verify_finding(
        candidate, _result(candidate, evidence_ids=("head", "base", "peer"), peer_ids=("b",)),
        service, [CHANGE], [{"id": "b", "affected_path": "safe.py", "line": 40}])
    assert finding is not None
    assert verification.decision == "confirmed"
    assert verification.equivalent_candidate_ids == []


def test_equivalence_accepts_peer_anchor_read_by_shared_execution():
    candidate = _candidate()
    service = _RefService({
        "head": _Ref("safe.py", "head", 10, 10), "base": _Ref("safe.py", "base", 10, 10),
        "peer": _Ref("safe.py", "head", 20, 20)})
    verification, finding = verify_finding(
        candidate, _result(candidate, evidence_ids=("head", "base", "peer"), peer_ids=("b",)),
        service, [CHANGE], [{"id": "b", "affected_path": "safe.py", "line": 20}])
    assert finding is not None
    assert verification.equivalent_candidate_ids == ["b"]
    unread = _result(candidate, evidence_ids=("head", "base"), peer_ids=("b",))
    verification, finding = verify_finding(
        candidate, unread, service, [CHANGE], [{"id": "b", "affected_path": "safe.py", "line": 20}])
    assert finding is not None
    assert verification.equivalent_candidate_ids == []


def test_equivalence_on_deleted_file_uses_base_anchor():
    candidate = _candidate(path="removed.py", line=1)
    service = _RefService({
        "base": _Ref("removed.py", "base", 1, 10),
        "peer": _Ref("removed.py", "base", 1, 10)})
    verification, finding = verify_finding(
        candidate, _result(candidate, evidence_ids=("base", "peer"), peer_ids=("b",)),
        service, [DELETED_CHANGE], [{"id": "b", "affected_path": "removed.py", "line": 1}])
    assert finding is not None
    assert verification.equivalent_candidate_ids == ["b"]


class EquivalentReviewModel(ReviewModel):
    reciprocal: bool = True
    different_attributions: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        message = result.generations[0].message
        for call in message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                proposal = call["args"]["findings"][0]
                proposal["title"] = "view one"
                proposal["trigger"] = "event one"
                proposal["severity"] = "high"
                second = {**proposal, "title": "view two", "trigger": "event two",
                          "attribution": "exposed" if self.different_attributions else "introduced"}
                call["args"]["findings"].append(second)
            elif call["name"] == "VerificationOutput":
                call["args"]["severity"] = "high"
                peers = payload.get("related_candidates", [])
                if peers and (self.reciprocal or payload["candidate"]["title"].endswith("two")):
                    call["args"]["equivalent_candidate_ids"] = payload["comparison_candidate_ids"][
                        payload["candidate"]["id"]]
                    call["args"]["equivalence_reason"] = "same VALUE contract regression and repair"
            elif call["name"] == "BatchVerificationOutput":
                by_id = {item["id"]: item for item in payload["candidates"]}
                for decision in call["args"]["decisions"]:
                    decision["severity"] = "high"
                    candidate = by_id[decision["candidate_id"]]
                    if self.reciprocal or candidate["title"].endswith("two"):
                        decision["equivalent_candidate_ids"] = payload["comparison_candidate_ids"][candidate["id"]]
                        decision["equivalence_reason"] = "same VALUE contract regression and repair"
        return result


@pytest.mark.parametrize("reciprocal,count", [(True, 1), (False, 2)])
@pytest.mark.parametrize("different_attributions", [False, True])
async def test_graph_keeps_candidate_hashes_and_all_verdicts(
    source_service, reciprocal, count, different_attributions,
):
    model = EquivalentReviewModel(
        reciprocal=reciprocal, different_attributions=different_attributions,
    )
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, model))
    report = state["report"]
    assert len(report["candidates"]) == len(report["decisions"]) == len(report["verification_results"]) == 2
    assert len(report["findings"]) == count
    assert len(model.observed) == 2
    assert report["termination"] == "completed"
    assert {item["attribution"] for item in report["candidates"]} == (
        {"introduced", "exposed"} if different_attributions else {"introduced"}
    )
    assert all(item["attribution"] == "introduced" for item in report["decisions"])
    assert all(item["id"] == candidate_identity(CandidateFinding.model_validate(item))
               for item in report["candidates"])


class BoundedComparisonModel(ReviewModel):
    candidate_count: int = 9
    pair_indices: tuple[int, int] = (-2, -1)
    pair: list[str] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                proposal = call["args"]["findings"][0]
                proposals = [{**proposal, "title": f"claim {index}", "severity": "high"}
                             for index in range(self.candidate_count)]
                call["args"]["findings"] = proposals
                identities = sorted(candidate_identity(CandidateProposal.model_validate(item)) for item in proposals)
                self.pair = [identities[index] for index in self.pair_indices]
            elif call["name"] in {"VerificationOutput", "BatchVerificationOutput"}:
                for decision in call["args"].get("decisions", [call["args"]]):
                    identity = decision.get("candidate_id", payload.get("candidate", {}).get("id"))
                    decision["severity"] = "high"
                    if identity in self.pair:
                        peer = next(item for item in self.pair if item != identity)
                        assert peer in payload["comparison_candidate_ids"][identity]
                        decision["equivalent_candidate_ids"] = [peer]
                        decision["equivalence_reason"] = "same causal mechanism and repair"
        return result


@pytest.mark.parametrize("candidate_count,pair_indices", [(9, (-2, -1)), (4, (2, 3))])
async def test_batching_preserves_grouping_at_cap_and_across_batches(
    source_service, monkeypatch, candidate_count, pair_indices,
):
    from app.review.nodes import verification as verification_node

    batching = verification_node.verification_batches
    reports = []
    for batched in (False, True):
        monkeypatch.setattr(verification_node, "verification_batches", batching if batched else
            lambda candidates: [{"id": item["id"], "candidates": [item]} for item in candidates])
        model = BoundedComparisonModel(candidate_count=candidate_count, pair_indices=pair_indices)
        state = await build_review_graph().ainvoke({"review_id": "review"}, context=review_context(source_service, model))
        report = state["report"]
        assert len(report["findings"]) == candidate_count - 1
        assert all(item["decision"] == "confirmed" for item in report["decisions"])
        assert all(len(item["comparison_candidate_ids"]) == min(8, candidate_count - 1)
                   for item in report["verification_results"])
        reports.append(report)
    for field in ("snapshot", "candidates", "decisions", "findings"):
        assert reports[0][field] == reports[1][field], field


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


@pytest.mark.parametrize("consume", [True, False])
async def test_unconsumed_search_pages_do_not_lower_termination(source_service, consume):
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, PaginatedModel(consume_pages=consume)))
    assert state["report"]["complete"]
    assert state["report"]["termination"] == "completed"
    assert all(item["unconsumed_pages"] == (0 if consume else 1)
               for item in state["report"]["discovery_results"])
    assert all(item["tool_failures"] == 0 for item in state["report"]["discovery_results"])


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
    assert result["status"] == "completed" and result["tool_failures"] == 1
    assert result["unconsumed_pages"] == 0
