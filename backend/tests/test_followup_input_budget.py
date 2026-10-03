import hashlib
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.models.review import (
    AgentAction, ChangedFile, ExecutionBudget, IssueVerification, UnitReviewRecord,
    IssueVerificationRequest, IssueVerificationBudget,
)
from app.services.coordination_runtime import CoordinationRuntime
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.model_usage import model_request_budget_hook
from app.services.unit_review_summary import build_record_input, validate_record
from test_cross_unit_coordination import Provider, plan, state
from test_provider import fake_chat as fake_chat, _sample_pr
from test_issue_validation import _issue


def two_checks(payload, *, duplicate=False):
    proposed = plan(payload)
    second = proposed.followups[0].model_copy(update={"id": "f2", **({} if duplicate else {
        "question": "同一文件的租户过滤是否正确？", "counterevidence_goal": "核验租户隔离",
    })})
    return proposed.model_copy(update={"followups": [*proposed.followups, second]})


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_same_scope_keeps_distinct_checks_and_merges_only_identical_tasks(duplicate):
    class Checks(Provider):
        async def coordinate_cross_units(self, payload, model):
            self.calls.append("coordinate")
            return two_checks(payload, duplicate=duplicate)
    provider = Checks(candidate=False)
    result = await CrossUnitCoordinationService(provider).run(state())
    count = 1 if duplicate else 2
    assert len(result["coordination_plan"]["followups"]) == count
    assert len(result["followup_results"]) == count
    assert provider.calls.count("review") == count
    assert bool([warning for warning in result.get("warnings", []) if "完全重复补查已合并" in warning]) == duplicate
    if duplicate:
        assert any("f2 -> f1" in warning for warning in result["warnings"])


def test_plan_normalization_does_not_skip_invalid_references_or_merge_case_distinctions():
    payload = CrossUnitCoordinationService.catalog(state())
    proposed = two_checks(payload, duplicate=True)
    proposed.followups[1].evidence_ids = ["stale"]
    with pytest.raises(ValueError, match="current evidence"):
        CrossUnitCoordinationService.normalize_plan(proposed, payload, ExecutionBudget())
    proposed = two_checks(payload, duplicate=True)
    proposed.followups[0].question = "调用 Foo 是否安全？"
    proposed.followups[1].question = "调用 foo 是否安全？"
    normalized, warnings = CrossUnitCoordinationService.normalize_plan(
        proposed, payload, ExecutionBudget(max_model_calls=16, max_token_usage=120000))
    assert len(normalized.followups) == 2 and not warnings
    proposed.followups[1].id = proposed.followups[0].id
    with pytest.raises(ValidationError, match="unique"):
        CrossUnitCoordinationService.normalize_plan(proposed, payload, ExecutionBudget())


