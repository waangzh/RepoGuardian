import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.models.review_plan import PlanSnapshot
from app.services.review.file_context import FileContextError, load_file_context


def task(path="src/new.py", old="src/old.py", change="renamed"):
    return SimpleNamespace(snapshot=PlanSnapshot(head_sha="a" * 40, effective_base_sha="b" * 40,
        declared_base_sha="c" * 40), pr=SimpleNamespace(
        base=SimpleNamespace(repo_clone_url="https://github.com/base/repo.git"),
        head=SimpleNamespace(repo_clone_url="https://github.com/fork/repo.git")),
        changed_files=[SimpleNamespace(file_path=path, old_file_path=old, change_type=change, is_binary=False)])


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"first\nsecond\n", b"first\r\nsecond\r\n", b"\n", b""])
async def test_immutable_renamed_context_and_blob_identity(content):
    seen = []
    sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()

    def handler(request):
        seen.append(str(request.url))
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": request.url.path.rsplit("/", 1)[1], "tree": {"sha": "e" * 40}})
        if "/trees/" in request.url.path:
            path = "src/old.py" if "/base/" in request.url.path else "src/new.py"
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": path, "type": "blob", "mode": "100644", "sha": sha, "size": len(content)}]})
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(content).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await load_file_context(task(), 0, client)
    expected = content.decode().split("\n")
    if expected[-1] == "":
        expected.pop()
    assert result.base_lines == result.head_lines == expected
    assert any("/base/repo/git/commits/" + "b" * 40 in url for url in seen)
    assert any("/fork/repo/git/commits/" + "a" * 40 in url for url in seen)
    assert not any("c" * 40 in url for url in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../secret", "/absolute", ".env", "src/../file", "a\\b"])
async def test_rejected_paths_never_contact_github(path):
    def handler(request):
        pytest.fail("unsafe path reached GitHub")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FileContextError):
            await load_file_context(task(path=path), 0, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,truncated,size", [("120000", False, 1), ("160000", False, 1),
    ("100644", True, 1), ("100644", False, 524289)])
async def test_symlink_submodule_truncation_and_size_are_rejected(mode, truncated, size):
    def handler(request):
        assert "/blobs/" not in request.url.path
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": request.url.path.rsplit("/", 1)[1], "tree": {"sha": "e" * 40}})
        return httpx.Response(200, json={"truncated": truncated, "tree": [
            {"path": "src/old.py", "mode": mode, "type": "blob", "size": size, "sha": "d" * 40}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FileContextError):
            await load_file_context(task(), 0, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"\x00binary", b"\xffinvalid", b"tampered"])
async def test_binary_encoding_and_blob_hash_fail_closed(content):
    sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    def handler(request):
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": request.url.path.rsplit("/", 1)[1], "tree": {"sha": "e" * 40}})
        if "/trees/" in request.url.path:
            return httpx.Response(200, json={"truncated": False, "tree": [{"path": "src/old.py",
                "mode": "100644", "type": "blob", "sha": sha, "size": len(content)}]})
        payload = b"different" if content == b"tampered" else content
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(payload).decode()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FileContextError, match="UTF-8"):
            await load_file_context(task(), 0, client)


@pytest.mark.asyncio
async def test_missing_snapshot_and_untrusted_host_are_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected request"))) as client:
        value = task()
        value.snapshot = None
        with pytest.raises(FileContextError):
            await load_file_context(value, 0, client)
        value = task()
        value.snapshot.head_sha = "main"
        with pytest.raises(FileContextError):
            await load_file_context(value, 0, client)
        value = task()
        value.pr.base.repo_clone_url = "https://github.com.evil.test/base/repo.git"
        with pytest.raises(FileContextError):
            await load_file_context(value, 0, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 403, 404, 429])
async def test_upstream_failure_is_not_empty_success(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="secret upstream"))) as client:
        with pytest.raises(FileContextError, match="固定快照源码不可用"):
            await load_file_context(task(), 0, client)


@pytest.mark.asyncio
async def test_context_route_serialization_and_failure_status(monkeypatch):
    from fastapi import FastAPI
    from app.api import reviews
    from app.models.review_plan import FileContextResponse

    value = task()
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        get_task=lambda task_id: value if task_id == "saved" else None))
    failure = None
    async def load(saved, file_id, client):
        if failure:
            raise failure
        return FileContextResponse(file_id=file_id, file_path=saved.changed_files[file_id].file_path,
            snapshot=saved.snapshot, base_lines=["old"], head_lines=["new"])
    monkeypatch.setattr(reviews, "load_file_context", load)
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/reviews/saved/files/0/context")
        assert response.status_code == 200
        assert response.json()["snapshot"]["effective_base_sha"] == "b" * 40
        assert response.json()["base_lines"] == ["old"]
        assert (await client.get("/api/reviews/saved/files/-1/context")).status_code == 404
        assert (await client.get("/api/reviews/missing/files/0/context")).status_code == 404
        failure = FileContextError("历史任务缺少固定快照")
        response = await client.get("/api/reviews/saved/files/0/context")
        assert response.status_code == 409
        assert response.json()["detail"] == "历史任务缺少固定快照"
        failure = httpx.ConnectError("sensitive network diagnostic")
        response = await client.get("/api/reviews/saved/files/0/context")
        assert response.status_code == 502
        assert "sensitive" not in response.text
