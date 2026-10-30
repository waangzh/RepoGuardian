"""Disposable prepared repositories; filesystem operations share a process-safe guard."""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.fingerprints import stable_hash
from app.languages.isolation import parser_fingerprint
from app.models.orm import PreparedWorkspaceOrm, utcnow
from app.review.store import as_utc
from app.tools.git_tool import (
    DIFF_CONTEXT_LINES, DIFF_POLICY_VERSION, GitToolCancelled, GitToolError,
)
from app.tools.workspace_cleanup import (
    PREPARED_WORKSPACE_PREFIX, _directory_size, cleanup_workspace,
)

logger = logging.getLogger("RepoGuardian.PreparedWorkspaces")


def prepared_workspace_key(pr):
    return stable_hash({"repository": pr.clone_url, "base": pr.base.sha, "head": pr.head.sha,
        "parser": parser_fingerprint(),
        "diff": DIFF_POLICY_VERSION, "context": DIFF_CONTEXT_LINES})


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise GitToolCancelled("Git 操作已取消")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class PreparedWorkspaces:
    def __init__(self, repository, workdir=None):
        self.sessions = repository.sessions
        self.root = Path(workdir if workdir is not None else settings.repoguardian_workdir).resolve()

    @contextmanager
    def guard(self, *, wait=True, cancel_event=None):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".prepared-workspaces.lock"
        if _is_link(lock_path):
            raise ValueError("invalid_prepared_workspace_lock")
        with lock_path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 30
            acquired = False
            try:
                while not acquired:
                    _check_cancelled(cancel_event)
                    handle.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError:
                        if not wait:
                            yield False
                            return
                        if time.monotonic() >= deadline:
                            raise TimeoutError("prepared_workspace_busy") from None
                        time.sleep(0.05)
                yield True
            finally:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, directory):
        if not isinstance(directory, str) or not re.fullmatch(r"\.prepared-[0-9a-f]{32}", directory):
            raise ValueError("invalid_prepared_workspace_directory")
        path = self.root / directory
        if _is_link(path) or path.resolve().parent != self.root:
            raise ValueError("invalid_prepared_workspace_path")
        return path

    def _rows(self):
        with self.sessions() as session:
            return list(session.scalars(select(PreparedWorkspaceOrm).order_by(
                PreparedWorkspaceOrm.last_used_at, PreparedWorkspaceOrm.id)))

    def _remove(self, row):
        with self.sessions.begin() as session:
            session.execute(update(PreparedWorkspaceOrm).where(
                PreparedWorkspaceOrm.id == row.id).values(state="deleting"))
        try:
            path = self._path(row.directory)
        except ValueError:
            # Drop corrupt metadata without following its path.
            path = None
        if path is not None and not cleanup_workspace(path, workdir=self.root):
            return False
        with self.sessions.begin() as session:
            session.execute(delete(PreparedWorkspaceOrm).where(PreparedWorkspaceOrm.id == row.id))
        return True

    def _prune(self, *, reserve_bytes=0, reserve_entries=0):
        now = utcnow()
        rows = self._rows()
        removed = 0
        for row in list(rows):
            if row.state != "ready" or as_utc(row.expires_at) <= now:
                if self._remove(row):
                    rows.remove(row)
                    removed += 1
        total = sum(max(0, row.size_bytes) for row in rows)
        for row in list(rows):
            if (len(rows) + reserve_entries <= settings.repoguardian_prepared_workspace_max_entries
                    and total + reserve_bytes <= settings.repoguardian_prepared_workspace_max_bytes):
                break
            if self._remove(row):
                rows.remove(row)
                total -= max(0, row.size_bytes)
                removed += 1
        fits = (len(rows) + reserve_entries <= settings.repoguardian_prepared_workspace_max_entries
                and total + reserve_bytes <= settings.repoguardian_prepared_workspace_max_bytes)
        return removed, fits

    def maintain(self):
        with self.guard(wait=False) as acquired:
            if not acquired:
                return 0
            removed, _ = self._prune()
            registered = {row.directory for row in self._rows()}
            cutoff = time.time() - settings.repoguardian_prepared_workspace_ttl_seconds
            for path in self.root.iterdir():
                if (not path.name.startswith(PREPARED_WORKSPACE_PREFIX)
                        or path.name in registered or _is_link(path) or not path.is_dir()):
                    continue
                try:
                    candidate = self._path(path.name)
                    if candidate.stat().st_mtime <= cutoff and cleanup_workspace(candidate, workdir=self.root):
                        removed += 1
                except (ValueError, OSError):
                    logger.warning("准备工作区孤儿回收失败")
            return removed

    def touch(self, key):
        with self.guard(wait=False) as acquired:
            if acquired:
                now = utcnow()
                with self.sessions.begin() as session:
                    session.execute(update(PreparedWorkspaceOrm).where(
                        PreparedWorkspaceOrm.input_key == key, PreparedWorkspaceOrm.state == "ready",
                        PreparedWorkspaceOrm.expires_at > now).values(
                            last_used_at=now, expires_at=now + timedelta(
                                seconds=settings.repoguardian_prepared_workspace_ttl_seconds)))

    def publish(self, key, repo_path, pr, diff_context, *, cancel_event=None):
        path = Path(repo_path)
        if (_is_link(path) or path.resolve().parent != self.root or not path.is_dir()
                or _is_link(path / ".git") or not (path / ".git").is_dir()):
            raise ValueError("invalid_prepared_workspace_source")
        with self.guard(cancel_event=cancel_event):
            _check_cancelled(cancel_event)
            self._prune()
            now = utcnow()
            if any(row.input_key == key and row.state == "ready" and as_utc(row.expires_at) > now
                   and self._path(row.directory).is_dir() for row in self._rows()):
                return False
            size = _directory_size(path)
            if (settings.repoguardian_prepared_workspace_max_entries == 0
                    or size > settings.repoguardian_prepared_workspace_max_bytes):
                return False
            _, fits = self._prune(reserve_bytes=size, reserve_entries=1)
            if not fits:
                return False
            identity = uuid4().hex
            destination = self._path(PREPARED_WORKSPACE_PREFIX + identity)
            path.rename(destination)
            try:
                _check_cancelled(cancel_event)
                with self.sessions.begin() as session:
                    session.add(PreparedWorkspaceOrm(id=identity, input_key=key,
                        directory=destination.name, base_sha=pr.base.sha, head_sha=pr.head.sha,
                        effective_base_sha=diff_context.get("effective_base_sha") or pr.base.sha,
                        size_bytes=size, state="ready", created_at=now, last_used_at=now,
                        expires_at=now + timedelta(seconds=settings.repoguardian_prepared_workspace_ttl_seconds)))
            except BaseException:
                cleanup_workspace(destination, workdir=self.root)
                raise
            return True

    def copy(self, pr, git_tool, *, progress_callback=None, cancel_event=None):
        key = prepared_workspace_key(pr)
        with self.guard(cancel_event=cancel_event):
            if (settings.repoguardian_prepared_workspace_max_entries == 0
                    or settings.repoguardian_prepared_workspace_max_bytes == 0):
                return None
            for row in reversed(self._rows()):
                if (row.input_key != key or row.state != "ready"
                        or as_utc(row.expires_at) <= utcnow()):
                    continue
                try:
                    if row.base_sha != pr.base.sha or row.head_sha != pr.head.sha:
                        raise ValueError("prepared_workspace_snapshot_mismatch")
                    result = git_tool.clone_prepared(pr, self._path(row.directory),
                        effective_base_sha=row.effective_base_sha,
                        progress_callback=progress_callback, cancel_event=cancel_event)
                except GitToolCancelled:
                    raise
                except (GitToolError, OSError, ValueError):
                    logger.warning("准备工作区校验失败，按冻结提交重新准备")
                    self._remove(row)
                    continue
                now = utcnow()
                try:
                    with self.sessions.begin() as session:
                        session.execute(update(PreparedWorkspaceOrm).where(
                            PreparedWorkspaceOrm.id == row.id).values(last_used_at=now,
                                expires_at=now + timedelta(
                                    seconds=settings.repoguardian_prepared_workspace_ttl_seconds)))
                except BaseException:
                    cleanup_workspace(result[0], workdir=self.root)
                    raise
                return result
        return None


def prepare_repository(pr, git_tool, repository, **options):
    """Try a guarded local copy, then use the existing immutable-SHA remote path."""
    _check_cancelled(options.get("cancel_event"))
    if repository is not None and callable(getattr(git_tool, "clone_prepared", None)):
        try:
            prepared = PreparedWorkspaces(repository, git_tool._workdir).copy(
                pr, git_tool, **options)
            if prepared is not None:
                return prepared
        except GitToolCancelled:
            raise
        except (SQLAlchemyError, OSError, ValueError, TimeoutError) as exc:
            logger.warning("准备工作区缓存不可用：%s", type(exc).__name__)
    _check_cancelled(options.get("cancel_event"))
    return git_tool.clone_and_diff(pr, **options)
