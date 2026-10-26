import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from app.llm.providers import OpenAICompatibleProvider
from app.models.pr_discussion import DiscussionComment, DiscussionSnapshot, DiscussionThread
from app.models.review import ReviewTask, ReviewToolScope
from app.models.review_runtime import Candidate, ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.services.review.artifact_store import LocalArtifactStore
from app.llm.model_request_budgeter import ModelRequestProfile
from app.services.discussions.pr_discussion_store import DiscussionStore
from app.services.discussions.pr_discussions import metadata, thread_input
from app.services.review.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.runtime.review_task_executor import ReviewTaskExecutor
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
from app.services.review.runtime.review_task_step import InspectTaskStep, discussion_usage
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from tests.support.runtime_v1_fixtures import evidence_catalog
from tests.support.persistence import persistence as persistence  # noqa: E402, F401


QUOTE = "retry branch may return null"


def snapshot(count=5):
    return DiscussionSnapshot(task_id="run", pr_url="https://github.com/o/r/pull/1",
        head_sha="a" * 40, base_sha="b" * 40, started_at="start", finished_at="end",
        sources={source: "complete" for source in ("conversation", "review", "inline")},
        threads=[DiscussionThread(id=f"inline:{index}", source="inline", path="src/a.py",
            comments=[DiscussionComment(id=f"comment:{index}",
                body=f"The {QUOTE}. Thread {index}.",
                url=f"https://github.com/o/r/pull/1#discussion_r{index}")])
            for index in range(1, count + 1)])


def identity(value):
    return {"pr_url": value.pr_url, "head_sha": value.head_sha, "base_sha": value.base_sha}


def match_response(index=3):
    return json.dumps({"kind": "submit", "verification": {"candidate_id": "c1",
        "decision": "supported", "evidence_ids": ["e1"], "reason": "核对历史讨论",
        "discussion_matches": [{"discussion_id": f"inline:{index}", "comment_id": f"comment:{index}",
            "quote": QUOTE, "same_subject": True, "same_trigger": True, "same_impact": True,
            "new_information": False, "reason": "同一重试入口"}]}})


async def seeded_store(root, value):
    store = DiscussionStore(value.task_id, identity(value),
        artifacts=LocalArtifactStore(root / "artifacts"))
    store.data["snapshot"] = await store.put("snapshot", value.model_dump(mode="json"))
    store.data["input"] = await store.put("input", {"items": [thread_input(t) for t in value.threads]})
    await store.save()
    return store


