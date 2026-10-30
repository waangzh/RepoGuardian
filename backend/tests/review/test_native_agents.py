"""Exercise the real create_agent loop against immutable Git fixtures."""

import json
import subprocess
import httpx

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.llm.providers import OpenAICompatibleProvider
from app.review.agents import build_agent
from app.review.context import AgentContext
from app.review.evidence import EvidenceService
from app.review.evidence import source_lines
from app.review.tools import read_base_file, read_diff, read_file, search_code
from langchain.tools import ToolRuntime
from app.review.models import DiscoveryOutput, ReviewLimits, ReviewSnapshot
from app.services.review.artifact_store import LocalArtifactStore
from app.tools.git_tool import GitTool


@pytest.fixture
def source_service(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True,
        ).stdout.decode().strip()

    git("init")
    for name, text in {"safe.py": "VALUE = 1\n", "other.py": "OTHER = 2\n",
                       "removed.py": "OLD = 3\n", ".env": "SECRET = hidden\n"}.items():
        (repo / name).write_text(text, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "safe.py").write_text("VALUE = 2\n", encoding="utf-8")
    git("rm", "removed.py")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "head")
    head = git("rev-parse", "HEAD")
    return EvidenceService(
        snapshot=ReviewSnapshot(review_id="review", repository="local/repo", pr_number=1,
                                base_sha=base, head_sha=head),
        repo_path=repo, allowed_paths=frozenset({"safe.py", "other.py", "removed.py", ".env"}),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"), git=GitTool(),
    )


class ToolCallingModel(BaseChatModel):
    """Deterministic provider responses, with actual framework tool dispatch."""

    @property
    def _llm_type(self):
        return "native-tool-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        responses = [m for m in messages if isinstance(m, ToolMessage)]
        if not responses:
            calls = [
                {"name": "read_file", "args": {"path": path, "start": 1, "end": 1}, "id": path}
                for path in ("safe.py", "other.py")
            ]
        elif len(responses) == 2:
            calls = [{"name": "read_file", "args": {"path": "../secret", "start": 1, "end": 1},
                      "id": "denied"}]
        else:
            calls = [{"name": "DiscoverySubmission",
                      "args": {"findings": [], "examined": [], "boundary_notes": ["越权文件读取被拒绝"]},
                      "id": "final"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


async def test_native_loop_parallel_reads_refusal_and_structured_response(source_service):
    agent = build_agent(ToolCallingModel(), "reviewer", ReviewLimits())
    result = await agent.ainvoke(
        {"messages": [("user", "Review this frozen change.")]},
        context=AgentContext(evidence=source_service),
    )
    assert isinstance(result["structured_response"], DiscoveryOutput)
    responses = [json.loads(m.content) for m in result["messages"]
                 if isinstance(m, ToolMessage) and m.name == "read_file"]
    assert len(responses) == 3
    assert all(item["ok"] for item in responses[:2]), responses
    assert responses[2] == {"ok": False, "error": "read_rejected",
                            "error_type": "ReviewPathPolicyError",
                            "truncated": False}
    for item in responses[:2]:
        ref = source_service.resolve(item["evidence"]["id"])
        assert ref.commit_sha == source_service.snapshot.head_sha


@pytest.mark.parametrize("path,start,end", [
    ("../safe.py", 1, 1), ("/safe.py", 1, 1), ("C:/safe.py", 1, 1),
    (".env", 1, 1), ("missing.py", 1, 1), ("safe.py", 0, 1),
    ("safe.py", 1, 999), ("safe.py", 2, 2),
])
async def test_source_refusal(source_service, path, start, end):
    with pytest.raises((ValueError, OSError)):
        await source_service.read(path, start, end)


async def test_frozen_source_base_deletion_and_evidence_integrity(source_service):
    (source_service.repo_path / "safe.py").write_text("DIRTY WORKTREE\n", encoding="utf-8")
    head = await source_service.read("safe.py", 1, 1)
    base = await source_service.read("safe.py", 1, 1, "base")
    deleted = await source_service.read("removed.py", 1, 1, "base")
    assert head["content"] == "VALUE = 2\n"
    assert base["content"] == "VALUE = 1\n"
    assert deleted["content"] == "OLD = 3\n"
    assert base["evidence"]["id"] != head["evidence"]["id"]
    with pytest.raises((ValueError, OSError)):
        source_service.resolve("0" * 64)
    identity = head["evidence"]["id"]
    archive = source_service.artifacts.root / "review" / f"evidence-{identity}.txt"
    archive.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupted"):
        source_service.resolve(identity)


async def test_source_size_limit(source_service):
    source_service.max_file_bytes = 1
    with pytest.raises(ValueError, match="too_large"):
        await source_service.read("safe.py", 1, 1)


async def test_native_call_limit_fails_explicitly(source_service):
    agent = build_agent(ToolCallingModel(), "reviewer", ReviewLimits(model_calls=1))
    with pytest.raises(Exception, match="[Mm]odel.*limit"):
        await agent.ainvoke({"messages": [("user", "Review.")]},
                            context=AgentContext(evidence=source_service))


def test_agent_model_has_no_forced_json_mode():
    provider = OpenAICompatibleProvider(api_key="fixture", base_url="https://example.test/v1",
                                         default_model="fixture")
    model = provider.agent_model()
    assert "response_format" not in model.model_kwargs
    assert model.max_retries == 0


async def test_sensitive_rename_excludes_previous_path_from_every_native_tool(source_service):
    context = AgentContext(evidence=source_service, denied_paths=frozenset({"safe.py"}),
        changed_files=[{"file_path": ".env", "old_file_path": "safe.py", "hunks": []}])
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda *_: None,
                          tool_call_id="test", store=None)
    for operation, args in (
        (read_file, {"path": "safe.py", "start": 1, "end": 1}),
        (read_base_file, {"path": "safe.py", "start": 1, "end": 1}),
        (read_diff, {"path": ".env"}),
        (search_code, {"query": "VALUE", "path": "safe.py"}),
    ):
        result = await operation.coroutine(**args, runtime=runtime)
        assert not result["ok"], (operation.name, result)


def test_source_line_identity_counts_only_lf():
    assert source_lines("value='a\u2028b'\r\nnext\r") == ["value='a\u2028b'\r\n", "next\r"]
    assert source_lines("") == []
    assert source_lines("\n") == ["\n"]


async def test_chatopenai_http_native_tools_and_structured_response(source_service):
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "response_format" not in payload
        assert payload["tools"]
        assert "runtime" not in json.dumps(payload["tools"])
        seen = [message for message in payload["messages"] if message["role"] == "tool"]
        if not seen:
            tool_calls = [{"id": "source", "type": "function", "function": {"name": "read_file",
                "arguments": json.dumps({"path": "safe.py", "start": 1, "end": 1})}}]
        else:
            assert json.loads(seen[0]["content"])["ok"]
            tool_calls = [{"id": "final", "type": "function", "function": {
                "name": "DiscoverySubmission", "arguments": json.dumps({"findings": [], "examined": []})}}]
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    provider = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture")
    model = provider.agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        from openai import AsyncOpenAI
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client = sdk
        model.async_client = sdk.chat.completions
        output = await build_agent(model, "reviewer", ReviewLimits()).ainvoke(
            {"messages": [("user", "Read the frozen source and submit.")]},
            context=AgentContext(evidence=source_service))
    assert len(requests) == 2
    assert isinstance(output["structured_response"], DiscoveryOutput)
