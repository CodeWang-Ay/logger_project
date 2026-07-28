"""可复用的结构化日志与请求链路追踪组件。

常规用法::

    from logger_utils import get_logger

    logger = get_logger(__name__)
    logger.info("服务启动")

导入包本身不会创建日志目录、打开文件或启动后台线程。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from logger_utils.logger_manager import (
    JsonFormatter,
    LoggerManager,
    get_default_manager,
)
from logger_utils.trace import (
    TraceFilter,
    bind_trace_id,
    clear_trace_id,
    extract_trace_id,
    get_trace_id,
    normalize_trace_id,
    parse_traceparent,
    reset_trace_id,
    set_trace_id,
)

__all__ = [
    "JsonFormatter",
    "LoggerManager",
    "TraceFilter",
    "bind_trace_id",
    "clear_trace_id",
    "extract_trace_id",
    "get_logger",
    "get_logger_manager",
    "get_trace_id",
    "normalize_trace_id",
    "parse_traceparent",
    "reset_trace_id",
    "set_trace_id",
    "setup_logger",
]

_registry_lock = threading.RLock()
_manager_registry: dict[str, LoggerManager] = {}


def _config_key(config_path: str | None) -> str:
    path = (
        Path(config_path).expanduser()
        if config_path is not None
        else Path(__file__).with_name("config.yml")
    )
    return str(path.resolve())


def get_logger_manager(config_path: str | None = None) -> LoggerManager:
    """返回指定配置文件对应的线程安全单例管理器。"""
    if config_path is None:
        return get_default_manager()

    key = _config_key(config_path)
    with _registry_lock:
        manager = _manager_registry.get(key)
        if manager is None:
            manager = LoggerManager(key)
            _manager_registry[key] = manager
        return manager


def _configured_base_logger(manager: LoggerManager) -> logging.Logger:
    if manager._logger_name is None:
        return manager.configure_from_config()
    return logging.getLogger(manager._logger_name)


def get_logger(
    name: str | None = None,
    *,
    config_path: str | None = None,
) -> logging.Logger:
    """获取配置好的 logger。

    配置文件只创建一个基础 logger。传入 ``name`` 时返回它的子 logger，
    子 logger 通过传播复用基础 handler，因此不同模块既能保留各自名称，
    又不会重复打开日志文件。
    """
    manager = get_logger_manager(config_path)
    base_logger = _configured_base_logger(manager)
    if not name or name == base_logger.name:
        return base_logger

    child_name = (
        name if name.startswith(f"{base_logger.name}.") else f"{base_logger.name}.{name}"
    )
    child = logging.getLogger(child_name)
    child.setLevel(logging.NOTSET)
    child.propagate = True
    return child


def setup_logger(
    name: str = "app",
    *,
    config_path: str | None = None,
    log_folder: str = "test-logs",
    log_level: str | int = "DEBUG",
    to_console: bool = True,
    json_format: bool = True,
) -> logging.Logger:
    """使用显式参数配置 logger；适合不希望读取 YAML 的场景。"""
    manager = get_logger_manager(config_path)
    return manager.setup_logger(
        name=name,
        log_folder=log_folder,
        log_level=log_level,
        to_console=to_console,
        json_format=json_format,
    )
