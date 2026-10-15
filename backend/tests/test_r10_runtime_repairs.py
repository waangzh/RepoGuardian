import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.graph.nodes.task_review_runtime import task_review_runtime_node
from app.graph.nodes.report import report_node
from app.services.review_task_derivation import TaskReviewDerivation
from app.services.task_review_projection import project_task_runtime
from test_task_runtime_p4 import production_input as production_input


def catalog_derivation(catalogs):
    derivation = TaskReviewDerivation.__new__(TaskReviewDerivation)
    derivation.steps = {str(i): SimpleNamespace(executor=SimpleNamespace(evidence_catalog=value))
                        for i, value in enumerate(catalogs)}
    return derivation


@pytest.mark.parametrize("field", ["content", "content_hash", "head_sha", "base_sha", "side", "file_path", "start_line", "end_line"])
def test_identity_conflicts_remain_rejected(field):
    value = {"file_path": "a.py", "start_line": 1, "end_line": 2, "content": "code",
             "content_hash": "hash", "head_sha": "head", "base_sha": "base", "side": "head"}
    changed = {**value, field: "different"}
    with pytest.raises(ValueError, match="conflicting evidence identity"):
        catalog_derivation([{"e": value}, {"e": changed}])._all_evidence()


def test_retrieval_metadata_does_not_change_source_identity():
    source = {"file_path": "a.py", "content": "code", "content_hash": "hash", "source": "fixed_snapshot", "hunk_id": "g"}
    reread = {**source, "source": "file_read", "review_unit_id": "other"}
    reread.pop("hunk_id")
    merged = catalog_derivation([{"e": source}, {"e": reread}])._all_evidence()["e"]
    assert merged["hunk_id"] == "g" and merged["review_unit_id"] == "other"


@pytest.mark.parametrize("name", ["0000", "0001"])
def test_r10_archived_catalogs_reproduce_metadata_only_conflict(name):
    archive = Path(".repoguardian/evaluation-runs/smoke-20261013-flash-r10-remaining") / (name + ".json")
    if not archive.exists():
        pytest.skip("historical r10 archive is local, not a portable fixture")
    data = json.loads(archive.read_text(encoding="utf-8"))
    catalogs = []
    seen, old_conflicts = {}, []
    for event in data["model_requests"]:
        if event["status"] != "started":
            continue
        payload = json.loads(event["prompt"].split("\n")[0])
        catalog = {item["id"]: {key: value for key, value in item.items() if key != "id"}
                   for item in payload["active_evidence"]}
        for identity, value in catalog.items():
            if identity in seen and seen[identity] != value:
                old_conflicts.append(identity)
            seen[identity] = value
        catalogs.append(catalog)
    assert old_conflicts  # 旧实现会在已见过的同一 ID 上抛 ValueError。
    merged = catalog_derivation(catalogs)._all_evidence()
    assert set(merged) == set(seen)
    assert data["status"] == "failed" and data["error_type"] == "ValueError"


@pytest.mark.parametrize("has_accepted", [False, True])
async def test_failed_runtime_exports_accepted_results_and_manifest(production_input, monkeypatch, has_accepted):
    state, provider, calls = production_input
    state["_provider"] = provider
    if not has_accepted:
        async def broken_advance(self):
            raise ValueError("secret should never be exported")
        monkeypatch.setattr("app.services.review_task_step.InspectTaskStep.advance", broken_advance)
    else:
        def broken_refresh(self):
            raise ValueError("secret should never be exported")
        monkeypatch.setattr(TaskReviewDerivation, "refresh", broken_refresh)
    update = await task_review_runtime_node(state)
    report = update["task_runtime_report"]
    assert update["status"] == report["execution_status"] == "failed"
    assert report["termination_reason"] == "technical_failure"
    assert report["diagnostics"] and "secret" not in json.dumps(report)
    assert (report["initial_scope_coverage"][0]["status"] == "examined") is has_accepted
    assert report["settled_tokens"] == (15 if has_accepted else 0)
    exported = await report_node({**state, **update})
    assert exported["status"] == "failed" and exported["run_manifest"]
    assert len(calls) == (1 if has_accepted else 0)


