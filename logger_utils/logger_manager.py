"""可配置的应用日志管理器。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import coloredlogs
import yaml

from logger_utils.trace import TraceFilter

_MANAGED_HANDLER = "_logger_manager_owned"
_SQLALCHEMY_LOGGERS = ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool")
_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "trace_id",
}


class JsonFormatter(logging.Formatter):
    """输出可被 ELK、Loki 等系统稳定解析的单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "logger": record.name,
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", "-"),
            "file": f"{record.filename}:{record.lineno}",
            "func": record.funcName,
            "message": record.getMessage(),
            "process": record.process,
            "thread": record.thread,
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        if extras:
            payload["extra"] = extras
        if record.exc_info:
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


class SensitiveDataFilter(logging.Filter):
    """递归脱敏结构化字段，并清理消息和异常中的常见凭证。"""

    DEFAULT_FIELDS = frozenset(
        {
            "password",
            "passwd",
            "token",
            "access_token",
            "refresh_token",
            "authorization",
            "cookie",
            "secret",
            "api_key",
        }
    )
    MASK = "***"

    def __init__(
        self,
        fields: list[str] | tuple[str, ...] | None = None,
        *,
        max_message_length: int = 16_384,
        max_exception_length: int = 32_768,
    ):
        super().__init__()
        self.max_message_length = max_message_length
        self.max_exception_length = max_exception_length
        configured = fields or self.DEFAULT_FIELDS
        self.fields = frozenset(self._normalize_key(item) for item in configured)
        alternatives = "|".join(
            sorted((re.escape(item) for item in self.fields), key=len, reverse=True)
        )
        self._credential_pattern = re.compile(
            rf"(?i)(\b(?:{alternatives})\b\s*[=:]\s*)([^\s,;&]+)"
        )
        self._authorization_pattern = re.compile(
            r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
        )

    @staticmethod
    def _normalize_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "_", str(value).strip().lower()).strip("_")

    def redact_text(self, value: str, *, max_length: int | None = None) -> str:
        value = self._authorization_pattern.sub(r"\1 ***", value)
        value = self._credential_pattern.sub(r"\1***", value)
        if max_length is not None and len(value) > max_length:
            omitted = len(value) - max_length
            return f"{value[:max_length]}… <truncated {omitted} chars>"
        return value

    def sanitize(
        self, value: Any, *, key: str | None = None, depth: int = 0, seen: set[int] | None = None
    ) -> Any:
        if key is not None and self._normalize_key(key) in self.fields:
            return self.MASK
        if depth > 10:
            return "<max-depth>"
        if isinstance(value, str):
            return self.redact_text(value, max_length=self.max_message_length)
        if isinstance(value, bytes):
            return self.redact_text(
                value.decode("utf-8", errors="replace"),
                max_length=self.max_message_length,
            )
        if not isinstance(value, (dict, list, tuple, set)):
            return value

        seen = seen or set()
        identity = id(value)
        if identity in seen:
            return "<recursive>"
        seen.add(identity)
        try:
            if isinstance(value, dict):
                return {
                    item_key: self.sanitize(
                        item_value,
                        key=str(item_key),
                        depth=depth + 1,
                        seen=seen,
                    )
                    for item_key, item_value in value.items()
                }
            values = [
                self.sanitize(item, depth=depth + 1, seen=seen) for item in value
            ]
            if isinstance(value, tuple):
                return tuple(values)
            if isinstance(value, set):
                return set(values)
            return values
        finally:
            seen.remove(identity)

    def filter(self, record: logging.LogRecord) -> bool:
        # 先完成 %-format，再脱敏，避免修改格式字符串后遗留未消费参数。
        record.msg = self.redact_text(
            record.getMessage(), max_length=self.max_message_length
        )
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                record.__dict__[key] = self.sanitize(value, key=key)
        if record.exc_info:
            record.exc_text = self.redact_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip(),
                max_length=self.max_exception_length,
            )
            record.exc_info = None
        if record.stack_info:
            record.stack_info = self.redact_text(
                record.stack_info, max_length=self.max_exception_length
            )
        return True


