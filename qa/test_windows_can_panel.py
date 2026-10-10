"""Driver CAN panel lifecycle checks; no USB hardware or OS configuration."""

from collections import deque
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

import can_panel
from qa.test_can_panel import FakeBus
from socketcan_bus import CanFrame


class FakeDriver(FakeBus):
    async_tx = True

    def __init__(self, backend, channel, bitrate, **kwargs):
        super().__init__(channel, **kwargs)
        self.backend = backend
        self.bitrate = bitrate
        self.state = 'opening'
        self.error = self.close_error = ''
        self.confirmed = deque()
        self.close_requested = False

    def drain_sent(self, max_count=256):
        return [self.confirmed.popleft() for _ in range(min(max_count, len(self.confirmed)))]

    def receive(self):
        frame = super().receive()
        if frame is None and self.error:
            raise RuntimeError(self.error)
        return frame

    def close(self):
        self.close_requested = True
        self.state = 'closing'


class DriverPanelTests(unittest.TestCase):
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
        self.stack.enter_context(patch('can_panel.interface_details', return_value={'mtu': 16}))
        self.discover = self.stack.enter_context(patch('can_panel.discover_channels', return_value=[]))
        self.linux_config = self.stack.enter_context(patch('can_panel.CanConfigurator'))
        self.linux_bus = self.stack.enter_context(patch('can_panel.SocketCanBus'))
        self.factory = self.stack.enter_context(patch('can_panel.DriverCanBus', side_effect=FakeDriver))
        self.panel = can_panel.CanPanel(self.settings)
        self.addCleanup(self.close_panel)
        self.sent = []
        self.panel.sent.connect(self.sent.append)

    def close_panel(self):
        self.panel.stop()
        if self.panel.bus is not None:
            self.panel.bus.closed = True
            self.panel.drain()
        self.panel.config_dialog.close()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def backend(self, name='pcan'):
        self.panel.backend.setCurrentIndex(self.panel.backend.findData(name))

    def start(self, name='pcan'):
        self.backend(name)
        self.panel.toggle_connection()
        self.assertTrue(self.panel.connecting, self.panel.status.text())
        self.panel.poll.stop()
        return self.panel.bus

    def open(self, name='pcan'):
        bus = self.start(name)
        bus.state = 'open'
        self.panel.drain()
        self.assertTrue(self.panel.active, self.panel.status.text())
        return bus

    def test_windows_defaults_to_pcan_and_socketcan_cannot_open(self):
        self.close_panel()
        with patch('can_panel.sys.platform', 'win32'):
            self.panel = can_panel.CanPanel(self.settings)
            self.assertEqual(self.panel.backend_name(), 'pcan')
            self.assertFalse(self.panel.backend.model().item(0).isEnabled())
            self.assertEqual(self.panel.interface.currentText(), 'PCAN_USBBUS1')
            self.panel.backend.setCurrentIndex(0)
            self.panel.toggle_connection()
            self.assertIn('SocketCAN 仅支持 Linux', self.panel.status.text())
        self.linux_config.assert_not_called()
        self.linux_bus.assert_not_called()

    def test_open_pcan_is_async_and_passes_filters_rates_without_linux_config(self):
        self.backend()
        self.panel.enable_fd.setChecked(True)
        self.panel.rx_format.setCurrentIndex(2)
        self.panel.rx_id.setText('12345')
        self.panel.rx_mask.setText('1FFFFFFF')
        self.panel.bitrate.setCurrentText('250000')
        self.panel.data_bitrate.setCurrentText('4000000')
        self.panel.toggle_connection()
        bus = self.panel.bus
        self.assertFalse(self.panel.active)
        self.assertTrue(self.panel.connecting)
        self.assertFalse(self.panel.backend.isEnabled())
        self.assertEqual(bus.backend, 'pcan')
        self.assertEqual(bus.bitrate, 250000)
        self.assertEqual(bus.options['data_bitrate'], 4000000)
        self.assertEqual(bus.options['filter_id'], 0x12345)
        self.assertTrue(bus.options['fd'])
        bus.state = 'open'
        self.panel.drain()
        self.assertTrue(self.panel.active)
        self.assertFalse(self.panel.connecting)
        self.linux_config.assert_not_called()
        self.linux_bus.assert_not_called()

    def test_pcan_confirmation_controls_tx_counts_and_signals(self):
        bus = self.open()
        self.assertTrue(self.panel.send_text('01 02'))
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (0, 0))
        self.assertEqual(self.sent, [])
        bus.confirmed.append(bus.sent[0])
        self.panel.drain()
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (1, 2))
        self.assertEqual(self.sent, bus.sent)
        self.panel.drain()
        self.assertEqual(len(self.sent), 1)

    def test_queue_full_stops_sending_without_losing_driver_connection(self):
        bus = self.open()
        bus.send_error = BufferError('CAN 发送队列已满，请降低发送速率')
        self.assertFalse(self.panel.send_text('01'))
        self.assertTrue(self.panel.active)
        self.assertFalse(bus.close_requested)
        self.assertIn('发送队列已满', self.panel.status.text())
        self.assertEqual(self.panel.tx_frames, 0)

    def test_close_waits_for_worker_and_preserves_inflight_confirmation(self):
        bus = self.open()
        self.panel.send_text('AA')
        self.panel.toggle_connection()
        self.assertTrue(bus.close_requested)
        self.assertFalse(self.panel.active)
        self.assertTrue(self.panel.closing)
        self.assertFalse(self.panel.toggle.isEnabled())
        self.assertNotIn('DOWN', self.panel.status.text())
        bus.confirmed.append(bus.sent[0])
        bus.closed = True
        self.panel.drain()
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (1, 1))
        self.assertIsNone(self.panel.bus)
        self.assertFalse(self.panel.closing)
        self.assertIn('驱动通道已关闭', self.panel.status.text())
        self.linux_config.assert_not_called()

    def test_closed_worker_drains_more_than_one_tx_batch_before_release(self):
        bus = self.open()
        bus.confirmed.extend([CanFrame(0x123, b'12')] * 300)
        self.panel.stop()
        bus.closed = True
        self.panel.drain()
        self.assertTrue(self.panel.closing)
        self.assertEqual(self.panel.tx_frames, 256)
        self.panel.drain()
        self.assertFalse(self.panel.closing)
        self.assertEqual(self.panel.tx_frames, 300)

    def test_closed_worker_delivers_all_tail_rx_in_bounded_batches_before_release(self):
        bus = self.open()
        received = []
        self.panel.received.connect(received.append)
        frames = [CanFrame(index % 0x800, b'123') for index in range(600)]
        bus.pending.extend(frames)
        self.panel.stop()
        bus.closed = True
        with patch('can_panel.time.monotonic', return_value=1):
            self.panel.drain()
            self.assertTrue(self.panel.closing)
            self.assertEqual(self.panel.rx_frames, 256)
            self.panel.drain()
            self.assertTrue(self.panel.closing)
            self.assertEqual(self.panel.rx_frames, 512)
            self.panel.drain()
        self.assertEqual([len(batch) for batch in received], [256, 256, 88])
        self.assertEqual([frame for batch in received for frame in batch], frames)
        self.assertEqual((self.panel.rx_frames, self.panel.rx_bytes), (600, 1800))
        self.assertIsNone(self.panel.bus)
        self.assertFalse(self.panel.closing)
        self.assertIn('驱动通道已关闭', self.panel.status.text())

    def test_closing_tail_rx_respects_time_budget(self):
        bus = self.open()
        bus.pending.extend([CanFrame(0x123)] * 100)
        self.panel.stop()
        bus.closed = True
        bus.receive_calls = 0
        clock = iter(index / 1000 for index in range(100))
        with patch('can_panel.time.monotonic', side_effect=lambda: next(clock)):
            self.panel.drain()
        self.assertEqual(bus.receive_calls, 5)
        self.assertEqual(self.panel.rx_frames, 5)
        self.assertTrue(self.panel.closing)
        self.assertEqual(len(bus.pending), 95)

    def test_worker_finishing_during_queue_reads_gets_one_more_drain(self):
        bus = self.open()
        self.panel.stop()
        original_receive = bus.receive
        tail = CanFrame(0x123, b'final')
        def finish_during_read():
            bus.receive = original_receive
            bus.confirmed.append(tail)
            bus.pending.append(tail)
            bus.closed = True
            return None
        bus.receive = finish_during_read
        self.panel.drain()
        self.assertTrue(self.panel.closing)
        self.assertIs(self.panel.bus, bus)
        self.panel.drain()
        self.assertFalse(self.panel.closing)
        self.assertEqual((self.panel.rx_frames, self.panel.tx_frames), (1, 1))

    def test_closing_error_preserves_cancellation_and_reports_only_once(self):
        bus = self.start()
        received = []
        messages = []
        self.panel.received.connect(received.append)
        self.panel.status_changed.connect(messages.append)
        self.panel.stop()
        bus.pending.extend([CanFrame(0x123, b'a')] * 300)
        bus.error = 'USB 设备已拔出'
        with patch('can_panel.time.monotonic', return_value=1):
            self.panel.drain()
            self.panel.drain()
            message_count = len(messages)
            self.panel.drain()
            self.panel.drain()
            self.assertEqual(len(messages), message_count)
            bus.closed = True
            self.panel.drain()
        self.assertEqual(sum(map(len, received)), 300)
        self.assertIn('已取消打开', self.panel.status.text())
        self.assertEqual(self.panel.status.text().count('USB 设备已拔出'), 1)
        self.assertIn('驱动通道已关闭', self.panel.status.text())

    def test_cancel_open_cannot_accept_late_worker_success(self):
        bus = self.start()
        self.panel.toggle_connection()
        self.assertTrue(bus.close_requested)
        self.assertTrue(self.panel.closing)
        bus.state = 'open'
        self.panel.drain()
        self.assertFalse(self.panel.active)
        self.panel.toggle_connection()
        self.assertEqual(self.factory.call_count, 1)
        bus.closed = True
        self.panel.drain()
        self.assertIn('已取消打开', self.panel.status.text())
        self.assertIn('驱动通道已关闭', self.panel.status.text())

    def test_mode_change_stop_releases_driver_without_linux_configuration(self):
        bus = self.open('gs_usb')
        self.panel.stop()
        self.assertTrue(bus.close_requested)
        self.assertFalse(self.panel.active)
        self.assertTrue(self.panel.closing)
        bus.closed = True
        self.panel.drain()
        self.assertNotIn('DOWN', self.panel.status.text())
        self.linux_config.assert_not_called()

    def test_backend_identity_cannot_change_until_driver_is_released(self):
        bus = self.open()
        self.backend('socketcan')
        self.assertEqual(self.panel.backend_name(), 'pcan')
        self.panel.stop()
        self.backend('gs_usb')
        self.assertEqual(self.panel.backend_name(), 'pcan')
        bus.closed = True
        self.panel.drain()
        self.backend('gs_usb')
        self.assertEqual(self.panel.backend_name(), 'gs_usb')

    def test_open_error_and_shutdown_error_have_honest_status(self):
        bus = self.start()
        bus.error = 'PCANBasic.dll 位数不匹配'
        bus.close_error = 'USB 设备无法释放'
        bus.closed = True
        self.panel.drain()
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.closing)
        self.assertIn('PCANBasic.dll', self.panel.status.text())
        self.assertIn('释放失败', self.panel.status.text())
        self.assertNotIn('已关闭', self.panel.status.text())

    def test_open_timeout_cancels_but_never_reopens_late_worker(self):
        bus = self.start()
        with patch('can_panel.time.monotonic', return_value=self.panel._driver_wait_since + 16):
            self.panel.drain()
        self.assertTrue(bus.close_requested)
        self.assertTrue(self.panel.closing)
        self.assertIn('打开超时', self.panel.status.text())
        with patch('can_panel.time.monotonic', return_value=self.panel._driver_wait_since + 16):
            self.panel.drain()
        self.assertIn('仍在后台等待', self.panel.status.text())

    def test_candlelight_rejects_fd_without_silent_classic_conversion(self):
        self.backend()
        self.panel.enable_fd.setChecked(True)
        self.panel.frame_type.setCurrentIndex(2)
        self.backend('gs_usb')
        self.assertFalse(self.panel.enable_fd.isChecked())
        self.assertFalse(self.panel.enable_fd.isEnabled())
        self.assertFalse(self.panel.frame_type.model().item(2).isEnabled())
        bus = self.open('gs_usb')
        self.assertEqual(self.panel.frame_type.currentIndex(), 2)
        self.assertFalse(self.panel.send_text('01'))
        self.assertEqual(bus.sent, [])
        self.assertIn('不支持 CAN FD', self.panel.status.text())
        self.panel.frame_type.setCurrentIndex(0)
        self.assertTrue(self.panel.send_text('01'))
        self.assertFalse(bus.sent[-1].fd)

    def test_windows_driver_help_does_not_show_linux_commands(self):
        for backend in ('pcan', 'gs_usb'):
            self.backend(backend)
            self.panel.update_config_commands()
            text = self.panel.config_commands.toPlainText()
            self.assertNotIn('sudo', text)
            self.assertNotIn('ip link', text)
        self.assertIn('WinUSB', text)

    def test_channels_are_saved_independently_and_legacy_socketcan_is_preserved(self):
        self.panel.interface.setCurrentText('can_old')
        self.backend()
        self.panel.interface.setCurrentText('PCAN_USBBUS3')
        self.backend('gs_usb')
        self.panel.interface.setCurrentText('2')
        self.backend('socketcan')
        self.assertEqual(self.panel.interface.currentText(), 'can_old')
        self.backend()
        self.assertEqual(self.panel.interface.currentText(), 'PCAN_USBBUS3')
        self.panel.save_settings()
        self.assertEqual(self.settings.value('can/channel_gs_usb'), '2')
        restored = can_panel.CanPanel(self.settings)
        try:
            self.assertEqual(restored.backend_name(), 'pcan')
            self.assertEqual(restored.interface.currentText(), 'PCAN_USBBUS3')
            self.assertFalse(restored.active)
            self.assertFalse(restored.connecting)
        finally:
            restored.close()
            restored.deleteLater()

    def test_blocked_enumeration_does_not_block_qt_or_replace_another_backend(self):
        release = threading.Event()
        entered = threading.Event()
        def delayed(backend):
            entered.set()
            release.wait(2)
            return ['PCAN_USBBUS9']
        self.discover.side_effect = delayed
        started = time.monotonic()
        self.backend()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(entered.wait(1))
        self.backend('socketcan')
        release.set()
        deadline = time.monotonic() + 1
        while self.panel._discovery_jobs and time.monotonic() < deadline:
            self.app.processEvents()
            self.panel._poll_discovery()
            time.sleep(0.001)
        self.assertEqual(self.panel.backend_name(), 'socketcan')
        self.assertNotIn('PCAN', self.panel.interface.currentText())

    def test_pcan_error_ids_are_not_interpreted_as_socketcan_busoff_mask(self):
        bus = self.open()
        bus.pending.append(CanFrame(0x40, b'12', error=True))
        self.panel.drain()
        self.assertNotIn('BUS-OFF', self.panel.status.text())
        self.assertTrue(self.panel.active)


if __name__ == '__main__':
    unittest.main()