def test_incomplete_derivation_keeps_accepted_candidates():
    # 投影无需再次执行派生；接受过而未映射到 Issue 的候选仍可诊断。
    from app.models.review_runtime import Candidate
    from app.services.review_runtime import TaskReviewRuntime
    candidate = Candidate(candidate_id="c", primary_anchor="g", claim="缺陷", affected_behavior="行为",
        failure_scenario="场景", supporting_evidence=[], source_result_ids=[])
    step = SimpleNamespace(executor=SimpleNamespace(work_item=SimpleNamespace(kind=SimpleNamespace(value="inspect")),
        projection=SimpleNamespace(candidates={"c": candidate}, examinations={}), spec=SimpleNamespace(readable_scope_files=())),
        last_observation={}, tool_observations=[], budget=None)
    queue = TaskReviewRuntime()
    queue.record_failure(ValueError(), stage="task_derivation")
    report = project_task_runtime(queue, {"w": step}, [], termination_reason="technical_failure")
    assert report.findings[0]["reason"] == "candidate_derivation_incomplete"
    assert report.findings[0]["status"] == "unresolved"


def test_investigation_citation_repair_is_bounded_and_preserves_valid_results():
    from app.models.review_runtime import ReviewRunSpec, ReviewGoal, ReviewWorkItem
    from app.services.review_task_executor import ReviewTaskExecutor
    from app.services.review_task_step import InspectTaskStep
    from app.services.review_task_protocol import TaskReviewV1Protocol
    from runtime_v1_fixtures import evidence_catalog
    spec = ReviewRunSpec(run_id="r", snapshot_head_sha="a" * 40, snapshot_base_sha="b" * 40,
        diff_manifest_hash="m", model_provider="stub", model_name="m", policy_version="p", max_calls=3,
        max_tokens=100, prompt_version="task-review-prompt-v5")
    goal = ReviewGoal(goal_id="g", unit_id="u", kind="investigate", target_ref="src/a.py")
    item = ReviewWorkItem(work_item_id="w", goal_ids=["g"], kind="investigate", scope_files=["src/a.py"],
        proposition="核对行为", required_evidence_ids=["e1", "e2"],
        evidence_requests=[{"file_path": "src/a.py", "start_line": 1, "end_line": 1}])
    executor = ReviewTaskExecutor(spec, item, [goal], active_evidence_ids={"e1", "e2"},
        evidence_catalog=evidence_catalog("e1", "e2"), request_id="q")
    payload = json.loads(TaskReviewV1Protocol(spec).prepare(item, [goal],
        active_evidence_ids={"e1", "e2"}, evidence_catalog=executor.evidence_catalog).prompt)
    assert payload["investigation_evidence_requirements"]["source_ranges"][0]["visible_evidence_ids"] == ["e1", "e2"]
    step = InspectTaskStep.__new__(InspectTaskStep)
    step.executor, step.repair_errors, step.rejected_items, step.read_observations = executor, [], [], {}
    raw = {"kind": "submit", "investigation": {"answer": "supported", "evidence_ids": ["e1"], "reason": "原文支持"}}
    first = step.apply_response(json.dumps(raw))
    assert first["correction_pending"] and step.rejected_items
    assert executor.projection.investigation is None
    step.repair_errors = []
    second = step.apply_response(json.dumps(raw))
    assert not second["correction_pending"] and second["errors"]
    raw["investigation"]["evidence_ids"] = ["e1", "e2"]
    assert not step.apply_response(json.dumps(raw))["errors"]
    assert executor.projection.investigation is not None
    executor.active_evidence_ids.remove("e2")
    assert executor.investigation_citation_readiness()["status"] == "required_evidence_not_visible"
    executor.active_evidence_ids.add("e2")
    item.evidence_requests[0].end_line = 2000
    assert executor.investigation_citation_readiness()["status"] == "requested_source_not_visible"