@pytest.mark.asyncio
async def test_dependency_reason_is_preserved_and_still_checks_scope_and_evidence(fake_chat):
    file = ChangedFile.model_validate({"file_path": "a.py", "change_type": "modified", "additions": 1,
        "deletions": 0, "hunks": [{"old_start": 1, "old_length": 0, "new_start": 1, "new_length": 1,
            "added_lines": [{"line_no": 1, "content": "return value"}]}]})
    catalog = build_record_input([file], [], None, "head", "base")
    raw = {"change_summary": "变更返回值", "contract_dependencies": [{"file_path": "a.py",
        "assumption": "调用方处理空值", "reason": "已经检查对应代码", "status": "verified",
        "evidence_ids": [catalog["evidence"][0]["id"]]}]}
    fake_chat.responses = [AIMessage(content=json.dumps({"issues": [], "review_record": raw}, ensure_ascii=False))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    response = await provider.review_unit(_sample_pr(), [file], "", None, catalog)
    assert not response.record_error
    dependency = response.review_record.contract_dependencies[0]
    assert dependency.assumption == "调用方处理空值\n检查说明：已经检查对应代码"
    assert validate_record(response.review_record, catalog, {"a.py"}).status == "reported"
    assert validate_record(response.review_record, catalog, set()).status == "unknown"
    dependency.evidence_ids = ["stale"]
    assert validate_record(response.review_record, catalog, {"a.py"}).status == "unknown"


@pytest.mark.parametrize("patch", [
    {"reason": 42}, {"reason": ""}, {"reason": "x" * 1001},
    {"assumption": None}, {"assumption": "x" * 1000}, {"server_status": "verified"},
])
def test_dependency_alias_rejects_invalid_or_unknown_fields(patch):
    raw = {"change_summary": "变更返回值", "contract_dependencies": [{"file_path": "a.py",
        "assumption": "依赖有效", "reason": "已核验", "status": "unresolved", **patch}]}
    with pytest.raises(ValidationError):
        UnitReviewRecord.model_validate(OpenAICompatibleProvider._normalize_review_record(raw))


def canonical_input():
    file = ChangedFile.model_validate({"file_path": "a.py", "change_type": "modified", "additions": 1,
        "deletions": 1, "hunks": [{"old_start": 1, "old_length": 1, "new_start": 1, "new_length": 1,
            "added_lines": [{"line_no": 1, "content": "ADDED_UNIQUE_" + "a" * 20000}],
            "removed_lines": [{"line_no": 1, "content": "REMOVED_UNIQUE"}]}]})
    context = [{"file": "dep.py", "start_line": 1, "end_line": 4,
                "content": "CONTEXT_UNIQUE_" + "c" * 2000}]
    catalog = build_record_input([file], context, None, "head", "base")
    catalog["readonly_context"] = context
    catalog["review_guidance"] = "核验返回契约"
    return file, catalog


def test_canonical_diagnosis_sends_body_once_preserving_base_and_context_and_reference_hash():
    file, catalog = canonical_input()
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    duplicate_diff = file.model_dump_json() + catalog["readonly_context"][0]["content"]
    prompt = provider._build_unit_review_prompt(_sample_pr(), [file], duplicate_diff, catalog)
    for marker in ("ADDED_UNIQUE_", "REMOVED_UNIQUE", "CONTEXT_UNIQUE_"):
        assert prompt.count(marker) == 1
    payload = json.loads(prompt.split("Bounded record input JSON:\n", 1)[1])
    diff, context = payload["evidence"]
    encoded = json.dumps(diff["body"], ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(encoded).hexdigest() == diff["content_hash"]
    assert diff["id"] == catalog["evidence"][0]["id"]
    assert diff["removed_lines"][0]["content"] == "REMOVED_UNIQUE"
    assert payload["readonly_context"][context["context_index"]]["content"] == catalog["readonly_context"][0]["content"]
    prefix = payload["readonly_context"][context["context_index"]]["content"][:context["context_chars"]]
    assert hashlib.sha256(prefix.encode()).hexdigest() == context["content_hash"]
    legacy = {key: value for key, value in catalog.items() if key not in {"readonly_context", "review_guidance"}}
    old_prompt = provider._build_unit_review_prompt(_sample_pr(), [file], duplicate_diff, legacy)
    assert len(prompt) < len(old_prompt) * 0.6


def test_canonical_input_does_not_offer_unseen_context_evidence():
    file, catalog = canonical_input()
    catalog["readonly_context"] = []
    with pytest.raises(ValueError, match="context_mismatch"):
        OpenAICompatibleProvider._unit_evidence_payload([file], catalog)


def test_canonical_input_rejects_reference_body_mismatch():
    file, catalog = canonical_input()
    catalog["evidence"][0]["content"] = catalog["evidence"][0]["content"].replace("ADDED_UNIQUE_", "other")
    with pytest.raises(ValueError, match="hunk_mismatch"):
        OpenAICompatibleProvider._unit_evidence_payload([file], catalog)


def test_context_reference_rejects_ambiguous_or_forged_hash():
    file, catalog = canonical_input()
    catalog["readonly_context"].append({**catalog["readonly_context"][0],
        "content": catalog["readonly_context"][0]["content"] + "different tail"})
    with pytest.raises(ValueError, match="context_mismatch"):
        OpenAICompatibleProvider._unit_evidence_payload([file], catalog)
    catalog["readonly_context"].pop()
    catalog["evidence"][1]["content_hash"] = "forged"
    with pytest.raises(ValueError, match="context_mismatch"):
        OpenAICompatibleProvider._unit_evidence_payload([file], catalog)


def test_missing_hunk_cannot_be_silently_omitted_from_canonical_input():
    file, catalog = canonical_input()
    catalog["evidence"] = catalog["evidence"][1:]
    with pytest.raises(ValueError, match="hunk_mismatch"):
        OpenAICompatibleProvider._unit_evidence_payload([file], catalog)


def test_diagnosis_contract_invalidates_unit_cache_and_old_checkpoint_namespace(monkeypatch):
    from app.services import fingerprints
    from app.graph.checkpointer import unit_thread_config
    kwargs = dict(base_sha="b", head_sha="h", normalized_unit_diff="diff", primary_files=["a.py"],
        related_files=[], rule_ids=[], rule_version="r", prompt_version="unchanged",
        tool_schema_version="t", planner_version="p", review_policy_version="policy",
        model="model", provider="provider")
    current = fingerprints.unit_fingerprint(**kwargs)
    monkeypatch.setattr(fingerprints, "DIAGNOSIS_INPUT_VERSION", "old-contract")
    assert fingerprints.unit_fingerprint(**kwargs) != current
    namespace = unit_thread_config("task", "u")["configurable"]["checkpoint_ns"]
    assert namespace != "unit:u"
    assert namespace.endswith("canonical-evidence-v2")


def test_verifier_sends_primary_and_supporting_anchors_once():
    issue = _issue("i")
    request = IssueVerificationRequest(issue=issue, primary_evidence=issue.primary_evidence,
        supporting_evidence=issue.supporting_evidence, unit_diff="[]",
        budget=IssueVerificationBudget(remaining_calls=2))
    prompt = OpenAICompatibleProvider._build_issue_verification_prompt(request)
    payload = json.loads(prompt.split("Bounded verifier input JSON:\n", 1)[1])
    assert "primary_evidence" not in payload["issue"] and "supporting_evidence" not in payload["issue"]
    assert payload["primary_evidence"]["existing_code"] == "return wrong"
    assert payload["primary_evidence"]["resolved_start_line"] == 10
    assert payload["issue"]["id"] == "i"


@pytest.mark.asyncio
async def test_exploration_cannot_consume_protected_verification_allowance():
    class Calls:
        calls = []
        def coordination_request_chars(self, name, args):
            return 40000 if name == "decide" else 4000
        async def decide(self, *args):
            self.calls.append("decide")
            return AgentAction(action="task_done", reason="完成")
        async def verify_issue(self, *args):
            self.calls.append("verify")
            return IssueVerification(issue_id="i", decision="needs_human", reason="缺证据")
    provider = Calls()
    runtime = CoordinationRuntime("task", "f", ExecutionBudget(max_model_calls=5, max_token_usage=18000))
    held = {"followup_id": "f1", "phase": "exploration", "model_calls": 2, "token_usage": 9000}
    with pytest.raises(LLMProviderError, match="budget_exhausted"):
        await runtime.call(provider, "decide", ({}, None), 0, holdback=held)
    assert provider.calls == [] and runtime.budget.token_usage == 0
    rejection = runtime.data["budget_rejections"][0]
    assert rejection["remaining_tokens"] == 18000 and rejection["available_tokens"] == 9000
    await runtime.call(provider, "verify_issue", ({}, None), 0,
                       holdback={"followup_id": "f1", "phase": "verification"})
    assert provider.calls == ["verify"] and runtime.budget.token_usage == 1000


@pytest.mark.asyncio
async def test_retry_also_respects_verification_and_future_followup_holdback():
    class Retry:
        def coordination_request_chars(self, name, args):
            return 24000
        async def decide(self, *args):
            await model_request_budget_hook.get()()
            raise AssertionError("重试额度不足时不应执行重试")
    runtime = CoordinationRuntime("task", "f", ExecutionBudget(max_token_usage=18000))
    held = {"followup_id": "f1", "phase": "exploration", "model_calls": 2, "token_usage": 9000}
    with pytest.raises(LLMProviderError, match="before_transport_retry"):
        await runtime.call(Retry(), "decide", ({}, None), 0, holdback=held)
    assert runtime.budget.model_calls == 1 and runtime.budget.token_usage == 6000
    assert runtime.data["budget_rejections"][0]["transport_retry"]
