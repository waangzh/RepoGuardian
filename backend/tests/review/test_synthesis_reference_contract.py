import json
from copy import deepcopy
from hashlib import sha256

import pytest

from app.models.review import PullRequestInfo, PullRequestRef
from app.models.review_synthesis import (
    ReviewSynthesisArtifact, ReviewSynthesisDraft, ReviewSynthesisInput,
    ReviewSynthesisOutput, SynthesisSource,
)
from app.core.fingerprints import stable_hash
from app.services.publication.publication_planner import PublicationPlanner
from app.services.review.synthesis.review_synthesis_input import SOURCE_POLICY_VERSION, build_synthesis_input
from app.services.review.synthesis.review_synthesis_repository import SynthesisStore
from app.services.review.synthesis.review_synthesis_service import bound_synthesis, freeze_review_synthesis
from app.services.review.synthesis.review_synthesis_validator import (
    SynthesisReferenceError, synthesis_output_schema, validate_review_synthesis,
)
from tests.support.persistence import persistence as persistence
from tests.review.test_review_synthesis import draft_for, stub_provider, synthesis_task


CODE_REF = "diff:4f304ba8a9b66694db6ec09f3309149bfd04c353ba374f2d784b0e3632f38fd2"
TEST_REF = "diff:8abb4d0b118f320e1bb915f0a7f5743a7706c5e780bdd2c4c5a4b8762251635e"
OBSERVED = {"diff_observation", "test_source_observation", "verified_observation"}


def reference_facts():
    return ReviewSynthesisInput(review_id="offline-reference-check", head_sha="a" * 40,
        effective_base_sha="b" * 40, sources=[
            SynthesisSource(id=CODE_REF, kind="diff_observation", text="lo = min(lo, point)"),
            SynthesisSource(id=TEST_REF, kind="test_source_observation", text="assert low <= point <= high"),
            SynthesisSource(id="pr-description:1", kind="author_claim", text="让区间包含点估计。"),
            SynthesisSource(id="observation:implementation", kind="verified_observation", text="扩展区间边界。"),
            SynthesisSource(id="boundary:review", kind="review_boundary", text="不描述测试运行结果。"),
            SynthesisSource(id="finding:key-a", kind="verified_finding", text="已经核验的问题。"),
        ])


def reference_draft():
    return {"schema_version": "review-synthesis-v2",
        "change_summary": {"text": "扩展区间边界，使其包含点估计。", "source_refs": [CODE_REF]}}


@pytest.mark.parametrize("field", [
    "change_summary", "key_changes", "code_inferred", "author_claim", "test_changes",
])
@pytest.mark.parametrize("source_index", range(6))
def test_field_reference_matrix_preserves_server_trust_boundary(field, source_index):
    facts = reference_facts()
    source = facts.sources[source_index]
    draft = reference_draft()
    item = {"text": "扩展区间边界。", "source_refs": [source.id]}
    if field in {"code_inferred", "author_claim"}:
        draft["pr_purpose"] = {**item, "source_kind": field}
        allowed = {"author_claim"} if field == "author_claim" else OBSERVED
        path = "pr_purpose"
    elif field in {"key_changes", "test_changes"}:
        draft[field] = [item]
        allowed = {"test_source_observation"} if field == "test_changes" else OBSERVED
        path = f"{field}[0]"
    else:
        draft[field] = item
        allowed, path = OBSERVED, field
    if source.kind in allowed:
        assert validate_review_synthesis(facts, draft)
    else:
        with pytest.raises(SynthesisReferenceError, match="^synthesis_reference_not_allowed$") as error:
            validate_review_synthesis(facts, draft)
        diagnostic = error.value.diagnostic
        assert diagnostic.field_path == f"{path}.source_refs[0]"
        assert diagnostic.reason == "source_kind_not_allowed"
        assert diagnostic.source_ref == source.id
        assert diagnostic.actual_kind == source.kind
        assert set(diagnostic.allowed_kinds) == allowed


def test_request_schema_scopes_each_field_and_purpose_kind_to_current_sources():
    facts = reference_facts()
    before = facts.model_dump(mode="json")
    generic = ReviewSynthesisOutput.model_json_schema()
    schema = synthesis_output_schema(facts)
    observed_ids = {source.id for source in facts.sources if source.kind in OBSERVED}
    cited = schema["$defs"]["CitedText"]["properties"]["source_refs"]
    assert set(cited["items"]["enum"]) == observed_ids
    variants = {item["properties"]["source_kind"]["const"]: item
                for item in schema["$defs"]["SynthesisPurpose"]["anyOf"]}
    for kind, allowed in [("author_claim", {"pr-description:1"}), ("code_inferred", observed_ids)]:
        assert set(variants[kind]["properties"]["source_refs"]["items"]["enum"]) == allowed
        assert "source_kind" in variants[kind]["required"]
    assert variants["unknown"]["properties"]["text"]["const"] == ""
    assert variants["unknown"]["properties"]["source_refs"]["maxItems"] == 0
    assert "enum" not in variants["unknown"]["properties"]["source_refs"]["items"]
    assert schema["properties"]["change_summary"]["$ref"] == "#/$defs/CitedText"
    assert schema["properties"]["key_changes"]["items"]["$ref"] == "#/$defs/CitedText"
    assert facts.model_dump(mode="json") == before
    assert ReviewSynthesisOutput.model_json_schema() == generic


