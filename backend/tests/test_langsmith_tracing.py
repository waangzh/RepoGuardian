from contextlib import contextmanager
from typing import Any

import pytest

from app.models.review import ReviewTask
from app.services import review_service
from app.services.report_service import ReportService
from app.services.review_service import ReviewService


class FakeCompiledGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, config))
        return {}


class FakeGraph:
    def __init__(self, compiled: FakeCompiledGraph) -> None:
        self._compiled = compiled

    def compile(self) -> FakeCompiledGraph:
        return self._compiled


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).instances.append(self)


class FakeTracer:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def graph_service(monkeypatch: pytest.MonkeyPatch) -> tuple[ReviewService, FakeCompiledGraph]:
    compiled = FakeCompiledGraph()
    monkeypatch.setattr(review_service, "build_review_graph", lambda phase: FakeGraph(compiled))
    service = ReviewService(
        github_tool=object(),  # type: ignore[arg-type]
        git_tool=object(),  # type: ignore[arg-type]
        diff_parser=object(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        report_service=ReportService(),
    )
    task = ReviewTask(id="task-123", pr_url="https://example.test/pull/1", model="override-model")
    service._tasks[task.id] = task
    monkeypatch.setattr(service, "_sync_result_to_task", lambda task, result: None)
    return service, compiled


@pytest.fixture
def fake_tracing(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []

    @contextmanager
    def fake_tracing_context(**kwargs: Any):
        contexts.append(kwargs)
        yield

    FakeClient.instances = []
    monkeypatch.setattr(review_service, "Client", FakeClient)
    monkeypatch.setattr(review_service, "LangChainTracer", FakeTracer)
    monkeypatch.setattr(review_service, "tracing_context", fake_tracing_context)
    return contexts


@pytest.mark.asyncio
async def test_langsmith_disabled_does_not_block_graph_execution(
    graph_service: tuple[ReviewService, FakeCompiledGraph],
    fake_tracing: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, compiled = graph_service
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_tracing", False)
    monkeypatch.setattr(review_service.settings, "langsmith_api_key", None)

    await service._run_graph("task-123")

    assert len(compiled.calls) == 1
    assert "callbacks" not in compiled.calls[0][1]
    assert fake_tracing == [{"enabled": False}]
    assert FakeClient.instances == []


@pytest.mark.asyncio
async def test_missing_langsmith_key_does_not_block_graph_execution(
    graph_service: tuple[ReviewService, FakeCompiledGraph],
    fake_tracing: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, compiled = graph_service
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_tracing", True)
    monkeypatch.setattr(review_service.settings, "langsmith_api_key", None)

    await service._run_graph("task-123")

    assert len(compiled.calls) == 1
    assert fake_tracing == [{"enabled": False}]


@pytest.mark.asyncio
async def test_langsmith_initialization_failure_does_not_block_graph_execution(
    graph_service: tuple[ReviewService, FakeCompiledGraph],
    fake_tracing: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("unavailable")

    service, compiled = graph_service
    monkeypatch.setattr(review_service, "Client", FailingClient)
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_tracing", True)
    monkeypatch.setattr(review_service.settings, "langsmith_api_key", "test-key")

    await service._run_graph("task-123")

    assert len(compiled.calls) == 1
    assert fake_tracing == [{"enabled": False}]
    assert service.get_task("task-123").error is None


@pytest.mark.asyncio
async def test_langsmith_trace_uses_safe_metadata_and_hides_content_by_default(
    graph_service: tuple[ReviewService, FakeCompiledGraph],
    fake_tracing: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, compiled = graph_service
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_tracing", True)
    monkeypatch.setattr(review_service.settings, "langsmith_api_key", "test-key")
    monkeypatch.setattr(review_service.settings, "langsmith_project", "test-project")
    monkeypatch.setattr(review_service.settings, "langsmith_endpoint", "https://smith.example")
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_include_content", False)

    await service._run_graph("task-123")

    _, config = compiled.calls[0]
    assert config["run_name"] == "repoguardian-pr-review"
    assert config["tags"] == ["repoguardian", "pr_review"]
    assert config["metadata"] == {
        "task_id": "task-123",
        "mode": "pr_review",
        "model_override": True,
    }
    assert len(config["callbacks"]) == 1
    assert fake_tracing[0]["project_name"] == "test-project"
    client = FakeClient.instances[0]
    assert client.kwargs["api_url"] == "https://smith.example"
    assert client.kwargs["hide_inputs"]({
        "diff_text": "private diff",
        "_provider": object(),
        "_github_tool": object(),
    }) == {}
    assert client.kwargs["hide_outputs"]({"review_issues": [{"title": "private"}]}) == {}


def test_content_upload_removes_injected_tools_and_sensitive_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_service.settings, "repoguardian_langsmith_include_content", True)

    filtered = review_service._trace_content_filter({
        "diff_text": "explicitly allowed",
        "repo_path": "C:/private/clone",
        "_provider": object(),
        "nested": {"_github_tool": object(), "safe": "kept"},
    })

    assert filtered == {"diff_text": "explicitly allowed", "nested": {"safe": "kept"}}
