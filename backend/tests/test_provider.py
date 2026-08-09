import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider, build_provider
from app.models.review import (
    ChangedFile,
    IssueVerificationBudget,
    IssueVerificationRequest,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
)


class FakeChatOpenAI:
    responses: list[AIMessage | Exception] = []
    instances: list["FakeChatOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.messages: list[SystemMessage | HumanMessage] = []
        type(self).instances.append(self)

    async def ainvoke(self, messages: list[SystemMessage | HumanMessage]) -> AIMessage:
        self.messages = messages
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> type[FakeChatOpenAI]:
    FakeChatOpenAI.responses = []
    FakeChatOpenAI.instances = []
    monkeypatch.setattr("app.agents.providers.ChatOpenAI", FakeChatOpenAI)
    return FakeChatOpenAI


@pytest.mark.asyncio
async def test_provider_constructs_chatopenai_with_json_mode_and_model_override(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"action":"finish_report","reason":"完成"}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "default-model")

    action = await provider.decide({}, "override-model")

    assert action.action == "finish_report"
    chat = fake_chat.instances[0]
    assert chat.kwargs == {
        "api_key": "key",
        "base_url": "https://example.com/v1",
        "model": "override-model",
        "temperature": 0.1,
        "max_tokens": 1200,
        "model_kwargs": {"response_format": {"type": "json_object"}},
    }
    assert isinstance(chat.messages[0], SystemMessage)
    assert isinstance(chat.messages[1], HumanMessage)


@pytest.mark.asyncio
async def test_provider_summarizes_english_pr_purpose_in_chinese(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"修复区间统计，使点估计始终位于区间内。"}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    summary = await provider.summarize_pr_purpose(
        _sample_pr(),
        [ChangedFile(file_path="stats.py", change_type="modified", additions=3, deletions=1)],
        None,
    )

    assert summary == "修复区间统计，使点估计始终位于区间内。"
    chat = fake_chat.instances[0]
    assert chat.kwargs["max_tokens"] == 700
    assert "必须使用简体中文" in chat.messages[1].content


@pytest.mark.asyncio
async def test_provider_rejects_pr_purpose_without_chinese(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"Keep the estimate in the interval."}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="PR purpose summary schema validation failed"):
        await provider.summarize_pr_purpose(_sample_pr(), [], None)


@pytest.mark.asyncio
async def test_deepseek_passes_thinking_control_via_extra_body(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"action":"finish_report","reason":"完成"}')]
    provider = build_provider("deepseek", "key", "https://api.deepseek.com", "deepseek-model")

    await provider.decide({}, None)

    options = fake_chat.instances[0].kwargs
    assert options["extra_body"] == {"thinking": {"type": "disabled"}}
    assert options["model_kwargs"] == {"response_format": {"type": "json_object"}}
    assert "thinking" not in options


