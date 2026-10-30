import pytest
from tests.support.source_fixtures import commit_sources
from app.review.evidence import source_lines
from app.llm.providers import OpenAICompatibleProvider



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


def test_agent_model_has_no_forced_json_mode():
    provider = OpenAICompatibleProvider(api_key="fixture", base_url="https://example.test/v1",
                                         default_model="fixture")
    model = provider.agent_model()
    assert "response_format" not in model.model_kwargs
    assert model.max_retries == 0


def test_source_line_identity_counts_only_lf():
    assert source_lines("value='a\u2028b'\r\nnext\r") == ["value='a\u2028b'\r\n", "next\r"]
    assert source_lines("") == []
    assert source_lines("\n") == ["\n"]



async def test_frozen_source_is_fetched_once_per_commit_path(source_service, monkeypatch):
    shows, trees = [], []
    original_show = source_service.git.get_file_content_at_revision
    original_tree = source_service.git.get_tree_entries

    def show(*args):
        shows.append(args[1:])
        return original_show(*args)

    def tree(*args):
        trees.append(args[1:])
        return original_tree(*args)

    monkeypatch.setattr(source_service.git, "get_file_content_at_revision", show)
    monkeypatch.setattr(source_service.git, "get_tree_entries", tree)
    head = await source_service.read("safe.py", 1, 1)
    await source_service.read("safe.py", 1, 1)
    await source_service.read("safe.py", 1, 1, "base")
    source_service.resolve(head["evidence"]["id"])
    source_service.resolve(head["evidence"]["id"])
    assert len(shows) == len(trees) == 2
    assert {item[0] for item in shows} == {
        source_service.snapshot.head_sha, source_service.snapshot.base_sha}


async def test_cached_source_still_enforces_path_policy_and_size(source_service):
    await source_service.read("safe.py", 1, 1)
    source_service.allowed_paths = frozenset({"other.py"})
    with pytest.raises(ValueError, match="path_not_authorized"):
        await source_service.read("safe.py", 1, 1)
    source_service.allowed_paths = frozenset({"safe.py"})
    source_service.max_file_bytes = 1
    with pytest.raises(ValueError, match="source_too_large"):
        await source_service.read("safe.py", 1, 1)


async def test_source_cache_is_scoped_to_the_fixed_commit(source_service):
    first = await source_service.read("safe.py", 1, 1)
    commit_sources(source_service, {"safe.py": "VALUE = 3\n"})
    second = await source_service.read("safe.py", 1, 1)
    assert first["content"] == "VALUE = 2\n"
    assert second["content"] == "VALUE = 3\n"
