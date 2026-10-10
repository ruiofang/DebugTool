"""Version selection and packaging arguments; never run a real build or pip."""

from contextlib import ExitStack
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import app_meta
import build


class VersionMetadataTests(unittest.TestCase):
    def test_versions_are_normalized_for_display_and_filenames(self):
        for value, expected in (
                ('1.2.3', 'V1.2.3'), ('v2.3.4', 'V2.3.4'),
                ('V12.34.56.78', 'V12.34.56.78'),
                ('  v01.002.00003  ', 'V1.2.3'),
                ('65535.0.65535.0', 'V65535.0.65535.0')):
            with self.subTest(value=value):
                self.assertEqual(app_meta.normalize_version(value), expected)

    def test_invalid_versions_cannot_become_paths_or_windows_resources(self):
        for value in ('', '1', '1.2', '1.2.3.4.5', '-1.2.3', '1.2.3-beta',
                      '1.2.65536', '000001.2.3', '../1.2.3', '1.2.3/linux',
                      '1.2.3;echo test', '1. 2.3', 'V1.2.3\n4'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app_meta.normalize_version(value)

    def test_windows_numeric_version_is_always_four_components(self):
        self.assertEqual(app_meta.version_tuple('v1.2.3'), (1, 2, 3, 0))
        self.assertEqual(app_meta.version_tuple('V1.2.3.4'), (1, 2, 3, 4))

    def test_read_write_and_rejected_update_keep_one_authoritative_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'VERSION'
            path.write_text('V1.0.2\n', encoding='utf-8')
            self.assertEqual(app_meta.read_version(path), 'V1.0.2')
            self.assertEqual(app_meta.write_version('v2.3.4', path), 'V2.3.4')
            self.assertEqual(path.read_text(encoding='utf-8'), 'V2.3.4\n')
            with self.assertRaises(ValueError):
                app_meta.write_version('V2.3.99999', path)
            self.assertEqual(app_meta.read_version(path), 'V2.3.4')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_frozen_application_reads_bundled_version_not_external_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / '_MEI_test'
            bundled.mkdir()
            (bundled / 'VERSION').write_text('V2.3.4\n', encoding='utf-8')
            external = root / 'VERSION'
            external.write_text('V9.9.9\n', encoding='utf-8')
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, '_MEIPASS', str(bundled), create=True), \
                    patch.object(sys, 'executable', str(root / 'DebugTool-V2.3.4-linux')), \
                    patch.object(app_meta, 'VERSION_PATH', external):
                self.assertEqual(app_meta.read_version(), 'V2.3.4')


class BuildVersionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.version_path = self.directory / 'VERSION'
        self.version_path.write_text('V1.2.3\n', encoding='utf-8')
        self.resource_path = self.directory / 'version_info.txt'
        self.resource_path.write_text(
            "filevers=(9, 9, 9, 9)\nprodvers=(9, 9, 9, 9)\n"
            "StringStruct(u'FileVersion', u'V9.9.9.9')\n"
            "StringStruct(u'ProductVersion', u'V9.9.9.9')\n",
            encoding='utf-8',
        )
        self.entry = self.directory / 'main.py'
        self.entry.write_text('__version__ = "V0.0.0"\n', encoding='utf-8')
        self.stack.enter_context(patch.object(build, 'HERE', self.directory))
        self.stack.enter_context(patch.object(build, 'ENTRY', self.entry))
        self.stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
        self.stack.enter_context(patch('sys.stderr', new_callable=io.StringIO))
        self.stdin = self.stack.enter_context(patch('sys.stdin'))
        self.stdin.isatty.return_value = False
        self.prompt = self.stack.enter_context(patch('builtins.input'))
        self.packager = self.stack.enter_context(patch.object(build, 'ensure_pyinstaller'))
        self.stack.enter_context(patch.object(build, 'ensure_icon'))
        self.run = self.stack.enter_context(patch.object(build.subprocess, 'check_call'))
        self.find_spec = self.stack.enter_context(patch.object(
            build.importlib.util, 'find_spec', return_value=object()))
        self.stack.enter_context(patch.dict(sys.modules, {'paramiko': Mock(), 'pyte': Mock()}))

    def test_metadata_reads_version_file_instead_of_legacy_main_literal(self):
        self.assertEqual(build.read_meta(), ('V1.2.3', app_meta.AUTHOR))

    def test_cli_version_is_saved_before_build_and_does_not_prompt(self):
        observed = []
        with patch.object(build, 'build', side_effect=lambda: observed.append(build.read_meta()[0])):
            self.assertEqual(build.main(['--version', 'v2.3.4']), 0)
        self.assertEqual(observed, ['V2.3.4'])
        self.assertEqual(self.version_path.read_text(encoding='utf-8'), 'V2.3.4\n')
        self.prompt.assert_not_called()

    def test_set_version_only_saves_and_never_builds(self):
        with patch.object(build, 'build') as package:
            self.assertEqual(build.main(['--set-version', '3.4.5.6']), 0)
        self.assertEqual(build.read_meta()[0], 'V3.4.5.6')
        package.assert_not_called()
        self.packager.assert_not_called()
        self.run.assert_not_called()
        self.prompt.assert_not_called()

    def test_yes_uses_current_version_without_prompt_even_in_terminal(self):
        self.stdin.isatty.return_value = True
        with patch.object(build, 'build') as package:
            self.assertEqual(build.main(['--yes']), 0)
        package.assert_called_once_with()
        self.prompt.assert_not_called()
        self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_noninteractive_invocation_uses_current_version(self):
        with patch.object(build, 'build') as package:
            self.assertEqual(build.main([]), 0)
        package.assert_called_once_with()
        self.prompt.assert_not_called()
        self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_terminal_enter_keeps_current_version(self):
        self.stdin.isatty.return_value = True
        self.prompt.return_value = ''
        with patch.object(build, 'build') as package:
            self.assertEqual(build.main([]), 0)
        package.assert_called_once_with()
        self.prompt.assert_called_once()
        self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_terminal_retries_invalid_version_before_saving_and_building(self):
        self.stdin.isatty.return_value = True
        self.prompt.side_effect = ['1.2', 'v4.5.6']
        observed = []
        with patch.object(build, 'build', side_effect=lambda: observed.append(build.read_meta()[0])):
            self.assertEqual(build.main([]), 0)
        self.assertEqual(self.prompt.call_count, 2)
        self.assertEqual(observed, ['V4.5.6'])

    def test_terminal_eof_or_interrupt_cancels_without_changing_version(self):
        self.stdin.isatty.return_value = True
        for interrupt in (EOFError, KeyboardInterrupt):
            with self.subTest(interrupt=interrupt.__name__):
                self.prompt.side_effect = interrupt
                with patch.object(build, 'build') as package:
                    self.assertEqual(build.main([]), 1)
                package.assert_not_called()
                self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_invalid_cli_values_never_save_or_build(self):
        for option in ('--version', '--set-version'):
            with self.subTest(option=option), patch.object(build, 'build') as package:
                with self.assertRaises(SystemExit) as error:
                    build.main([option, '1.2.65536'])
                self.assertEqual(error.exception.code, 2)
                package.assert_not_called()
                self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_clean_does_not_prompt_or_require_valid_version(self):
        self.version_path.write_text('invalid\n', encoding='utf-8')
        self.stdin.isatty.return_value = True
        with patch.object(build, 'clean') as clean, patch.object(build, 'build') as package:
            self.assertEqual(build.main(['--clean']), 0)
        clean.assert_called_once_with()
        package.assert_not_called()
        self.prompt.assert_not_called()

    def test_clean_and_version_options_are_mutually_exclusive(self):
        for args in (['--clean', '--version', '2.3.4'],
                     ['--clean', '--set-version', '2.3.4'],
                     ['--version', '2.3.4', '--set-version', '3.4.5']):
            with self.subTest(args=args), patch.object(build, 'clean') as clean, \
                    patch.object(build, 'build') as package:
                with self.assertRaises(SystemExit) as error:
                    build.main(args)
                self.assertEqual(error.exception.code, 2)
                clean.assert_not_called()
                package.assert_not_called()
                self.assertEqual(build.read_meta()[0], 'V1.2.3')

    def test_linux_package_name_and_embedded_version_share_saved_value(self):
        with patch.object(sys, 'platform', 'linux'):
            build.build()
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index('--name') + 1], 'DebugTool-V1.2.3-linux')
        self.assertNotIn('--version-file', command)
        data = [command[index + 1] for index, value in enumerate(command) if value == '--add-data']
        self.assertTrue(any(value.startswith(str(self.version_path)) for value in data))
        self.assertEqual(command[-1], str(self.entry))
        self.assertEqual(self.run.call_args.kwargs['cwd'], str(self.directory))
        resource = self.resource_path.read_text(encoding='utf-8')
        self.assertIn('filevers=(1, 2, 3, 0)', resource)
        self.assertIn('prodvers=(1, 2, 3, 0)', resource)
        self.assertIn("u'FileVersion',      u'V1.2.3'", resource)
        self.assertIn("u'ProductVersion',   u'V1.2.3'", resource)
        self.assertIn("StringStruct(u'OriginalFilename', u'DebugTool-V1.2.3.exe')", resource)
        self.assertNotIn('9, 9, 9, 9', resource)
        self.assertNotIn('V9.9.9.9', resource)
        self.assertEqual(self.version_path.read_text(encoding='utf-8'), 'V1.2.3\n')

    def test_windows_resources_match_versioned_executable_name(self):
        self.version_path.write_text('V2.3.4.5\n', encoding='utf-8')
        with patch.object(sys, 'platform', 'win32'):
            build.build()
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index('--name') + 1], 'DebugTool-V2.3.4.5')
        self.assertEqual(command[command.index('--version-file') + 1], str(self.resource_path))
        hidden = [command[i + 1] for i, value in enumerate(command) if value == '--hidden-import']
        for module in ('can.interfaces.pcan', 'can.interfaces.gs_usb', 'usb.backend.libusb1', 'uptime'):
            self.assertIn(module, hidden)
        collected = [command[i + 1] for i, value in enumerate(command) if value == '--collect-all']
        self.assertIn('libusb_package', collected)
        resource = self.resource_path.read_text(encoding='utf-8')
        self.assertIn('filevers=(2, 3, 4, 5)', resource)
        self.assertIn('prodvers=(2, 3, 4, 5)', resource)
        self.assertIn("StringStruct(u'OriginalFilename', u'DebugTool-V2.3.4.5.exe')", resource)
        self.assertIn("u'FileVersion',      u'V2.3.4.5'", resource)
        self.assertIn("u'ProductVersion',   u'V2.3.4.5'", resource)
        self.assertNotIn('9, 9, 9, 9', resource)
        self.assertNotIn('V9.9.9.9', resource)
        self.assertEqual(self.version_path.read_text(encoding='utf-8'), 'V2.3.4.5\n')

    def test_windows_missing_can_dependency_stops_before_packaging(self):
        for missing in build.CAN_MODULES:
            with self.subTest(missing=missing), patch.object(sys, 'platform', 'win32'):
                self.find_spec.side_effect = lambda name: None if name == missing else object()
                with self.assertRaisesRegex(RuntimeError, '缺少 Windows CAN 打包依赖'):
                    build.build()
                self.run.assert_not_called()

    def test_linux_socketcan_packaging_does_not_require_usb_driver_packages(self):
        self.find_spec.return_value = None
        with patch.object(sys, 'platform', 'linux'):
            build.build()
        command = self.run.call_args.args[0]
        self.assertNotIn('can.interfaces.pcan', command)
        self.assertNotIn('can.interfaces.gs_usb', command)
        self.assertNotIn('libusb_package', command)


if __name__ == '__main__':
    unittest.main()
