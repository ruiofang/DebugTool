"""Check the T113 source updater in temporary directories, without a board.

These tests do not run the ARM interpreter, access I2C or framebuffer devices,
or start/stop services. Python 3.8 parsing is a source compatibility check only.
"""

import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "t113"
RUNTIME_MODULES = {
    "main.py", "app_meta.py", "async_log.py", "can_config.py",
    "can_configurator.py", "can_panel.py", "console_output.py",
    "driver_can_bus.py", "local_terminal.py", "network_panel.py",
    "qt_input_method.py", "quick_command_table.py", "serial_access.py",
    "serial_terminal.py", "socketcan_bus.py", "ssh_panel.py", "ssh_terminal.py",
}


class T113DeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="debugtool-t113-qa-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        spec = importlib.util.spec_from_file_location(
            "debugtool_t113_make_update", DEPLOY / "make_update.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        cls.archive = Path(builder.make_package(cls.root / "output"))
        if not cls.archive.is_file():
            raise AssertionError("make_package must return the generated archive path")
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        cls.package_name = "DebugTool-{}-t113-update".format(version)
        cls.extracted = cls.root / "extracted"
        cls.extracted.mkdir()
        with tarfile.open(cls.archive, "r:gz") as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if (not parts or parts[0] != cls.package_name
                        or member.name.startswith("/") or ".." in parts
                        or not (member.isfile() or member.isdir())):
                    raise AssertionError("unsafe or unexpected archive member: " + member.name)
            archive.extractall(cls.extracted)
        cls.package = cls.extracted / cls.package_name

    def new_target(self, name):
        target = self.root / self.id().rsplit(".", 1)[-1] / name
        target.mkdir(parents=True)
        return target

    def update(self, package, target):
        """Service operations are forbidden even if an updater regresses."""
        command_dir = target.parent / "blocked-commands"
        command_dir.mkdir(exist_ok=True)
        command_log = target.parent / "service-operations.log"
        for command in ("systemctl", "service", "sudo", "pkexec", "reboot", "shutdown"):
            executable = command_dir / command
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$T113_QA_COMMAND_LOG"\nexit 97\n',
                encoding="utf-8")
            executable.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(command_dir) + os.pathsep + env.get("PATH", "")
        env["T113_QA_COMMAND_LOG"] = str(command_log)
        result = subprocess.run(
            ["sh", str(package / "update.sh"), str(target)],
            cwd=str(target.parent), env=env, capture_output=True, text=True,
            timeout=30, check=False)
        self.assertFalse(command_log.exists(), "updater attempted a service/privilege operation")
        return result

    def test_package_has_complete_current_runtime_sources_and_assets(self):
        app = self.package / "app"
        files = {path.name for path in app.iterdir() if path.is_file()}
        self.assertTrue(RUNTIME_MODULES | {"VERSION", "app.png", "touch_poll.py"} <= files)
        self.assertNotIn("serial_tool.ini", files)
        self.assertNotIn("build.py", files)
        self.assertNotIn("gen_icon.py", files)
        self.assertFalse((self.package / "runtime").exists())
        self.assertFalse((app / ".venv").exists())
        for name in RUNTIME_MODULES - {"main.py"}:
            with self.subTest(source=name):
                self.assertEqual((app / name).read_bytes(), (REPO / name).read_bytes())
        for name in ("VERSION", "app.png"):
            self.assertEqual((app / name).read_bytes(), (REPO / name).read_bytes())
        for name in ("bin/python", "bin/qt5com", "fonts.conf", "qt5com.service",
                     "SHA256SUMS", "README.md", "update.sh"):
            self.assertTrue((self.package / name).is_file(), name)

    def test_all_sources_parse_as_python38_and_local_imports_are_shipped(self):
        app = self.package / "app"
        modules = {path.stem for path in REPO.glob("*.py")} | {"touch_poll"}
        for path in sorted(app.glob("*.py")):
            with self.subTest(source=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name,
                                 feature_version=(3, 8))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module.split(".")[0]]
                    for name in set(names) & modules:
                        self.assertTrue((app / (name + ".py")).is_file(), name)

    def test_manifest_covers_every_other_regular_file_with_matching_sha256(self):
        manifest = self.package / "SHA256SUMS"
        entries = {}
        for line in manifest.read_text(encoding="utf-8").splitlines():
            digest, name = line.split(maxsplit=1)
            name = name.lstrip(" *")
            self.assertNotIn(name, entries)
            self.assertFalse(Path(name).is_absolute())
            self.assertNotIn("..", Path(name).parts)
            self.assertEqual(len(digest), 64)
            entries[name[2:] if name.startswith("./") else name] = digest
        expected = {
            path.relative_to(self.package).as_posix()
            for path in self.package.rglob("*")
            if path.is_file() and path != manifest
        }
        self.assertEqual(set(entries), expected)
        for name, digest in entries.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((self.package / name).read_bytes()).hexdigest(), digest)

    def test_shell_entrypoints_are_valid_and_launcher_touch_can_be_disabled(self):
        for name in ("bin/python", "bin/qt5com", "update.sh"):
            result = subprocess.run(["sh", "-n", str(self.package / name)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        fake_home = self.new_target("launcher")
        (fake_home / "app").mkdir()
        (fake_home / "bin").mkdir()
        shutil.copyfile(self.package / "bin/qt5com", fake_home / "bin/qt5com")
        python = fake_home / "bin/python"
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$QT5COM_TOUCH_POLL" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        for value, expected in ((None, "1"), ("0", "0")):
            with self.subTest(touch_poll=value):
                env = os.environ.copy()
                env.pop("QT5COM_TOUCH_POLL", None)
                if value is not None:
                    env["QT5COM_TOUCH_POLL"] = value
                result = subprocess.run(
                    ["sh", str(fake_home / "bin/qt5com"), "--qa-argument"],
                    env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    expected, str(fake_home / "app/main.py"), "--qa-argument"])

    def test_touch_hook_starts_only_when_enabled_and_connects_cleanup(self):
        tree = ast.parse((self.package / "app/main.py").read_text(encoding="utf-8"))
        entrypoint = next(node for node in tree.body
                          if isinstance(node, ast.FunctionDef) and node.name == "main")
        code = compile(ast.Module(body=[entrypoint], type_ignores=[]), "t113-main-entry", "exec")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                events = []
                callbacks = []

                class App:
                    def __init__(self, args):
                        self.aboutToQuit = types.SimpleNamespace(connect=callbacks.append)

                    def __getattr__(self, name):
                        return lambda *args: None

                    def exec_(self):
                        return 0

                class Window:
                    def show(self):
                        events.append("show")

                class Poller:
                    def __init__(self, window):
                        events.append("poller")

                    def start(self):
                        events.append("start")

                    def stop(self):
                        events.append("stop")

                window = Window()
                touch = types.ModuleType("touch_poll")
                touch.FocalTechTouchPoller = Poller
                namespace = {
                    "QApplication": App, "QIcon": types.SimpleNamespace(fromTheme=lambda name: None),
                    "DebugTool": lambda: window, "APP_NAME": "DebugTool", "__version__": "qa",
                    "app_icon_path": lambda: "/nonexistent/t113-qa-icon.png",
                    "os": os, "sys": types.SimpleNamespace(argv=[], exit=lambda result: None),
                }
                exec(code, namespace)
                with patch.dict(os.environ, {"QT5COM_TOUCH_POLL": "1" if enabled else "0",
                                             "QT5COM_FULLSCREEN": "0"}), \
                        patch.dict(sys.modules, {"touch_poll": touch}), \
                        patch("builtins.print"):
                    namespace["main"]()
                self.assertEqual(events, ["show", "poller", "start"] if enabled else ["show"])
                self.assertEqual(len(callbacks), 1 if enabled else 0)
                if enabled:
                    self.assertIsInstance(window.touch_poller, Poller)
                    callbacks[0]()
                    self.assertEqual(events[-1], "stop")

    def test_update_preserves_configuration_runtime_and_unknown_board_files(self):
        target = self.new_target("existing-home")
        kept = {
            "app/serial_tool.ini": b"[settings]\ncustom=preserve\n",
            "app/site_local.py": b"# board local extension\n",
            "bin/site-tool": b"#!/bin/sh\nexit 0\n",
            "runtime/usr/qa-runtime-marker": b"independent ARM runtime\n",
            "operator-notes.txt": b"local board state\n",
        }
        for name, content in kept.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (target / "app/main.py").write_text("# previous source\n", encoding="utf-8")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.update(self.package, target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((target / "app/main.py").read_bytes(),
                                 (self.package / "app/main.py").read_bytes())
                for name, content in kept.items():
                    self.assertEqual((target / name).read_bytes(), content, name)
                for name in ("app/quick_command_table.py", "app/touch_poll.py", "bin/qt5com", "bin/python"):
                    self.assertEqual((target / name).read_bytes(), (self.package / name).read_bytes())

    def test_corrupt_package_is_rejected_before_overwriting_existing_source(self):
        target = self.new_target("checksum-target")
        (target / "app").mkdir()
        main = target / "app/main.py"
        original = b"# preserve existing application on checksum failure\n"
        main.write_bytes(original)
        damaged = target.parent / "damaged-package"
        shutil.copytree(self.package, damaged)
        with (damaged / "app/quick_command_table.py").open("ab") as stream:
            stream.write(b"\n# corrupted in transit\n")
        result = self.update(damaged, target)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(main.read_bytes(), original)
        self.assertFalse((target / "app/quick_command_table.py").exists())


if __name__ == "__main__":
    unittest.main()
