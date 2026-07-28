import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import logger_utils
from logger_utils import close_logger_manager, get_logger, get_logger_manager


class ComponentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "logger.yml"
        self.config_path.write_text(
            "log_config:\n"
            "  name: component-test\n"
            f"  log_folder: {self.root.as_posix()}/logs\n"
            "  log_level: INFO\n"
            "  to_console: false\n"
            "  json_format: false\n",
            encoding="utf-8",
        )

    def tearDown(self):
        manager = get_logger_manager(str(self.config_path))
        manager.stop_config_watcher()
        for name in (
            "component-test",
            "component-test.orders",
            "component-test.users",
            "sqlalchemy",
            "sqlalchemy.engine",
            "sqlalchemy.pool",
        ):
            manager._remove_managed_handlers(logging.getLogger(name))
        self.temp_dir.cleanup()

    def test_public_exports_are_available(self):
        for name in logger_utils.__all__:
            self.assertTrue(hasattr(logger_utils, name), name)

    def test_plain_import_has_no_default_logger_side_effect(self):
        code = (
            "import logger_utils.logger_manager as module;"
            "assert module._default_manager_instance is None;"
            "assert not logging.getLogger('websocket_proxy_logs').handlers"
        )
        result = subprocess.run(
            [sys.executable, "-c", "import logging;" + code],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_path_returns_configured_named_child(self):
        logger = get_logger("orders", config_path=str(self.config_path))
        self.assertEqual(logger.name, "component-test.orders")
        self.assertTrue(logger.propagate)
        self.assertEqual(logger.handlers, [])
        self.assertTrue(logging.getLogger("component-test").handlers)

    def test_multiple_modules_share_one_handler_set(self):
        orders = get_logger("orders", config_path=str(self.config_path))
        users = get_logger("users", config_path=str(self.config_path))
        self.assertNotEqual(orders.name, users.name)
        self.assertEqual(orders.parent, users.parent)
        self.assertEqual(orders.parent.name, "component-test")

    def test_manager_is_cached_by_resolved_config_path(self):
        direct = get_logger_manager(str(self.config_path))
        equivalent = get_logger_manager(str(self.root / "." / "logger.yml"))
        self.assertIs(direct, equivalent)

    def test_close_removes_manager_from_registry(self):
        original = get_logger_manager(str(self.config_path))
        original.configure_from_config()
        close_logger_manager(str(self.config_path))
        replacement = get_logger_manager(str(self.config_path))
        self.assertIsNot(original, replacement)


if __name__ == "__main__":
    unittest.main()
