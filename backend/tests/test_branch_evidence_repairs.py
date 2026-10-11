import json

import pytest

from app.evidence import DiffIndex, EvidenceResolver
from app.evidence.branch import branch_error
from app.models.review import EvidenceAnchor, EvidenceBranchClaim, IssueVerification, IssueVerificationRequest
from app.services.issue_verifier import IssueVerifierService
from app.tools.diff_parser import DiffParser
from test_issue_validation import _issue, _unit


SOURCE = '''def forward(directml):
    if directml:
        mask = 0
    else:
        mask = float("-inf")
    return mask
'''


@pytest.mark.parametrize("arm,expected", [("if", "branch_claim_mismatch"), ("else", None)])
def test_comfy_style_else_assignment_cannot_be_attributed_to_if(arm, expected):
    claim = EvidenceBranchClaim(condition_code="directml", arm=arm)
    assert branch_error(SOURCE, 5, 5, claim) == expected
    lines = SOURCE.splitlines()
    diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,6 @@\n" + "\n".join("+" + line for line in lines) + "\n"
    resolver = EvidenceResolver(DiffIndex(DiffParser().parse(diff)), file_loader=lambda *_: SOURCE)
    anchor = EvidenceAnchor(file_path="app.py", existing_code=lines[4], branch_claim=claim)
    result = resolver.resolve_anchor(anchor)
    assert result.unresolved_reason == expected
    if expected is None:
        assert result.resolved_start_line == 5 and result.resolved_side == "head"


def test_unclaimed_conditional_evidence_stays_unresolved():
    assert branch_error(SOURCE, 5, 5, None) == "branch_claim_required"
    assert branch_error(SOURCE, 6, 6, None) is None


@pytest.mark.parametrize("source,condition", [("if (", "directml"), (SOURCE, "directml()(")])
def test_unparseable_branch_claim_does_not_gain_trust(source, condition):
    assert branch_error(source, 1, 1, EvidenceBranchClaim(condition_code=condition, arm="if")) == "branch_claim_unverified"


@pytest.mark.parametrize("arm,valid", [("if", False), ("else", True)])
def test_verifier_checks_branch_from_displayed_body(arm, valid):
    issue, unit = _issue("i"), _unit()
    lines = SOURCE.splitlines()
    request = IssueVerificationRequest(issue=issue, primary_evidence=issue.primary_evidence,
        budget={"remaining_calls": 1},
        unit_diff=json.dumps([{"file_path": "app.py", "hunks": [{"lines": [
            {"kind": "added", "content": line} for line in lines]}]}]))
    verification = IssueVerification(issue_id="i", decision="drop", reason="引用对应分支",
        contradicting_evidence=[{"file_path": "app.py", "existing_code": lines[4],
            "branch_claim": {"condition_code": "directml", "arm": arm}}])
    if valid:
        IssueVerifierService._validate_decision(issue, unit, verification, request)
    else:
        with pytest.raises(ValueError, match="verifier_branch_claim_unverified"):
            IssueVerifierService._validate_decision(issue, unit, verification, request)


def test_nested_branch_and_entire_conditional_have_distinct_claims():
    source = "if enabled:\n    if directml:\n        x = 1\n    else:\n        x = 2\n"
    assert branch_error(source, 5, 5, EvidenceBranchClaim(condition_code="directml", arm="else")) is None
    assert branch_error(source, 5, 5, EvidenceBranchClaim(condition_code="directml", arm="if")) == "branch_claim_mismatch"
    assert branch_error(source, 1, 5, EvidenceBranchClaim(condition_code="enabled", arm="both")) is None


def test_else_starting_with_nested_if_keeps_outer_branch_scope():
    source = "if enabled:\n    x = 1\nelse:\n    if directml:\n        x = 2\n"
    assert branch_error(source, 5, 5, EvidenceBranchClaim(condition_code="enabled", arm="else")) is None
    assert branch_error(source, 5, 5, EvidenceBranchClaim(condition_code="enabled", arm="if")) == "branch_claim_mismatch"


def test_elif_uses_its_own_condition_claim():
    source = "if enabled:\n    x = 1\nelif directml:\n    x = 2\n"
    assert branch_error(source, 4, 4, EvidenceBranchClaim(condition_code="directml", arm="if")) is None
    assert branch_error(source, 4, 4, EvidenceBranchClaim(condition_code="enabled", arm="else")) == "branch_claim_mismatch"
