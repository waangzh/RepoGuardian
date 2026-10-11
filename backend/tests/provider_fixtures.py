"""显式支持 canonical 输入的图编排夹具，不代表真实模型检查质量。"""

from app.agents.providers import LLMProvider, OpenAICompatibleProvider
from app.models.review import ModelCallResult, UnitReviewResponse
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL, legacy_unit_input_allowed
from app.services.model_usage import unpack_model_call
from app.services.review_input_context import EVIDENCE_INPUT_VERSION


class CanonicalTestProvider(LLMProvider):
    unit_input_protocol = CANONICAL_UNIT_INPUT_PROTOCOL

    async def review_unit(self, pr, changed_files, diff_text, model, record_input):
        assert not legacy_unit_input_allowed.get(), "canonical fixture requires the canonical execution path"
        assert record_input["input_protocol"] == CANONICAL_UNIT_INPUT_PROTOCOL
        assert record_input["snapshot"]["input_version"] == EVIDENCE_INPUT_VERSION
        OpenAICompatibleProvider._unit_evidence_payload(changed_files, record_input)
        raw = await self.review(pr, changed_files, diff_text, model)
        issues, usage = unpack_model_call(raw)
        response = UnitReviewResponse(issues=issues)
        return ModelCallResult(response, usage) if usage else response