@pytest.mark.parametrize("source_kinds", [set(), {"author_claim"}, {"test_source_observation"}])
def test_schema_without_observations_or_author_has_no_empty_enum_or_unavailable_purpose(source_kinds):
    facts = reference_facts()
    facts.sources = [source for source in facts.sources if source.kind in source_kinds]
    schema = synthesis_output_schema(facts)
    variants = {item["properties"]["source_kind"]["const"]: item
                for item in schema["$defs"]["SynthesisPurpose"]["anyOf"]}
    assert ("author_claim" in variants) == ("author_claim" in source_kinds)
    assert ("code_inferred" in variants) == ("test_source_observation" in source_kinds)
    assert "unknown" in variants
    if "test_source_observation" not in source_kinds:
        refs = schema["$defs"]["CitedText"]["properties"]["source_refs"]
        assert refs["maxItems"] == 0 and "enum" not in refs["items"]
    assert '"enum": []' not in json.dumps(schema)


def test_dynamic_schema_does_not_reuse_source_ids_from_another_bundle():
    first = reference_facts()
    second = first.model_copy(deep=True)
    second.sources[0].id = "diff:another-snapshot"
    second_schema = json.dumps(synthesis_output_schema(second))
    assert CODE_REF not in second_schema and "diff:another-snapshot" in second_schema
    assert CODE_REF in json.dumps(synthesis_output_schema(first))


def test_mixed_refs_report_first_disallowed_reference_at_its_actual_index():
    facts = reference_facts()
    draft = reference_draft()
    draft["key_changes"] = [deepcopy(draft["change_summary"]), {
        "text": "区间仅放宽。", "source_refs": [CODE_REF, "pr-description:1", "invented"]}]
    with pytest.raises(SynthesisReferenceError) as error:
        validate_review_synthesis(facts, draft)
    assert error.value.diagnostic.field_path == "key_changes[1].source_refs[1]"
    assert error.value.diagnostic.source_ref == "pr-description:1"


@pytest.mark.parametrize("kind,refs", [
    ("author_claim", ["pr-description:1"]),
    ("code_inferred", [CODE_REF, TEST_REF]),
    ("unknown", []),
])
def test_purpose_variants_accept_valid_references(kind, refs):
    draft = reference_draft()
    draft["pr_purpose"] = {"source_kind": kind, "text": "让区间包含点估计。" if refs else "", "source_refs": refs}
    assert validate_review_synthesis(reference_facts(), draft)


