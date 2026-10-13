from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from app.models.review import ReviewTask
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.review_run_budget import ReviewRunBudget, ReviewRepositoryRunBudgetStore
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.services.task_review_projection import project_task_runtime, task_runtime_issues
from test_phase6a_persistence import persistence as persistence
from test_task_runtime_p3 import configured, candidate, inspect_submit, verification


def public_runtime(tmp_path, responses):
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime, steps, derivation, messages = configured(tmp_path, responses)
    root = steps["w1"]
    root.executor.spec = root.executor.spec.model_copy(update={"publication_contract": "claim-publication-v1"})
    root.protocol = TaskReviewV1Protocol(root.executor.spec)
    return runtime, steps, derivation, messages


def narrow(payload):
    value = verification(payload)
    value["verification"].update(publication_action="narrow", narrowing={
        "affected_behavior": "仅空返回值路径下调用方字段读取失败", "basis": "原失败情景只支持空返回值这一条件"})
    return value


async def test_narrowing_binds_original_verified_core_and_published_issue(tmp_path):
    runtime, steps, derivation, messages = public_runtime(tmp_path, [inspect_submit([candidate()]), narrow])
    await runtime.run(steps, deadline_seconds=5)
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "confirmed" and len(messages) == 2
    view = record["publication"]
    assert view["original_text"]["affected_behavior"] == "调用方字段读取失败"
    assert view["published_text"]["affected_behavior"] == "仅空返回值路径下调用方字段读取失败"
    assert view["published_text"]["claim"] == record["candidate"]["claim"]
    assert view["published_text"]["failure_scenario"] == record["candidate"]["failure_scenario"]
    report = project_task_runtime(runtime, steps, list(derivation.initial_goals.values()), termination_reason="plan_exhausted")
    assert task_runtime_issues(report)[0]["affected_behavior"] == view["published_text"]["affected_behavior"]
    assert view["attempt_id"] and view["subject_hash"] and view["basis"]
    derivation.invalidate_evidence({"e1"}, reason="stale")
    assert record["status"] == "unresolved" and "publication" not in record
    assert record["invalidated_publications"][0]["published_text"]
    assert record["issue"]["affected_behavior"] == "调用方字段读取失败"


@pytest.mark.parametrize("problem", ["claim", "failure_scenario", "refuted_narrow", "subject", "no_change"])
async def test_cannot_rewrite_core_or_attach_another_subject_to_confirmation(tmp_path, problem):
    def bad(payload):
        value = narrow(payload)
        if problem in {"claim", "failure_scenario"}:
            value["verification"]["narrowing"][problem] = "另一种真实但不同的机制"
        elif problem == "refuted_narrow":
            value["verification"]["decision"] = "refuted"
        elif problem == "subject":
            value["verification"]["subject_hash"] = "0" * 64
        else:
            value["verification"]["narrowing"]["affected_behavior"] = payload["candidate"]["affected_behavior"]
        return value
    runtime, steps, derivation, _ = public_runtime(tmp_path, [inspect_submit([candidate()]), bad, bad])
    await runtime.run(steps, deadline_seconds=5)
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and not record.get("publication")
    assert not derivation.data["verification_cache"]


async def test_refuted_original_cannot_pass_its_confirmation_to_new_mechanism(tmp_path):
    original = candidate()
    different = candidate(claim="另一处边界值破坏返回约定", scenario="边界数值导致不同分支返回错误值")
    def reveal(payload):
        value = verification(payload, "refuted")
        value["candidates"] = [different]
        return value
    runtime, steps, derivation, messages = public_runtime(tmp_path, [inspect_submit([original]), reveal, verification])
    await runtime.run(steps, deadline_seconds=5)
    records = list(derivation.data["candidates"].values())
    assert len(records) == 2 and len(messages) == 3
    old = next(record for record in records if record["candidate"]["claim"] == original["claim"])
    new = next(record for record in records if record is not old)
    assert old["status"] == "dismissed" and old["publication"]["published_text"] is None
    assert new["status"] == "confirmed" and new["discovery_origin"]["original_candidate_id"] == old["candidate"]["candidate_id"]
    assert old["publication"]["subject_hash"] != new["publication"]["subject_hash"]
    assert old["verification_work_item_id"] != new["verification_work_item_id"]


async def test_different_display_text_does_not_share_publication_confirmation(tmp_path):
    first = candidate()
    second = {**first, "recommendation": "仅在特定调用处处理空返回值"}
    runtime, steps, derivation, messages = public_runtime(tmp_path, [inspect_submit([first, second]), verification, verification])
    await runtime.run(steps, deadline_seconds=5)
    assert len(derivation.data["candidates"]) == 2 and len(messages) == 3
    assert len({record["publication"]["subject_hash"] for record in derivation.data["candidates"].values()}) == 2


async def test_unknown_premises_and_unknown_usage_never_publish(tmp_path):
    runtime, steps, derivation, _ = public_runtime(tmp_path / "unknown", [inspect_submit([candidate()]),
        lambda payload: verification(payload, "insufficient_evidence")])
    await runtime.run(steps, deadline_seconds=5)
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and record["publication"]["published_text"] is None
    second, steps, derivation, _ = public_runtime(tmp_path / "usage", [inspect_submit([candidate()])])
    await second.advance_one(steps)
    class SDK:
        async def ainvoke(self, messages):
            return AIMessage(content=json.dumps(verification(json.loads(messages[1].content))))
    steps["w1"].provider._build_chat_model = lambda *args: SDK()
    await second.run(steps, deadline_seconds=5)
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and record["publication"]["published_text"] is None
    assert not derivation.data["verification_cache"]


async def test_publication_survives_checkpoint_restore_without_resend(tmp_path, persistence):
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1", engine="task_review_v1"))
    async def configure(path, responses):
        path.mkdir()
        runtime, steps, derivation, messages = public_runtime(path, responses)
        steps["w1"].budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        checkpoint = TaskRuntimeCheckpoint(runtime, steps, repository)
        await checkpoint.restore()
        return runtime, steps, derivation, messages
    runtime, steps, derivation, messages = await configure(tmp_path / "first", [inspect_submit([candidate()]), narrow])
    await runtime.run(steps, deadline_seconds=5)
    expected = deepcopy(next(iter(derivation.data["candidates"].values()))["publication"])
    restored, steps, derivation, new_messages = await configure(tmp_path / "second", [])
    assert next(iter(derivation.data["candidates"].values()))["publication"] == expected
    assert not new_messages and await restored.advance_one(steps) is None
    assert len(messages) == 2 and steps["w1"].budget.settled_actual == 30
