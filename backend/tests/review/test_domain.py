"""New review contracts keep model claims separate from runtime authority."""

import pytest
from pydantic import ValidationError

from app.review.domain import CandidateInput, Options, UnitCompletion, VerificationOutput


def test_completion_cannot_claim_material_delivery():
    with pytest.raises(ValidationError):
        UnitCompletion(delivered_hunk_ids=["h1"])


def test_candidate_cannot_supply_runtime_identity():
    with pytest.raises(ValidationError):
        CandidateInput(claim="failure", trigger="input", impact="lost data",
                       source_refs=["S1"], attempt_id="forged")


def test_insufficient_is_a_terminal_verification_not_a_finding():
    result = VerificationOutput(verdict="insufficient", attribution="uncertain",
                                reason="Missing caller precondition")
    assert result.source_refs == []
    assert result.severity is None


def test_scope_uses_safe_normalized_paths():
    assert Options(paths=["src/b.py", "src/a.py", "src/b.py"]).paths == ["src/a.py", "src/b.py"]
    with pytest.raises(ValidationError):
        Options(paths=["../secret"])
