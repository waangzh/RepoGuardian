import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.checkpointer import _checkpoint_serializer
from app.review.graph import build_review_graph
from app.review.limitations import review_limitations, validate_resolutions
from app.review.models import LimitationResolution, ReviewReport, VerificationOutput
from app.review.submission import submission_feedback
from app.services.publication.publication_planner import PublicationPlanner
from app.services.review.review_outcome import derive_review_outcome
from tests.review.test_native_graph import ReviewModel, review_context

pytest_plugins = ["tests.review.test_native_agents"]

GAP = "未穷尽检查其他外部消费者是否按 method 字段分支"
OTHER_GAP = "必要的存储调用方源码未读取，无法判断持久化影响"


class LimitationModel(ReviewModel):
    assessment: str | None = None
    other_gap: bool = False
    multiple_candidates: bool = False
    empty: bool = False
    investigate: bool = False
    verdict: str = "refuted"
    mixed_assessment: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        if messages[0].content.startswith("You independently review proposed evidence gaps"):
            evidence = [item["evidence"]["id"] for item in payload["source_materials"]]
            assessments = [{
                "limitation_id": item["id"], "status": "unresolved" if item["text"] == OTHER_GAP else "boundary",
                "explanation": "独立核对变更依赖，保留具体必要缺口并归类一般边界",
                "goal_ids": item["goal_ids"], "evidence_ids": evidence,
                "missing_fact": "存储调用方如何校验返回值", "dependent_conclusion": "变更是否破坏存储契约",
                "change_relevance": "safe.py 的返回值发生变化", "next_check": "读取存储调用方的校验函数",
            } for item in payload["review_limitations"]]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                tool_calls=[{"name": "LimitationReviewOutput", "id": "scope-output",
                             "args": {"assessments": assessments}}],
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])
        response = super()._generate(messages, stop, run_manager, **kwargs)
        for call in response.generations[0].message.tool_calls:
            args = call["args"]
            if call["name"] == "DiscoveryOutput":
                args["limitations"] = [GAP, OTHER_GAP] if self.other_gap else [GAP]
                if self.multiple_candidates:
                    args["findings"].append({**args["findings"][0], "title": "另一候选"})
                if self.investigate:
                    args["findings"][0]["severity"] = "high"
                if self.empty:
                    args["findings"] = []
            elif call["name"] in {"VerificationOutput", "BatchVerificationOutput"}:
                investigating = "bounded independent investigation" in messages[0].content
                outputs = args["decisions"] if "decisions" in args else [args]
                for output in outputs:
                    output["decision"] = self.verdict
                    if self.investigate and not investigating:
                        output.update(decision="insufficient_evidence", limitations=["尚缺调用链前提"])
                    elif self.assessment:
                        candidates = payload.get("candidates", [payload.get("candidate")])
                        current = next((item for item in candidates if item and
                            item["id"] == output.get("candidate_id", candidates[0]["id"])), candidates[0])
                        status = ("boundary" if current["title"] == "另一候选" else "resolved"
                                  ) if self.mixed_assessment else self.assessment
                        output["limitation_resolutions"] = [{
                            "limitation_id": item["id"], "status": status,
                            "explanation": "源码已核查必要读取点，未指定的仓库外使用不构成本次必要前提",
                            "evidence_ids": output["evidence_ids"],
                        } for item in payload["review_limitations"] if item["text"] == GAP]
        return response


def task_for(report):
    return SimpleNamespace(id=report.snapshot.review_id, status="completed", error=None,
        review_report=report, snapshot=SimpleNamespace(head_sha=report.snapshot.head_sha,
            effective_base_sha=report.snapshot.base_sha),
        pr=SimpleNamespace(owner="local", repo="repo", number=1))


async def run_review(service, **options):
    result = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(service, LimitationModel(**options)))
    return ReviewReport.model_validate(result["report"])


