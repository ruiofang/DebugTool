"""Portable settings must support QSettings' directory-level atomic writes."""

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSettings
import main


class ConfigPathTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.app_dir = self.root / "opt/qt5com"
        self.app_dir.mkdir(parents=True)
        self.user_dir = self.root / "user-config"
        self.stack.enter_context(patch("main.app_dir", return_value=str(self.app_dir)))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.user_dir), "APPDATA": str(self.user_dir)}))

    def test_new_portable_config_supports_atomic_qsettings_save(self):
        path = main.config_path()
        self.assertEqual(path, str(self.app_dir / "serial_tool.ini"))
        settings = QSettings(path, QSettings.IniFormat)
        settings.setValue("baud", "921600")
        settings.sync()
        self.assertEqual(settings.status(), QSettings.NoError)
        self.assertEqual(QSettings(path, QSettings.IniFormat).value("baud"), "921600")
        self.assertFalse(self.user_dir.exists())

    def test_writable_file_in_readonly_directory_falls_back_and_copies(self):
        portable = self.app_dir / "serial_tool.ini"
        portable.write_text("[General]\nbaud=460800\n", encoding="utf-8")
        real_access = os.access
        with patch("main.os.access", side_effect=lambda path, mode: (
                False if str(path) == str(self.app_dir) else real_access(path, mode))):
            selected = Path(main.config_path())
        self.assertEqual(selected, self.user_dir / "qt5com/serial_tool.ini")
        self.assertEqual(selected.read_bytes(), portable.read_bytes())

    def test_readonly_portable_file_preserves_existing_user_settings(self):
        portable = self.app_dir / "serial_tool.ini"
        portable.write_text("[General]\nbaud=115200\n", encoding="utf-8")
        user_ini = self.user_dir / "qt5com/serial_tool.ini"
        user_ini.parent.mkdir(parents=True)
        user_ini.write_text("[General]\nbaud=9600\n", encoding="utf-8")
        real_access = os.access
        with patch("main.os.access", side_effect=lambda path, mode: (
                False if str(path) == str(portable) else real_access(path, mode))):
            self.assertEqual(main.config_path(), str(user_ini))
        self.assertIn("baud=9600", user_ini.read_text(encoding="utf-8"))


class AppDirectoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX installation symlinks")
    def test_frozen_command_symlink_uses_real_install_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "opt/qt5com/DebugTool"
            executable.parent.mkdir(parents=True)
            executable.touch()
            link = root / "bin/DebugTool"
            link.parent.mkdir()
            link.symlink_to(executable)
            with patch.object(main.sys, "frozen", True, create=True), \
                    patch.object(main.sys, "executable", str(link)):
                self.assertEqual(main.app_dir(), str(executable.parent))


if __name__ == "__main__":
    unittest.main()
