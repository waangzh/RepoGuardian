import subprocess
import pytest
from app.review.domain import Snapshot
from app.review.evidence import EvidenceService
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
        snapshot=Snapshot(review_id="review", repository="local/repo", repository_id=1, pr_number=1, declared_base_sha=base,
                                base_sha=base, head_sha=head),
        repo_path=repo, allowed_paths=frozenset({"safe.py", "other.py", "removed.py", ".env"}),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"), git=GitTool(),
    )


def commit_sources(service, texts):
    for path, content in texts.items():
        target = service.repo_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for args in (("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.test",
                                 "commit", "-m", "search fixtures")):
        subprocess.run(["git", "-C", str(service.repo_path), *args],
                       check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(service.repo_path), "rev-parse", "HEAD"],
                          check=True, capture_output=True).stdout.decode().strip()
    service.snapshot = service.snapshot.model_copy(update={"head_sha": head})
    service.allowed_paths = frozenset(set(service.allowed_paths) | set(texts))
