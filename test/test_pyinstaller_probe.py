"""Scope the Linux ctypes probe workaround to the PyInstaller subprocess."""

from contextlib import ExitStack
import ctypes.util
import io
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import build


class PyInstallerProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        parent = ModuleType('PyInstaller')
        parent.__path__ = []
        self.packager = ModuleType('PyInstaller.__main__')
        self.packager.run = Mock()
        parent.__main__ = self.packager
        self.stack.enter_context(patch.dict(sys.modules, {
            'PyInstaller': parent, 'PyInstaller.__main__': self.packager}))
        self.stack.enter_context(patch.object(sys, 'platform', 'linux'))
        self.stack.enter_context(patch.object(sys, 'argv', ['-c', '--onefile', 'main.py']))
        self.stderr = self.stack.enter_context(patch.object(sys, 'stderr', new_callable=io.StringIO))

    def test_preserve_available_and_missing_probe_results_and_pass_packager_arguments(self):
        probe = Mock(side_effect=['libusb-1.0.so.0', None])
        observed = []
        def package(arguments):
            observed.extend([ctypes.util.find_library('usb-1.0'),
                             ctypes.util.find_library('absent')])
            return 17
        self.packager.run.side_effect = package
        with patch.object(ctypes.util, 'find_library', probe):
            self.assertEqual(build.run_pyinstaller(), 17)
            self.assertIs(ctypes.util.find_library, probe)
        self.assertEqual(observed, ['libusb-1.0.so.0', None])
        self.packager.run.assert_called_once_with(['--onefile', 'main.py'])
        self.assertEqual(self.stderr.getvalue(), '')

    def test_missing_intermediate_probe_file_becomes_none_with_warning(self):
        probe = Mock(side_effect=FileNotFoundError('liblibusb-1.0.a'))
        observed = []
        self.packager.run.side_effect = lambda arguments: observed.append(
            ctypes.util.find_library('libusb-1.0'))
        with patch.object(ctypes.util, 'find_library', probe):
            build.run_pyinstaller()
            self.assertIs(ctypes.util.find_library, probe)
        self.assertEqual(observed, [None])
        self.assertIn('liblibusb-1.0.a', self.stderr.getvalue())
        self.assertIn('libusb-1.0', self.stderr.getvalue())

    def test_other_probe_errors_propagate_and_restore_original_function(self):
        for error in (PermissionError('access denied'), RuntimeError('probe failed')):
            with self.subTest(error=type(error).__name__):
                probe = Mock(side_effect=error)
                self.packager.run.side_effect = lambda arguments: ctypes.util.find_library('usb-1.0')
                with patch.object(ctypes.util, 'find_library', probe):
                    with self.assertRaises(type(error)) as raised:
                        build.run_pyinstaller()
                    self.assertIs(raised.exception, error)
                    self.assertIs(ctypes.util.find_library, probe)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_packager_failure_also_restores_probe_function(self):
        probe = Mock(return_value=None)
        self.packager.run.side_effect = SystemExit(2)
        with patch.object(ctypes.util, 'find_library', probe):
            with self.assertRaises(SystemExit):
                build.run_pyinstaller()
            self.assertIs(ctypes.util.find_library, probe)

    def test_non_linux_helper_preserves_probe_behavior(self):
        probe = Mock(side_effect=FileNotFoundError('missing probe file'))
        self.packager.run.side_effect = lambda arguments: ctypes.util.find_library('usb-1.0')
        with patch.object(sys, 'platform', 'win32'), patch.object(ctypes.util, 'find_library', probe):
            with self.assertRaises(FileNotFoundError):
                build.run_pyinstaller()
            self.assertIs(ctypes.util.find_library, probe)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_platform_build_commands_keep_windows_module_entry_and_wrap_only_linux(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / 'VERSION').write_text('V1.0.5\n', encoding='utf-8')
            stack.enter_context(patch.object(build, 'HERE', root))
            stack.enter_context(patch.object(build, 'ensure_pyinstaller'))
            stack.enter_context(patch.object(build, 'ensure_icon'))
            stack.enter_context(patch.object(build, 'can_packaging_options', return_value=[]))
            stack.enter_context(patch.object(build, 'controlcanfd_packaging_options', return_value=[]))
            stack.enter_context(patch.dict(sys.modules, {'paramiko': Mock(), 'pyte': Mock()}))
            stack.enter_context(patch.object(sys, 'stdout', new_callable=io.StringIO))
            run = stack.enter_context(patch.object(build.subprocess, 'check_call'))
            for platform, expected in (
                    ('linux', [sys.executable, '-c', 'from build import run_pyinstaller; run_pyinstaller()']),
                    ('win32', [sys.executable, '-m', 'PyInstaller']),
                    ('darwin', [sys.executable, '-m', 'PyInstaller'])):
                with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                    build.build()
                    command = run.call_args.args[0]
                    self.assertEqual(command[:3], expected)
                    self.assertEqual(command[3], '--onefile')


if __name__ == '__main__':
    unittest.main()
