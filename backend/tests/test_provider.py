import pytest

from app.agents.providers import LLMProviderError, MockProvider, OpenAICompatibleProvider, build_provider
from app.models.review import PullRequestInfo, PullRequestRef


def test_openai_provider_rejects_invalid_json() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        provider._parse_issues("not json")


def test_openai_provider_parses_json_object_with_issues() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """
    {
      "issues": [
        {
          "file_path": "app.py",
          "line_no": 10,
          "severity": "high",
          "category": "correctness",
          "title": "空值未处理",
          "description": "存在空值风险。",
          "suggestion": "增加空值检查。",
          "confidence": 0.8
        }
      ]
    }
    """

    issues = provider._parse_issues(content)

    assert len(issues) == 1
    assert issues[0].confidence == 0.8


def test_openai_provider_normalizes_confidence_labels() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """
    {
      "issues": [
        {
          "file_path": "app.py",
          "line_no": 10,
          "severity": "high",
          "category": "correctness",
          "title": "空值未处理",
          "description": "存在空值风险。",
          "suggestion": "增加空值检查。",
          "confidence": "high"
        },
        {
          "file_path": "app.py",
          "line_no": 20,
          "severity": "medium",
          "category": "test",
          "title": "缺少测试",
          "description": "新增逻辑缺少测试。",
          "suggestion": "补充测试。",
          "confidence": "medium"
        }
      ]
    }
    """

    issues = provider._parse_issues(content)

    assert issues[0].confidence == 0.85
    assert issues[1].confidence == 0.65


def test_openai_provider_normalizes_numeric_confidence_strings() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """
    {"issues":[{
      "file_path":"app.py",
      "line_no":1,
      "severity":"low",
      "category":"maintainability",
      "title":"命名不清晰",
      "description":"变量命名不清晰。",
      "suggestion":"改为更明确的名称。",
      "confidence":"75%"
    }]}
    """

    issues = provider._parse_issues(content)

    assert issues[0].confidence == 0.75


def test_openai_provider_accepts_legacy_issue_array() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """
    [{
      "file_path":"app.py",
      "line_no":1,
      "severity":"low",
      "category":"maintainability",
      "title":"命名不清晰",
      "description":"变量命名不清晰。",
      "suggestion":"改为更明确的名称。",
      "confidence":0.5
    }]
    """

    issues = provider._parse_issues(content)

    assert len(issues) == 1


def test_openai_provider_extracts_message_content() -> None:
    payload = {"choices": [{"message": {"content": "[]"}}]}

    assert OpenAICompatibleProvider._extract_message_content(payload) == "[]"


def test_openai_provider_rejects_missing_message_content() -> None:
    payload = {"choices": [{"message": {"content": None}}]}

    with pytest.raises(LLMProviderError, match="missing content"):
        OpenAICompatibleProvider._extract_message_content(payload)


def test_openai_provider_rejects_reasoning_without_final_content() -> None:
    payload = {"choices": [{"message": {"content": None, "reasoning_content": "analysis"}}]}

    with pytest.raises(LLMProviderError, match="reasoning_content"):
        OpenAICompatibleProvider._extract_message_content(payload)


def test_build_provider_supports_deepseek_alias() -> None:
    provider = build_provider("deepseek", "key", "https://api.deepseek.com", "deepseek-v4-pro")

    assert isinstance(provider, OpenAICompatibleProvider)


def test_deepseek_provider_disables_thinking() -> None:
    provider = build_provider("deepseek", "key", "https://api.deepseek.com", "deepseek-v4-pro")
    payload = provider._build_request_payload("prompt", None, "system", 100)

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}


def test_deepseek_base_url_disables_thinking_for_openai_compatible_provider() -> None:
    provider = build_provider(
        "openai-compatible",
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-pro",
    )
    payload = provider._build_request_payload("prompt", None, "system", 100)

    assert payload["thinking"] == {"type": "disabled"}


def test_openai_provider_does_not_send_thinking_parameter() -> None:
    provider = build_provider("openai", "key", "https://api.openai.com/v1", "gpt-4.1-mini")
    payload = provider._build_request_payload("prompt", None, "system", 100)

    assert "thinking" not in payload

def test_build_provider_normalizes_provider_name() -> None:
    provider = build_provider(" OpenAI-Compatible ", "key", "https://example.com", "model")

    assert isinstance(provider, OpenAICompatibleProvider)


def test_build_provider_supports_mock() -> None:
    provider = build_provider("mock", None, "https://example.com", "model")

    assert isinstance(provider, MockProvider)


@pytest.mark.asyncio
async def test_mock_provider_returns_scripted_agent_action() -> None:
    provider = MockProvider(action_sequence=[{"action": "run_static_analysis", "reason": "先跑工具"}])

    action = await provider.decide({}, None)

    assert action.action == "run_static_analysis"
    assert action.reason == "先跑工具"


def test_build_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="REPOGUARDIAN_PROVIDER"):
        build_provider("unknown", None, "https://example.com", "model")

def _sample_pr() -> PullRequestInfo:
    ref = PullRequestRef(
        ref="main", sha="abc123", repo_clone_url="https://github.com/owner/repo.git"
    )
    return PullRequestInfo(
        owner="owner",
        repo="repo",
        number=1,
        title="Test PR",
        html_url="https://github.com/owner/repo/pull/1",
        clone_url="https://github.com/owner/repo.git",
        base=ref,
        head=ref,
    )