@pytest.mark.asyncio
async def test_openai_compatible_deepseek_endpoint_disables_thinking(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"action":"finish_report","reason":"完成"}')]
    provider = build_provider("openai-compatible", "key", "https://api.deepseek.com", "deepseek-model")

    await provider.decide({}, None)

    assert fake_chat.instances[0].kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_provider_parses_decision_review_and_patch_results(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [
        AIMessage(content='{"action":"review_code","reason":"审查变更"}'),
        AIMessage(content="""{"issues":[{"line_no":10,
            "severity":"high","category":"correctness","title":"空值未处理",
            "failure_scenario":"存在空值风险。","recommendation":"增加空值检查。",
            "confidence":"high","primary_evidence":{"file_path":"app.py",
            "existing_code":"value = item.name"},"affected_behavior":"空输入可能失败。"}]}"""),
        AIMessage(content='{"patches":[{"diff_content":"diff --git a/app.py b/app.py","status":"generated"}]}'),
    ]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    action = await provider.decide({}, None)
    issues = await provider.review(
        _sample_pr(),
        [ChangedFile(file_path="app.py", change_type="modified", additions=1, deletions=0)],
        "diff --git a/app.py b/app.py",
        None,
    )
    patches = await provider.generate_patch(
        {"review_issues": [issues[0].model_dump(mode="json")]},
        None,
    )

    assert action.action == "review_code"
    assert issues[0].confidence == 0.85
    assert issues[0].primary_evidence.resolved_start_line is None
    assert patches[0].diff_content == "diff --git a/app.py b/app.py"
    assert [instance.kwargs["max_tokens"] for instance in fake_chat.instances] == [1200, 4096, 4096]


@pytest.mark.asyncio
async def test_provider_rejects_invalid_json(fake_chat: type[FakeChatOpenAI]) -> None:
    fake_chat.responses = [AIMessage(content="not json")]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="not valid JSON"):
        await provider.decide({}, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_plan, expected_files",
    [
        ({"files": ["chainladder/core/tests/test_triangle.py"]},
         ["chainladder/core/tests/test_triangle.py"]),
        ({"file_requests": [{"file": "chainladder/core/triangle.py",
                              "reason": "读取相关实现"}]},
         ["chainladder/core/triangle.py"]),
    ],
)
async def test_provider_normalizes_observed_retrieval_plan_aliases(
    fake_chat: type[FakeChatOpenAI],
    legacy_plan: dict[str, Any],
    expected_files: list[str],
) -> None:
    fake_chat.responses = [AIMessage(content=json.dumps({
        "action": "retrieve_context",
        "reason": "需要补充上下文",
        "tool_args": {"plan": legacy_plan},
    }, ensure_ascii=False))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    action = await provider.decide({"phase": "discovery", "unit_agent": True}, None)

    assert action.tool_args["plan"]["target_files"] == expected_files
    assert action.tool_args["plan"]["reason"] == "需要补充上下文"
    assert action.tool_args["plan"]["relevance_types"] == ["direct"]


def test_unit_decision_prompt_documents_exact_retrieval_plan_schema() -> None:
    prompt = OpenAICompatibleProvider._build_decision_prompt({
        "phase": "discovery",
        "unit_agent": True,
    })

    assert '"target_files":[]' in prompt
    assert '"relevance_types":["direct"]' in prompt
    assert "never use files or file_requests" in prompt


@pytest.mark.asyncio
async def test_provider_wraps_chatopenai_failures(fake_chat: type[FakeChatOpenAI]) -> None:
    fake_chat.responses = [RuntimeError("network unavailable")]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="LLM request failed"):
        await provider.decide({}, None)


def test_provider_rejects_non_string_ai_message_content() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="missing string content"):
        provider._extract_message_content(AIMessage(content=[{"type": "text", "text": "{}"}]))


def test_openai_provider_parses_json_object_with_issues() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """{"issues":[{"line_no":10,"severity":"high",
    "category":"correctness","title":"空值未处理","failure_scenario":"存在空值风险。",
    "recommendation":"增加空值检查。","confidence":0.8,
    "primary_evidence":{"file_path":"app.py","existing_code":"value = item.name"},
    "affected_behavior":"空输入可能失败。"}]}"""

    issues = provider._parse_issues(content)

    assert len(issues) == 1
    assert issues[0].confidence == 0.8


def test_openai_provider_normalizes_confidence_values() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    content = """{"issues":[
    {"line_no":10,"severity":"high","category":"correctness",
    "title":"空值未处理","failure_scenario":"存在空值风险。","recommendation":"增加空值检查。",
    "confidence":"high","primary_evidence":{"file_path":"app.py","existing_code":"value = item.name"},
    "affected_behavior":"空输入可能失败。"},
    {"line_no":20,"severity":"medium","category":"test",
    "title":"缺少测试","failure_scenario":"新增逻辑缺少测试。","recommendation":"补充测试。",
    "confidence":"75%","primary_evidence":{"file_path":"app.py","existing_code":"return value"},
    "affected_behavior":"回归无法验证。"}]}"""

    issues = provider._parse_issues(content)

    assert [issue.confidence for issue in issues] == [0.85, 0.75]


def test_openai_provider_rejects_invalid_json() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        provider._parse_issues("not json")


