"""Exercise CAN permission installation entirely inside a temporary sysroot.

The installer is sourced, never run with sudo. Ownership is simulated because
the test process is an ordinary user. No polkit action or CAN device is used.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


REPO = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO / "install.sh"


# All mutating command wrappers reject non-test destinations. The only skipped
# removals are the installer's existing user-config cleanup during uninstall.
SANDBOX_SHELL = r'''
source "$1"
TEST_ROOT="$2"
HERE="$TEST_ROOT/source"
CAN_INSTALL_DIR="$TEST_ROOT/usr/local/libexec/debugtool"
CAN_LAUNCHER="$CAN_INSTALL_DIR/can-config"
CAN_HELPER="$CAN_INSTALL_DIR/can_config.py"
CAN_POLICY_DIR="$TEST_ROOT/usr/share/polkit-1/actions"
CAN_POLICY="$CAN_POLICY_DIR/org.ruio.debugtool.configure-can.policy"
CAN_PYTHON=/usr/bin/python3
CAN_PKEXEC="$TEST_ROOT/bin/pkexec"
CAN_IP="$TEST_ROOT/bin/ip"
INSTALL_DIR="$TEST_ROOT/opt/qt5com"
BIN_LINK="$TEST_ROOT/usr/local/bin/DebugTool"
LEGACY_BIN_LINK="$TEST_ROOT/usr/local/bin/qt5com"
DESKTOP_FILE="$TEST_ROOT/usr/share/applications/qt5com.desktop"
ICON_DEST="$TEST_ROOT/usr/share/pixmaps/qt5com.png"
THEME_ICON_DEST="$TEST_ROOT/usr/share/icons/hicolor/256x256/apps/qt5com.png"
ICON_THEME_DIR="$TEST_ROOT/usr/share/icons/hicolor"
SERIAL_RULES_DIR="$TEST_ROOT/etc/udev/rules.d"
SERIAL_RULES="$SERIAL_RULES_DIR/70-debugtool-serial.rules"
SERIAL_DEVICE_DIR="$TEST_ROOT/dev"
SUDO_USER='testuser'

need_root() { :; }
getent() {
    case "$*" in
        'passwd testuser') printf 'testuser:x:1234:4321::%s/home/testuser:/bin/bash\n' "$TEST_ROOT" ;;
        'group dialout') printf 'dialout:x:20:\n' ;;
        *) return 1 ;;
    esac
}
usermod() { printf '%s\n' "usermod $*" >> "$TEST_ROOT/mutations.log"; }
udevadm() { printf '%s\n' "udevadm $*" >> "$TEST_ROOT/mutations.log"; }
setfacl() {
    test_destination "${!#}" || return
    printf '%s\n' "setfacl $*" >> "$TEST_ROOT/mutations.log"
}
update-desktop-database() { :; }
gtk-update-icon-cache() { :; }

test_destination() {
    [[ "$1" == "$TEST_ROOT" || "$1" == "$TEST_ROOT"/* ]] || {
        echo "test rejected non-temporary destination: $1" >&2
        return 96
    }
}
install() {
    local final="${!#}" skip=0 arg
    local -a actual=()
    test_destination "$final" || return
    printf '%s\n' "install $*" >> "$TEST_ROOT/mutations.log"
    for arg in "$@"; do
        if (( skip )); then skip=0; continue; fi
        case "$arg" in
            -o|-g|--owner|--group) skip=1 ;;
            --owner=*|--group=*) ;;
            *) actual+=("$arg") ;;
        esac
    done
    command install "${actual[@]}"
}
chown() {
    test_destination "${!#}" || return
    printf '%s\n' "chown $*" >> "$TEST_ROOT/mutations.log"
}
chmod() {
    test_destination "${!#}" || return
    command chmod "$@"
}
mkdir() {
    local arg
    for arg in "$@"; do
        [[ "$arg" == -* ]] || test_destination "$arg" || return
    done
    command mkdir "$@"
}
ln() {
    test_destination "${!#}" || return
    command ln "$@"
}
mv() {
    test_destination "${!#}" || return
    command mv "$@"
}
rm() {
    local arg
    printf '%s\n' "rm $*" >> "$TEST_ROOT/mutations.log"
    for arg in "$@"; do
        [[ "$arg" == -* ]] && continue
        case "$arg" in
            /root/.config/qt5com|/home/*/.config/qt5com) return 0 ;;
            *) test_destination "$arg" || return ;;
        esac
    done
    command rm "$@"
}

# Keep the real mode and symlink behavior, but pretend temporary files belong
# to root. /tmp itself is writable by design, so simulate its system-parent
# counterpart as 0755. Individual tests inject unsafe metadata explicitly.
stat() {
    local format='' previous='' arg target="${!#}" owner=0 mode output
    for arg in "$@"; do
        case "$previous" in -c|--format|--printf) format="$arg" ;; esac
        case "$arg" in --format=*|--printf=*) format="${arg#*=}" ;; esac
        previous="$arg"
    done
    if [[ "$format" != *%u* && "$format" != *%a* ]]; then
        command stat "$@"
        return
    fi
    mode=$(command stat -c '%a' -- "$target") || return
    [[ "$target" != /tmp ]] || mode=755
    [[ "$target" != "${UNSAFE_OWNER:-}" ]] || owner=1000
    output="${format//%u/$owner}"
    output="${output//%a/$mode}"
    printf '%s\n' "$output"
}
'''


class InstallCanPermissionsTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        self.temp = tempfile.TemporaryDirectory(prefix="debugtool-install-can-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        shutil.copyfile(REPO / "can_config.py", self.source / "can_config.py")
        self.can_dir = self.root / "usr/local/libexec/debugtool"
        self.launcher = self.can_dir / "can-config"
        self.helper = self.can_dir / "can_config.py"
        self.policy_dir = self.root / "usr/share/polkit-1/actions"
        self.policy_dir.mkdir(parents=True)
        self.policy = self.policy_dir / "org.ruio.debugtool.configure-can.policy"
        (self.root / "bin").mkdir()
        for name in ("ip", "pkexec"):
            path = self.root / "bin" / name
            path.write_text("#!/bin/sh\nexit 92\n", encoding="utf-8")
            path.chmod(0o755)

    def shell(self, body, *, check=False):
        result = subprocess.run(
            ["bash", "-c", SANDBOX_SHELL + "\n" + body,
             "installer-can-test", str(INSTALL_SCRIPT), str(self.root)],
            text=True, capture_output=True, check=False, timeout=10,
            cwd=str(self.root), env=os.environ.copy())
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def install_permissions(self):
        return self.shell("install_can_permissions", check=True)

    def test_policy_authorizes_only_dedicated_launcher_for_active_local_users(self):
        self.install_permissions()
        tree = ET.parse(self.policy)
        actions = tree.getroot().findall("action")
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.get("id"), "org.ruio.debugtool.configure-can")
        defaults = action.find("defaults")
        self.assertEqual(defaults.findtext("allow_active"), "yes")
        self.assertEqual(defaults.findtext("allow_inactive"), "auth_admin")
        self.assertEqual(defaults.findtext("allow_any"), "auth_admin")
        annotations = {item.get("key"): item.text for item in action.findall("annotate")}
        self.assertEqual(annotations.get("org.freedesktop.policykit.exec.path"),
                         str(self.launcher))
        self.assertNotIn("org.freedesktop.policykit.exec.allow_gui", annotations)

    def test_installed_helper_is_separate_and_owned_with_restrictive_modes(self):
        self.install_permissions()
        self.assertEqual(self.helper.read_bytes(), (self.source / "can_config.py").read_bytes())
        for path, expected in ((self.can_dir, 0o755), (self.launcher, 0o755),
                               (self.helper, 0o644), (self.policy, 0o644)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))
        calls = (self.root / "mutations.log").read_text(encoding="utf-8")
        self.assertIn("-o root", calls)
        self.assertIn("-g root", calls)
        self.assertNotIn("setcap", calls)

    def test_launcher_isolates_python_and_forwards_literal_arguments(self):
        self.install_permissions()
        self.helper.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ), "
            "'isolated':sys.flags.isolated, 'no_site':sys.flags.no_site, "
            "'python':sys.executable}))\n", encoding="utf-8")
        marker = self.root / "unexpected"
        (self.root / "sitecustomize.py").write_text(
            "from pathlib import Path\nPath(%r).touch()\n" % str(marker), encoding="utf-8")
        injected = "$(touch %s); --help" % marker
        args = ["--interface", "vcan0", "--down", injected]
        env = os.environ.copy()
        env.update(PYTHONPATH=str(self.root), PYTHONHOME="/does/not/exist",
                   DEBUGTOOL_TEST_SECRET="should-not-survive", _PYI_TEST="should-not-survive")
        result = subprocess.run([str(self.launcher), *args], cwd=str(self.root),
                                text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], args)
        self.assertEqual(payload["isolated"], 1)
        self.assertEqual(payload["no_site"], 1)
        self.assertEqual(payload["python"], "/usr/bin/python3")
        self.assertEqual(payload["env"].get("LC_ALL"), "C")
        for variable in ("PYTHONPATH", "PYTHONHOME", "DEBUGTOOL_TEST_SECRET", "_PYI_TEST"):
            self.assertNotIn(variable, payload["env"])
        self.assertFalse(marker.exists())

    def test_reinstall_updates_helper_and_policy_without_duplicates(self):
        self.install_permissions()
        self.policy.write_text("obsolete policy", encoding="utf-8")
        self.launcher.write_text("obsolete launcher", encoding="utf-8")
        new_source = (self.source / "can_config.py").read_text(encoding="utf-8") + "\n# update\n"
        (self.source / "can_config.py").write_text(new_source, encoding="utf-8")
        self.install_permissions()
        self.assertEqual(self.helper.read_text(encoding="utf-8"), new_source)
        self.assertEqual(len(ET.parse(self.policy).getroot().findall("action")), 1)
        self.assertIn("/usr/bin/python3", self.launcher.read_text(encoding="utf-8"))
        self.assertEqual(sorted(path.name for path in self.can_dir.iterdir()),
                         ["can-config", "can_config.py"])

    def test_main_uninstall_retains_can_helper_and_policy(self):
        self.install_permissions()
        before = {path: path.read_bytes() for path in (self.helper, self.launcher, self.policy)}
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in before}
        app = self.root / "opt/qt5com"
        app.mkdir(parents=True)
        (app / "DebugTool").write_text("not executable", encoding="utf-8")
        result = self.shell("uninstall", check=True)
        self.assertFalse(app.exists())
        self.assertIn("CAN", result.stdout)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[path])
        # The retained entry must still run without the removed GUI directory.
        help_result = subprocess.run([str(self.launcher), '--help'],
                                     text=True, capture_output=True, timeout=5)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--interface', help_result.stdout)

    def test_default_application_install_also_installs_can_permissions(self):
        artifact = self.source / "DebugTool"
        artifact.write_text("test artifact; never executed\n", encoding="utf-8")
        artifact.chmod(0o755)
        for relative in ("usr/local/bin", "usr/share/applications"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.shell('install_app "$HERE/DebugTool"', check=True)
        self.assertEqual((self.root / "opt/qt5com/DebugTool").read_bytes(), artifact.read_bytes())
        self.assertTrue(self.launcher.is_file())
        self.assertTrue(self.helper.is_file())
        self.assertTrue(self.policy.is_file())

    def test_required_dependencies_are_checked_without_executing_ip_or_pkexec(self):
        self.shell("check_can_dependencies", check=True)
        self.assertFalse(self.can_dir.exists())
        self.assertFalse(self.policy.exists())

    def test_missing_helper_is_reported_before_application_is_modified(self):
        (self.source / "can_config.py").unlink()
        (self.source / "DebugTool").write_text("never executed\n", encoding="utf-8")
        result = self.shell('install_app "$HERE/DebugTool"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("can_config.py", result.stdout + result.stderr)
        self.assertFalse((self.root / "opt/qt5com").exists())
        self.assertFalse(self.policy.exists())

    def test_dependency_failure_preserves_existing_application(self):
        app = self.root / "opt/qt5com"
        app.mkdir(parents=True)
        installed = app / "DebugTool"
        installed.write_text("previous version", encoding="utf-8")
        (self.source / "DebugTool").write_text("replacement version", encoding="utf-8")
        result = self.shell('CAN_PKEXEC="$TEST_ROOT/missing"; install_app "$HERE/DebugTool"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pkexec", result.stdout + result.stderr)
        self.assertEqual(installed.read_text(encoding="utf-8"), "previous version")
        self.assertFalse(self.policy.exists())

    def test_missing_python_ip_or_pkexec_fails_preflight(self):
        for variable, hint in (("CAN_PYTHON", "python"), ("CAN_IP", "ip"),
                               ("CAN_PKEXEC", "pkexec")):
            with self.subTest(variable=variable):
                result = self.shell('%s="$TEST_ROOT/missing"; check_can_dependencies' % variable)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(hint, (result.stdout + result.stderr).lower())
                self.assertFalse(self.can_dir.exists())

    def test_old_system_python_fails_preflight(self):
        old_python = self.root / "bin/old-python"
        old_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        old_python.chmod(0o755)
        result = self.shell('CAN_PYTHON="$TEST_ROOT/bin/old-python"; check_can_dependencies')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("3.7", result.stdout + result.stderr)

    def test_missing_polkit_actions_directory_fails_preflight(self):
        self.policy_dir.rmdir()
        result = self.shell("check_can_dependencies")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("polkit", (result.stdout + result.stderr).lower())
        self.assertFalse(self.policy_dir.exists())

    def test_writable_or_nonroot_system_parent_is_rejected(self):
        parent = self.root / "usr/local"
        parent.mkdir(parents=True)
        for mode in (0o775, 0o777):
            with self.subTest(mode=oct(mode)):
                parent.chmod(mode)
                result = self.shell('ensure_secure_directory "$CAN_INSTALL_DIR"')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.can_dir.exists())
        parent.chmod(0o755)
        result = self.shell('UNSAFE_OWNER="$TEST_ROOT/usr/local"; '
                            'ensure_secure_directory "$CAN_INSTALL_DIR"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.can_dir.exists())

    def test_symlink_parent_is_rejected_without_writing_through_it(self):
        target = self.root / "redirected"
        target.mkdir()
        (self.root / "usr/local").symlink_to(target, target_is_directory=True)
        result = self.shell('ensure_secure_directory "$CAN_INSTALL_DIR"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_existing_helper_or_policy_symlink_cannot_overwrite_its_target(self):
        self.can_dir.mkdir(parents=True)
        target = self.root / "untouched"
        target.write_text("must survive", encoding="utf-8")
        for destination in (self.helper, self.launcher, self.policy):
            with self.subTest(destination=destination.name):
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                destination.symlink_to(target)
                result = self.shell("install_can_permissions")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(target.read_text(encoding="utf-8"), "must survive")
                destination.unlink()

    def test_writable_existing_privileged_files_are_rejected(self):
        self.install_permissions()
        for destination in (self.helper, self.launcher, self.policy):
            with self.subTest(destination=destination.name):
                original = destination.read_bytes()
                mode = stat.S_IMODE(destination.stat().st_mode)
                destination.chmod(mode | 0o020)
                result = self.shell("check_can_dependencies")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(destination), result.stderr)
                self.assertEqual(destination.read_bytes(), original)
                destination.chmod(mode)

    def test_failed_launcher_install_does_not_publish_authorization(self):
        result = self.shell(r'''
mv() {
    test_destination "${!#}" || return
    [[ "${!#}" != "$CAN_LAUNCHER" ]] || return 93
    command mv "$@"
}
install_can_permissions
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.helper.exists())
        self.assertFalse(self.launcher.exists())
        self.assertFalse(self.policy.exists())
        self.assertEqual(sorted(path.name for path in self.can_dir.iterdir()), ["can_config.py"])


if __name__ == "__main__":
    unittest.main()
