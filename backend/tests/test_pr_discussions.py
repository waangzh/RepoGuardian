import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.models.pr_discussion import (
    CONTRACT, DiscussionComment, DiscussionThread, DiscussionSnapshot, DiscussionPolicy, DiscussionSummary,
    DiscussionReadArguments, DiscussionMatch,
)
from app.models.review_runtime import Candidate, ReviewGoal, ReviewWorkItem, RuntimeReadArguments
from app.services.review.artifact_store import LocalArtifactStore
from app.services.discussions.pr_discussion_store import DiscussionStore
from app.services.discussions.pr_discussions import (
    summary_valid, thread_input, read_original, discussion_page, prepare_discussions_node, metadata,
)
from app.services.discussions.pr_discussion_matching import valid_matches, safe_discussion_url
from app.services.review.review_run_budget import activate_run_budget
from app.services.review.review_task_executor import ReviewTaskExecutor
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.tools.github.github_discussions import GitHubDiscussionReader
from tests.support.runtime_v1_fixtures import evidence_catalog
from tests.support.runtime_harness import make_slice
from tests.support.runtime_harness import production_input as production_input  # noqa: E402, F401


def recheck_executor(*, base_visible):
    """历史复核执行器：只能读取冻结 Head；可选地加入已归档的 Base 证据。"""
    from app.models.review_runtime import ReviewRunSpec
    import hashlib
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="manifest", model_provider="openai-compatible", model_name="fixture",
        policy_version="p1", max_tokens=100_000, max_calls=5, discussion_manifest_hash="frozen",
        discussion_profile={"context_window": 20_000}, discussion_recheck=True,
        readable_scope_files=("src/a.py",))
    goal = ReviewGoal(goal_id="history-1", unit_id="history-1", kind="investigate",
                      target_ref="src/a.py", origin="derived")
    goals, related = [goal], []
    if base_visible:
        # 冻结的删除目标才会开放 Base 侧可读范围；这里模拟该路径实际可用。
        base_goal = ReviewGoal(goal_id="history-1-base", unit_id="history-1", kind="investigate",
            target_ref="src/a.py", target_kind="base_source_range", origin="derived")
        goals.append(base_goal)
        related.append(base_goal.goal_id)
    work = ReviewWorkItem(work_item_id="history-1", kind="investigate", goal_ids=["history-1"],
        related_goal_ids=related,
        unit_id="history-1", scope_files=["src/a.py"], readable_scope_hash=spec.readable_scope_hash,
        scheduling_class="investigation",
        proposition="核验历史讨论中的原始问题在当前固定代码中是否仍成立。",
        evidence_requests=[RuntimeReadArguments(file_path="src/a.py", start_line=1, end_line=1)])
    def evidence(identity, side, content, start=1):
        return {"file_path": "src/a.py", "side": side, "start_line": start, "end_line": start,
                "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "head_sha": "a" * 40, "base_sha": "b" * 40, "source": "file_read"}
    catalog = {"head-1": evidence("head-1", "head", "return value\n")}
    active = {"head-1"}
    if base_visible:
        catalog["base-1"] = evidence("base-1", "base", "return None\n")
        active.add("base-1")
    return ReviewTaskExecutor(spec, work, goals, active_evidence_ids=active,
        evidence_catalog=catalog, request_id="history-request")


def discussion_submission(resolution, evidence_ids):
    return {"kind": "submit", "investigation": {"answer": "refuted", "resolution": resolution,
        "evidence_ids": evidence_ids, "limitations": [], "reason": "核对历史问题的当前实现"}}


@pytest.mark.parametrize("limitations", [[], ["模型限制"] * 20])
def test_fixed_without_base_evidence_is_insufficient_not_not_present(limitations):
    executor = recheck_executor(base_visible=False)
    submission = discussion_submission("fixed", ["head-1"])
    submission["investigation"]["limitations"] = limitations
    parsed = executor.parse_and_apply(submission)
    assert not parsed.errors
    result = executor.projection.investigation
    # Base 不可读时不能伪造 fixed；也不能把“已修复”改写为“主张不成立”。
    assert result.resolution == "insufficient_evidence"
    assert result.answer.value == "insufficient_evidence"
    assert "fixed_requires_base_and_head_evidence" in result.limitations
    assert len(result.limitations) <= 20


def test_fixed_holds_only_when_base_and_head_evidence_are_both_accepted():
    executor = recheck_executor(base_visible=True)
    parsed = executor.parse_and_apply(discussion_submission("fixed", ["base-1", "head-1"]))
    assert not parsed.errors
    result = executor.projection.investigation
    assert result.resolution == "fixed" and result.answer.value == "refuted"
    assert set(result.evidence_ids) == {"base-1", "head-1"}



def snapshot(task_id="run", body="The retry branch may return null.", path="src/a.py"):
    return DiscussionSnapshot(task_id=task_id, pr_url="https://github.com/o/r/pull/1",
        head_sha="a" * 40, base_sha="b" * 40, started_at="start", finished_at="end",
        sources={s: "complete" for s in ("conversation", "review", "inline")},
        threads=[DiscussionThread(id="inline:1", source="inline", path=path, resolved=True,
            comments=[DiscussionComment(id="comment:1", body=body,
                url="https://github.com/o/r/pull/1#discussion_r1")])])


async def store_for(tmp_path, value=None, repository=None):
    value = value or snapshot()
    store = DiscussionStore(value.task_id, {"pr_url": value.pr_url, "head_sha": value.head_sha,
        "base_sha": value.base_sha}, repository=repository,
        artifacts=repository._artifacts if repository else LocalArtifactStore(tmp_path / "artifacts"))
    store.data["snapshot"] = await store.put("snapshot", value.model_dump(mode="json"))
    store.data["input"] = await store.put("input", {"items": [thread_input(t) for t in value.threads]})
    await store.save()
    return store


def response(items, *, next_page=False):
    return httpx.Response(200, json=items, request=httpx.Request("GET", "https://api.github.com/test"),
                          headers={"Link": '<https://api.github.com/test?page=2>; rel="next"'} if next_page else {})


async def test_three_sources_rest_pagination_and_unknown_thread_status():
    calls = []
    class GitHub:
        _token = None
        async def _get(self, url, headers, *, params):
            calls.append((url, params["page"]))
            if "/issues/" in url:
                if params["page"] == 1:
                    return response([{"id": 1, "body": "Question", "user": {"login": "reviewer"}}], next_page=True)
                return response([{"id": 2, "body": "Rebuttal"}])
            if url.endswith("/reviews"):
                return response([{"id": 3, "body": "Review body", "commit_id": "a" * 40}])
            return response([{"id": 4, "body": "Root", "path": "src/a.py"},
                             {"id": 5, "in_reply_to_id": 4, "body": "Fixed", "path": "src/a.py"}])
    result = await GitHubDiscussionReader(GitHub()).collect(task_id="run",
        pr_url=snapshot().pr_url, head_sha="a" * 40, base_sha="b" * 40, owner="o", repo="r", number=1)
    assert len(result.threads) == 4
    inline = next(t for t in result.threads if t.source == "inline")
    assert inline.resolved is None and inline.outdated is None and len(inline.comments) == 2
    assert result.sources == {"conversation": "complete", "review": "complete", "inline": "partial"}
    assert any(page == 2 for _, page in calls)


async def test_graphql_replies_are_paginated_and_resolution_is_only_platform_state():
    reader = GitHubDiscussionReader(SimpleNamespace(_token="fixture"))
    calls = []
    root = {"databaseId": 1, "body": "Problem", "url": snapshot().threads[0].comments[0].url}
    async def graphql(query, variables):
        calls.append(variables)
        if "owner" in variables:
            return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{
                "id": "node", "path": "src/a.py", "isResolved": True, "isOutdated": True,
                "comments": {"nodes": [root], "pageInfo": {"hasNextPage": True, "endCursor": "reply"}}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        return {"node": {"comments": {"nodes": [{**root, "databaseId": 2, "body": "Not fixed"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
    reader.graphql = graphql
    await reader.inline("o", "r", 1)
    assert len(reader.threads["inline:1"].comments) == 2
    assert reader.threads["inline:1"].resolved is True and reader.threads["inline:1"].complete
    assert calls[1] == {"id": "node", "cursor": "reply"}


async def test_null_body_review_is_skipped_without_aborting_the_source():
    class GitHub:
        _token = None
        async def _get(self, url, headers, *, params):
            if "/issues/" in url:
                return response([])
            if url.endswith("/reviews"):
                return response([{"id": 3, "body": None, "state": "APPROVED"},
                                 {"id": 4, "body": "Review body", "commit_id": "a" * 40}])
            return response([])
    result = await GitHubDiscussionReader(GitHub()).collect(task_id="run",
        pr_url=snapshot().pr_url, head_sha="a" * 40, base_sha="b" * 40, owner="o", repo="r", number=1)
    assert result.sources["review"] == "complete"
    assert [t.id for t in result.threads if t.source == "review"] == ["review:4"]


async def test_memory_mode_graph_nodes_share_one_frozen_discussion_store(tmp_path, monkeypatch, production_input):
    from app.core.config import settings
    from app.graph.nodes.pr_discussions import discussion_prepare_node
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    from app.graph.state import ReviewState, ReviewRunContext
    from langgraph.graph import END, StateGraph
    from app.services.review.review_run_budget import ReviewRunBudget, activate_run_budget
    monkeypatch.setattr(settings, "repoguardian_artifact_dir", tmp_path / "artifacts")
    state, provider, calls = production_input
    class EmptyGitHub:
        _token = None
        async def _get(self, url, headers, *, params):
            return response([])
    state = {**state, "_provider": provider, "_github_tool": EmptyGitHub()}
    state["task_runtime_configuration"] = {**state["task_runtime_configuration"],
        "discussion_contract": CONTRACT, "prompt_version": "task-review-prompt-v7"}
    graph = StateGraph(ReviewState, context_schema=ReviewRunContext)
    graph.add_node("discussion_prepare", discussion_prepare_node)
    graph.add_node("task_review_runtime", task_review_runtime_node)
    graph.set_entry_point("discussion_prepare")
    graph.add_edge("discussion_prepare", "task_review_runtime")
    graph.add_edge("task_review_runtime", END)
    budget = ReviewRunBudget("run", max_tokens=100_000, max_calls=5)
    with activate_run_budget(budget):
        result = await graph.compile().ainvoke(state)
    # 无仓储时 save() 不落库；运行时节点必须复用 prepare 写入的同一份冻结讨论数据，
    # 否则 data["input"] 缺失会在运行时节点失败。
    assert result["pr_discussion"]["manifest_hash"]
    assert result["task_runtime_report"]["scope_complete"]
    assert result["_discussion_store"].data["input"]
    assert calls[0]["discussion_manifest_hash"] == result["pr_discussion"]["manifest_hash"]


async def test_memory_mode_missing_frozen_input_is_a_domain_error(tmp_path, production_input):
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    state, provider, _ = production_input
    value = snapshot(task_id="run", path="a.py").model_copy(update={
        "pr_url": state["pr_url"], "head_sha": state["head_sha"], "base_sha": state["base_sha"]})
    store = DiscussionStore("run", {"pr_url": value.pr_url, "head_sha": value.head_sha,
        "base_sha": value.base_sha}, artifacts=LocalArtifactStore(tmp_path / "artifacts"))
    state = {**state, "_provider": provider, "_discussion_store": store}
    state["task_runtime_configuration"] = {**state["task_runtime_configuration"],
        "discussion_contract": CONTRACT}
    with pytest.raises(ValueError, match="discussion frozen input missing"):
        await task_review_runtime_node(state)


async def test_checkpoint_restore_replays_visible_discussions_for_duplicate_matching(tmp_path, persistence):
    from langchain_core.messages import AIMessage
    from app.llm.providers import OpenAICompatibleProvider
    from app.models.review import ReviewTask, ReviewToolScope
    from app.models.review_runtime import ReviewRunSpec
    from app.services.review.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
    from app.services.review.review_runtime import TaskReviewRuntime
    from app.services.review.review_task_protocol import TaskReviewV1Protocol
    from app.services.review.review_task_step import InspectTaskStep
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url=snapshot().pr_url, engine="task_review_v1"))
    entry = thread_input(snapshot().threads[0])
    response = json.dumps({"kind": "submit", "verification": {"candidate_id": "c1",
        "decision": "supported", "evidence_ids": ["e1"], "reason": "复核",
        "discussion_matches": [{"discussion_id": "inline:1", "comment_id": "comment:1",
            "quote": "retry branch may return null", "same_subject": True, "same_trigger": True,
            "same_impact": True, "new_information": False, "reason": "同一重试入口"}]}})
    async def configure(folder):
        folder.mkdir()
        (folder / "src").mkdir()
        (folder / "src/a.py").write_text("return value\n", encoding="utf-8")
        spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
            diff_manifest_hash="manifest", model_provider="openai-compatible", model_name="fixture",
            policy_version="p1", max_tokens=100_000, max_calls=5, prompt_version="task-review-prompt-v7",
            discussion_manifest_hash="frozen", discussion_context=[entry],
            discussion_profile={"context_window": 20_000})
        goal = ReviewGoal(goal_id="t1", unit_id="u1", kind="verify", target_ref="src/a.py")
        work = ReviewWorkItem(work_item_id="w1", kind="verify", goal_ids=["t1"], scope_files=["src/a.py"],
            verification_candidate=Candidate(candidate_id="c1", claim="候选", primary_anchor="t1",
                supporting_evidence=["e1"], source_result_ids=["inspect-1"]))
        executor = ReviewTaskExecutor(spec, work, [goal], active_evidence_ids={"e1"},
            evidence_catalog=evidence_catalog("e1"), request_id="inspect-1")
        provider = OpenAICompatibleProvider("stub-key", "https://example.test/v1", "fixture",
            provider_name="openai-compatible", request_attempts=1)
        class SDK:
            async def ainvoke(self, payload):
                return AIMessage(content=response, usage_metadata={"input_tokens": 10,
                    "output_tokens": 5, "total_tokens": 15})
        provider._build_chat_model = lambda *args: SDK()
        class SnapshotGit:
            _git = "git"
            def _run(self, argv):
                return "a" * 40
        scope = ReviewToolScope(review_unit_id="w1", commentable_files={"src/a.py"},
            readable_files={"src/a.py"}, repository_root=str(folder),
            max_lines_per_read=240, max_search_results=12)
        budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        step = InspectTaskStep(executor, provider, budget, scope, git_tool=SnapshotGit())
        step.protocol = TaskReviewV1Protocol(spec)
        runtime = TaskReviewRuntime([executor.work_item])
        checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
        await checkpoint.restore()
        return runtime, checkpoint, step
    runtime, checkpoint, step = await configure(tmp_path / "first")
    def crash(*args):
        raise asyncio.CancelledError()
    step.apply_response = crash
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    assert step.executor.projection.verification is None
    restored, _, restored_step = await configure(tmp_path / "second")
    # 崩溃后重放已保存响应：可见讨论集合必须与首次执行一致，否则重复匹配会被静默丢弃。
    assert restored_step.executor.projection.verification is not None
    assert [match.discussion_id for match in
        restored_step.executor.projection.verification.discussion_matches] == ["inline:1"]
    assert restored_step.executor.visible_discussions == [entry]
    assert not restored_step.pending_read


async def test_limits_partial_failure_timeout_and_cancellation():
    class GitHub:
        _token = None
        async def _get(self, url, headers, *, params):
            if "/reviews" in url:
                raise httpx.ConnectError("sensitive transport detail")
            return response([{"id": n, "body": "x" * 100, "path": "a.py"} for n in range(10)])
    result = await GitHubDiscussionReader(GitHub(), DiscussionPolicy(max_comments=2)).collect(
        task_id="run", pr_url=snapshot().pr_url, head_sha="a" * 40, base_sha="b" * 40,
        owner="o", repo="r", number=1)
    assert sum(len(t.comments) for t in result.threads) <= 2
    assert "sensitive" not in result.model_dump_json() and result.sources["review"] == "unavailable"
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    reader = GitHubDiscussionReader(GitHub())
    reader.simple = cancelled
    with pytest.raises(asyncio.CancelledError):
        await reader.collect(task_id="run", pr_url=snapshot().pr_url, head_sha="a" * 40,
            base_sha="b" * 40, owner="o", repo="r", number=1)


def test_summary_cannot_invent_quotes_or_silently_drop_rebuttals():
    original = [{"id": "1", "body": "retry may be null"}, {"id": "2", "body": "NOT fixed on retry"}]
    summary = DiscussionSummary(claims=["retry may be null"], objections=["NOT fixed on retry"],
        quotes={"1": original[0]["body"], "2": original[1]["body"]}, covered_comment_ids=["1", "2"])
    assert summary_valid(summary, original).objections
    with pytest.raises(ValueError):
        summary_valid(summary.model_copy(update={"quotes": {"1": "fixed"}}), original)
    with pytest.raises(ValueError):
        summary_valid(summary.model_copy(update={"covered_comment_ids": ["1"]}), original)


async def test_task_scoped_read_pagination_details_and_corruption(tmp_path):
    store = await store_for(tmp_path)
    rejected = await read_original(store, DiscussionReadArguments(discussion_id="other"))
    assert rejected["status"] == "rejected" and not store.data["used"]
    read = await read_original(store, DiscussionReadArguments(discussion_id="inline:1", limit=20))
    assert read["truncated"] and read["next_offset"] == 20
    assert (await metadata(store))["stats"]["referenced"] == 0
    foreign = dict(store.data["snapshot"], uri=(tmp_path / "foreign").as_uri())
    with pytest.raises(ValueError, match="outside task"):
        await store.read(foreign)
    store.artifacts.read_text = lambda uri: "{}"
    with pytest.raises(ValueError, match="invalid"):
        await store.read(store.data["snapshot"])


async def test_budgeted_original_read_is_not_code_evidence(tmp_path):
    step, messages = make_slice(tmp_path, [{"kind": "read", "tool": "discussion_read",
        "arguments": {"discussion_id": "inline:1"}, "purpose": "Read prior objections"}])
    store = await store_for(tmp_path)
    from app.llm.model_request_budgeter import ModelRequestProfile
    step.provider._profile_override = ModelRequestProfile(context_window=20_000)
    step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v7",
        "discussion_manifest_hash": "frozen", "discussion_context": [thread_input(snapshot().threads[0])],
        "discussion_profile": {"context_window": 20_000}})
    from app.services.review.review_task_protocol import TaskReviewV1Protocol
    step.protocol = TaskReviewV1Protocol(step.executor.spec)
    step.discussion_store = store
    before = set(step.executor.evidence_catalog)
    await step.advance()
    result = await step.advance()
    assert result["discussion_id"] == "inline:1"
    assert set(step.executor.evidence_catalog) == before
    assert len(messages) == step.budget.calls_consumed == 1
    assert "untrusted" in messages[0][0].content


async def test_paid_response_is_replayed_without_resending_and_unknown_is_not_retried(tmp_path):
    step, messages = make_slice(tmp_path, ['{"answer":"preserved"}'])
    store = await store_for(tmp_path)
    request = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids=step.executor.active_evidence_ids, evidence_catalog=step.executor.evidence_catalog)
    result = await store.call("summary:test", request, step.provider, step.budget)
    assert await store.call("summary:test", request, step.provider, step.budget) == result
    assert len(messages) == 1 and step.budget.settled_actual == 15
    store.data["calls"]["summary:test"]["response"] = None
    assert await store.call("summary:test", request, step.provider, step.budget) is None
    assert len(messages) == 1
    with pytest.raises(ValueError, match="changed"):
        await store.call("summary:test", replace(request, prompt="different"), step.provider, step.budget)


def test_duplicate_requires_visible_exact_source_and_same_subject_trigger_impact():
    entry = thread_input(snapshot().threads[0])
    match = DiscussionMatch(discussion_id=entry["id"], comment_id="comment:1",
        quote="retry branch may return null", same_subject=True, same_trigger=True, same_impact=True,
        new_information=False, reason="Same retry entry and null dereference")
    assert valid_matches([match], [entry], "src/a.py") == [match]
    assert not valid_matches([match], [entry], "src/other.py")
    assert not valid_matches([match.model_copy(update={"quote": "invented"})], [entry], "src/a.py")
    assert safe_discussion_url("https://evil.test/o/r/pull/1#discussion_r1", snapshot().pr_url) is None
    assert safe_discussion_url("https://github.com/o/r/pull/2#discussion_r1", snapshot().pr_url) is None


async def test_history_only_gaps_do_not_change_diff_outcome_or_legacy_inputs(tmp_path):
    from tests.support.review_tasks import completed_task as completed_task
    from app.services.publication.publication_planner import PublicationPlanner, summary_projection
    task = completed_task()
    original = PublicationPlanner().build(task).outcome
    task.pr_discussion = {"stats": {"status": "complete", "collected": 1, "comments": 1, "referenced": 1,
        "referenced_comments": 1, "checked": 0, "unchecked": 1, "limitations": [], "termination_reason": "budget_exhausted"},
        "rechecks": [{"discussion_id": "inline:1", "status": "not_checked", "reason": "budget"}]}
    plan = PublicationPlanner().build(task)
    # 历史复核缺口只进入独立的 historical_discussion，不改变主审查结论字段。
    assert plan.outcome.kind == original.kind and plan.outcome.reason_codes == original.reason_codes
    assert plan.outcome.review_completeness == original.review_completeness
    assert plan.outcome.runtime_reliability == original.runtime_reliability
    assert plan.outcome.historical_discussion.status == "partial"
    assert plan.outcome.historical_discussion.affects_risk is False
    assert summary_projection(plan).digest.discussion_stats["unchecked"] == 1
    assert "历史讨论复核" in plan.user_report_markdown and "本轮变更范围" in plan.review_comment_markdown
    assert await prepare_discussions_node({"task_runtime_configuration": {}}) == {}
    task.pr_discussion = None
    assert (await discussion_page(task, None))["stats"]["status"] == "not_recorded"


async def test_supplemental_queue_checks_files_outside_diff_and_preserves_main_termination(tmp_path, monkeypatch):
    from app.services.discussions import pr_discussion_recheck as module
    step, messages = make_slice(tmp_path, [lambda payload: {"kind": "submit", "investigation": {
        "answer": "supported", "resolution": "still_present", "reason": "Current source still returns null",
        "severity": "high", "affected_behavior": "Unauthenticated calls still reach the protected path",
        "recommendation": "Reject unauthenticated callers before accessing the protected path",
        "evidence_ids": [v["id"] for v in payload["active_evidence"]], "limitations": []}}])
    store = await store_for(tmp_path)
    async def open_frozen(*args, **kwargs):
        return store
    monkeypatch.setattr(module, "open_store", open_frozen)
    monkeypatch.setattr(module, "list_git_tracked_files", lambda root: ["src/a.py"])
    configuration = {"discussion_contract": "pr-discussion-context-v1", "provider": "openai-compatible",
        "model": "fixture", "endpoint": "https://example.test/v1", "policy_version": "p1",
        "prompt_version": "task-review-prompt-v7", "request_attempts": 1, "request_timeout_seconds": 60,
        "request_profile": {"context_window": 20_000}, "max_lines_per_read": 240, "max_search_results": 12,
        "deadline_seconds": 60}
    state = {"task_id": "run", "pr_url": snapshot().pr_url, "head_sha": "a" * 40, "base_sha": "b" * 40,
        "repo_path": str(tmp_path), "pr_discussion": await metadata(store), "task_runtime_configuration": configuration,
        "changed_files": [], "review_plan": {}, "created_at": datetime.now(timezone.utc).isoformat(),
        "task_runtime_report": {"execution_status": "completed", "termination_reason": "plan_exhausted",
                                "usage_integrity": "complete", "initial_scope_coverage": []},
        "_provider": step.provider, "_git_tool": step.git}
    with activate_run_budget(step.budget):
        result = await module.recheck_discussions_node(state)
        repeated = await module.recheck_discussions_node(state)
    assert result["task_runtime_report"]["termination_reason"] == "plan_exhausted"
    assert store.data["rechecks"]["inline:1"]["status"] == "still_present", {
        "termination": store.data.get("termination_reason"), "queue": store.data.get("history_checkpoint", {}).get("items")}
    assert result["pr_discussion"]["stats"]["checked"] == 1 and len(messages) == 1
    assert repeated["pr_discussion"]["stats"]["checked"] == 1
    assert store.data["rechecks"]["inline:1"]["severity"] == "high"
    assert store.data["rechecks"]["inline:1"]["affected_behavior"].startswith("Unauthenticated")


async def test_outer_deadline_during_recheck_keeps_completed_part_and_marks_rest_unchecked(tmp_path, monkeypatch):
    from app.services.discussions import pr_discussion_recheck as module
    import asyncio

    value = snapshot()
    value.threads.append(DiscussionThread(id="inline:2", source="inline", path="src/a.py",
        resolved=False, comments=[DiscussionComment(id="comment:2", body="Second historical issue",
            url="https://github.com/o/r/pull/1#discussion_r2")]))
    store = await store_for(tmp_path, value=value)
    async def open_frozen(*args, **kwargs):
        return store
    monkeypatch.setattr(module, "open_store", open_frozen)
    monkeypatch.setattr(module, "list_git_tracked_files", lambda root: ["src/a.py"])
    step, _ = make_slice(tmp_path, [])
    from app.models.review_runtime import DiscussionInvestigationResult

    async def run_until_outer_deadline(queue, steps, *, deadline_seconds):
        # 第一个复核已产生结论；第二次模型调用在途时外层 asyncio.timeout 取消本节点。
        identities = list(steps)
        steps[identities[0]].executor.projection.investigation = DiscussionInvestigationResult(
            answer="supported", resolution="still_present", evidence_ids=[], limitations=[],
            reason="当前实现仍存在原问题", work_item_id=identities[0],
            proposition="历史问题", request_id="r1", attempt_id="a1",
            snapshot_head_sha="a" * 40)
        raise asyncio.CancelledError()

    monkeypatch.setattr(module.TaskReviewRuntime, "run", run_until_outer_deadline)
    configuration = {"discussion_contract": "pr-discussion-context-v1", "provider": "openai-compatible",
        "model": "fixture", "endpoint": "https://example.test/v1", "policy_version": "p1",
        "prompt_version": "task-review-prompt-v7", "request_attempts": 1, "request_timeout_seconds": 60,
        "request_profile": {"context_window": 20_000}, "max_lines_per_read": 240, "max_search_results": 12,
        "deadline_seconds": 60}
    state = {"task_id": "run", "pr_url": snapshot().pr_url, "head_sha": "a" * 40, "base_sha": "b" * 40,
        "repo_path": str(tmp_path), "pr_discussion": await metadata(store), "task_runtime_configuration": configuration,
        "changed_files": [], "review_plan": {}, "created_at": datetime.now(timezone.utc).isoformat(),
        "task_runtime_report": {"execution_status": "completed", "termination_reason": "plan_exhausted",
                                "usage_integrity": "complete", "initial_scope_coverage": []},
        "_provider": step.provider, "_git_tool": step.git}
    with activate_run_budget(step.budget), pytest.raises(asyncio.CancelledError):
        await module.recheck_discussions_node(state)
    statuses = {key: item["status"] for key, item in store.data["rechecks"].items()}
    # 已完成复核保留结论；被期限打断的项显式未核验，不能维持旧快照或伪造成功。
    assert statuses["inline:1"] == "still_present"
    assert statuses["inline:2"] == "not_checked"
    assert "recheck_incomplete" in store.data["rechecks"]["inline:2"]["limitations"]
    assert "history_recheck:cancelled" in store.data["limitations"]


async def test_summary_cap_rejects_before_sending(tmp_path):
    from app.llm.providers import LLMProviderError
    step, messages = make_slice(tmp_path, [])
    store = await store_for(tmp_path)
    request = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids=step.executor.active_evidence_ids, evidence_catalog=step.executor.evidence_catalog)
    with pytest.raises(LLMProviderError, match="discussion_summary_token_cap"):
        await store.call("summary:cap", request, step.provider, step.budget, token_cap=0, call_cap=1)
    assert not messages and step.budget.calls_consumed == 0
    assert not step.budget.unknown_reserved