def make_step(root, responses):
    folder = root / "repo"
    folder.mkdir(parents=True)
    (folder / "src").mkdir()
    (folder / "src/a.py").write_text("return value\n", encoding="utf-8")
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="openai-compatible", model_name="fixture",
        policy_version="p1", max_tokens=100_000, max_calls=5, prompt_version="task-review-prompt-v7",
        discussion_manifest_hash="frozen",
        discussion_context=[thread_input(t) for t in snapshot().threads],
        discussion_profile={"context_window": 200_000})
    goal = ReviewGoal(goal_id="t1", unit_id="u1", kind="verify", target_ref="src/a.py")
    work = ReviewWorkItem(work_item_id="w1", kind="verify", goal_ids=["t1"], scope_files=["src/a.py"],
        verification_candidate=Candidate(candidate_id="c1", claim="候选", primary_anchor="t1",
            supporting_evidence=["e1"], source_result_ids=["inspect-1"]))
    executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"},
        evidence_catalog=evidence_catalog("e1"), request_id="inspect-1")
    provider = OpenAICompatibleProvider("stub-key", "https://example.test/v1", "fixture",
        provider_name="openai-compatible", request_attempts=1,
        request_profile=ModelRequestProfile(context_window=20_000))
    messages = []

    class SDK:
        async def ainvoke(self, payload):
            messages.append(payload)
            return AIMessage(content=responses.pop(0), usage_metadata={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    provider._build_chat_model = lambda *args: SDK()
    scope = ReviewToolScope(review_unit_id="w1", commentable_files={"src/a.py"},
        readable_files={"src/a.py"}, repository_root=str(folder),
        max_lines_per_read=240, max_search_results=12)
    budget = ReviewRunBudget("run", max_tokens=spec.max_tokens, max_calls=spec.max_calls)
    step = InspectTaskStep(executor, provider, budget, scope)
    step.protocol = TaskReviewV1Protocol(spec)
    return step, messages


async def test_only_the_cited_comment_is_counted_as_used(tmp_path):
    value = snapshot()
    store = await seeded_store(tmp_path, value)
    step, messages = make_step(tmp_path, [match_response(3)])
    step.discussion_store = store
    observation = await step.advance()
    assert not observation["errors"] and len(messages) == 1
    # 五个线程都展示给了模型，但只有被引用注释所在线程与评论会计入统计。
    assert [thread["id"] for thread in step.executor.visible_discussions] == [
        f"inline:{index}" for index in range(1, 6)]
    assert store.data["used"] == {"inline:3": ["comment:3"]}
    stats = (await metadata(store))["stats"]
    assert stats["referenced"] == 1 and stats["referenced_comments"] == 1


async def test_summary_quote_registers_covered_comments_and_read_returns_only_served_ids():
    entry = thread_input(snapshot(count=1).threads[0])
    entry["summary"] = {"quotes": {"comment:1": QUOTE},
                        "covered_comment_ids": ["comment:1"], "omitted_comment_ids": []}
    raw = match_response(1).replace(QUOTE, QUOTE, 1)
    assert discussion_usage(raw, [entry], []) == {"inline:1": {"comment:1"}}
    read = {"kind": "read", "tool": "discussion_read",
            "arguments": {"discussion_id": "inline:1"}, "purpose": "复核原文"}
    observation = {"action": "tool", "status": "completed", "discussion_id": "inline:1",
                   "content": "部分原文", "comment_ids": ["comment:1"]}
    # discussion_read 只登记本次真正返回的评论；未引用的可见线程不计入。
    assert discussion_usage(json.dumps(read), [entry], [observation]) == {"inline:1": {"comment:1"}}


async def test_repeated_registration_within_one_response_saves_once(tmp_path):
    store = await seeded_store(tmp_path, snapshot())
    saves = 0
    original = store.save

    async def counting():
        nonlocal saves
        saves += 1
        await original()

    store.save = counting
    usage = {"inline:1": {"comment:1", "comment:1"}, "inline:2": {"comment:2"}}
    await store.mark_used(usage)
    await store.mark_used({"inline:1": {"comment:1"}, "inline:2": {"comment:2"}})
    assert saves == 1
    assert store.data["used"] == {"inline:1": ["comment:1"], "inline:2": ["comment:2"]}


async def test_store_save_failure_invalidates_and_propagates(tmp_path):
    value = snapshot()
    store = DiscussionStore(value.task_id, identity(value),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"))

    def broken(*args, **kwargs):
        raise OSError("disk unavailable")

    store.repository = type("Repository", (), {"save_discussion_runtime": staticmethod(broken)})()
    with pytest.raises(OSError):
        await store.mark_used({"inline:1": {"comment:1"}})
    assert store.invalidated


async def test_restore_replay_records_the_same_usage_as_the_online_path(tmp_path, persistence):
    value = snapshot()
    online_store = await seeded_store(tmp_path / "online", value)
    online_step, _ = make_step(tmp_path / "online", [match_response(3)])
    online_step.discussion_store = online_store
    await online_step.advance()
    expected = deepcopy(online_store.data["used"])
    assert expected == {"inline:3": ["comment:3"]}

    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=value.pr_url, engine="task_review_v1"))
    journal = DiscussionStore(value.task_id, identity(value), repository=repository)
    journal.data["snapshot"] = await journal.put("snapshot", value.model_dump(mode="json"))
    journal.data["input"] = await journal.put("input", {"items": [thread_input(t) for t in value.threads]})
    await journal.save()

    async def configure(name, responses):
        root = tmp_path / name
        step, messages = make_step(root, responses)
        step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        step.discussion_store = await DiscussionStore(value.task_id, identity(value),
            repository=repository).load()
        runtime = TaskReviewRuntime([step.executor.work_item])
        checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
        await checkpoint.restore()
        return runtime, step, messages

    runtime, step, _ = await configure("first", [match_response(3)])
    async def crash(*args):
        raise asyncio.CancelledError()
    # 响应已在检查点落盘，但引用统计在崩溃窗口内未写入；恢复必须补齐。
    step.record_discussion_usage = crash
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    assert step.discussion_store.data["used"] == {}

    _, restored_step, messages = await configure("second", [])
    assert not messages
    assert restored_step.executor.projection.verification is not None
    assert restored_step.discussion_store.data["used"] == expected
