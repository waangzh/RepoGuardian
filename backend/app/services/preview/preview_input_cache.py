"""只保存成功且绑定不可变提交的准备输入，不缓存审查结论。"""
import hashlib
import base64
import json
import logging
import zlib
from datetime import timedelta

from sqlalchemy import delete, select

from app.models.orm import PreviewInputCacheOrm, utcnow
from app.core.fingerprints import stable_hash
from app.tools.git_tool import DIFF_CONTEXT_LINES, DIFF_POLICY_VERSION
from app.languages.isolation import parser_fingerprint

logger = logging.getLogger("RepoGuardian.PreviewCache")
INDEX_CACHE_VERSION = "preview-input-v7"
MAX_BYTES = 32 * 1024 * 1024
MAX_RAW_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 4


def input_cache_key(pr, configuration):
    return stable_hash({"repository": pr.clone_url, "base": pr.base.sha, "head": pr.head.sha,
        "index": INDEX_CACHE_VERSION, "parser": parser_fingerprint(),
        "diff": DIFF_POLICY_VERSION, "context": DIFF_CONTEXT_LINES,
        "call_resolution": configuration["call_resolution_version"],
        "tool_schema": configuration.get("tool_schema_version"),
        "config_version": configuration.get("config_version")})


def encode(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PreviewInputCache:
    def __init__(self, repository):
        self.sessions = repository._session_factory

    def get(self, key):
        with self.sessions() as session:
            row = session.get(PreviewInputCacheOrm, key)
            if row is None:
                return None
            expiry = row.expires_at
            from app.services.preview.preview_jobs import as_utc
            if as_utc(expiry) <= utcnow():
                return None
            envelope = row.payload
            try:
                if (not isinstance(envelope, dict) or envelope.get("codec") != "zlib-base64-v1"
                        or len(encode(envelope)) > MAX_BYTES):
                    return None
                compressed = base64.b64decode(envelope["data"], validate=True)
                decoder = zlib.decompressobj()
                raw = decoder.decompress(compressed, MAX_RAW_BYTES + 1)
                if (len(raw) > MAX_RAW_BYTES or not decoder.eof or decoder.unused_data
                        or decoder.unconsumed_tail or len(raw) != envelope["raw_bytes"]):
                    return None
                data = json.loads(raw)
            except (ValueError, TypeError, KeyError, zlib.error):
                return None
            valid = (hashlib.sha256(raw).hexdigest() == row.digest
                and isinstance(data, dict) and isinstance(data.get("diff"), str)
                and isinstance(data.get("index"), dict) and isinstance(data.get("diff_context"), dict)
                and all(name in data["index"] for name in (
                    "file_index", "symbol_index", "repository_graph", "project_meta")))
            return data if valid else None

    def put(self, key, payload):
        raw = encode(payload)
        if len(raw) > MAX_RAW_BYTES:
            logger.info("准备输入超过缓存展开上限：%d 字节，本次不缓存", len(raw))
            return
        envelope = {"codec": "zlib-base64-v1", "raw_bytes": len(raw),
                    "data": base64.b64encode(zlib.compress(raw, level=3)).decode("ascii")}
        stored_bytes = len(encode(envelope))
        if stored_bytes > MAX_BYTES:
            logger.info("准备输入压缩后超过缓存单项上限：%d 字节，本次不缓存", stored_bytes)
            return
        logger.info("缓存准备输入：原始 %d 字节，存储 %d 字节", len(raw), stored_bytes)
        now = utcnow()
        with self.sessions.begin() as session:
            session.execute(delete(PreviewInputCacheOrm).where(PreviewInputCacheOrm.expires_at <= now))
            session.merge(PreviewInputCacheOrm(key=key, payload=envelope,
                digest=hashlib.sha256(raw).hexdigest(), created_at=now, expires_at=now + timedelta(hours=24)))
            session.flush()
            obsolete = list(session.scalars(select(PreviewInputCacheOrm.key)
                .order_by(PreviewInputCacheOrm.created_at.desc(), PreviewInputCacheOrm.key).offset(MAX_ENTRIES)))
            if obsolete:
                session.execute(delete(PreviewInputCacheOrm).where(PreviewInputCacheOrm.key.in_(obsolete)))