@pytest.mark.parametrize("case", ["author", "mixed", "purpose", "unknown", "long_unknown"])
@pytest.mark.asyncio
async def test_reference_failure_is_safe_persisted_and_never_resends(persistence, caplog, case):
    repo, _, _ = persistence
    task = synthesis_task()
    task.pr = PullRequestInfo(owner="o", repo="r", number=1, title="调整区间边界", body="让区间包含点估计。",
        html_url="https://github.com/o/r/pull/1", clone_url="https://github.com/o/r.git",
        base=PullRequestRef(ref="main", sha="b" * 40, repo_clone_url="https://github.com/o/r.git"),
        head=PullRequestRef(ref="feature", sha="a" * 40, repo_clone_url="https://github.com/o/r.git"))
    before = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, before)
    ref = next(source.id for source in facts.sources if source.kind == "diff_observation")
    unknown = "Authorization: Bearer private-model-data\n\u202e" + ("x" * 8000 if case == "long_unknown" else "")

    def change(draft):
        if case == "purpose":
            draft["pr_purpose"] = {"source_kind": "author_claim", "text": "作者希望调整边界。", "source_refs": [ref]}
        else:
            refs = ([ref, "pr-description:1"] if case == "mixed" else
                    [unknown] if case in {"unknown", "long_unknown"} else ["pr-description:1"])
            draft["change_summary"]["source_refs"] = refs

    repo.create_task(task)
    provider, calls = stub_provider(change=change)
    artifact = await freeze_review_synthesis(task, repo, provider)
    assert artifact.status == "fallback" and artifact.synthesis is None
    assert artifact.failure_code == "synthesis_reference_not_allowed"
    diagnostic = artifact.reference_failure
    field = "pr_purpose" if case == "purpose" else "change_summary"
    assert diagnostic.field_path == f"{field}.source_refs[{1 if case == 'mixed' else 0}]"
    if case in {"unknown", "long_unknown"}:
        assert diagnostic.reason == "unknown_source"
        assert diagnostic.source_ref is None and diagnostic.actual_kind is None
        assert diagnostic.source_ref_hash == sha256(unknown.encode("utf-8")).hexdigest()
    else:
        assert diagnostic.reason == "source_kind_not_allowed"
        assert diagnostic.source_ref == (ref if case == "purpose" else "pr-description:1")
        assert diagnostic.actual_kind == ("diff_observation" if case == "purpose" else "author_claim")
    restored = repo.get_task(task.id)
    saved = restored.review_synthesis
    assert saved["reference_failure"] == diagnostic.model_dump(mode="json")
    assert saved["usage"]["actual_total_tokens"] == 50
    assert saved["budget"]["calls_consumed"] == 1
    assert saved["budget"]["settled_actual"] == 50
    assert "private-model-data" not in json.dumps(saved) and "private-model-data" not in caplog.text
    assert diagnostic.field_path in caplog.text
    assert diagnostic.reason in caplog.text
    assert "review_synthesis" not in restored.model_dump(mode="json")
    plan = PublicationPlanner().build(restored)
    assert plan.outcome == before.outcome
    assert plan.presentation.synthesis_status == "fallback"
    assert plan.presentation.summary_source == "metadata"
    assert plan.presentation.validation_state == "not_executed"
    assert "private-model-data" not in plan.summary_markdown
    again = await freeze_review_synthesis(restored, repo, provider)
    assert again.reference_failure == diagnostic and len(calls) == 1
    prompt = calls[0][1].content
    schema = json.loads(prompt.split("Schema:\n", 1)[1].split("\nFact Bundle:\n", 1)[0])
    assert "pr-description:1" not in schema["$defs"]["CitedText"]["properties"]["source_refs"]["items"]["enum"]
    assert ref in schema["$defs"]["CitedText"]["properties"]["source_refs"]["items"]["enum"]
    assert "不得引用 author_claim、verified_finding 或 review_boundary" in prompt
    assert "不能引用审查阶段的 evidence ID" in prompt
    assert saved["usage"]["response_metadata"]["request_admission"]["prompt_chars"] >= len(prompt)


@pytest.mark.parametrize("prompt_version", [f"review-synthesis-prompt-v{index}" for index in range(1, 7)])
def test_old_artifact_without_diagnostics_remains_byte_equivalent_and_readable(prompt_version):
    task = synthesis_task()
    plan = PublicationPlanner().build(task)
    facts = build_synthesis_input(task, plan)
    artifact = ReviewSynthesisArtifact(prompt_version=prompt_version,
        source_policy_version=SOURCE_POLICY_VERSION, review_id=task.id,
        head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")), status="ready",
        synthesis=ReviewSynthesisDraft.model_validate(draft_for(facts)),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons,
        omitted_files=facts.omitted_files, omitted_hunks=facts.omitted_hunks)
    saved = artifact.model_dump(mode="json")
    assert "reference_failure" not in saved
    task.review_synthesis = deepcopy(saved)
    assert bound_synthesis(task, plan).model_dump(mode="json") == saved
    assert task.review_synthesis == saved


@pytest.mark.asyncio
async def test_existing_v6_failure_is_not_replaced_or_resent_by_v7(persistence):
    repo, _, _ = persistence
    task = synthesis_task()
    repo.create_task(task)
    facts = build_synthesis_input(task, PublicationPlanner().build(task))
    old = ReviewSynthesisArtifact(prompt_version="review-synthesis-prompt-v6",
        source_policy_version=SOURCE_POLICY_VERSION, review_id=task.id,
        head_sha=facts.head_sha, effective_base_sha=facts.effective_base_sha,
        fact_bundle_hash=stable_hash(facts.model_dump(mode="json")),
        failure_code="synthesis_reference_not_allowed")
    store = SynthesisStore(repo, task.id)
    assert store.claim(old)[0]
    store.finish(old)
    provider, calls = stub_provider()
    restored = await freeze_review_synthesis(task, repo, provider)
    assert restored.model_dump(mode="json") == old.model_dump(mode="json")
    assert restored.reference_failure is None and calls == []
    assert repo.get_task(task.id).review_synthesis["prompt_version"] == "review-synthesis-prompt-v6"
