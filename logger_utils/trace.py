"""
请求链路追踪模块

通过 contextvars 存储每个请求的唯一 trace_id，
配合 TraceFilter 自动注入到每条日志中，
在高并发场景下也能精确定位某个请求的完整日志链路。
"""
import uuid
import logging
from contextvars import ContextVar

# 每个异步上下文独立的 trace_id
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def set_trace_id(trace_id: str | None = None) -> str:
    """设置当前上下文的 trace_id，不传则自动生成。"""
    tid = trace_id or str(uuid.uuid4())[:8]  # 截短便于阅读
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str:
    """获取当前上下文的 trace_id。"""
    return _trace_id.get()


def clear_trace_id() -> None:
    """清除上下文中的 trace_id（使用默认值）。"""
    _trace_id.set("-")


class TraceFilter(logging.Filter):
    """注入 trace_id 到每条 LogRecord 中。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True