@pytest.mark.parametrize("assessment", [None, "resolved", "boundary"])
async def test_final_refutation_only_closes_explicit_source_backed_limitations(source_service, assessment):
    report = await run_review(source_service, assessment=assessment)
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert report.complete and report.termination == "completed"
    assert len(report.candidates) == 1 and not report.findings
    assert report.decisions[0].decision == "refuted"
    assert report.discovery_results[0].investigation_limitations == [GAP]
    assert outcome.unresolved_count == 0
    assert outcome.incomplete_investigations == 0 and outcome.pending_limitation_count == 0
    assert outcome.kind == "clean"
    record = outcome.review_limitations[0]
    assert record.status == (assessment or "boundary")
    assert record.candidate_ids == [report.candidates[0].id]
    assert record.source_id == report.discovery_results[0].id and record.text == GAP
    if assessment:
        assert record.resolution.evidence_ids
        assert record.resolved_by == report.candidates[0].id


async def test_independent_gap_survives_and_each_gap_is_counted(source_service):
    report = await run_review(source_service, other_gap=True)
    assert derive_review_outcome(task_for(report)).pending_limitation_count == 2
    report = await run_review(source_service, assessment="boundary", other_gap=True)
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "inconclusive" and outcome.incomplete_investigations == 1
    assert [(item.text, item.status) for item in outcome.review_limitations] == [
        (GAP, "boundary"), (OTHER_GAP, "unresolved")]


async def test_zero_candidates_review_a_declaration_before_treating_it_as_a_gap(source_service):
    report = await run_review(source_service, empty=True)
    assert not report.candidates and not report.verification_results
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.confirmed_count == outcome.unresolved_count == 0
    assert outcome.incomplete_investigations == 0 and outcome.kind == "clean"
    assert len(report.limitation_results) == 1


async def test_resolution_survives_batch_splitting_and_serialization(source_service):
    report = await run_review(source_service, assessment="resolved", multiple_candidates=True)
    assert len(report.candidates) == len(report.decisions) == 2
    restored = ReviewReport.model_validate_json(report.model_dump_json())
    outcome = derive_review_outcome(task_for(restored), evidence=source_service)
    assert outcome.kind == "clean" and outcome.incomplete_investigations == 0
    serializer = _checkpoint_serializer()
    for decision in report.decisions:
        assert serializer.loads_typed(serializer.dumps_typed(decision)) == decision


@pytest.mark.parametrize("case", ["mixed", "missing", "independent_gap"])
async def test_nonblocking_judgments_reconcile_per_candidate_without_erasing_gaps(source_service, case):
    from app.review.evidence import digest

    report = await run_review(source_service, assessment="resolved", multiple_candidates=True,
                              mixed_assessment=True)
    assert not report.limitation_results
    record = review_limitations(report, source_service)[0]
    assert record.status == "nonblocking"
    assert {item.status for item in record.resolution_claims} == {"resolved", "boundary"}
    assert {item.candidate_id for item in record.resolution_claims} == set(record.candidate_ids)
    if case == "missing":
        missing = report.decisions[0].candidate_id
        report.decisions[0].limitation_resolutions = []
        next(item for item in report.verification_results if item.id == missing).output["limitation_resolutions"] = []
        assert review_limitations(report, source_service)[0].status == "pending"
    elif case == "independent_gap":
        source = report.verification_results[0]
        assessment = {
            "limitation_id": record.id, "status": "unresolved", "explanation": "独立复核发现具体必要前提",
            "goal_ids": record.goal_ids, "evidence_ids": source.evidence_ids,
            "missing_fact": "存储调用方校验", "dependent_conclusion": "存储契约是否被破坏",
            "change_relevance": "返回值变化", "next_check": "读取存储调用方",
        }
        report.limitation_results = [source.model_copy(update={
            "id": "scope-" + digest(record.id),
            "output": {"assessments": [assessment], "findings": []}})]
        assert review_limitations(report, source_service)[0].status == "unresolved"
    else:
        restored = ReviewReport.model_validate_json(report.model_dump_json())
        outcome = derive_review_outcome(task_for(restored), evidence=source_service)
        assert outcome.kind == "clean" and outcome.pending_limitation_count == 0


async def test_investigator_receives_and_resolves_original_discovery_limitations(source_service):
    report = await run_review(source_service, assessment="resolved", investigate=True)
    assert len(report.verification_results) == 2
    assert report.verification_results[0].investigation_limitations == ["尚缺调用链前提"]
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "clean" and outcome.incomplete_investigations == 0


