"""Exercise Linux artifact selection in temporary dirs, never system installation."""

from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


class InstallVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="debugtool-install-version-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "dist").mkdir()

    def artifact(self, version, executable=True, suffix=""):
        path = self.root / "dist" / ("DebugTool-{}-linux{}".format(version, suffix))
        path.write_text("test artifact; never executed\n", encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
        return path

    def version(self, text):
        (self.root / "VERSION").write_text(text, encoding="utf-8")

    def select(self, explicit=""):
        # Sourcing only defines functions. The source guard keeps install_app,
        # uninstall, root checks, and all real-system writes out of these tests.
        return subprocess.run(
            ["bash", "-c",
             'source "$1"; HERE="$2"; select_executable "$3"; printf "%s" "$EXE"',
             "installer-selection-test", str(INSTALL_SCRIPT), str(self.root), str(explicit)],
            text=True, capture_output=True, check=False, timeout=5)

    def test_version_selects_exact_artifact_even_when_newer_one_exists(self):
        self.version("V1.2.3\n")
        target = self.artifact("V1.2.3")
        self.artifact("V1.2.4")
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target))

    def test_version_without_matching_artifact_never_falls_back(self):
        self.version("V1.2.4\n")
        self.artifact("V1.2.3")
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VERSION 对应", result.stderr)

    def test_no_version_uses_numeric_sort_and_ignores_backups(self):
        self.artifact("V1.0.9")
        target = self.artifact("V1.0.10")
        self.artifact("V99.0.0", executable=False)
        self.artifact("V99.0.0", suffix=".before-can")
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target))

    def test_explicit_file_overrides_version(self):
        self.version("V1.2.4\n")
        target = self.artifact("V1.2.3", executable=False)
        result = self.select(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target))

    def test_missing_explicit_file_never_falls_back(self):
        self.artifact("V1.2.3")
        result = self.select(self.root / "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("指定的可执行文件不存在", result.stderr)

    def test_malformed_version_is_not_evaluated_or_used_as_a_path(self):
        for value in ("", "V1.2", "V1.2.3.4.5", "V65536.0.0", "V1. 2.3",
                      "V999999999999999999.1.1", "V1.2.3\nV2.0.0",
                      '$(touch "$HERE/unexpected")', "V../../bin/sh"):
            with self.subTest(value=value):
                self.version(value)
                result = self.select()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("VERSION", result.stderr)
                self.assertFalse((self.root / "unexpected").exists())

    def test_manual_version_is_normalized_like_build_metadata(self):
        target = self.artifact("V1.2.3")
        for value in ("1.2.3", "v01.02.03", "V00001.00002.00003",
                      " \t\r\n v01.02.03 \t\r\n"):
            with self.subTest(value=value):
                self.version(value)
                result = self.select()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(target))

    def test_zero_components_are_decimal_and_four_part_version_is_normalized(self):
        self.version("v00.000.0.00008")
        target = self.artifact("V0.0.0.8")
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target))

    def test_four_components_and_crlf_are_accepted(self):
        self.version("V1.2.3.65535\r\n")
        target = self.artifact("V1.2.3.65535")
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target))

    def test_matching_nonexecutable_artifact_is_rejected(self):
        self.version("V1.2.3\n")
        self.artifact("V1.2.3", executable=False)
        result = self.select()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VERSION 对应", result.stderr)


if __name__ == "__main__":
    unittest.main()
