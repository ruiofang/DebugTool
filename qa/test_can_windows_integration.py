"""Windows CAN UI/main-window regression checks without driver or USB access.

The panel's platform view and driver boundary are simulated. Settings and logs
remain in temporary directories, while Qt runs with the offscreen platform.
"""

from collections import deque
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

import can_panel
import main
from socketcan_bus import CanFrame


class AsyncFakeBus:
    """Model submission, driver confirmation, and delayed device shutdown."""

    instances = []
    async_tx = True

    def __init__(self, backend, channel, bitrate, **options):
        self.backend, self.channel, self.bitrate = backend, channel, bitrate
        self.options = options
        self.state = 'opening'
        self.error = self.close_error = ''
        self.closed = self.close_requested = False
        self.auto_close = True
        self.incoming = deque()
        self.accepted = []
        self.confirmed = deque()
        self.rx_dropped = 0
        self.instances.append(self)

    def complete_open(self):
        self.state = 'open'

    def send(self, frame):
        if self.error or self.close_requested:
            raise OSError(self.error or 'driver is closing')
        self.accepted.append(frame)

    def confirm(self, frame=None):
        self.confirmed.append(self.accepted[-1] if frame is None else frame)

    def drain_sent(self, maximum):
        return [self.confirmed.popleft()
                for _ in range(min(maximum, len(self.confirmed)))]

    def receive(self):
        if self.incoming:
            return self.incoming.popleft()
        if self.error:
            raise OSError(self.error)
        return None

    def close(self):
        self.close_requested = True
        if self.auto_close:
            self.complete_close()

    def complete_close(self):
        self.closed = True
        self.state = 'closed'


class WindowsCanWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch('main.config_path', return_value=str(
            self.directory / 'settings.ini')))
        self.stack.enter_context(patch.object(main.DebugTool, 'refresh_ports'))
        # Replacing only this module's reference avoids changing pathlib or Qt.
        self.stack.enter_context(patch.object(can_panel, 'sys',
                                             SimpleNamespace(platform='win32')))
        self.stack.enter_context(patch('can_panel.discover_channels', side_effect=lambda backend:
            ['PCAN_USBBUS1', 'PCAN_USBBUS2'] if backend == 'pcan' else ['0', '1']))
        self.linux_config = self.stack.enter_context(patch('can_panel.CanConfigurator',
            side_effect=AssertionError('Windows must not launch the Linux helper')))
        self.socketcan = self.stack.enter_context(patch('can_panel.SocketCanBus',
            side_effect=AssertionError('Windows must not open a SocketCAN socket')))
        self.stack.enter_context(patch('can_panel.list_can_interfaces',
            side_effect=AssertionError('Windows must not enumerate Linux CAN links')))
        self.stack.enter_context(patch('can_panel.interface_details',
            side_effect=AssertionError('Windows must not read Linux CAN details')))
        AsyncFakeBus.instances = []
        self.factory = self.stack.enter_context(patch('can_panel.DriverCanBus',
                                                       side_effect=AsyncFakeBus))
        for method in ('warning', 'critical', 'information'):
            self.stack.enter_context(patch.object(main.QMessageBox, method,
                side_effect=lambda *args, **kwargs: self.fail(
                    'Unexpected modal dialog: ' + repr(args[1:]))))
        self.windows = []
        self.addCleanup(self.close_windows)
        self.window = self.make_window()

    def make_window(self):
        window = main.DebugTool()
        self.windows.append(window)
        window.settings.setValue('log_dir', str(self.directory / 'logs'))
        window.show()
        self.app.processEvents()
        return window

    def close_windows(self):
        for bus in AsyncFakeBus.instances:
            bus.auto_close = True
            bus.complete_close()
        for window in reversed(self.windows):
            window.can_panel.drain()
            window.close()
            window.can_panel.drain()
            self.app.processEvents()
            window.deleteLater()
        self.app.processEvents()

    def pump_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertTrue(predicate(), 'Timed out waiting for Qt/driver activity')

    def open_can(self, backend='pcan', *, logging=False, fd=False):
        window = self.window
        window.transport_mode.setCurrentIndex(window.CAN_MODE)
        panel = window.can_panel
        panel.backend.setCurrentIndex(panel.backend.findData(backend))
        panel.interface.setCurrentText('PCAN_USBBUS1' if backend == 'pcan' else '0')
        panel.enable_fd.setChecked(fd)
        window.chk_log_save.setChecked(logging)
        panel.toggle.click()
        self.assertTrue(panel.connecting)
        self.assertFalse(panel.active)
        bus = panel.bus
        bus.complete_open()
        panel.drain()
        self.assertTrue(panel.active)
        self.assertFalse(panel.connecting)
        self.assertTrue(panel.poll.isActive())
        return bus

    def log_text(self):
        return ''.join(path.read_text(encoding='utf-8')
                       for path in (self.directory / 'logs').glob('*.log'))

    def test_windows_defaults_to_pcan_and_never_uses_linux_helpers(self):
        panel = self.window.can_panel
        self.assertEqual(panel.backend.currentData(), 'pcan')
        self.assertFalse(panel.backend.model().item(0).isEnabled())
        self.window.transport_mode.setCurrentIndex(4)
        self.assertIs(self.window.connection_stack.currentWidget(), panel)
        self.assertTrue(panel.send_options.isVisible())
        self.assertIn('PCAN', panel.interface.currentText())
        self.assertNotIn('sudo', panel.config_commands.toPlainText())
        self.assertIn('PCANBasic.dll', panel.config_commands.toPlainText())
        self.linux_config.assert_not_called()
        self.socketcan.assert_not_called()

    def test_background_driver_discovery_does_not_pollute_serial_page(self):
        window = self.window
        window.transport_mode.setCurrentIndex(window.CAN_MODE)
        window.transport_mode.setCurrentIndex(0)
        window.append_log('串口接收内容应保持不变')
        previous_status = window.lbl_state.text()
        previous_display = window.recv_edit.toPlainText()
        message = '无法枚举 CAN 设备：PCANBasic.dll 不可用'
        with patch.object(window, '_write_log_raw') as write_log:
            window.can_panel.note(message)
        self.assertEqual(window.can_panel.status.text(), message)
        self.assertEqual(window.lbl_state.text(), previous_status)
        self.assertEqual(window.recv_edit.toPlainText(), previous_display)
        write_log.assert_not_called()

    def test_tx_and_log_wait_for_driver_confirmation(self):
        bus = self.open_can(logging=True)
        window = self.window
        window.chk_tx_newline.setChecked(True)
        window.chk_checksum.setChecked(True)
        window.send_edit.setPlainText('01 02 AA FF')
        window.btn_send.click()
        self.assertEqual(bus.accepted, [CanFrame(0x123, bytes.fromhex('01 02 AA FF'))])
        self.assertEqual(window.can_panel.tx_frames, 0)
        self.assertEqual(window.tx_bytes, 0)
        self.assertNotIn(' TX [CAN ', self.log_text())
        bus.confirm()
        window.can_panel.drain()
        self.assertEqual(window.can_panel.tx_frames, 1)
        self.assertEqual(window.tx_bytes, 4)
        window.can_panel.toggle.click()
        window.can_panel.drain()
        self.assertIn(' TX [CAN pcan:PCAN_USBBUS1] ID=0x123 STD DATA LEN=4 DATA=01 02 AA FF',
                      self.log_text())

    def test_gs_usb_disables_fd_and_rejects_restored_fd_frame_type(self):
        panel = self.window.can_panel
        panel.enable_fd.setChecked(True)
        panel.frame_type.setCurrentIndex(2)
        bus = self.open_can('gs_usb')
        self.assertFalse(panel.enable_fd.isChecked())
        self.assertFalse(panel.enable_fd.isEnabled())
        self.assertFalse(panel.frame_type.model().item(2).isEnabled())
        self.window.send_edit.setPlainText('00')
        self.window.chk_auto_send.setChecked(True)
        self.pump_until(lambda: not self.window.chk_auto_send.isChecked())
        self.assertEqual(bus.accepted, [])
        self.assertIn('不支持 CAN FD', panel.status.text())
        self.assertTrue(panel.active)

    def test_close_waits_for_device_release_without_linux_down(self):
        bus = self.open_can('gs_usb')
        bus.auto_close = False
        panel = self.window.can_panel
        panel.toggle.click()
        self.assertTrue(bus.close_requested)
        self.assertTrue(panel.closing)
        self.assertFalse(panel.active)
        self.assertFalse(panel.backend.isEnabled())
        bus.complete_close()
        panel.drain()
        self.assertFalse(panel.closing)
        self.assertIsNone(panel.bus)
        self.assertIn('已关闭', panel.status.text())
        self.assertNotIn('DOWN', panel.status.text())
        self.linux_config.assert_not_called()

    def test_mode_switch_cancels_pending_open_and_ignores_late_success(self):
        window = self.window
        window.transport_mode.setCurrentIndex(4)
        panel = window.can_panel
        panel.toggle.click()
        bus = panel.bus
        bus.auto_close = False
        window.transport_mode.setCurrentIndex(0)
        self.assertTrue(bus.close_requested)
        self.assertFalse(panel.connecting)
        bus.complete_open()
        panel.drain()
        self.assertFalse(panel.active)
        bus.complete_close()
        panel.drain()
        self.assertFalse(panel.closing)
        self.assertIsNone(panel.bus)
        self.linux_config.assert_not_called()

    def test_driver_receive_error_stops_shared_cyclic_timer(self):
        bus = self.open_can()
        window = self.window
        window.send_edit.setPlainText('AA')
        window.chk_auto_send.setChecked(True)
        self.assertTrue(window.auto_send_timer.isActive())
        bus.error = 'PCAN BUS-OFF'
        window.can_panel.drain()
        window.can_panel.drain()
        self.assertFalse(window.chk_auto_send.isChecked())
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertFalse(window.can_panel.active)
        self.assertTrue(bus.close_requested)
        self.assertIn('BUS-OFF', window.lbl_state.text())

    def test_tx_backpressure_stops_loop_without_crashing_or_fake_confirmation(self):
        bus = self.open_can()
        window = self.window
        window.send_edit.setPlainText('AA')
        with patch.object(bus, 'send', side_effect=BufferError('CAN 发送队列已满')):
            # Call the slot directly so a missing exception handler fails this
            # test instead of allowing a PyQt unhandled slot to abort Python.
            result = window.can_panel.send_text('AA')
        self.assertFalse(result)
        self.assertEqual(window.can_panel.tx_frames, 0)
        self.assertIn('队列已满', window.lbl_state.text())
        self.assertTrue(window.can_panel.active)

    def test_window_close_waits_for_driver_shutdown(self):
        bus = self.open_can('gs_usb')
        bus.auto_close = False
        window = self.window
        window.close()
        self.assertTrue(bus.close_requested)
        self.assertTrue(window.isVisible(), 'Application exited before CAN controller stop completed')
        bus.complete_close()
        window.can_panel.drain()
        self.pump_until(lambda: not window.isVisible())
        self.linux_config.assert_not_called()

    def test_last_confirmation_during_close_is_saved_once(self):
        bus = self.open_can(logging=True)
        bus.auto_close = False
        window = self.window
        window.send_edit.setPlainText('DE AD BE EF')
        window.btn_send.click()
        window.can_panel.toggle.click()
        bus.confirm()
        bus.complete_close()
        window.can_panel.drain()
        self.assertEqual(window.can_panel.tx_frames, 1)
        self.assertEqual(self.log_text().count(' TX [CAN pcan:PCAN_USBBUS1]'), 1)
        self.assertIn('DATA=DE AD BE EF', self.log_text())

    def test_mode_switch_saves_last_confirmation_with_original_can_channel(self):
        bus = self.open_can(logging=True)
        bus.auto_close = False
        window = self.window
        window.send_edit.setPlainText('CA FE')
        window.btn_send.click()
        window.transport_mode.setCurrentIndex(1)
        bus.confirm()
        bus.complete_close()
        window.can_panel.drain()
        self.assertEqual(self.log_text().count(' TX [CAN pcan:PCAN_USBBUS1]'), 1)
        self.assertIn('DATA=CA FE', self.log_text())
        self.assertEqual(window.transport_mode.currentIndex(), 1)
        self.assertNotIn('CAN：', window.lbl_state.text())

    def test_mode_switch_preserves_buffered_and_final_inflight_rx_in_log(self):
        bus = self.open_can(logging=True)
        bus.auto_close = False
        window = self.window
        panel = window.can_panel
        frames = [CanFrame(0x123, value.to_bytes(2, 'big'))
                  for value in range(panel.MAX_RX_BATCH * 2 + 3)]
        bus.incoming.extend(frames[:-1])
        window.transport_mode.setCurrentIndex(1)
        panel.drain()
        self.assertTrue(panel.closing)
        self.assertLessEqual(panel.rx_frames, panel.MAX_RX_BATCH)
        self.assertIsNotNone(window.log_file)
        # The USB read that was already in flight can finish after Close.
        bus.incoming.append(frames[-1])
        bus.complete_close()
        self.pump_until(lambda: not panel.closing)
        self.assertEqual(panel.rx_frames, len(frames))
        self.assertEqual(window.rx_bytes, len(frames) * 2)
        records = [line for line in self.log_text().splitlines()
                   if ' RX [CAN pcan:PCAN_USBBUS1]' in line]
        self.assertEqual(len(records), len(frames))
        for record, frame in zip(records, frames):
            self.assertTrue(record.endswith('DATA=' + frame.data.hex(' ').upper()))
        self.assertIsNone(window.log_file)
        self.assertEqual(window.transport_mode.currentIndex(), 1)
        self.assertNotIn('CAN：', window.lbl_state.text())

    def test_window_exit_flushes_multiple_batches_of_received_frames(self):
        bus = self.open_can('gs_usb', logging=True)
        window = self.window
        panel = window.can_panel
        count = panel.MAX_RX_BATCH * 2 + 7
        bus.incoming.extend(CanFrame(0x321, index.to_bytes(2, 'big'))
                            for index in range(count))
        writer = window.log_file
        window.close()
        self.assertTrue(bus.close_requested)
        self.assertTrue(window.isVisible())
        panel.drain()
        self.assertTrue(panel.closing, 'Pending receive batches were discarded during exit')
        self.assertFalse(writer.closed)
        self.pump_until(lambda: not window.isVisible())
        self.assertEqual(panel.rx_frames, count)
        self.assertTrue(writer.closed)
        self.assertIsNone(writer.error)
        self.assertEqual(self.log_text().count(' RX [CAN gs_usb:0]'), count)
        self.linux_config.assert_not_called()

    def test_backend_channel_settings_restore_without_opening_device(self):
        window = self.window
        panel = window.can_panel
        window.transport_mode.setCurrentIndex(4)
        panel.interface.setCurrentText('PCAN_USBBUS2')
        panel.backend.setCurrentIndex(panel.backend.findData('gs_usb'))
        panel.interface.setCurrentText('1')
        window._save_settings()
        window.settings.sync()
        window.close()
        restored = self.make_window()
        self.assertEqual(restored.can_panel.backend.currentData(), 'gs_usb')
        self.assertEqual(restored.can_panel.interface.currentText(), '1')
        restored.can_panel.backend.setCurrentIndex(restored.can_panel.backend.findData('pcan'))
        self.assertEqual(restored.can_panel.interface.currentText(), 'PCAN_USBBUS2')
        self.assertFalse(restored.can_panel.active)
        self.assertFalse(restored.can_panel.connecting)
        self.factory.assert_not_called()

    def test_burst_reception_keeps_qt_responsive_and_logs_all_frames(self):
        bus = self.open_can('gs_usb', logging=True)
        window = self.window
        count = 10_000
        frames = [CanFrame(0x123, index.to_bytes(8, 'big'),
                           timestamp=1_700_000_000 + index / 1000)
                  for index in range(count)]
        bus.incoming.extend(frames)
        batches = []
        window.can_panel.received.connect(lambda batch: batches.append(len(batch)))
        beats = [time.monotonic()]
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        self.pump_until(lambda: window.can_panel.rx_frames == count, timeout=10)
        heartbeat.stop()
        self.assertGreater(len(beats), 5)
        longest = max(b - a for a, b in zip(beats, beats[1:]))
        self.assertLess(longest, 0.5, 'Driver receive processing starved Qt')
        self.assertLessEqual(max(batches), window.can_panel.MAX_RX_BATCH)
        self.assertEqual(sum(batches), count)
        self.assertLessEqual(window.recv_edit.document().characterCount() - 1,
                             window.MAX_DISPLAY_CHARS)
        window.can_panel.toggle.click()
        window.can_panel.drain()
        records = [line for line in self.log_text().splitlines() if ' RX [CAN ' in line]
        self.assertEqual(len(records), count)
        for record, frame in zip(records, frames):
            self.assertTrue(record.endswith('DATA=' + frame.data.hex(' ').upper()))
        print(f'\nWindows CAN simulated burst: {count:,} frames, {len(batches)} batches, '
              f'max Qt heartbeat {longest * 1000:.1f} ms', flush=True)


if __name__ == '__main__':
    unittest.main()
