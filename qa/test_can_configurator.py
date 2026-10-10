"""Async configuration lifecycle tests using fake jobs and harmless subprocesses."""

import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QObject, QProcess, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

import can_configurator as gui


class FakeJob(QObject):
    completed = pyqtSignal(int, str)

    def __init__(self, executable, args, elevated, interface):
        super().__init__()
        self.executable, self.args, self.elevated = executable, args, elevated
        self.interface = interface
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class CanConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.runner = gui.CanConfigurator()
        self.results = []
        self.runner.finished.connect(lambda ok, message: self.results.append((ok, message)))
        self.addCleanup(self.runner.cancel)

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(predicate())

    def test_auth_required_retries_once_through_graphical_pkexec(self):
        with patch('can_configurator._ProcessJob', FakeJob), \
                patch('can_configurator.find_pkexec', return_value='/usr/bin/pkexec'):
            self.runner.start('can_qa', 500000)
            ordinary = self.runner._job
            self.assertNotEqual(ordinary.executable, '/usr/bin/pkexec')
            ordinary.completed.emit(77, 'authorize')
            authorized = self.runner._job
            self.assertTrue(authorized.elevated)
            self.assertEqual(authorized.executable, '/usr/bin/pkexec')
            self.assertEqual(authorized.args[0], '--disable-internal-agent')
            self.assertTrue(self.runner.running)
            authorized.completed.emit(0, 'enabled')
            self.assertEqual(self.results, [(True, 'enabled')])
            self.assertFalse(self.runner.running)

    def test_ordinary_failure_does_not_request_privilege(self):
        with patch('can_configurator._ProcessJob', FakeJob), patch('can_configurator.find_pkexec') as pkexec:
            self.runner.start('can_qa', 500000)
            self.runner._job.completed.emit(1, '不存在')
            pkexec.assert_not_called()
            self.assertEqual(self.results, [(False, '不存在')])

    def test_cancel_then_restart_ignores_old_completion(self):
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.start('can_qa', 500000)
            old = self.runner._job
            self.runner.cancel()
            self.assertFalse(self.runner.running)
            self.assertTrue(old.cancelled)
            self.runner.start('can_new', 250000)
            new = self.runner._job
            old.completed.emit(0, 'stale')
            self.assertEqual(self.results, [])
            self.assertTrue(self.runner.running)
            new.completed.emit(0, 'current')
            self.assertEqual(self.results, [(True, 'current')])

    def test_same_interface_waits_for_cancelled_privileged_process_to_exit(self):
        old_job = Mock(interface='can_qa')
        old_job.process.state.return_value = QProcess.Running
        gui._LIVE_JOBS.add(old_job)
        self.addCleanup(gui._LIVE_JOBS.discard, old_job)
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.start('can_qa', 250000)
            self.assertFalse(self.runner.running)
            self.assertIn('仍在结束', self.results[-1][1])
            old_job.process.state.return_value = QProcess.NotRunning
            self.runner.start('can_qa', 250000)
            self.assertTrue(self.runner.running)

    def test_different_interface_can_configure_while_cancelled_process_finishes(self):
        old_job = Mock(interface='can_old')
        old_job.process.state.return_value = QProcess.Running
        gui._LIVE_JOBS.add(old_job)
        self.addCleanup(gui._LIVE_JOBS.discard, old_job)
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.start('can_qa', 250000)
            self.assertTrue(self.runner.running)

    def test_auth_cancel_and_failure_have_clear_messages(self):
        for code, expected in ((126, '取消系统认证'), (127, '桌面认证服务'), (77, '仍无法配置')):
            with self.subTest(code=code), patch('can_configurator._ProcessJob', FakeJob), \
                    patch('can_configurator.find_pkexec', return_value='/usr/bin/pkexec'):
                self.runner.start('can_qa', 500000)
                self.runner._job.completed.emit(77, '')
                self.runner._job.completed.emit(code, 'generic')
                self.assertFalse(self.results[-1][0])
                self.assertIn(expected, self.results[-1][1])
                self.assertFalse(self.runner.running)

    def test_missing_pkexec_is_actionable(self):
        with patch('can_configurator._ProcessJob', FakeJob), \
                patch('can_configurator.find_pkexec', return_value=None):
            self.runner.start('can_qa', 500000)
            self.runner._job.completed.emit(77, '')
            self.assertIn('安装 polkit', self.results[-1][1])
            self.assertFalse(self.runner.running)

    def test_invalid_input_does_not_launch_helper(self):
        with patch('can_configurator._ProcessJob') as job:
            self.runner.start('../can0', 500000)
            job.assert_not_called()
            self.assertFalse(self.results[-1][0])

    def test_frozen_helper_restarts_same_executable_with_dispatch_flag(self):
        with patch.object(sys, 'frozen', True, create=True):
            executable, args = gui.helper_command('can_qa', 500000, True, 2000000)
            self.assertEqual(executable, sys.executable)
            self.assertEqual(args[0], '--configure-can')
            self.assertIn('--fd', args)

    def test_source_helper_uses_isolated_python(self):
        executable, args = gui.helper_command('can_qa', 500000, False, 2000000)
        self.assertEqual(executable, sys.executable)
        self.assertEqual(args[0], '-I')
        self.assertTrue(args[1].endswith('/can_config.py'))

    def test_down_helper_arguments_never_include_bitrate_or_fd(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.object(sys, 'frozen', frozen, create=True):
                executable, args = gui.helper_command('can_qa', down=True)
                self.assertEqual(executable, sys.executable)
                self.assertEqual(args[-3:], ['--interface', 'can_qa', '--down'])
                self.assertNotIn('--bitrate', args)
                self.assertNotIn('--data-bitrate', args)
                self.assertNotIn('--fd', args)

    def test_stop_interface_is_async_and_needs_no_bitrate(self):
        progress = []
        self.runner.progress.connect(progress.append)
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.stop_interface('can_qa')
            job = self.runner._job
            self.assertTrue(job.started)
            self.assertTrue(self.runner.running)
            self.assertIn('--down', job.args)
            self.assertIn('停用', progress[-1])
            job.completed.emit(0, '')
            self.assertEqual(self.results, [(True, 'CAN 接口已停用（DOWN）')])
            self.assertFalse(self.runner.running)

    def test_down_auth_retry_preserves_operation_and_interface(self):
        progress = []
        self.runner.progress.connect(progress.append)
        with patch('can_configurator._ProcessJob', FakeJob), \
                patch('can_configurator.find_pkexec', return_value='/usr/bin/pkexec'):
            self.runner.stop_interface('can_qa')
            self.runner._job.completed.emit(77, 'authorize')
            authorized = self.runner._job
            self.assertTrue(authorized.elevated)
            self.assertEqual(authorized.args[-3:], ['--interface', 'can_qa', '--down'])
            self.assertIn('授权停用', progress[-1])
            authorized.completed.emit(0, 'can_qa DOWN verified')
            self.assertEqual(self.results, [(True, 'can_qa DOWN verified')])

    def test_down_authorization_failure_does_not_claim_interface_stopped(self):
        for code, expected in ((126, '停用 CAN 接口的系统认证已取消'),
                               (127, '桌面认证服务'), (77, '仍无法停用')):
            with self.subTest(code=code), patch('can_configurator._ProcessJob', FakeJob), \
                    patch('can_configurator.find_pkexec', return_value='/usr/bin/pkexec'):
                self.runner.stop_interface('can_qa')
                self.runner._job.completed.emit(77, '')
                self.runner._job.completed.emit(code, 'generic')
                ok, message = self.results[-1]
                self.assertFalse(ok)
                self.assertIn(expected, message)
                self.assertNotIn('已停用', message)
                self.assertNotIn('未打开', message)
                self.assertFalse(self.runner.running)

    def test_down_failure_without_privilege_request_is_forwarded(self):
        with patch('can_configurator._ProcessJob', FakeJob), \
                patch('can_configurator.find_pkexec') as pkexec:
            self.runner.stop_interface('can_qa')
            self.runner._job.completed.emit(1, '接口仍处于 UP 状态')
            self.assertEqual(self.results, [(False, '接口仍处于 UP 状态')])
            pkexec.assert_not_called()

    def test_down_cancel_ignores_late_completion(self):
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.stop_interface('can_qa')
            old_job = self.runner._job
            self.runner.cancel()
            self.assertTrue(old_job.cancelled)
            old_job.completed.emit(0, 'DOWN')
            self.assertEqual(self.results, [])
            self.assertFalse(self.runner.running)

    def test_down_waits_for_prior_operation_on_same_interface(self):
        old_job = Mock(interface='can_qa')
        old_job.process.state.return_value = QProcess.Running
        gui._LIVE_JOBS.add(old_job)
        self.addCleanup(gui._LIVE_JOBS.discard, old_job)
        with patch('can_configurator._ProcessJob', FakeJob):
            self.runner.stop_interface('can_qa')
            self.assertFalse(self.runner.running)
            self.assertIn('仍在结束', self.results[-1][1])
            self.assertIsNone(self.runner._job)

    def test_down_invalid_interface_is_rejected_before_launch(self):
        with patch('can_configurator._ProcessJob') as job:
            self.runner.stop_interface('../eth0')
            job.assert_not_called()
            self.assertFalse(self.results[-1][0])

    def test_down_missing_pkexec_message_names_stop_operation(self):
        with patch('can_configurator._ProcessJob', FakeJob), \
                patch('can_configurator.find_pkexec', return_value=None):
            self.runner.stop_interface('can_qa')
            self.runner._job.completed.emit(77, '')
            self.assertIn('自动停用', self.results[-1][1])
            self.assertIn('安装 polkit', self.results[-1][1])

    def test_real_subprocess_keeps_qt_timer_running_and_is_reaped(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        self.addCleanup(timer.stop)
        command = (sys.executable, ['-I', '-c', 'import time; time.sleep(.12); print("ready")'])
        with patch('can_configurator.helper_command', return_value=command):
            start = time.monotonic()
            self.runner.start('can_qa', 500000)
            self.assertLess(time.monotonic() - start, .08)
            self.wait_for(lambda: bool(self.results))
            self.assertEqual(self.results, [(True, 'ready')])
            self.assertGreater(len(ticks), 5)
            self.assertFalse(gui._LIVE_JOBS)

    def test_failed_process_start_reports_and_cleans_up(self):
        with patch('can_configurator.helper_command', return_value=('/nonexistent/debugtool-helper', [])):
            self.runner.start('can_qa', 500000)
            self.wait_for(lambda: bool(self.results))
            self.assertFalse(self.results[0][0])
            self.assertIn('无法启动', self.results[0][1])
            self.assertFalse(gui._LIVE_JOBS)

    def test_cancel_real_process_does_not_emit_finished_and_is_reaped(self):
        command = (sys.executable, ['-I', '-c', 'import time; time.sleep(10)'])
        with patch('can_configurator.helper_command', return_value=command):
            self.runner.start('can_qa', 500000)
            self.runner.cancel()
            self.wait_for(lambda: not gui._LIVE_JOBS)
            self.assertEqual(self.results, [])


if __name__ == '__main__':
    unittest.main()