class LoggerManager:
    def __init__(self, config_path: str):
        self.config_path = os.path.abspath(config_path)
        self.CONFIG: dict[str, Any] = {}
        self.config_lock = threading.RLock()
        self.observer: Any = None
        self._logger_name: str | None = None
        self._managed_logger_names: set[str] = set()
        self._sqlalchemy_state: dict[str, tuple[int, bool]] = {}
        self.reload_config(apply=False)

    def _load_yaml(self, file_path: str) -> dict[str, Any]:
        with open(file_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        if not isinstance(config, dict):
            raise ValueError("配置文件顶层必须是映射")
        self._validate_config(config)
        return config

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        log_config = config.get("log_config", {})
        if not isinstance(log_config, dict):
            raise ValueError("log_config 必须是映射")
        for key in ("name", "log_folder", "log_level"):
            value = log_config.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"log_config.{key} 必须是非空字符串")
        for key in (
            "to_console",
            "json_format",
            "file_output",
            "redaction_enabled",
            "capture_sqlalchemy",
        ):
            value = log_config.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"log_config.{key} 必须是布尔值")
        console_format = log_config.get("console_format")
        if console_format not in (None, "colored", "json"):
            raise ValueError("log_config.console_format 必须是 colored 或 json")
        console_stream = log_config.get("console_stream")
        if console_stream not in (None, "stdout", "stderr"):
            raise ValueError("log_config.console_stream 必须是 stdout 或 stderr")
        redact_fields = log_config.get("redact_fields")
        if redact_fields is not None and (
            not isinstance(redact_fields, list)
            or not all(isinstance(item, str) and item.strip() for item in redact_fields)
        ):
            raise ValueError("log_config.redact_fields 必须是非空字符串列表")
        for key in ("max_message_length", "max_exception_length"):
            value = log_config.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"log_config.{key} 必须是正整数")
        sqlalchemy_level = log_config.get("sqlalchemy_level")
        if sqlalchemy_level is not None:
            if (
                not isinstance(sqlalchemy_level, str)
                or sqlalchemy_level.upper() not in logging.getLevelNamesMapping()
            ):
                raise ValueError(f"未知 SQLAlchemy 日志等级: {sqlalchemy_level}")
        level = log_config.get("log_level")
        if level and level.upper() not in logging.getLevelNamesMapping():
            raise ValueError(f"未知日志等级: {level}")

    def reload_config(self, *, apply: bool = True) -> bool:
        """原子加载配置；读取失败时继续使用上一份有效配置。"""
        previous_config: dict[str, Any] | None = None
        try:
            new_config = self._load_yaml(self.config_path)
            with self.config_lock:
                previous_config = self.CONFIG
                self.CONFIG = new_config
                if apply and self._logger_name is not None:
                    self.configure_from_config(default_log_name=self._logger_name)
            return True
        except Exception as exc:
            if previous_config is not None:
                with self.config_lock:
                    self.CONFIG = previous_config
            logging.getLogger(__name__).error("加载日志配置失败: %s", exc)
            return False

    def get_config(self, key: str, default: Any = None) -> Any:
        with self.config_lock:
            current: Any = self.CONFIG
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current

    def start_config_watcher(self) -> None:
        """启动可选的 watchdog 配置热更新。"""
        with self.config_lock:
            if self.observer is not None and self.observer.is_alive():
                return
            try:
                from watchdog.events import FileSystemEventHandler
                from watchdog.observers import Observer
            except ImportError as exc:
                raise RuntimeError(
                    "配置监听需要可选依赖，请执行: pip install 'logger-project[watch]'"
                ) from exc

            manager = self

            class ConfigEventHandler(FileSystemEventHandler):
                @staticmethod
                def _matches(path: str) -> bool:
                    return os.path.abspath(path) == manager.config_path

                def on_modified(self, event: Any) -> None:
                    if not event.is_directory and self._matches(event.src_path):
                        manager.reload_config()

                on_created = on_modified

                def on_moved(self, event: Any) -> None:
                    if not event.is_directory and self._matches(event.dest_path):
                        manager.reload_config()

            observer = Observer()
            observer.daemon = True
            observer.schedule(
                ConfigEventHandler(),
                path=os.path.dirname(self.config_path) or ".",
                recursive=False,
            )
            observer.start()
            self.observer = observer

    def stop_config_watcher(self) -> None:
        with self.config_lock:
            observer, self.observer = self.observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)

    @staticmethod
    def _remove_managed_handlers(target: logging.Logger) -> None:
        for handler in target.handlers[:]:
            if getattr(handler, _MANAGED_HANDLER, False):
                target.removeHandler(handler)
                try:
                    handler.flush()
                finally:
                    handler.close()

    @staticmethod
    def _mark(handler: logging.Handler) -> logging.Handler:
        setattr(handler, _MANAGED_HANDLER, True)
        return handler

    def _restore_sqlalchemy_state(self) -> None:
        for logger_name, (level, propagate) in self._sqlalchemy_state.items():
            target = logging.getLogger(logger_name)
            target.setLevel(level)
            target.propagate = propagate
        self._sqlalchemy_state.clear()

    def setup_logger(
        self,
        name: str = "MQI",
        log_folder: str = "test-logs",
        log_level: str | int = "DEBUG",
        to_console: bool = True,
        json_format: bool = True,
        file_output: bool = True,
        console_format: str = "colored",
        console_stream: str = "stdout",
        redaction_enabled: bool = True,
        redact_fields: list[str] | None = None,
        capture_sqlalchemy: bool = False,
        sqlalchemy_level: str | int = "WARNING",
        max_message_length: int = 16_384,
        max_exception_length: int = 32_768,
    ) -> logging.Logger:
        """先完整构建新 handler，成功后再切换，失败时保留旧日志链路。"""
        level = (
            logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)
            if isinstance(log_level, str)
            else log_level
        )
        sa_level = (
            logging.getLevelNamesMapping().get(
                sqlalchemy_level.upper(), logging.WARNING
            )
            if isinstance(sqlalchemy_level, str)
            else sqlalchemy_level
        )
        base_dir = Path(__file__).resolve().parent.parent
        folder = Path(log_folder)
        if not folder.is_absolute():
            folder = base_dir / folder

        if console_format not in {"colored", "json"}:
            raise ValueError("console_format 必须是 'colored' 或 'json'")
        if console_stream not in {"stdout", "stderr"}:
            raise ValueError("console_stream 必须是 'stdout' 或 'stderr'")
        if max_message_length <= 0 or max_exception_length <= 0:
            raise ValueError("日志长度限制必须是正整数")

        debug_folder = folder / "debug"
        error_folder = folder / "error"
        if file_output:
            debug_folder.mkdir(parents=True, exist_ok=True)
            error_folder.mkdir(parents=True, exist_ok=True)

        trace_filter = TraceFilter()
        redaction_filter = (
            SensitiveDataFilter(
                redact_fields,
                max_message_length=max_message_length,
                max_exception_length=max_exception_length,
            )
            if redaction_enabled
            else None
        )
        text_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - "
            "[%(trace_id)s] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        error_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] - "
            "[%(filename)s:%(lineno)d - %(funcName)s()] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        created_handlers: list[logging.Handler] = []

        def prepare(handler: logging.Handler) -> logging.Handler:
            handler.addFilter(trace_filter)
            if redaction_filter:
                handler.addFilter(redaction_filter)
            created_handlers.append(self._mark(handler))
            return handler

        def rotating_handler(
            path: Path, handler_level: int, formatter: logging.Formatter
        ) -> logging.Handler:
            handler = TimedRotatingFileHandler(
                path,
                when="midnight",
                interval=1,
                backupCount=30,
                encoding="utf-8",
            )
            handler.setLevel(handler_level)
            handler.setFormatter(formatter)
            return prepare(handler)

        def console_handler() -> logging.Handler:
            stream = sys.stdout if console_stream == "stdout" else sys.stderr
            handler = logging.StreamHandler(stream)
            handler.setLevel(level)
            handler.setFormatter(
                JsonFormatter()
                if console_format == "json"
                else coloredlogs.ColoredFormatter(
                    "%(asctime)s - %(name)s - %(levelname)s - "
                    "[%(trace_id)s] - [%(filename)s:%(lineno)d - "
                    "%(funcName)s()] - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            return prepare(handler)

        app_handlers: list[logging.Handler] = []
        sqlalchemy_handlers: list[logging.Handler] = []
        try:
            if file_output:
                app_handlers.extend(
                    [
                        rotating_handler(
                            debug_folder / "debug.log", logging.DEBUG, text_formatter
                        ),
                        rotating_handler(
                            error_folder / "error.log", logging.ERROR, error_formatter
                        ),
                    ]
                )
                if json_format:
                    json_folder = folder / "json"
                    json_folder.mkdir(parents=True, exist_ok=True)
                    app_handlers.append(
                        rotating_handler(
                            json_folder / "app.jsonl",
                            logging.DEBUG,
                            JsonFormatter(),
                        )
                    )
            if to_console:
                app_handlers.append(console_handler())

            if capture_sqlalchemy:
                if file_output:
                    sqlalchemy_handlers.extend(
                        [
                            rotating_handler(
                                debug_folder / "debug.log",
                                logging.DEBUG,
                                text_formatter,
                            ),
                            rotating_handler(
                                error_folder / "error.log",
                                logging.ERROR,
                                error_formatter,
                            ),
                        ]
                    )
                if to_console:
                    sqlalchemy_handlers.append(console_handler())
        except Exception:
            for handler in created_handlers:
                handler.close()
            raise

        with self.config_lock:
            logger = logging.getLogger(name)
            old_names = set(self._managed_logger_names)
            old_names.update({name, *_SQLALCHEMY_LOGGERS})
            old_handlers: list[logging.Handler] = []
            for logger_name in old_names:
                target = logging.getLogger(logger_name)
                target.filters[:] = [
                    item
                    for item in target.filters
                    if not isinstance(item, (TraceFilter, SensitiveDataFilter))
                ]
                for handler in target.handlers[:]:
                    if getattr(handler, _MANAGED_HANDLER, False):
                        target.removeHandler(handler)
                        old_handlers.append(handler)

            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            for handler in app_handlers:
                logger.addHandler(handler)

            if capture_sqlalchemy:
                if not self._sqlalchemy_state:
                    self._sqlalchemy_state = {
                        logger_name: (
                            logging.getLogger(logger_name).level,
                            logging.getLogger(logger_name).propagate,
                        )
                        for logger_name in _SQLALCHEMY_LOGGERS
                    }
                sqlalchemy_logger = logging.getLogger("sqlalchemy")
                sqlalchemy_logger.setLevel(sa_level)
                sqlalchemy_logger.propagate = False
                for handler in sqlalchemy_handlers:
                    sqlalchemy_logger.addHandler(handler)
                for child_name in ("sqlalchemy.engine", "sqlalchemy.pool"):
                    child = logging.getLogger(child_name)
                    child.setLevel(logging.NOTSET)
                    child.propagate = True
            else:
                self._restore_sqlalchemy_state()

            self._logger_name = name
            self._managed_logger_names = {name}
            if capture_sqlalchemy:
                self._managed_logger_names.update(_SQLALCHEMY_LOGGERS)

        for handler in old_handlers:
            try:
                handler.flush()
            finally:
                handler.close()
        return logger

    def close(self) -> None:
        """停止 watcher，flush 并关闭此管理器创建的所有 handler。"""
        self.stop_config_watcher()
        with self.config_lock:
            names = set(self._managed_logger_names)
            if self._logger_name:
                names.add(self._logger_name)
            for name in names:
                self._remove_managed_handlers(logging.getLogger(name))
            self._restore_sqlalchemy_state()
            self._managed_logger_names.clear()
            self._logger_name = None

    def configure_from_config(self, default_log_name: str = "TEST") -> logging.Logger:
        config = self.get_config("log_config", {})
        if not isinstance(config, dict):
            config = {}
        configured_name = config.get("name", default_log_name)
        # 模块使用方通常持有既有 Logger 引用；热更新期间保留名称，避免
        # 配置创建了新 logger 而调用方仍继续写入旧 logger。
        active_name = self._logger_name or configured_name
        return self.setup_logger(
            name=active_name,
            log_folder=config.get("log_folder", "test-logs"),
            log_level=config.get("log_level", "DEBUG"),
            to_console=bool(config.get("to_console", True)),
            json_format=bool(config.get("json_format", True)),
            file_output=bool(config.get("file_output", True)),
            console_format=config.get("console_format", "colored"),
            console_stream=config.get("console_stream", "stdout"),
            redaction_enabled=bool(config.get("redaction_enabled", True)),
            redact_fields=config.get("redact_fields"),
            capture_sqlalchemy=bool(config.get("capture_sqlalchemy", False)),
            sqlalchemy_level=config.get("sqlalchemy_level", "WARNING"),
            max_message_length=config.get("max_message_length", 16_384),
            max_exception_length=config.get("max_exception_length", 32_768),
        )


def initialize_logger(
    logger_manager: LoggerManager, default_log_name: str = "TEST"
) -> logging.Logger:
    return logger_manager.configure_from_config(default_log_name)


_default_lock = threading.RLock()
_default_manager_instance: LoggerManager | None = None


def get_default_manager() -> LoggerManager:
    """延迟创建默认管理器，导入模块本身不会创建目录或打开文件。"""
    global _default_manager_instance
    with _default_lock:
        if _default_manager_instance is None:
            config_path = str(Path(__file__).with_name("config.yml"))
            _default_manager_instance = LoggerManager(config_path)
        return _default_manager_instance


def close_default_manager() -> None:
    """关闭并清除默认管理器；后续访问会创建全新实例。"""
    global _default_manager_instance
    with _default_lock:
        manager, _default_manager_instance = _default_manager_instance, None
    if manager is not None:
        manager.close()


def get_default_logger() -> logging.Logger:
    """延迟创建默认 logger。"""
    manager = get_default_manager()
    if manager._logger_name is None:
        return manager.configure_from_config()
    return logging.getLogger(manager._logger_name)


def __getattr__(name: str) -> Any:
    """兼容旧代码，同时保持真正的延迟初始化。"""
    if name == "logger_manager":
        return get_default_manager()
    if name == "logger":
        return get_default_logger()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    get_default_logger().info("日志管理器启动成功")
