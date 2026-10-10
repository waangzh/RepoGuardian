"""评测的模型请求/读取审计旁路，默认不采集，不进入模型或业务状态。"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone


_sinks = ContextVar("run_audit_sinks", default={})


@contextmanager
def capture_run_audit(*, model_sink, tool_sink):
    token = _sinks.set({"model": model_sink, "tool": tool_sink})
    try:
        yield
    finally:
        _sinks.reset(token)


def emit_run_audit(kind: str, **event):
    sink = _sinks.get().get(kind)
    if sink is not None:
        try:
            sink({"timestamp": datetime.now(timezone.utc).isoformat(), **event})
        except Exception:
            logging.getLogger(__name__).warning("评测审计写入失败", exc_info=False)
