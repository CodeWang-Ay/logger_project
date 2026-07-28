"""请求级链路追踪工具。"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token
from collections.abc import Mapping

_DEFAULT_TRACE_ID = "-"
_CUSTOM_TRACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_16 = re.compile(r"^[0-9a-f]{16}$")
_HEX_2 = re.compile(r"^[0-9a-f]{2}$")

_trace_id: ContextVar[str] = ContextVar("trace_id", default=_DEFAULT_TRACE_ID)


def _generate_trace_id() -> str:
    """生成与 W3C Trace Context 长度一致的 trace ID。"""
    return uuid.uuid4().hex


def normalize_trace_id(trace_id: str | None) -> str | None:
    """校验自定义 trace ID，拒绝超长内容和日志注入字符。"""
    if trace_id is None:
        return None
    value = trace_id.strip()
    return value if _CUSTOM_TRACE_ID.fullmatch(value) else None


def bind_trace_id(trace_id: str | None = None) -> tuple[str, Token[str]]:
    """绑定 trace ID，并返回可用于恢复上层上下文的 token。"""
    tid = normalize_trace_id(trace_id) or _generate_trace_id()
    return tid, _trace_id.set(tid)


def set_trace_id(trace_id: str | None = None) -> str:
    """设置当前上下文的 trace ID；保留原有的字符串返回接口。"""
    tid, _ = bind_trace_id(trace_id)
    return tid


def reset_trace_id(token: Token[str]) -> None:
    """使用 ContextVar token 恢复进入当前作用域前的 trace ID。"""
    _trace_id.reset(token)


def get_trace_id() -> str:
    return _trace_id.get()


def clear_trace_id() -> None:
    """兼容旧调用；新代码应优先使用 reset_trace_id。"""
    _trace_id.set(_DEFAULT_TRACE_ID)


def parse_traceparent(traceparent: str) -> str | None:
    """严格解析 W3C ``traceparent`` v00，并返回完整 32 位 trace ID。"""
    parts = traceparent.strip().split("-")
    if len(parts) != 4:
        return None

    version, trace_id, parent_id, flags = parts
    if version != "00":
        return None
    if not _HEX_32.fullmatch(trace_id) or trace_id == "0" * 32:
        return None
    if not _HEX_16.fullmatch(parent_id) or parent_id == "0" * 16:
        return None
    if not _HEX_2.fullmatch(flags):
        return None
    return trace_id


def extract_trace_id(headers: Mapping[str, str]) -> str | None:
    """优先读取合法的 ``X-Trace-Id``，其次读取 W3C traceparent。"""
    custom = headers.get("X-Trace-Id") or headers.get("x-trace-id")
    normalized = normalize_trace_id(custom)
    if normalized:
        return normalized

    traceparent = headers.get("traceparent")
    return parse_traceparent(traceparent) if traceparent else None


class TraceFilter(logging.Filter):
    """为每条 LogRecord 注入 trace_id。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True
