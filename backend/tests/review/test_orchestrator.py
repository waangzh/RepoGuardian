import json
import logging

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.agent import AgentRunner
from app.review.orchestrator import ReviewOrchestrator
from tests.review.vnext_helpers import setup_source_run


class ReviewModel(BaseChatModel):
    fail_after_submit: bool = False
    empty: bool = False
    overview_failure: bool = False
    unknown_usage: bool = False

    @property
    def _llm_type(self):
        return "review-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = str(messages[0].content)
        packet = json.loads(messages[1].content)
        tools = [message for message in messages if isinstance(message, ToolMessage)]
        if system.startswith("Summarize"):
            if self.overview_failure:
                raise ValueError("fixture_failure")
            name = "OverviewOutput"
            payload = {"change_overview": [{"text": "更新比例计算逻辑。",
                "source_refs": [packet["source_materials"][0]["source_ref"]]}], "finding_order": []}
        elif system.startswith("Independently"):
            handles = [source["source_ref"] for material in packet["materials"] for source in material["sources"]]
            name = "VerificationOutput"
            payload = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                "reason": "零输入现在到达除零路径，BASE 使用非零回退。", "source_refs": handles,
                "author_comment": "传入零时会除零并中断请求。恢复非零回退或显式处理零输入。"}
        elif tools or self.empty:
            if tools and self.fail_after_submit:
                raise ValueError("reviewer_crashed")
            name, payload = "UnitCompletion", {}
        else:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            name = "submit_candidate"
            payload = {"proposal": {"claim": "零输入导致除零", "trigger": "x=0", "impact": "请求失败",
                "source_refs": [handle], "location": {"path": "a.py", "line": 2, "side": "head"}}}
        usage = None if self.unknown_usage else {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", usage_metadata=usage,
            tool_calls=[{"name": name, "args": payload, "id": f"tool-{len(tools)}"}]))])


async def test_actual_agent_loop_candidate_verifier_and_default_overview(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "completed", report
    assert len(report.findings) == 1
    assert report.findings[0].publishable
    assert report.overview.status == "ready", report.overview
    assert report.usage["model_calls"] == 4
    assert all(item.material_delivery == "complete" for item in report.units)
    assert store.get(run.id).report == report


async def test_candidate_and_completion_in_one_response_wait_for_saved_receipt(tmp_path):
    class CombinedModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            if message.tool_calls and message.tool_calls[0]["name"] == "submit_candidate":
                message.tool_calls.append({"name": "UnitCompletion", "args": {}, "id": "completion"})
            return result
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(CombinedModel())).run(run.id, provider, lease)
    assert len(report.candidates) == 1
    assert len(report.findings) == 1
    assert report.status == "completed"


async def test_candidate_survives_reviewer_failure_and_is_verified(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(fail_after_submit=True))).run(
        run.id, provider, lease)
    assert report.status == "partial"
    assert len(report.findings) == 1
    assert report.overview.status == "ready"
    assert report.usage["unknown_calls"] == 1


async def test_normal_completion_without_material_is_partial(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    provider.evidence.max_file_bytes = 1
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(empty=True))).run(run.id, provider, lease)
    assert report.status == "partial"
    assert report.units[0].status == "completed"
    assert report.units[0].material_delivery == "incomplete"
    assert report.findings == []


async def test_overview_failure_keeps_completed_facts_and_unknown_usage(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(overview_failure=True,
        unknown_usage=True))).run(run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    assert report.overview.status == "failed"
    assert report.usage["input_tokens"] is None
    assert report.usage["committed_input_tokens"] > 0


@pytest.mark.parametrize("verdict", ["confirmed", "refuted"])
async def test_missing_decision_sources_cannot_admit_a_finding(tmp_path, verdict):
    from app.review.budget import BudgetGate
    from app.review.context import AgentContext
    from app.review.domain import Candidate, CandidateInput, VerificationOutput
    from app.review.findings import admit_verification
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease))
    candidate = Candidate(id="candidate", unit_id=work.unit_id, attempt_id=work.attempt_id,
        tool_call_id="call", proposal=CandidateInput(claim="bug", trigger="x", impact="y",
                                                    source_refs=["S1"]), sources=[])
    output = VerificationOutput(verdict=verdict, attribution="introduced", reason="claim")
    result = admit_verification(candidate, output, context)
    assert result.verification.verdict == "insufficient"
    assert result.finding is None


async def test_model_failure_logs_unknown_usage_without_provider_exception(tmp_path, caplog):
    class PrivateFailureModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if str(messages[0].content).startswith("Investigate") and any(
                isinstance(message, ToolMessage) for message in messages
            ):
                raise ValueError("PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE")
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    caplog.set_level(logging.INFO, logger="RepoGuardian.Review")
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(PrivateFailureModel())).run(run.id, provider, lease)
    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name.startswith("RepoGuardian.Review"))
    assert report.status == "partial"
    assert "模型请求失败" in text
    assert "error_type=ValueError usage=unknown" in text
    assert "Agent未完成" in text
    assert "PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE" not in text
    assert report.usage["unknown_calls"] == 1