def test_duplicate_default_does_not_withhold_risk_and_explicit_selection_restores_comment():
    from tests.support.review_tasks import publishable_task as publishable_task
    from app.services.publication.publication_planner import PublicationPlanner
    from app.services.publication.publication_renderer import render_review_comment
    task = publishable_task()
    task.pr_discussion = {"manifest_hash": "frozen", "links": {"inline:1": snapshot().threads[0].comments[0].url}}
    verification = task.task_runtime_report["findings"][0]["verification"]
    verification["discussion_manifest_hash"] = "frozen"
    verification["discussion_matches"] = [DiscussionMatch(discussion_id="inline:1", comment_id="comment:1",
        quote="retry may be null", same_subject=True, same_trigger=True, same_impact=True,
        new_information=False, reason="Same triggering path").model_dump(mode="json")]
    plan = PublicationPlanner().build(task)
    assert plan.outcome.confirmed_count == plan.outcome.publishable_count == 1
    assert len(plan.inline_drafts) == 1 and not plan.withheld_findings
    assert plan.inline_drafts[0].default_selected is False
    assert "已有讨论不代表已修复" in plan.review_comment_markdown
    assert plan.inline_drafts[0].title in plan.review_comment_markdown
    assert plan.inline_drafts[0].title in render_review_comment(plan, include_discussed=True)
    verification["discussion_matches"][0]["new_information"] = True
    assert PublicationPlanner().build(task).inline_drafts[0].default_selected is True


