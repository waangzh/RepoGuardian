import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.models.pr_discussion import (
    DiscussionComment, DiscussionThread, DiscussionSnapshot, DiscussionPolicy, DiscussionSummary,
    DiscussionReadArguments, DiscussionMatch,
)
from app.services.artifact_store import LocalArtifactStore
from app.services.pr_discussion_store import DiscussionStore
from app.services.pr_discussions import (
    summary_valid, thread_input, read_original, discussion_page, prepare_discussions_node, metadata,
)
from app.services.pr_discussion_matching import valid_matches, safe_discussion_url
from app.services.review_run_budget import activate_run_budget
from app.tools.github_discussions import GitHubDiscussionReader
from test_task_review_sdk_slice import make_slice


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
    from app.services.model_request_budgeter import ModelRequestProfile
    step.provider._profile_override = ModelRequestProfile(context_window=20_000)
    step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v7",
        "discussion_manifest_hash": "frozen", "discussion_context": [thread_input(snapshot().threads[0])],
        "discussion_profile": {"context_window": 20_000}})
    from app.services.review_task_protocol import TaskReviewV1Protocol
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
    from test_review_outcome import completed_task
    from app.services.publication_planner import PublicationPlanner, summary_projection
    task = completed_task()
    original = PublicationPlanner().build(task).outcome
    task.pr_discussion = {"stats": {"status": "complete", "collected": 1, "comments": 1, "referenced": 1,
        "referenced_comments": 1, "checked": 0, "unchecked": 1, "limitations": [], "termination_reason": "budget_exhausted"},
        "rechecks": [{"discussion_id": "inline:1", "status": "not_checked", "reason": "budget"}]}
    plan = PublicationPlanner().build(task)
    assert plan.outcome == original
    assert summary_projection(plan).digest.discussion_stats["unchecked"] == 1
    assert "历史讨论复核" in plan.user_report_markdown and "本轮变更范围" in plan.review_comment_markdown
    assert await prepare_discussions_node({"task_runtime_configuration": {}}) == {}
    task.pr_discussion = None
    assert (await discussion_page(task, None))["stats"]["status"] == "not_recorded"


async def test_supplemental_queue_checks_files_outside_diff_and_preserves_main_termination(tmp_path, monkeypatch):
    from app.services import pr_discussion_recheck as module
    step, messages = make_slice(tmp_path, [lambda payload: {"kind": "submit", "investigation": {
        "answer": "supported", "resolution": "still_present", "reason": "Current source still returns null",
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


async def test_summary_cap_rejects_before_sending(tmp_path):
    from app.agents.providers import LLMProviderError
    step, messages = make_slice(tmp_path, [])
    store = await store_for(tmp_path)
    request = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids=step.executor.active_evidence_ids, evidence_catalog=step.executor.evidence_catalog)
    with pytest.raises(LLMProviderError, match="discussion_summary_token_cap"):
        await store.call("summary:cap", request, step.provider, step.budget, token_cap=0, call_cap=1)
    assert not messages and step.budget.calls_consumed == 0
    assert not step.budget.unknown_reserved


def test_duplicate_default_does_not_withhold_risk_and_explicit_selection_restores_comment():
    from test_publication_planner import publishable_task
    from app.services.publication_planner import PublicationPlanner
    from app.services.publication_renderer import render_review_comment
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
    assert "默认不重复列出" in plan.review_comment_markdown
    assert plan.inline_drafts[0].title in render_review_comment(plan, include_discussed=True)
    verification["discussion_matches"][0]["new_information"] = True
    assert PublicationPlanner().build(task).inline_drafts[0].default_selected is True


from test_phase6a_persistence import persistence as persistence  # noqa: E402, F401


async def test_discussion_journal_persistence_cas_cancellation_and_readonly_api(tmp_path, persistence, monkeypatch):
    from fastapi import FastAPI
    from app.api import reviews
    from app.models.review import ReviewTask
    from app.services.coordination_runtime import CoordinationLeaseLost
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
        get_task=repository.get_task, _repository=repository))
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
    from app.services.pr_discussion_recheck import thread_path
    from app.services.pr_discussions import fit_discussion_observations
    from app.services.model_request_budgeter import PreparedModelRequest, ModelRequestProfile, ModelContextBudget, estimate_tokens
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
    from app.services.pr_discussions import select_discussions
    from app.services.model_request_budgeter import PreparedModelRequest, ModelRequestProfile
    item = thread_input(snapshot(body="Potential retry issue. " + "x" * 20_000).threads[0])
    request = PreparedModelRequest(model="fixture", operation="task_review_v1", system="source", prompt="source", output_tokens=4096)
    selected = select_discussions([item], {"src/a.py"}, "fixture", ModelRequestProfile(context_window=20_000), request)
    assert selected and selected[0]["id"] == "inline:1"
    assert selected[0]["comments"][0]["omitted_chars"] > 0
