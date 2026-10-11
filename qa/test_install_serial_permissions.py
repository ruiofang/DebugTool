"""Installer ownership, config migration and serial rules in a temporary sysroot.

All user/group, udev and ACL changes use the guarded command wrappers shared
with CAN installer tests. No host installation, serial open or sudo is used.
"""

import os
import stat
import unittest

from qa import test_install_can_permissions as install_test_harness


class InstallSerialPermissionsTests(unittest.TestCase):
    setUp = install_test_harness.InstallCanPermissionsTests.setUp
    shell = install_test_harness.InstallCanPermissionsTests.shell

    def installed_app(self):
        app = self.root / "opt/qt5com"
        app.mkdir(parents=True)
        (app / "DebugTool").write_text("installed binary unchanged", encoding="utf-8")
        return app

    def fallback_config(self):
        config = self.root / "home/testuser/.config/qt5com/serial_tool.ini"
        config.parent.mkdir(parents=True)
        config.write_text("[General]\nhistory=preserved\n", encoding="utf-8")
        return config

    def test_repair_sets_user_ownership_and_migrates_config_without_reinstalling(self):
        app = self.installed_app()
        previous = (app / "DebugTool").read_bytes()
        fallback = self.fallback_config()
        original = fallback.read_bytes()
        result = self.shell("repair_permissions", check=True)
        self.assertEqual((app / "DebugTool").read_bytes(), previous)
        self.assertEqual((app / "serial_tool.ini").read_bytes(), original)
        self.assertEqual(fallback.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(app.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((app / "serial_tool.ini").stat().st_mode), 0o600)
        calls = (self.root / "mutations.log").read_text(encoding="utf-8")
        self.assertIn("install -d -o 1234 -g 4321 -m 0755", calls)
        self.assertIn("chown 1234:4321", calls)
        self.assertIn("usermod -a -G dialout -- testuser", calls)
        self.assertIn("udevadm control --reload-rules", calls)
        self.assertIn("udevadm settle --timeout=5", calls)
        self.assertIn("Errno 11", result.stdout)
        self.assertFalse(self.can_dir.exists())
        self.assertFalse(self.policy.exists())

    def test_existing_portable_config_wins_and_survives_repeated_repairs(self):
        app = self.installed_app()
        portable = app / "serial_tool.ini"
        portable.write_text("[General]\nhistory=portable\n", encoding="utf-8")
        portable.chmod(0o644)
        fallback = self.fallback_config()
        for _ in range(2):
            self.shell("repair_permissions", check=True)
            self.assertIn("history=portable", portable.read_text(encoding="utf-8"))
            self.assertIn("history=preserved", fallback.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(portable.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in app.parent.iterdir()), ["qt5com"])

    def test_new_config_is_initialized_with_private_mode(self):
        app = self.installed_app()
        self.shell("repair_permissions", check=True)
        self.assertEqual((app / "serial_tool.ini").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE((app / "serial_tool.ini").stat().st_mode), 0o600)

    def test_serial_rules_limit_access_to_serial_hardware_and_precede_uaccess(self):
        self.installed_app()
        self.shell("repair_permissions", check=True)
        rules = self.root / "etc/udev/rules.d/70-debugtool-serial.rules"
        contents = rules.read_text(encoding="utf-8")
        lines = [line for line in contents.splitlines() if not line.startswith("#")]
        self.assertEqual(len(lines), 6)
        for prefix in ("ttyUSB", "ttyACM", "ttyS", "ttyAMA", "ttyTHS", "ttymxc"):
            self.assertIn('KERNEL=="%s[0-9]*"' % prefix, contents)
        self.assertTrue(all('SUBSYSTEM=="tty"' in line and 'GROUP="dialout"' in line
                            and 'MODE="0660"' in line and 'TAG+="uaccess"' in line
                            for line in lines))
        self.assertEqual(stat.S_IMODE(rules.stat().st_mode), 0o644)
        self.assertLess(rules.name, "73-seat-late.rules")

    def test_uucp_is_supported_when_dialout_is_absent(self):
        self.installed_app()
        self.shell(r'''
getent() {
    case "$*" in
        'passwd testuser') printf 'testuser:x:1234:4321::%s/home/testuser:/bin/bash\n' "$TEST_ROOT" ;;
        'group uucp') printf 'uucp:x:14:\n' ;;
        *) return 1 ;;
    esac
}
repair_permissions
''', check=True)
        calls = (self.root / "mutations.log").read_text(encoding="utf-8")
        self.assertIn("usermod -a -G uucp -- testuser", calls)
        rules = self.root / "etc/udev/rules.d/70-debugtool-serial.rules"
        self.assertIn('GROUP="uucp"', rules.read_text(encoding="utf-8"))

    def test_root_without_selected_user_fails_before_changes(self):
        app = self.installed_app()
        for sudo_user in ("", "root"):
            with self.subTest(sudo_user=sudo_user):
                result = self.shell('SUDO_USER="%s"; repair_permissions' % sudo_user)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--user", result.stderr)
                self.assertFalse((app / "serial_tool.ini").exists())
                self.assertFalse((self.root / "mutations.log").exists())

    def test_explicit_user_supports_root_login_and_repair_needs_no_artifact(self):
        app = self.installed_app()
        self.shell('SUDO_USER=root; main --permissions-only --user testuser', check=True)
        self.assertTrue((app / "serial_tool.ini").exists())
        self.assertFalse((self.source / "DebugTool").exists())

    def test_unknown_user_and_missing_serial_group_fail_before_changes(self):
        app = self.installed_app()
        for body in ('INSTALL_USER=missing; repair_permissions',
                     'resolve_install_user; getent() { return 1; }; check_serial_dependencies'):
            with self.subTest(body=body):
                result = self.shell(body)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((app / "serial_tool.ini").exists())
                self.assertFalse((self.root / "mutations.log").exists())

    def test_missing_python_fails_before_directory_permissions_change(self):
        app = self.installed_app()
        app.chmod(0o700)
        result = self.shell('CAN_PYTHON="$TEST_ROOT/missing"; repair_permissions')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python", result.stderr)
        self.assertEqual(stat.S_IMODE(app.stat().st_mode), 0o700)
        self.assertFalse((self.root / "mutations.log").exists())

    def test_install_directory_symlink_is_rejected(self):
        target = self.root / "untouched"
        target.mkdir()
        (self.root / "opt").mkdir()
        (self.root / "opt/qt5com").symlink_to(target, target_is_directory=True)
        result = self.shell("repair_permissions")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_portable_config_symlink_cannot_modify_or_copy_its_target(self):
        app = self.installed_app()
        target = self.root / "untouched"
        target.write_text("private", encoding="utf-8")
        target.chmod(0o640)
        (app / "serial_tool.ini").symlink_to(target)
        result = self.shell("repair_permissions")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "private")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertFalse((self.root / "mutations.log").exists())

    def test_portable_config_hardlink_is_rejected_without_changing_original(self):
        app = self.installed_app()
        target = self.root / "untouched"
        target.write_text("private", encoding="utf-8")
        target.chmod(0o640)
        os.link(target, app / "serial_tool.ini")
        result = self.shell("repair_permissions")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "private")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_fallback_config_symlink_is_not_followed(self):
        app = self.installed_app()
        fallback = self.fallback_config()
        original = self.root / "untouched"
        fallback.rename(original)
        fallback.symlink_to(original)
        self.shell("repair_permissions", check=True)
        self.assertEqual((app / "serial_tool.ini").read_bytes(), b"")
        self.assertIn("history=preserved", original.read_text(encoding="utf-8"))

    def test_binary_symlink_is_replaced_atomically_without_overwriting_target(self):
        app = self.installed_app()
        target = self.root / "untouched"
        target.write_text("private", encoding="utf-8")
        (app / "DebugTool").unlink()
        (app / "DebugTool").symlink_to(target)
        (self.source / "new-binary").write_text("replacement", encoding="utf-8")
        self.shell('install_app_file "$HERE/new-binary" "$INSTALL_DIR/DebugTool" 0755', check=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "private")
        self.assertFalse((app / "DebugTool").is_symlink())
        self.assertEqual((app / "DebugTool").read_text(encoding="utf-8"), "replacement")
        self.assertEqual(stat.S_IMODE((app / "DebugTool").stat().st_mode), 0o755)

    def test_argument_conflicts_are_rejected(self):
        for args in ('--user', '--user root', '--unknown',
                     '--permissions-only --uninstall', '--permissions-only artifact',
                     'one two'):
            with self.subTest(args=args):
                result = self.shell("main " + args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "mutations.log").exists())


if __name__ == "__main__":
    unittest.main()
