import json

import pytest

from app.review.models import ChangeObservationProposal, DiscoveryOutput, ReviewLimits
from app.review.observations import collect_observations, validate_observation
from app.review.nodes.agent_execution import run_agent
from app.review.submission import submission_feedback
from tests.review.test_native_graph import review_context
from tests.review.test_submission_feedback import SubmissionModel, goals

pytest_plugins = ["tests.review.test_native_agents"]


def proposal(ids, **updates):
    return ChangeObservationProposal(change="VALUE 由 1 调整为 2。", before="原值为 1。",
        after="现值为 2。", impact="读取该值的调用方。", kind="implementation",
        evidence_ids=ids, **updates)


async def test_observation_feedback_identifies_missing_sides_and_reuses_read_base(source_service):
    head = (await source_service.read("safe.py", 1, 1))["evidence"]["id"]
    base = (await source_service.read("safe.py", 1, 1, "base"))["evidence"]["id"]
    output = DiscoveryOutput(observations=[proposal([head])],
        examined=[{"goal_id": goal["id"], "evidence_ids": [head if goal["side"] == "head" else base]}
                  for goal in goals()])
    feedback = submission_feedback(output, goals(), source_service, {head, base}, "assignment")
    body = json.loads(feedback.split("\n", 1)[1])
    assert body["invalid_observations"] == [{"index": 0, "code": "observation_comparison_source_missing"}]
    assert base in {item["id"] for item in body["available_observation_evidence"]}
    output.observations[0].evidence_ids.append(base)
    assert submission_feedback(output, goals(), source_service, {head, base}, "assignment") is None


@pytest.mark.parametrize("case", ["omit_before", "other_path_base", "unissued"])
async def test_comparison_cannot_bypass_source_sides(source_service, case):
    head = (await source_service.read("safe.py", 1, 1))["evidence"]["id"]
    base = (await source_service.read("other.py", 1, 1, "base"))["evidence"]["id"]
    value = proposal([head, base] if case == "other_path_base" else [head])
    if case == "omit_before":
        value.before = None
    with pytest.raises(ValueError, match="evidence_not_read_by_agent" if case == "unissued"
                       else "observation_comparison_source_missing"):
        validate_observation(value, source_service, [] if case == "unissued" else [head, base],
                             {"safe.py"}, goals())


async def test_head_state_and_added_or_deleted_sources_have_explicit_requirements(source_service):
    head = (await source_service.read("safe.py", 1, 1))["evidence"]["id"]
    base = (await source_service.read("safe.py", 1, 1, "base"))["evidence"]["id"]
    value = proposal([head]).model_copy(update={"basis": "head_state", "before": None,
                                               "change": "HEAD 返回 2。"})
    assert validate_observation(value, source_service, [head], {"safe.py"}, goals())
    value.basis = "change"
    assert validate_observation(value, source_service, [head], {"safe.py"}, [goals()[0]])
    value.evidence_ids, value.before, value.after = [base], "BASE 原行为。", None
    assert validate_observation(value, source_service, [base], {"safe.py"}, [goals()[1]])


async def test_head_state_does_not_silently_replace_missing_change_observation(source_service):
    head = (await source_service.read("safe.py", 1, 1))["evidence"]["id"]
    base = (await source_service.read("safe.py", 1, 1, "base"))["evidence"]["id"]
    value = proposal([head]).model_copy(update={"basis": "head_state", "before": None,
                                               "change": "HEAD 返回 2。"})
    output = DiscoveryOutput(observations=[value],
        examined=[{"goal_id": goal["id"], "evidence_ids": [head if goal["side"] == "head" else base]}
                  for goal in goals()])
    feedback = submission_feedback(output, goals(), source_service, {head, base}, "assignment")
    body = json.loads(feedback.split("\n", 1)[1])
    assert body["invalid_observations"] == []
    assert body["missing_observation_paths"] == ["safe.py"]


class ObservationSubmissionModel(SubmissionModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        refs = {packet["evidence"]["side"]: packet["evidence"]["id"]
                for packet in payload["source_materials"]}
        args = result.generations[0].message.tool_calls[0]["args"]
        args["examined"] = [{"goal_id": goal["id"], "evidence_ids": [refs[goal["side"]]]}
                            for goal in payload["goals"]]
        ids = [refs["head"]] if self.calls == 1 or self.repeat else list(refs.values())
        args["observations"] = [proposal(ids).model_dump()]
        return result


@pytest.mark.parametrize("repeat", [False, True])
async def test_observation_correction_uses_existing_final_request_without_extra_reads(source_service, repeat):
    model = ObservationSubmissionModel(repeat=repeat)
    context = review_context(source_service, model)
    context.limits = ReviewLimits(model_calls=3, total_model_calls=8)
    result = await run_agent(context, "assignment", "reviewer", {"goals": goals()})
    assert result["status"] == "completed" and model.feedback_seen
    assert result["model_calls"] == model.calls == 2
    assert result["structured_output_retries"] == 1 and result["tool_failures"] == 0
    accepted, errors = collect_observations([result], [{"id": "assignment", "reviewer": "reviewer",
        "unit_id": "unit", "goal_ids": [goal["id"] for goal in goals()], "focus_paths": ["safe.py"]}],
        [{"id": "unit", "changed_paths": ["safe.py"]}], source_service, goals())
    assert bool(accepted) is not repeat and bool(errors) is repeat
    assert (await context.budget.summary())["model_calls"] == 2
