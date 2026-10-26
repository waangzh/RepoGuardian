from copy import deepcopy

import pytest

from app.evaluation.runtime_capability import freeze_capability_plan, investigation_increment


def inputs():
    baseline = dict(model="current", reasoning="off", dependency_docs=[], task_expression="trace", context=[])
    case = dict(case_id="operator", split="diagnostic", snapshot={"repository": "repo", "base_sha": "b", "head_sha": "h"},
                question="比较两个分支的值来源、下游行为、触发条件与未知前提", evidence=[{"content": "fixed source"}])
    return [case], baseline, {"reasoning": {**baseline, "reasoning": "on"}}


def test_freeze_fixed_evidence_and_single_variable_without_answers():
    cases, baseline, variants = inputs()
    plan = freeze_capability_plan(cases, baseline, variants)
    assert plan["comparisons"][0]["variable"] == "reasoning" and plan["provider_calls"] is False
    variants["reasoning"]["model"] = "other"
    with pytest.raises(ValueError, match="exactly one"):
        freeze_capability_plan(cases, baseline, variants)
    cases[0]["answer"] = "label"
    with pytest.raises(ValueError, match="unexpected fields"):
        freeze_capability_plan(cases, baseline, {})


def test_holdout_rejects_diagnostic_and_development_snapshot_overlap():
    cases, baseline, variants = inputs()
    holdout = deepcopy(cases[0])
    holdout.update(case_id="holdout", split="holdout")
    with pytest.raises(ValueError, match="overlaps"):
        freeze_capability_plan([*cases, holdout], baseline, variants)
    with pytest.raises(ValueError, match="overlaps"):
        freeze_capability_plan([holdout], baseline, variants, development_snapshots=[("repo", "b", "h")])


def test_increment_preserves_unknown_usage_and_disallows_self_reported_status():
    baseline = dict(sample_snapshot="fixed", configuration="same", adjudication_status="complete", blind=True,
                    defect_identity_status="independently_matched", valid_defect_ids=["old"], false_positive_ids=[],
                    tokens=100, usage_integrity="complete")
    investigated = {**baseline, "valid_defect_ids": ["new"], "tokens": None, "usage_integrity": "unknown"}
    report = investigation_increment(baseline, investigated)
    assert report["new_valid_defects"] == ["new"] and report["lost_valid_defects"] == ["old"]
    assert report["additional_tokens"] is None and report["usage_integrity"] == "unknown"
    investigated["defect_identity_status"] = "confirmed"
    with pytest.raises(ValueError, match="independently matched"):
        investigation_increment(baseline, investigated)