@pytest.mark.parametrize("failure", ["unknown", "other_candidate", "duplicate", "unread", "invalid"])
async def test_resolution_scope_and_evidence_are_rejected(source_service, failure):
    head = await source_service.read("safe.py", 1, 1)
    identity = head["evidence"]["id"]
    record = {"id": "limitation", "candidate_ids": ["candidate"]}
    resolution = LimitationResolution(limitation_id="limitation", status="resolved",
        explanation="读取源码确认前提", evidence_ids=[identity])
    if failure == "unknown":
        resolution.limitation_id = "unknown"
    elif failure == "other_candidate":
        record["candidate_ids"] = ["other"]
    elif failure == "invalid":
        resolution.evidence_ids = ["f" * 64]
    issued = [] if failure == "unread" else resolution.evidence_ids
    claims = [resolution] * (2 if failure == "duplicate" else 1)
    with pytest.raises((ValueError, OSError)):
        validate_resolutions(claims, [record], "candidate", issued, source_service)
    feedback = submission_feedback(VerificationOutput(
        decision="refuted", explanation="候选不成立", attribution="uncertain",
        limitation_resolutions=claims), [], source_service, issued, "candidate", [record])
    assert "invalid_limitation_resolutions" in feedback


@pytest.mark.parametrize("tamper", ["missing_evidence", "failed_branch", "decision_only", "no_archive"])
async def test_projection_cannot_close_gaps_with_unverifiable_assessments(source_service, tamper):
    report = await run_review(source_service, assessment="resolved")
    if tamper == "missing_evidence":
        identity = report.decisions[0].limitation_resolutions[0].evidence_ids[0]
        path = source_service.artifacts.root / "review" / f"evidence-{identity}.txt"
        path.unlink()
    elif tamper == "failed_branch":
        report.verification_results[0].status = "failed"
    elif tamper == "decision_only":
        report.verification_results[0].output["limitation_resolutions"] = []
    records = review_limitations(report, None if tamper == "no_archive" else source_service)
    assert records[0].status == "pending"


async def test_verified_finding_with_explicit_resolution_still_passes_publication_revalidation(source_service):
    report = await run_review(source_service, assessment="resolved", verdict="confirmed")
    from app.services.publication.review_publication import validated_findings
    task = task_for(report)
    task.configuration = SimpleNamespace(options=SimpleNamespace(scope=SimpleNamespace(mode="all")))
    from app.models.review.diff import ChangedFile
    from tests.review.test_native_graph import CHANGE
    task.changed_files = [ChangedFile.model_validate({**CHANGE, "additions": 1, "deletions": 1})]
    findings, withheld = validated_findings(task, source_service.artifacts)
    assert findings == report.findings and not withheld


async def test_all_report_surfaces_show_specific_remaining_gap_and_source(source_service):
    from app.review.contracts import ReviewConfiguration
    from app.models.review.diff import ChangedFile
    from app.models.review_plan import ReviewPlanOptions
    from app.review.models import ReviewLimits
    from tests.review.test_native_graph import CHANGE
    report = await run_review(source_service, other_gap=True, assessment="boundary")
    task = task_for(report)
    task.configuration = ReviewConfiguration(provider="test", endpoint="http://localhost", model="test",
        options=ReviewPlanOptions(), limits=ReviewLimits())
    task.pr_url = "https://github.com/local/repo/pull/1"
    task.pr.title, task.pr.body = "Test", ""
    task.changed_files = [ChangedFile.model_validate({**CHANGE, "additions": 1, "deletions": 1})]
    task.pr_discussion = None
    plan = PublicationPlanner(source_service.artifacts).build(task)
    assert plan.outcome.incomplete_investigations == 1
    for text in (plan.summary_markdown, plan.user_report_markdown, plan.review_comment_markdown):
        assert OTHER_GAP in text
        assert "来源：主审" in text
        assert report.discovery_results[0].id[:12] in text
        assert "关联调查尚未完成" not in text
    assert "1 项必要证据缺口尚未解决" in plan.review_comment_markdown
    assert "一般审查边界" in plan.user_report_markdown
