import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from logger_utils.logger_manager import JsonFormatter, LoggerManager


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
        self.manager.stop_config_watcher()
        for name in ("test-managed", "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool"):
            self.manager._remove_managed_handlers(logging.getLogger(name))
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


if __name__ == "__main__":
    unittest.main()
