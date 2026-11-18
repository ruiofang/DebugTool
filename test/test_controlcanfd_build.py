"""Native CANFD packaging checks without loading SDK code or probing hardware."""

from contextlib import ExitStack
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import build
import controlcanfd_bus as native


class ControlCanFdBuildTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(build, 'HERE', self.root))
        self.stack.enter_context(patch.object(sys, 'platform', 'linux'))
        self.arch = self.stack.enter_context(patch.object(native, 'sdk_arch', return_value='x86_64'))
        self.stderr = self.stack.enter_context(patch.object(sys, 'stderr', new_callable=io.StringIO))
        self.loader = self.stack.enter_context(patch.object(native, '_load_library',
            side_effect=AssertionError('A build must not load or enumerate the USB driver')))

    def library(self, arch):
        path = self.root / native.SDK_DIRECTORY / arch / 'libcontrolcanfd.so'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'packaging fixture, never loaded')
        return path

    def test_bundle_only_selected_process_architecture(self):
        for arch in ('x86_64', 'x86_32', 'aarch64', 'aarch32', 'riscv64'):
            self.library(arch)
        for arch in ('x86_64', 'x86_32', 'aarch64', 'aarch32', 'riscv64'):
            with self.subTest(arch=arch):
                self.arch.return_value = arch
                expected = str(self.root / native.SDK_DIRECTORY / arch / 'libcontrolcanfd.so')
                self.assertEqual(build.controlcanfd_packaging_options(), [
                    '--add-binary', expected + os.pathsep + 'lib/controlcanfd/' + arch])
        self.assertEqual(self.stderr.getvalue(), '')
        self.loader.assert_not_called()

    def test_missing_sdk_aborts_packaging_before_starting_pyinstaller(self):
        (self.root / 'VERSION').write_text('V1.0.6\n', encoding='utf-8')
        self.library('aarch64')  # A different architecture cannot satisfy the build.
        with patch.object(build, 'ensure_pyinstaller') as ensure, \
                patch.object(build.subprocess, 'check_call') as run, \
                patch.object(sys, 'stdout', new_callable=io.StringIO):
            self.assertEqual(build.main(['--yes']), 1)
        self.assertIn('打包失败', self.stderr.getvalue())
        self.assertIn('x86_64/libcontrolcanfd.so', self.stderr.getvalue())
        self.assertIn('重新打包', self.stderr.getvalue())
        ensure.assert_not_called()
        run.assert_not_called()
        self.loader.assert_not_called()

    def test_unsupported_architecture_is_an_explicit_optional_backend_warning(self):
        self.arch.side_effect = RuntimeError('unsupported test architecture')
        self.assertEqual(build.controlcanfd_packaging_options(), [])
        self.assertIn('unsupported test architecture', self.stderr.getvalue())
        self.assertIn('创芯科技 CANFD', self.stderr.getvalue())

    def test_windows_and_macos_keep_existing_packaging(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                self.assertEqual(build.controlcanfd_packaging_options(), [])
        self.arch.assert_not_called()
        self.loader.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), '')

    def test_linux_build_command_includes_sdk_in_runtime_search_directory(self):
        library = self.library('x86_64')
        (self.root / 'VERSION').write_text('V1.0.5\n', encoding='utf-8')
        with patch.object(build, 'ensure_pyinstaller'), patch.object(build, 'ensure_icon'), \
                patch.object(build, 'can_packaging_options', return_value=[]), \
                patch.object(build.subprocess, 'check_call') as run, \
                patch.dict(sys.modules, {'paramiko': Mock(), 'pyte': Mock()}), \
                patch.object(sys, 'stdout', new_callable=io.StringIO):
            build.build()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--add-binary') + 1],
                         str(library) + os.pathsep + 'lib/controlcanfd/x86_64')
        self.loader.assert_not_called()


class ControlCanFdLibraryPathTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / 'source'
        self.source.mkdir()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        unrelated = self.root / 'unrelated-working-directory'
        unrelated.mkdir()
        self.stack.callback(os.chdir, Path.cwd())
        os.chdir(unrelated)
        self.stack.enter_context(patch.object(native, '__file__', str(self.source / 'controlcanfd_bus.py')))
        self.stack.enter_context(patch.object(native, 'sdk_arch', return_value='x86_64'))
        self.stack.enter_context(patch.object(native, 'sys', SimpleNamespace()))

    def library(self, root, directory='lib/controlcanfd'):
        library = root / directory / 'x86_64/libcontrolcanfd.so'
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_bytes(b'path lookup fixture, never loaded')
        return library

    def test_source_lookup_uses_dedicated_directory_independent_of_cwd(self):
        expected = self.library(self.source)
        self.library(Path.cwd())  # Working-directory files must not shadow source libraries.
        self.assertEqual(native.library_path(), expected)

    def test_frozen_lookup_uses_same_relative_path_inside_bundle(self):
        self.library(self.source)
        expected = self.library(self.bundle)
        with patch.object(native, 'sys', SimpleNamespace(_MEIPASS=str(self.bundle))):
            self.assertEqual(native.library_path(), expected)

    def test_missing_bundled_library_cannot_fall_back_to_source_or_working_directory(self):
        self.library(self.source)
        self.library(Path.cwd())
        with patch.object(native, 'sys', SimpleNamespace(_MEIPASS=str(self.bundle))), \
                self.assertRaisesRegex(FileNotFoundError, 'lib/controlcanfd/x86_64/libcontrolcanfd.so'):
            native.library_path()

    def test_source_lookup_has_no_legacy_doc_or_drivers_fallback(self):
        for directory in ('DOC/Linux资料包V1.03/二次开发库文件/libcontrolcanfd', 'drivers/controlcanfd'):
            self.library(self.source, directory)
        with self.assertRaisesRegex(FileNotFoundError, 'lib/controlcanfd/x86_64/libcontrolcanfd.so'):
            native.library_path()


class ControlCanFdArchitectureTests(unittest.TestCase):
    def test_python_bitness_selects_library_when_kernel_cpu_is_64_bit(self):
        for machine, pointer_bytes, expected in (
                ('x86_64', 4, 'x86_32'), ('AMD64', 8, 'x86_64'),
                ('aarch64', 4, 'aarch32'), ('arm64', 8, 'aarch64'),
                ('riscv64', 8, 'riscv64')):
            with self.subTest(machine=machine, pointer_bytes=pointer_bytes), \
                    patch.object(native.platform, 'machine', return_value=machine), \
                    patch.object(native.C, 'sizeof', return_value=pointer_bytes), \
                    patch.object(native, 'sys', SimpleNamespace(byteorder='little')):
                self.assertEqual(native.sdk_arch(), expected)

    def test_unavailable_architecture_and_endianness_are_rejected(self):
        for machine, byteorder in (('ppc64le', 'little'), ('aarch64', 'big')):
            with self.subTest(machine=machine, byteorder=byteorder), \
                    patch.object(native.platform, 'machine', return_value=machine), \
                    patch.object(native, 'sys', SimpleNamespace(byteorder=byteorder)), \
                    self.assertRaisesRegex(RuntimeError, '不支持当前架构'):
                native.sdk_arch()


if __name__ == '__main__':
    unittest.main()
