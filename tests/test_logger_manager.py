import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logger_utils.logger_manager import (
    JsonFormatter,
    LoggerManager,
    SensitiveDataFilter,
)


class JsonFormatterTests(unittest.TestCase):
    def test_special_characters_and_exception_produce_valid_json(self):
        formatter = JsonFormatter()
        try:
            raise ValueError('坏消息 "quoted"\nnext')
        except ValueError:
            exc_info = sys.exc_info()
            record = logging.getLogger("test").makeRecord(
                "test",
                logging.ERROR,
                __file__,
                10,
                '消息 "quoted"\nnext %s',
                ("\\",),
                exc_info=exc_info,
                extra={"order_id": 42},
            )

        payload = json.loads(formatter.format(record))
        self.assertEqual(payload["message"], '消息 "quoted"\nnext \\')
        self.assertEqual(payload["extra"]["order_id"], 42)
        self.assertIn("ValueError", payload["exception"])


class SensitiveDataFilterTests(unittest.TestCase):
    def test_nested_fields_message_and_authorization_are_redacted(self):
        record = logging.getLogger("test").makeRecord(
            "test",
            logging.INFO,
            __file__,
            10,
            "password=%s Authorization: Bearer abc.def",
            ("plain-secret",),
            exc_info=None,
            extra={
                "request": {
                    "token": "token-value",
                    "profile": {"name": "alice", "api-key": "api-secret"},
                }
            },
        )
        redactor = SensitiveDataFilter()
        self.assertTrue(redactor.filter(record))
        payload = json.loads(JsonFormatter().format(record))

        self.assertNotIn("plain-secret", payload["message"])
        self.assertNotIn("abc.def", payload["message"])
        self.assertEqual(payload["extra"]["request"]["token"], "***")
        self.assertEqual(payload["extra"]["request"]["profile"]["api-key"], "***")
        self.assertEqual(payload["extra"]["request"]["profile"]["name"], "alice")

    def test_exception_text_is_redacted(self):
        try:
            raise RuntimeError("token=exception-secret")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.getLogger("test").makeRecord(
            "test", logging.ERROR, __file__, 10, "failed", (), exc_info
        )
        SensitiveDataFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
        self.assertNotIn("exception-secret", payload["exception"])
        self.assertIn("token=***", payload["exception"])

    def test_message_and_exception_lengths_are_bounded(self):
        record = logging.getLogger("test").makeRecord(
            "test", logging.INFO, __file__, 10, "x" * 100, (), None
        )
        SensitiveDataFilter(
            max_message_length=16, max_exception_length=32
        ).filter(record)
        self.assertTrue(record.getMessage().startswith("x" * 16))
        self.assertIn("<truncated 84 chars>", record.getMessage())


class LoggerManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            "log_config:\n"
            "  name: test-managed\n"
            f"  log_folder: {self.root.as_posix()}/logs\n"
            "  log_level: INFO\n"
            "  to_console: false\n"
            "  json_format: true\n",
            encoding="utf-8",
        )
        self.manager = LoggerManager(str(self.config_path))

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_reconfiguration_does_not_accumulate_handlers(self):
        self.manager.configure_from_config()
        first = {
            name: len(
                [
                    handler
                    for handler in logging.getLogger(name).handlers
                    if getattr(handler, "_logger_manager_owned", False)
                ]
            )
            for name in ("test-managed", "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool")
        }
        self.manager.configure_from_config()
        second = {
            name: len(
                [
                    handler
                    for handler in logging.getLogger(name).handlers
                    if getattr(handler, "_logger_manager_owned", False)
                ]
            )
            for name in first
        }
        self.assertEqual(first, second)

    def test_reload_applies_valid_configuration(self):
        logger = self.manager.configure_from_config()
        self.assertEqual(logger.name, "test-managed")
        self.config_path.write_text(
            "log_config:\n"
            "  name: test-managed\n"
            f"  log_folder: {self.root.as_posix()}/logs\n"
            "  log_level: ERROR\n"
            "  to_console: true\n"
            "  json_format: false\n",
            encoding="utf-8",
        )
        self.assertTrue(self.manager.reload_config())
        console_handlers = [
            handler
            for handler in logger.handlers
            if type(handler) is logging.StreamHandler
        ]
        self.assertEqual(len(console_handlers), 1)
        self.assertEqual(console_handlers[0].level, logging.ERROR)

    def test_invalid_reload_keeps_previous_config(self):
        self.manager.configure_from_config()
        previous = self.manager.CONFIG
        self.config_path.write_text("- invalid\n- top-level\n", encoding="utf-8")
        self.assertFalse(self.manager.reload_config())
        self.assertEqual(self.manager.CONFIG, previous)

    def test_invalid_setting_type_keeps_previous_config(self):
        self.manager.configure_from_config()
        previous = self.manager.CONFIG
        self.config_path.write_text(
            "log_config:\n  to_console: 'false'\n", encoding="utf-8"
        )
        self.assertFalse(self.manager.reload_config())
        self.assertEqual(self.manager.CONFIG, previous)

    def test_json_stdout_mode_does_not_create_log_directory(self):
        log_folder = self.root / "stdout-only"
        logger = self.manager.setup_logger(
            name="test-managed",
            log_folder=str(log_folder),
            log_level="INFO",
            to_console=True,
            console_format="json",
            console_stream="stdout",
            file_output=False,
            json_format=False,
        )
        managed = [
            handler
            for handler in logger.handlers
            if getattr(handler, "_logger_manager_owned", False)
        ]
        self.assertEqual(len(managed), 1)
        self.assertIsInstance(managed[0].formatter, JsonFormatter)
        self.assertFalse(log_folder.exists())

    def test_config_load_does_not_write_to_stdout(self):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            LoggerManager(str(self.config_path))
        self.assertEqual(output.getvalue(), "")

    def test_failed_reconfiguration_keeps_old_handlers(self):
        logger = self.manager.configure_from_config()
        old_handlers = logger.handlers[:]
        with patch(
            "logger_utils.logger_manager.TimedRotatingFileHandler",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(PermissionError):
                self.manager.setup_logger(
                    name="test-managed",
                    log_folder=str(self.root / "unavailable"),
                    to_console=False,
                )
        self.assertEqual(logger.handlers, old_handlers)
        self.assertTrue(all(handler.stream is not None for handler in old_handlers))

    def test_renaming_logger_removes_old_managed_handlers(self):
        old_logger = self.manager.setup_logger(
            name="old-name",
            log_folder=str(self.root / "old"),
            to_console=False,
            json_format=False,
        )
        self.manager.setup_logger(
            name="new-name",
            log_folder=str(self.root / "new"),
            to_console=False,
            json_format=False,
        )
        self.assertFalse(
            any(
                getattr(handler, "_logger_manager_owned", False)
                for handler in old_logger.handlers
            )
        )

    def test_close_releases_handlers_and_resets_manager(self):
        logger = self.manager.configure_from_config()
        handlers = logger.handlers[:]
        self.manager.close()
        self.assertEqual(logger.handlers, [])
        self.assertIsNone(self.manager._logger_name)
        self.assertTrue(
            all(getattr(handler, "stream", None) is None for handler in handlers)
        )

    def test_sqlalchemy_capture_is_opt_in(self):
        sqlalchemy_logger = logging.getLogger("sqlalchemy")
        original_level = sqlalchemy_logger.level
        original_propagate = sqlalchemy_logger.propagate
        self.manager.setup_logger(
            name="test-managed",
            log_folder=str(self.root / "without-sqlalchemy"),
            to_console=False,
            json_format=False,
            capture_sqlalchemy=False,
        )
        self.assertFalse(
            any(
                getattr(handler, "_logger_manager_owned", False)
                for handler in logging.getLogger("sqlalchemy").handlers
            )
        )
        self.manager.setup_logger(
            name="test-managed",
            log_folder=str(self.root / "with-sqlalchemy"),
            to_console=False,
            json_format=False,
            capture_sqlalchemy=True,
        )
        self.assertTrue(
            any(
                getattr(handler, "_logger_manager_owned", False)
                for handler in logging.getLogger("sqlalchemy").handlers
            )
        )
        self.manager.close()
        self.assertEqual(sqlalchemy_logger.level, original_level)
        self.assertEqual(sqlalchemy_logger.propagate, original_propagate)


if __name__ == "__main__":
    unittest.main()
