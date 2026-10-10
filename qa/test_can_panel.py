"""CAN panel checks with an in-memory backend; never opens a real CAN device."""

from collections import deque
from contextlib import ExitStack
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QObject, QSettings, pyqtSignal
from PyQt5.QtWidgets import QApplication

import can_panel
from socketcan_bus import CanFrame, encode_frame


class FakeBus:
    def __init__(self, interface, **kwargs):
        self.interface = interface
        self.options = kwargs
        self.pending = deque()
        self.sent = []
        self.rx_dropped = 0
        self.receive_calls = 0
        self.closed = False
        self.send_error = None
        self.on_close = lambda: None

    def receive(self):
        self.receive_calls += 1
        if not self.pending:
            return None
        item = self.pending.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, frame):
        if self.send_error:
            raise self.send_error
        encode_frame(frame)
        self.sent.append(frame)

    def close(self):
        self.on_close()
        self.closed = True


class FakeConfigurator(QObject):
    """No OS changes; tests can defer completion to exercise authentication races."""
    finished = pyqtSignal(bool, str)
    progress = pyqtSignal(str)

    def __init__(self, parent=None, auto_complete=True):
        super().__init__(parent)
        self.auto_complete = auto_complete
        self.running = False
        self.request = None
        self.cancelled = False

    def start(self, interface, bitrate, fd, data_bitrate):
        self.request = (interface, bitrate, fd, data_bitrate)
        self.running = True
        if self.auto_complete:
            self.complete()

    def stop_interface(self, interface):
        self.request = ('down', interface)
        self.running = True
        if self.auto_complete:
            self.complete(message='接口已 DOWN')

    def complete(self, success=True, message='配置完成'):
        self.running = False
        self.finished.emit(success, message)

    def cancel(self):
        self.cancelled = True
        self.running = False


class CanPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = QSettings(str(directory / 'can.ini'), QSettings.IniFormat)
        self.stack.enter_context(patch('can_panel.list_can_interfaces', return_value=['vcan_qa']))
        self.details = self.stack.enter_context(patch('can_panel.interface_details', return_value={
            'name': 'vcan_qa', 'state': 'unknown', 'mtu': 72, 'up': True}))
        self.factory = self.stack.enter_context(patch('can_panel.SocketCanBus', side_effect=FakeBus))
        self.configurators = []
        self.auto_configure = True
        self.config_factory = self.stack.enter_context(patch(
            'can_panel.CanConfigurator', side_effect=self.make_configurator))
        self.panel = can_panel.CanPanel(self.settings)
        self.addCleanup(self.close_panel)
        self.stopped = Mock()
        self.panel.stop_sending.connect(self.stopped)
        self.received = []
        self.panel.received.connect(self.received.append)
        self.sent = []
        self.panel.sent.connect(self.sent.append)

    def make_configurator(self, parent=None):
        runner = FakeConfigurator(parent, auto_complete=self.auto_configure)
        self.configurators.append(runner)
        return runner

    def close_panel(self):
        self.panel.stop()
        self.panel.config_dialog.close()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def open_panel(self, fd=False):
        self.panel.enable_fd.setChecked(fd)
        self.panel.toggle_connection()
        self.assertTrue(self.panel.active, self.panel.status.text())
        self.panel.poll.stop()  # Drive batches explicitly in deterministic tests.
        return self.panel.bus

    def test_default_does_not_open_any_interface(self):
        self.factory.assert_not_called()
        self.config_factory.assert_not_called()
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.connecting)
        self.assertFalse(self.panel.receive_own.isChecked())
        self.assertTrue(self.panel.error_frames.isChecked())
        self.assertEqual(self.panel.interface.currentText(), 'vcan_qa')

    def test_open_applies_socket_options_filter_and_locks_connection_controls(self):
        self.panel.rx_format.setCurrentIndex(2)
        self.panel.rx_id.setText('1234567')
        self.panel.rx_mask.setText('1FFFFFFF')
        self.panel.receive_own.setChecked(True)
        self.panel.error_frames.setChecked(False)
        bus = self.open_panel(fd=True)
        self.assertEqual(bus.options, dict(fd=True, receive_own=True, error_frames=False,
                                          filter_id=0x1234567, filter_mask=0x1FFFFFFF,
                                          filter_extended=True))
        self.assertFalse(self.panel.interface.isEnabled())
        self.assertFalse(self.panel.rx_id.isEnabled())
        self.assertFalse(self.panel.enable_fd.isEnabled())
        self.panel.stop()
        self.assertTrue(bus.closed)
        self.assertTrue(self.panel.interface.isEnabled())
        self.assertFalse(self.panel.poll.isActive())

    def test_down_interface_configures_and_opens_without_manual_commands(self):
        self.details.return_value = {'up': False}
        self.panel.bitrate.setCurrentText('250000')
        self.panel.toggle_connection()
        self.assertTrue(self.panel.active)
        self.assertEqual(self.configurators[0].request, ('vcan_qa', 250000, False, 2000000))
        self.assertEqual(self.factory.call_count, 1)

    def test_close_disconnects_then_requests_down_for_connected_interface(self):
        bus = self.open_panel()
        order = []
        self.panel.stop_sending.connect(lambda: order.append('stop'))
        bus.on_close = lambda: order.append('close')
        # Disabled widgets can still change programmatically; never target that new name.
        self.panel.interface.setCurrentText('different-can')
        self.panel.toggle_connection()
        self.assertEqual(order, ['stop', 'close'])
        self.assertEqual(self.configurators[-1].request, ('down', 'vcan_qa'))
        self.assertTrue(bus.closed)
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.closing)
        self.assertFalse(self.panel.poll.isActive())
        self.assertEqual(self.panel.toggle.text(), '打开')
        self.assertIn('vcan_qa 接口已 DOWN', self.panel.status.text())
        self.assertTrue(self.panel.retry_stop.isHidden())

    def test_pending_down_disables_open_and_connection_settings(self):
        bus = self.open_panel(fd=True)
        self.auto_configure = False
        self.panel.toggle_connection()
        runner = self.configurators[-1]
        self.assertTrue(self.panel.closing)
        self.assertFalse(self.panel.active)
        self.assertTrue(bus.closed)
        self.assertFalse(self.panel.toggle.isEnabled())
        self.assertFalse(self.panel.bitrate.isEnabled())
        self.assertFalse(self.panel.data_bitrate.isEnabled())
        self.assertFalse(self.panel.interface.isEnabled())
        self.assertFalse(self.panel.config_button.isEnabled())
        self.assertFalse(self.panel.poll.isActive())
        self.assertEqual(runner.request, ('down', 'vcan_qa'))
        runner.progress.emit('等待系统认证')
        self.assertIn('连接已断开；等待系统认证', self.panel.status.text())
        self.panel.toggle_connection()
        self.assertEqual(len(self.configurators), 2)
        runner.complete(message='接口已 DOWN')
        self.assertFalse(self.panel.closing)
        self.assertTrue(self.panel.toggle.isEnabled())
        self.assertTrue(self.panel.interface.isEnabled())
        self.assertIn('接口已 DOWN', self.panel.status.text())

    def test_down_failure_keeps_disconnected_and_retries_original_target(self):
        bus = self.open_panel()
        self.auto_configure = False
        self.panel.toggle_connection()
        self.configurators[-1].complete(False, '用户取消了系统认证')
        self.assertTrue(bus.closed)
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.closing)
        self.assertIn('DOWN 状态未确认', self.panel.status.text())
        self.assertIn('用户取消了系统认证', self.panel.status.text())
        self.assertFalse(self.panel.retry_stop.isHidden())
        self.assertEqual(self.panel.retry_stop.text(), '重试停用 vcan_qa')
        self.panel.interface.setCurrentText('different-can')
        self.panel.retry_stop.click()
        retry = self.configurators[-1]
        self.assertEqual(retry.request, ('down', 'vcan_qa'))
        self.assertEqual(self.factory.call_count, 1)
        retry.complete()
        self.assertIn('vcan_qa 接口已 DOWN', self.panel.status.text())
        self.assertTrue(self.panel.retry_stop.isHidden())

    def test_close_start_exception_offers_retry_without_reopening(self):
        self.open_panel()
        self.config_factory.side_effect = RuntimeError('无法启动认证进程')
        self.panel.toggle_connection()
        self.assertFalse(self.panel.closing)
        self.assertFalse(self.panel.active)
        self.assertIn('DOWN 状态未确认', self.panel.status.text())
        self.assertIn('无法启动认证进程', self.panel.status.text())
        self.assertFalse(self.panel.retry_stop.isHidden())
        self.assertEqual(self.factory.call_count, 1)

    def test_reopen_after_down_and_after_down_failure_configures_again(self):
        self.open_panel()
        self.panel.toggle_connection()
        self.assertIn('接口已 DOWN', self.panel.status.text())
        self.panel.toggle_connection()
        self.assertTrue(self.panel.active)
        self.assertEqual(self.configurators[-1].request, ('vcan_qa', 500000, False, 2000000))
        self.auto_configure = False
        self.panel.toggle_connection()
        self.configurators[-1].complete(False, '没有停用接口的权限')
        self.panel.toggle_connection()
        self.assertTrue(self.panel.connecting)
        self.assertTrue(self.panel.retry_stop.isHidden())
        self.assertIsNone(self.panel._failed_close_interface)
        self.configurators[-1].complete()
        self.assertTrue(self.panel.active)
        self.assertEqual(self.factory.call_count, 3)

    def test_cancel_pending_down_ignores_late_callbacks_and_allows_reopen(self):
        self.open_panel()
        self.auto_configure = False
        self.panel.toggle_connection()
        old_runner = self.configurators[-1]
        self.panel.stop()
        self.assertTrue(old_runner.cancelled)
        self.assertFalse(self.panel.closing)
        stopped_status = self.panel.status.text()
        self.assertIn('DOWN 状态未确认', stopped_status)
        old_runner.progress.emit('停用旧接口')
        old_runner.complete()
        self.assertEqual(self.panel.status.text(), stopped_status)
        self.panel.toggle_connection()
        new_runner = self.configurators[-1]
        old_runner.complete()
        self.assertTrue(self.panel.connecting)
        self.assertFalse(self.panel.active)
        new_runner.complete()
        self.assertTrue(self.panel.active)

    def test_local_stop_only_disconnects_without_requesting_down(self):
        bus = self.open_panel()
        self.panel.stop()
        self.assertTrue(bus.closed)
        self.assertEqual(len(self.configurators), 1)
        self.assertIn('未停用 CAN 接口', self.panel.status.text())

    def test_invalid_filter_never_configures_or_opens(self):
        self.panel.rx_format.setCurrentIndex(1)
        self.panel.rx_id.setText('800')
        self.panel.toggle_connection()
        self.config_factory.assert_not_called()
        self.factory.assert_not_called()
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.connecting)

    def test_invalid_bitrate_never_starts_configuration(self):
        for value in ('0', '-1', '500k', '５０００００', '500000; touch /tmp/nope'):
            self.panel.bitrate.setCurrentText(value)
            self.panel.toggle_connection()
            self.assertIn('必须是正整数', self.panel.status.text())
            self.assertFalse(self.panel.connecting)
        self.panel.bitrate.setCurrentText('500000')
        self.panel.enable_fd.setChecked(True)
        self.panel.data_bitrate.setCurrentText('invalid')
        self.panel.toggle_connection()
        self.assertIn('FD 数据比特率', self.panel.status.text())
        self.config_factory.assert_not_called()
        self.factory.assert_not_called()

    def test_async_configuration_disables_controls_until_completed(self):
        self.auto_configure = False
        self.panel.enable_fd.setChecked(True)
        self.panel.data_bitrate.setCurrentText('4000000')
        self.panel.toggle_connection()
        runner = self.configurators[-1]
        self.assertTrue(self.panel.connecting)
        self.assertFalse(self.panel.active)
        self.assertEqual(self.panel.toggle.text(), '取消打开')
        self.assertFalse(self.panel.interface.isEnabled())
        self.assertFalse(self.panel.bitrate.isEnabled())
        self.assertFalse(self.panel.data_bitrate.isEnabled())
        self.factory.assert_not_called()
        runner.progress.emit('等待系统认证')
        self.assertEqual(self.panel.status.text(), '等待系统认证')
        runner.complete()
        self.assertFalse(self.panel.connecting)
        self.assertTrue(self.panel.active)
        self.assertEqual(runner.request, ('vcan_qa', 500000, True, 4000000))
        self.assertEqual(self.panel.toggle.text(), '关闭')
        self.assertFalse(self.panel.bitrate.isEnabled())
        self.assertFalse(self.panel.data_bitrate.isEnabled())

    def test_configuration_failure_unlocks_controls_without_opening_socket(self):
        self.auto_configure = False
        self.panel.toggle_connection()
        self.configurators[-1].complete(False, '用户取消了系统认证')
        self.assertFalse(self.panel.connecting)
        self.assertFalse(self.panel.active)
        self.assertTrue(self.panel.bitrate.isEnabled())
        self.assertEqual(self.panel.toggle.text(), '打开')
        self.assertIn('用户取消了系统认证', self.panel.status.text())
        self.factory.assert_not_called()
        self.stopped.assert_called_once()

    def test_cancel_ignores_late_progress_and_success(self):
        self.auto_configure = False
        self.panel.toggle_connection()
        runner = self.configurators[-1]
        self.panel.toggle_connection()
        self.assertTrue(runner.cancelled)
        self.assertFalse(self.panel.connecting)
        self.assertTrue(self.panel.interface.isEnabled())
        cancelled_status = self.panel.status.text()
        self.assertIn('已取消打开', cancelled_status)
        self.assertIn('接口配置可能仍在完成', cancelled_status)
        runner.progress.emit('旧任务配置完成')
        runner.complete()
        self.assertEqual(self.panel.status.text(), cancelled_status)
        self.assertFalse(self.panel.active)
        self.factory.assert_not_called()

    def test_cancel_then_reopen_cannot_accept_previous_completion(self):
        self.auto_configure = False
        self.panel.toggle_connection()
        old_runner = self.configurators[-1]
        self.panel.stop()
        self.panel.bitrate.setCurrentText('83333')
        self.panel.toggle_connection()
        new_runner = self.configurators[-1]
        old_runner.complete()
        self.assertTrue(self.panel.connecting)
        self.assertFalse(self.panel.active)
        self.factory.assert_not_called()
        new_runner.complete()
        self.assertTrue(self.panel.active)
        self.assertEqual(new_runner.request[1], 83333)
        self.assertEqual(self.factory.call_count, 1)

    def test_socket_failure_after_configuration_is_reported_and_unlocks_controls(self):
        self.factory.side_effect = OSError('CAN 接口已拔出')
        self.panel.toggle_connection()
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.connecting)
        self.assertTrue(self.panel.bitrate.isEnabled())
        self.assertIn('CAN 接口已拔出', self.panel.status.text())

    def test_classic_extended_and_empty_frames_count_payload_bytes(self):
        bus = self.open_panel()
        self.assertTrue(self.panel.send_text('01 02 ff'))
        self.assertEqual(bus.sent[-1], CanFrame(0x123, b'\x01\x02\xff'))
        self.panel.tx_extended.setChecked(True)
        self.panel.tx_id.setText('1FFFFFFF')
        self.assertTrue(self.panel.send_text(''))
        self.assertTrue(bus.sent[-1].extended)
        self.assertEqual(bus.sent[-1].can_id, 0x1FFFFFFF)
        self.assertEqual(self.sent, bus.sent)
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (2, 3))
        self.assertIn('2 帧 / 3 B', self.panel.counts.text())

    def test_invalid_payload_and_id_stop_cycle_without_closing_or_sending(self):
        bus = self.open_panel()
        for text in ('ABC', 'Hello', '00 ' * 9):
            self.assertFalse(self.panel.send_text(text))
            self.assertTrue(self.panel.active)
        self.panel.tx_id.setText('800')
        self.assertFalse(self.panel.send_text('00'))
        self.assertEqual(bus.sent, [])
        self.assertEqual(self.stopped.call_count, 4)
        self.assertEqual(self.panel.tx_frames, 0)

    def test_rtr_is_payload_free_with_explicit_request_length(self):
        bus = self.open_panel()
        self.panel.frame_type.setCurrentIndex(1)
        self.panel.rtr_dlc.setValue(8)
        self.assertFalse(self.panel.send_text('00'))
        self.assertTrue(self.panel.send_text(' \n'))
        self.assertEqual(bus.sent[-1], CanFrame(0x123, b'', remote=True, dlc=8))
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (1, 0))

    def test_fd_requires_socket_support_and_never_pads_payload(self):
        bus = self.open_panel()
        self.panel.frame_type.setCurrentIndex(2)
        self.assertFalse(self.panel.send_text('00'))
        self.assertEqual(bus.sent, [])
        self.panel.stop()
        bus = self.open_panel(fd=True)
        self.panel.brs.setChecked(True)
        self.assertFalse(self.panel.send_text('00 ' * 9))
        self.assertEqual(bus.sent, [])
        self.assertTrue(self.panel.send_text('AA ' * 12))
        self.assertEqual(bus.sent[-1], CanFrame(0x123, b'\xaa' * 12, fd=True, bitrate_switch=True))
        self.assertEqual(self.panel.tx_bytes, 12)

    def test_send_socket_error_stops_cycle_before_closing(self):
        bus = self.open_panel()
        order = []
        self.panel.stop_sending.connect(lambda: order.append('stop'))
        bus.on_close = lambda: order.append('close')
        bus.send_error = OSError('interface removed')
        self.assertFalse(self.panel.send_text('01'))
        self.assertEqual(order, ['stop', 'close'])
        self.assertFalse(self.panel.active)
        self.assertIn('interface removed', self.panel.status.text())
        self.assertEqual(self.panel.tx_frames, 0)

    def test_receive_flood_has_one_signal_and_bounded_frame_count(self):
        bus = self.open_panel()
        payload = [CanFrame(i % 0x800, b'\x11' * 8) for i in range(1000)]
        bus.pending.extend(payload)
        with patch('can_panel.time.monotonic', return_value=1.0):
            self.panel.drain()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0], payload[:256])
        self.assertEqual(bus.receive_calls, 256)
        self.assertEqual(len(bus.pending), 744)
        self.assertEqual((self.panel.rx_frames, self.panel.rx_bytes), (256, 2048))

    def test_receive_batch_has_time_budget(self):
        bus = self.open_panel()
        bus.pending.extend(CanFrame(1) for _ in range(1000))
        clock = iter(i / 1000 for i in range(300))
        with patch('can_panel.time.monotonic', side_effect=lambda: next(clock)):
            self.panel.drain()
        self.assertEqual(bus.receive_calls, 5)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.panel.rx_frames, 5)

    def test_receive_error_preserves_completed_frames_then_closes(self):
        bus = self.open_panel()
        frame = CanFrame(0x456, b'\x01')
        bus.pending.extend([frame, OSError('link gone')])
        self.panel.drain()
        self.assertEqual(self.received, [[frame]])
        self.assertEqual((self.panel.rx_frames, self.panel.rx_bytes), (1, 1))
        self.assertTrue(bus.closed)
        self.assertFalse(self.panel.active)
        self.assertIn('link gone', self.panel.status.text())

    def test_kernel_drop_notice_only_changes_for_new_drops(self):
        bus = self.open_panel()
        messages = []
        self.panel.status_changed.connect(messages.append)
        bus.rx_dropped = 12
        self.panel.drain()
        self.panel.drain()
        self.assertEqual(len(messages), 1)
        self.assertIn('12 帧', self.panel.overflow.text())
        bus.rx_dropped = 20
        self.panel.drain()
        self.assertEqual(len(messages), 2)
        self.assertIn('20 帧', self.panel.overflow.text())

    def test_bus_off_stops_cycle_without_losing_diagnostic_connection(self):
        bus = self.open_panel()
        frame = CanFrame(0x40, b'\0' * 8, error=True)
        bus.pending.append(frame)
        self.panel.drain()
        self.stopped.assert_called_once()
        self.assertTrue(self.panel.active)
        self.assertFalse(bus.closed)
        self.assertEqual(self.received, [[frame]])
        self.assertIn('BUS-OFF', self.panel.status.text())

    def test_rx_rtr_bytes_and_reset_counts(self):
        bus = self.open_panel()
        bus.pending.extend([CanFrame(0x123, b'', remote=True, dlc=8), CanFrame(0x123, b'12')])
        self.panel.drain()
        self.assertEqual((self.panel.rx_frames, self.panel.rx_bytes), (2, 2))
        self.panel.send_text('AA')
        self.panel.reset_counts()
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes,
                          self.panel.rx_frames, self.panel.rx_bytes), (0, 0, 0, 0))

    def test_refresh_and_persistence_preserve_settings_without_auto_open(self):
        self.panel.interface.setCurrentText('can_custom')
        self.panel.refresh_interfaces()
        self.assertEqual(self.panel.interface.currentText(), 'can_custom')
        self.panel.enable_fd.setChecked(True)
        self.panel.frame_type.setCurrentIndex(2)
        self.panel.tx_id.setText('ABCDE')
        self.panel.tx_extended.setChecked(True)
        self.panel.brs.setChecked(True)
        self.panel.bitrate.setCurrentText('83333')
        self.panel.save_settings()
        restored = can_panel.CanPanel(self.settings)
        try:
            self.assertEqual(restored.interface.currentText(), 'can_custom')
            self.assertTrue(restored.enable_fd.isChecked())
            self.assertEqual(restored.frame_type.currentIndex(), 2)
            self.assertEqual(restored.tx_id.text(), 'ABCDE')
            self.assertTrue(restored.tx_extended.isChecked())
            self.assertTrue(restored.brs.isChecked())
            self.assertEqual(restored.bitrate.currentText(), '83333')
            self.assertFalse(restored.active)
            self.factory.assert_not_called()
        finally:
            restored.close()
            restored.deleteLater()
            self.app.processEvents()

    def test_configuration_commands_quote_interface_and_use_explicit_fd_rates(self):
        # Even pasted shell syntax must remain one quoted interface argument.
        name = "can0;$(touch /tmp/nope)'"
        self.panel.interface.setCurrentText(name)
        self.panel.enable_fd.setChecked(True)
        self.panel.bitrate.setCurrentText('250000')
        self.panel.data_bitrate.setCurrentText('4000000')
        lines = self.panel.config_commands.toPlainText().splitlines()
        self.assertEqual(shlex.split(lines[0]), ['sudo', 'ip', 'link', 'set', 'dev', name, 'down'])
        self.assertEqual(shlex.split(lines[1]), [
            'sudo', 'ip', 'link', 'set', 'dev', name, 'type', 'can', 'bitrate', '250000',
            'dbitrate', '4000000', 'fd', 'on'])
        self.factory.assert_not_called()

    def test_classic_configuration_avoids_unsupported_fd_ctrlmode(self):
        self.details.return_value = {'mtu': 16}
        self.panel.update_config_commands()
        self.assertNotIn(' fd ', self.panel.config_commands.toPlainText())
        self.details.return_value = {'mtu': 72}
        self.panel.update_config_commands()
        self.assertIn(' fd off', self.panel.config_commands.toPlainText())
        self.details.return_value = {'mtu': None}
        self.panel.update_config_commands()
        self.assertNotIn(' fd ', self.panel.config_commands.toPlainText())

    def test_configuration_commands_quote_custom_bitrate_text(self):
        value = '500000; touch /tmp/nope'
        self.panel.bitrate.setCurrentText(value)
        line = self.panel.config_commands.toPlainText().splitlines()[1]
        arguments = shlex.split(line)
        self.assertEqual(arguments[arguments.index('bitrate') + 1], value)


if __name__ == '__main__':
    unittest.main()