from tests.support.persistence import persistence as persistence  # noqa: E402, F401


async def test_discussion_journal_persistence_cas_cancellation_and_readonly_api(tmp_path, persistence, monkeypatch):
    from fastapi import FastAPI
    from app.api import reviews
    from app.models.review import ReviewTask
    from app.services.review.coordination_runtime import CoordinationLeaseLost
    repository, _, _ = persistence
    task = ReviewTask(id="run", pr_url=snapshot().pr_url, engine="task_review_v1")
    repository.create_task(task)
    store = await store_for(tmp_path, repository=repository)
    task.pr_discussion = await metadata(store)
    repository.save_task(task)
    restored = await DiscussionStore("run", store.data["identity"], repository=repository).load()
    assert restored.data["snapshot"] == store.data["snapshot"]
    stale = dict(store.data, revision=store.data["revision"])
    with pytest.raises(CoordinationLeaseLost):
        repository.save_discussion_runtime("run", stale, stale["revision"] - 1)
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        get_task=repository.get_task, repository=repository))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/reviews/run/discussions")
        assert result.status_code == 200 and result.json()["items"][0]["comments"] == []
        detail = await client.get("/api/reviews/run/discussions?discussion_id=inline:1")
        assert detail.status_code == 200 and detail.json()["items"][0]["comments"][0]["body"]
        assert (await client.get("/api/reviews/run/discussions?discussion_id=other")).status_code == 404
        assert (await client.get("/api/reviews/run/discussions?page_size=101")).status_code == 422
    repository.cancel_task("run")
    with pytest.raises(CoordinationLeaseLost):
        await store.save()


