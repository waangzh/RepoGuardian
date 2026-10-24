"""Disposable, bounded cache of content-bound successful static analyses."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

logger = logging.getLogger("RepoGuardian.ParserCache")
CACHE_PROTOCOL = "parser-success-v1"
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHE_ENTRIES = 4096


def _cache_path() -> Path:
    from app.core.config import settings

    return settings.repoguardian_artifact_dir.parent / "parser-cache.db"


def load(key: str) -> bytes | None:
    path = _cache_path()
    try:
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            row = db.execute(
                "SELECT payload, digest FROM analyses WHERE key = ? AND size <= ? "
                "AND length(payload) <= ?", (key, MAX_ENTRY_BYTES, MAX_ENTRY_BYTES),
            ).fetchone()
        if row and isinstance(row[0], bytes) and hashlib.sha256(row[0]).hexdigest() == row[1]:
            return row[0]
    except (OSError, sqlite3.Error, ValueError):
        logger.debug("解析成功缓存不可读，重新解析")
    return None


def save(key: str, payload: bytes) -> None:
    if len(payload) > min(MAX_ENTRY_BYTES, MAX_CACHE_BYTES) or MAX_CACHE_ENTRIES < 1:
        return
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS analyses ("
                "key TEXT PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL, "
                "size INTEGER NOT NULL, created_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?)",
                (key, payload, hashlib.sha256(payload).hexdigest(), len(payload), time.time()),
            )
            total, count = db.execute("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM analyses").fetchone()
            if total > MAX_CACHE_BYTES or count > MAX_CACHE_ENTRIES:
                for old_key, size in db.execute(
                    "SELECT key, size FROM analyses ORDER BY created_at, key"
                ).fetchall():
                    if total <= MAX_CACHE_BYTES and count <= MAX_CACHE_ENTRIES:
                        break
                    db.execute("DELETE FROM analyses WHERE key = ?", (old_key,))
                    total -= size
                    count -= 1
    except (OSError, sqlite3.Error):
        logger.debug("解析成功缓存不可写，本次使用已校验结果")