def test_build_provider_supports_expected_names() -> None:
    for name in ("openai", "deepseek", "openai-compatible", " OpenAI-Compatible "):
        assert isinstance(build_provider(name, "key", "https://example.com/v1", "model"), OpenAICompatibleProvider)


def test_build_provider_rejects_unknown_provider() -> None:
    for name in ("unknown", "mock"):
        with pytest.raises(ValueError, match="REPOGUARDIAN_PROVIDER"):
            build_provider(name, None, "https://example.com", "model")


@pytest.mark.asyncio
async def test_issue_verifier_uses_strict_bounded_schema(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    issue = ReviewIssue(
        id="issue-1",
        review_unit_id="unit-1",
        title="错误",
        category="correctness",
        severity="medium",
        confidence=0.9,
        affected_behavior="返回错误结果",
        failure_scenario="有效输入会返回错误值",
        recommendation="修复条件",
        primary_evidence={"file_path": "app.py", "existing_code": "return wrong"},
    )
    request = IssueVerificationRequest(
        issue=issue,
        primary_evidence=issue.primary_evidence,
        unit_diff="[]",
        budget=IssueVerificationBudget(remaining_calls=1),
    )
    fake_chat.responses = [AIMessage(content=(
        '{"issue_id":"issue-1","decision":"keep","reason":"证据成立",'
        '"contradicting_evidence":[],"adjusted_severity":null}'
    ))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    decision = await provider.verify_issue(request, None)

    assert decision.decision == "keep"
    assert fake_chat.instances[0].kwargs["max_tokens"] == 1200
    prompt = fake_chat.instances[0].messages[1].content
    assert "cannot add an issue" in prompt
    assert "execute code" in prompt


@pytest.mark.asyncio
async def test_issue_verifier_rejects_extra_output_fields(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    issue = ReviewIssue(
        id="issue-1",
        review_unit_id="unit-1",
        title="错误",
        category="correctness",
        severity="medium",
        confidence=0.9,
        affected_behavior="返回错误结果",
        failure_scenario="有效输入会返回错误值",
        recommendation="修复条件",
        primary_evidence={"file_path": "app.py", "existing_code": "return wrong"},
    )
    request = IssueVerificationRequest(
        issue=issue,
        primary_evidence=issue.primary_evidence,
        unit_diff="[]",
        budget=IssueVerificationBudget(remaining_calls=1),
    )
    fake_chat.responses = [AIMessage(content=(
        '{"issue_id":"issue-1","decision":"keep","reason":"证据成立",'
        '"contradicting_evidence":[],"adjusted_severity":null,"issues":[]}'
    ))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="schema validation failed"):
        await provider.verify_issue(request, None)


@pytest.mark.asyncio
async def test_issue_verifier_preserves_decision_when_reason_is_too_long(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    issue = ReviewIssue(
        id="issue-1",
        review_unit_id="unit-1",
        title="错误",
        category="correctness",
        severity="medium",
        confidence=0.9,
        affected_behavior="返回错误结果",
        failure_scenario="有效输入会返回错误值",
        recommendation="修复条件",
        primary_evidence={"file_path": "app.py", "existing_code": "return wrong"},
    )
    request = IssueVerificationRequest(
        issue=issue,
        primary_evidence=issue.primary_evidence,
        unit_diff="[]",
        budget=IssueVerificationBudget(remaining_calls=1),
    )
    long_reason = "分析" * 1_100 + "最终结论：证据成立"
    fake_chat.responses = [AIMessage(content=json.dumps({
        "issue_id": "issue-1",
        "decision": "keep",
        "reason": long_reason,
        "contradicting_evidence": [],
        "adjusted_severity": None,
    }, ensure_ascii=False))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    decision = await provider.verify_issue(request, None)

    assert decision.decision == "keep"
    assert len(decision.reason) <= 2_000
    assert decision.reason.startswith("分析")
    assert decision.reason.endswith("最终结论：证据成立")
    assert "verifier reason truncated" in decision.reason


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