def test_renamed_history_and_original_read_share_the_context_cap():
    from app.services.discussions.pr_discussion_recheck import thread_path
    from app.services.discussions.pr_discussions import fit_discussion_observations
    from app.llm.model_request_budgeter import PreparedModelRequest, ModelRequestProfile, ModelContextBudget, estimate_tokens
    from app.models.review_runtime import ReviewRunSpec
    import json
    assert thread_path(snapshot().threads[0], {"src/renamed.py"}, {"src/a.py": "src/renamed.py"}) == "src/renamed.py"
    profile = ModelRequestProfile(context_window=20_000)
    spec = ReviewRunSpec(run_id="run", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="d", model_provider="fixture", model_name="fixture", policy_version="p1",
        max_tokens=100_000, max_calls=5, discussion_manifest_hash="frozen", discussion_profile=profile.model_dump())
    request = PreparedModelRequest(model="fixture", operation="task_review_v1", system="source",
        prompt=json.dumps({"pr_discussions": [], "source": "return value"}), output_tokens=4096)
    original = [{"discussion_id": "inline:1", "offset": 0, "content": "x" * 8000, "comment_ranges": [], "comment_ids": []}]
    fitted, observations = fit_discussion_observations(request, original, spec)
    fitted.estimate(profile)
    assert json.loads(fitted.prompt.split("\n")[0])["source"] == "return value"
    assert len(observations[0]["content"]) < 8000 and observations[0]["truncated"]
    assert estimate_tokens(json.dumps([[], observations], ensure_ascii=False), "fixture")[0] <= ModelContextBudget(request, profile).input_limit // 10


def test_long_discussion_keeps_readable_id_when_full_text_does_not_fit():
    from app.services.discussions.pr_discussions import select_discussions
    from app.llm.model_request_budgeter import PreparedModelRequest, ModelRequestProfile
    item = thread_input(snapshot(body="Potential retry issue. " + "x" * 20_000).threads[0])
    request = PreparedModelRequest(model="fixture", operation="task_review_v1", system="source", prompt="source", output_tokens=4096)
    selected = select_discussions([item], {"src/a.py"}, "fixture", ModelRequestProfile(context_window=20_000), request)
    assert selected and selected[0]["id"] == "inline:1"
    assert selected[0]["comments"][0]["omitted_chars"] > 0
