"""可配置的应用日志管理器。"""

from __future__ import annotations

import json
import logging
import os
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
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


class LoggerManager:
    def __init__(self, config_path: str):
        self.config_path = os.path.abspath(config_path)
        self.CONFIG: dict[str, Any] = {}
        self.config_lock = threading.RLock()
        self.observer: Any = None
        self._logger_name: str | None = None
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
        for key in ("to_console", "json_format"):
            value = log_config.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"log_config.{key} 必须是布尔值")
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
            print("[CONFIG] 配置已重新加载")
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
                handler.close()

    @staticmethod
    def _mark(handler: logging.Handler) -> logging.Handler:
        setattr(handler, _MANAGED_HANDLER, True)
        return handler

    def setup_logger(
        self,
        name: str = "MQI",
        log_folder: str = "test-logs",
        log_level: str | int = "DEBUG",
        to_console: bool = True,
        json_format: bool = True,
    ) -> logging.Logger:
        """创建或原子重配应用 logger。"""
        level = (
            logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)
            if isinstance(log_level, str)
            else log_level
        )
        base_dir = Path(__file__).resolve().parent.parent
        folder = Path(log_folder)
        if not folder.is_absolute():
            folder = base_dir / folder

        debug_folder = folder / "debug"
        error_folder = folder / "error"
        debug_folder.mkdir(parents=True, exist_ok=True)
        error_folder.mkdir(parents=True, exist_ok=True)

        with self.config_lock:
            logger = logging.getLogger(name)
            targets = [logger, *(logging.getLogger(n) for n in _SQLALCHEMY_LOGGERS)]
            for target in targets:
                self._remove_managed_handlers(target)

            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.filters[:] = [
                item for item in logger.filters if not isinstance(item, TraceFilter)
            ]
            trace_filter = TraceFilter()
            logger.addFilter(trace_filter)

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
                handler.addFilter(trace_filter)
                return self._mark(handler)

            app_handlers: list[logging.Handler] = [
                rotating_handler(debug_folder / "debug.log", logging.DEBUG, text_formatter),
                rotating_handler(error_folder / "error.log", logging.ERROR, error_formatter),
            ]

            if json_format:
                json_folder = folder / "json"
                json_folder.mkdir(parents=True, exist_ok=True)
                app_handlers.append(
                    rotating_handler(
                        json_folder / "app.jsonl", logging.DEBUG, JsonFormatter()
                    )
                )

            if to_console:
                console = logging.StreamHandler()
                console.setLevel(level)
                console.setFormatter(
                    coloredlogs.ColoredFormatter(
                        "%(asctime)s - %(name)s - %(levelname)s - "
                        "[%(trace_id)s] - [%(filename)s:%(lineno)d - "
                        "%(funcName)s()] - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                console.addFilter(trace_filter)
                app_handlers.append(self._mark(console))

            for handler in app_handlers:
                logger.addHandler(handler)

            # 只在 SQLAlchemy 顶层 logger 注册一次，子 logger 向其传播，
            # 避免同一条记录被多个层级重复写入。
            sqlalchemy_logger = logging.getLogger("sqlalchemy")
            sqlalchemy_logger.setLevel(logging.INFO)
            sqlalchemy_logger.propagate = False
            sqlalchemy_logger.addHandler(
                rotating_handler(
                    debug_folder / "debug.log", logging.DEBUG, text_formatter
                )
            )
            sqlalchemy_logger.addHandler(
                rotating_handler(
                    error_folder / "error.log", logging.ERROR, error_formatter
                )
            )
            for child_name in ("sqlalchemy.engine", "sqlalchemy.pool"):
                child = logging.getLogger(child_name)
                child.setLevel(logging.NOTSET)
                child.propagate = True

            self._logger_name = name
            return logger

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
        )


def initialize_logger(
    logger_manager: LoggerManager, default_log_name: str = "TEST"
) -> logging.Logger:
    return logger_manager.configure_from_config(default_log_name)


config_path = str(Path(__file__).with_name("config.yml"))
logger_manager = LoggerManager(config_path)
logger = initialize_logger(logger_manager)


if __name__ == "__main__":
    logger.info("日志管理器启动成功")
