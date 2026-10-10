"""Headless CAN/main-window integration checks with a fake SocketCAN endpoint.

Run with ``QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
qa.test_can_integration -v``. Real serial/CAN devices are never enumerated,
opened, configured or sent to; settings and logs stay in temporary directories.
"""

from collections import deque
from contextlib import ExitStack
import datetime
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

import main
from socketcan_bus import CanFrame


class FakeBus:
    """Implements only the public transport used by CanPanel."""

    def __init__(self, interface, **options):
        self.interface = interface
        self.options = options
        self.incoming = deque()
        self.sent = []
        self.rx_dropped = 0
        self.closed = False

    def receive(self):
        if self.closed:
            raise OSError("fake endpoint is closed")
        return self.incoming.popleft() if self.incoming else None

    def send(self, frame):
        if self.closed:
            raise OSError("fake endpoint is closed")
        self.sent.append(frame)

    def close(self):
        self.closed = True


class FakeConfigurator(QObject):
    finished = pyqtSignal(bool, str)
    progress = pyqtSignal(str)
    instances = []
    defer = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.cancelled = False
        self.requests = []
        self.instances.append(self)

    def start(self, interface, bitrate, fd, data_bitrate):
        self.running = True
        self.requests.append((interface, bitrate, fd, data_bitrate))
        self.progress.emit('正在配置测试接口')
        if not self.defer:
            self.complete()

    def stop_interface(self, interface):
        self.running = True
        self.requests.append(('down', interface))
        self.progress.emit('正在停用测试接口')
        if not self.defer:
            self.complete(message='接口已 DOWN')

    def complete(self, success=True, message='接口已就绪'):
        self.running = False
        self.finished.emit(success, message)

    def cancel(self):
        self.cancelled = True
        self.running = False


class CanWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("main.config_path", return_value=str(
            self.directory / "debugtool.ini")))
        self.stack.enter_context(patch.object(main.DebugTool, "refresh_ports"))
        self.stack.enter_context(patch("can_panel.list_can_interfaces", return_value=["vcan-test"]))
        self.stack.enter_context(patch("can_panel.interface_details", return_value={
            "name": "vcan-test", "type": 280, "up": True, "mtu": 72}))
        FakeConfigurator.instances = []
        FakeConfigurator.defer = False
        self.stack.enter_context(patch("can_panel.CanConfigurator", FakeConfigurator))
        self.bus_factory = self.stack.enter_context(patch(
            "can_panel.SocketCanBus", side_effect=FakeBus))
        for method in ("warning", "critical", "information"):
            self.stack.enter_context(patch.object(
                main.QMessageBox, method,
                side_effect=lambda *args, **kwargs: self.fail(
                    "Unexpected modal dialog: " + repr(args[1:]))))
        self.windows = []
        self.addCleanup(self.close_windows)
        self.window = self.make_window()

    def make_window(self):
        window = main.DebugTool()
        self.windows.append(window)
        window.settings.setValue("log_dir", str(self.directory / "logs"))
        window.show()
        self.app.processEvents()
        return window

    def close_windows(self):
        for window in reversed(self.windows):
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def open_can(self, *, logging=False, fd=False):
        window = self.window
        window.transport_mode.setCurrentIndex(4)
        window.can_panel.interface.setCurrentText("vcan-test")
        window.can_panel.enable_fd.setChecked(fd)
        window.chk_log_save.setChecked(logging)
        window.can_panel.toggle.click()
        self.assertTrue(window.can_panel.active)
        self.assertTrue(window.can_panel.poll.isActive())
        self.assertIsInstance(window.can_panel.bus, FakeBus)
        return window.can_panel.bus

    def pump_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertTrue(predicate(), "Timed out waiting for CAN/Qt activity")

    def read_log(self):
        paths = list((self.directory / "logs").glob("*.log"))
        self.assertEqual(len(paths), 1)
        return paths[0].read_text(encoding="utf-8")

    def assert_display_bounded(self):
        document = self.window.recv_edit.document()
        self.assertLessEqual(document.characterCount() - 1,
                             self.window.MAX_DISPLAY_CHARS)
        self.assertLessEqual(document.blockCount(), self.window.MAX_DISPLAY_BLOCKS)
        block = document.begin()
        while block.isValid():
            self.assertLessEqual(len(block.text()), self.window.MAX_DISPLAY_LINE_CHARS)
            block = block.next()

    def test_can_is_fifth_mode_and_preserves_shared_send_preferences(self):
        window = self.window
        self.assertEqual(window.transport_mode.count(), 5)
        self.assertIn("CAN", window.transport_mode.itemText(4))
        for widget in (window.chk_tx_hex, window.chk_tx_newline,
                       window.chk_checksum, window.chk_rx_hex):
            widget.setChecked(True)
        window.transport_mode.setCurrentIndex(4)
        self.assertIs(window.connection_stack.currentWidget(), window.can_panel)
        self.assertTrue(window.can_panel.send_options.isVisible())
        self.assertIn("HEX", window.send_edit.placeholderText())
        self.assertIn("CAN", window.btn_send.text())
        for widget in (window.chk_tx_hex, window.chk_tx_newline,
                       window.chk_checksum, window.chk_rx_hex):
            self.assertFalse(widget.isEnabled())
            self.assertTrue(widget.isChecked())
        for index in (1, 2, 3, 4):
            self.assertFalse(window.io_tabs.isTabEnabled(index))
        window.transport_mode.setCurrentIndex(0)
        self.assertFalse(window.can_panel.send_options.isVisible())
        for widget in (window.chk_tx_hex, window.chk_tx_newline,
                       window.chk_checksum, window.chk_rx_hex):
            self.assertTrue(widget.isEnabled())
            self.assertTrue(widget.isChecked())

    def test_shared_send_routes_hex_to_can_and_bypasses_newline_checksum(self):
        window = self.window
        window.chk_tx_hex.setChecked(False)
        window.chk_tx_newline.setChecked(True)
        window.chk_checksum.setChecked(True)
        bus = self.open_can()
        with patch.object(window.network_panel, "send_bytes") as network_send, \
                patch.object(window, "_maybe_append_checksum") as checksum:
            window.send_edit.setPlainText("01 02 AA FF")
            window.btn_send.click()
        self.assertEqual(bus.sent, [CanFrame(0x123, b"\x01\x02\xaa\xff")])
        network_send.assert_not_called()
        checksum.assert_not_called()
        self.assertIsNone(window.ser)
        self.assertEqual(window.can_panel.tx_frames, 1)
        self.assertEqual(window.can_panel.tx_bytes, 4)
        self.assertEqual(window.tx_bytes, 4)
        self.assertEqual(window.cmb_history.itemText(0), "01 02 AA FF")

    def test_empty_data_and_rtr_frames_can_be_sent_from_shared_editor(self):
        bus = self.open_can()
        window = self.window
        window.send_edit.clear()
        window.btn_send.click()
        window.can_panel.frame_type.setCurrentIndex(1)
        window.can_panel.rtr_dlc.setValue(8)
        window.btn_send.click()
        self.assertEqual(bus.sent, [CanFrame(0x123), CanFrame(0x123, remote=True, dlc=8)])
        self.assertEqual(window.can_panel.tx_frames, 2)
        self.assertEqual(window.can_panel.tx_bytes, 0)
        self.assertIn("TX: 2 帧 / 0 B", window.lbl_counter.text())

    def test_extended_fd_send_and_shared_cyclic_timer(self):
        bus = self.open_can(fd=True)
        window = self.window
        window.can_panel.tx_id.setText("1ABCDE")
        window.can_panel.tx_extended.setChecked(True)
        window.can_panel.frame_type.setCurrentIndex(2)
        window.can_panel.brs.setChecked(True)
        payload = bytes(range(12))
        window.send_edit.setPlainText(payload.hex(" "))
        window.spn_interval.setValue(20)
        window.chk_auto_send.setChecked(True)
        self.pump_until(lambda: len(bus.sent) >= 3)
        window.chk_auto_send.setChecked(False)
        self.assertFalse(window.auto_send_timer.isActive())
        expected = CanFrame(0x1ABCDE, payload, extended=True, fd=True, bitrate_switch=True)
        self.assertTrue(all(frame == expected for frame in bus.sent))
        self.assertEqual(window.can_panel.tx_frames, len(bus.sent))

    def test_invalid_frame_stops_cyclic_send_without_disconnect_or_modal(self):
        bus = self.open_can()
        window = self.window
        window.send_edit.setPlainText("01 02 03 04 05 06 07 08 09")
        window.spn_interval.setValue(20)
        window.chk_auto_send.setChecked(True)
        self.pump_until(lambda: not window.chk_auto_send.isChecked())
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertEqual(bus.sent, [])
        self.assertTrue(window.can_panel.active)
        self.assertIn("发送失败", window.lbl_state.text())

    def test_rx_records_frame_identity_flags_timestamp_and_payload_counts(self):
        bus = self.open_can(logging=True, fd=True)
        window = self.window
        stamp = 1_700_000_000.125
        frames = [
            CanFrame(0x123, b"\x00\xff", timestamp=stamp),
            CanFrame(0x1ABCDE, b"ABCD", extended=True, timestamp=stamp),
            CanFrame(0x321, remote=True, dlc=8, timestamp=stamp),
            CanFrame(0x456, bytes(range(64)), fd=True, bitrate_switch=True,
                     error_state_indicator=True, timestamp=stamp),
            CanFrame(0x4, b"\x00" * 8, error=True, timestamp=stamp),
        ]
        bus.incoming.extend(frames)
        self.pump_until(lambda: window.can_panel.rx_frames == len(frames))
        self.assertEqual(window.can_panel.rx_bytes, 78)
        self.assertEqual(window.rx_bytes, 78)
        self.assertIn("RX: 5 帧 / 78 B", window.lbl_counter.text())
        shown = window.recv_edit.toPlainText()
        details = [
            "ID=0x123 STD DATA LEN=2 DATA=00 FF",
            "ID=0x001ABCDE EXT DATA LEN=4 DATA=41 42 43 44",
            "ID=0x321 STD RTR LEN=8 DATA=-",
            "ID=0x456 STD FD BRS ESI LEN=64 DATA=00 01 02",
            "ID=0x00000004 STD ERROR LEN=8 DATA=00 00 00 00 00 00 00 00",
        ]
        for fragment in details:
            self.assertIn(fragment, shown)
        window.can_panel.stop()
        recorded = self.read_log()
        prefix = datetime.datetime.fromtimestamp(stamp).isoformat(timespec="milliseconds")
        self.assertEqual(recorded.count(prefix + " RX [CAN vcan-test]"), 5)
        for fragment in details:
            self.assertIn(fragment, recorded)

    def test_mode_switch_closes_endpoint_stops_timers_and_flushes_log(self):
        bus = self.open_can(logging=True)
        window = self.window
        window.send_edit.setPlainText("AA")
        window.btn_send.click()
        writer = window.log_file
        self.assertIsNotNone(writer)
        window.chk_auto_send.setChecked(True)
        window.transport_mode.setCurrentIndex(1)
        self.assertTrue(bus.closed)
        self.assertFalse(window.can_panel.active)
        self.assertFalse(window.can_panel.poll.isActive())
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertFalse(window.chk_auto_send.isChecked())
        self.assertIsNone(window.log_file)
        self.assertTrue(writer.closed)
        self.assertIsNone(writer.error)
        self.assertIn(" TX [CAN vcan-test] ID=0x123 STD DATA LEN=1 DATA=AA", self.read_log())

    def test_mode_switch_cancels_pending_configuration_without_late_connection(self):
        FakeConfigurator.defer = True
        self.window.transport_mode.setCurrentIndex(4)
        panel = self.window.can_panel
        panel.interface.setCurrentText('vcan-test')
        panel.toggle.click()
        self.assertTrue(panel.connecting)
        pending = FakeConfigurator.instances[-1]
        self.window.transport_mode.setCurrentIndex(0)
        self.assertTrue(pending.cancelled)
        self.assertFalse(panel.connecting)
        pending.complete()
        self.assertFalse(panel.active)
        self.bus_factory.assert_not_called()

    def test_explicit_close_stops_cyclic_send_and_flushes_log_before_down(self):
        bus = self.open_can(logging=True)
        window = self.window
        window.send_edit.setPlainText('AA')
        window.btn_send.click()
        writer = window.log_file
        window.chk_auto_send.setChecked(True)
        FakeConfigurator.defer = True
        window.can_panel.toggle.click()
        pending = FakeConfigurator.instances[-1]
        self.assertTrue(bus.closed)
        self.assertTrue(window.can_panel.closing)
        self.assertFalse(window.can_panel.active)
        self.assertFalse(window.can_panel.poll.isActive())
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertFalse(window.chk_auto_send.isChecked())
        self.assertTrue(writer.closed)
        self.assertIsNone(window.log_file)
        self.assertEqual(pending.requests, [('down', 'vcan-test')])
        self.assertIn(' DATA=AA', self.read_log())
        pending.complete()
        self.assertIn('vcan-test 接口已 DOWN', window.lbl_state.text())

    def test_down_authentication_failure_is_visible_and_retry_stays_disconnected(self):
        bus = self.open_can()
        panel = self.window.can_panel
        FakeConfigurator.defer = True
        panel.toggle.click()
        FakeConfigurator.instances[-1].complete(False, '用户取消了系统认证')
        self.assertTrue(bus.closed)
        self.assertIn('DOWN 状态未确认', self.window.lbl_state.text())
        self.assertTrue(panel.retry_stop.isVisible())
        panel.retry_stop.click()
        self.assertFalse(panel.active)
        self.assertTrue(panel.closing)
        self.assertEqual(self.bus_factory.call_count, 1)
        FakeConfigurator.instances[-1].complete()
        self.assertIn('接口已 DOWN', self.window.lbl_state.text())

    def test_mode_switch_cancels_pending_down_and_ignores_its_completion(self):
        self.open_can()
        panel = self.window.can_panel
        FakeConfigurator.defer = True
        panel.toggle.click()
        pending = FakeConfigurator.instances[-1]
        self.assertTrue(panel.closing)
        self.window.transport_mode.setCurrentIndex(0)
        self.assertTrue(pending.cancelled)
        self.assertFalse(panel.closing)
        status = panel.status.text()
        pending.complete()
        self.assertEqual(panel.status.text(), status)
        self.assertIn('DOWN 状态未确认', status)
        self.assertFalse(panel.active)

    def test_window_close_cancels_pending_down(self):
        self.open_can()
        FakeConfigurator.defer = True
        panel = self.window.can_panel
        panel.toggle.click()
        pending = FakeConfigurator.instances[-1]
        self.window.close()
        self.assertTrue(pending.cancelled)
        self.assertFalse(panel.closing)
        pending.complete()
        self.assertFalse(panel.active)

    def test_window_close_cancels_pending_configuration(self):
        FakeConfigurator.defer = True
        self.window.transport_mode.setCurrentIndex(4)
        panel = self.window.can_panel
        panel.toggle.click()
        self.assertTrue(panel.connecting)
        pending = FakeConfigurator.instances[-1]
        self.window.close()
        self.assertTrue(pending.cancelled)
        self.assertFalse(panel.connecting)
        pending.complete()
        self.assertFalse(panel.active)
        self.bus_factory.assert_not_called()

    def test_authentication_failure_keeps_socket_closed_and_can_retry(self):
        FakeConfigurator.defer = True
        self.window.transport_mode.setCurrentIndex(4)
        panel = self.window.can_panel
        panel.toggle.click()
        FakeConfigurator.instances[-1].complete(False, '已取消系统认证')
        self.assertFalse(panel.active)
        self.assertFalse(panel.connecting)
        self.assertIn('已取消系统认证', panel.status.text())
        self.bus_factory.assert_not_called()
        FakeConfigurator.defer = False
        self.open_can()

    def test_settings_restore_can_mode_and_options_without_autoconnect(self):
        bus = self.open_can(fd=True)
        panel = self.window.can_panel
        panel.tx_id.setText("1ABCDE")
        panel.tx_extended.setChecked(True)
        panel.frame_type.setCurrentIndex(2)
        panel.brs.setChecked(True)
        panel.bitrate.setCurrentText('250000')
        panel.data_bitrate.setCurrentText('4000000')
        self.window.send_edit.setPlainText("AA BB CC DD")
        self.window._save_settings()
        self.window.settings.sync()
        self.window.close()
        self.assertTrue(bus.closed)
        self.bus_factory.reset_mock()
        restored = self.make_window()
        self.assertEqual(restored.transport_mode.currentIndex(), 4)
        self.assertTrue(restored.can_panel.send_options.isVisible())
        self.assertEqual(restored.can_panel.interface.currentText(), "vcan-test")
        self.assertEqual(restored.can_panel.tx_id.text(), "1ABCDE")
        self.assertTrue(restored.can_panel.tx_extended.isChecked())
        self.assertTrue(restored.can_panel.enable_fd.isChecked())
        self.assertEqual(restored.can_panel.frame_type.currentIndex(), 2)
        self.assertTrue(restored.can_panel.brs.isChecked())
        self.assertEqual(restored.can_panel.bitrate.currentText(), '250000')
        self.assertEqual(restored.can_panel.data_bitrate.currentText(), '4000000')
        self.assertEqual(restored.send_edit.toPlainText(), "AA BB CC DD")
        self.assertFalse(restored.can_panel.active)
        self.assertFalse(restored.can_panel.poll.isActive())
        self.assertFalse(restored.auto_send_timer.isActive())
        self.bus_factory.assert_not_called()

    def test_counter_reset_clears_frame_and_byte_totals(self):
        bus = self.open_can()
        window = self.window
        window.send_edit.setPlainText("01 02")
        window.btn_send.click()
        bus.incoming.extend([CanFrame(0x123, b"hello"), CanFrame(0x124, remote=True, dlc=8)])
        self.pump_until(lambda: window.can_panel.rx_frames == 2)
        self.assertEqual(window.can_panel.rx_bytes, 5)
        window._reset_counter()
        panel = window.can_panel
        self.assertEqual((panel.tx_frames, panel.rx_frames, panel.tx_bytes, panel.rx_bytes), (0, 0, 0, 0))
        self.assertEqual((window.tx_bytes, window.rx_bytes), (0, 0))
        self.assertIn("TX: 0 帧 / 0 B", window.lbl_counter.text())
        self.assertIn("RX: 0 帧 / 0 B", window.lbl_counter.text())
        self.assertTrue(panel.active)

    def test_window_close_releases_can_and_background_logger(self):
        bus = self.open_can(logging=True)
        window = self.window
        writer = window.log_file
        window.chk_auto_send.setChecked(True)
        window.close()
        self.assertTrue(bus.closed)
        self.assertFalse(window.can_panel.active)
        self.assertFalse(window.can_panel.poll.isActive())
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertIsNone(window.log_file)
        self.assertTrue(writer.closed)
        self.assertIsNone(writer.error)

    def test_burst_reception_bounds_display_preserves_log_and_services_qt_timer(self):
        bus = self.open_can(logging=True)
        window = self.window
        count = 10_000
        frames = [CanFrame(0x123, index.to_bytes(8, "big"), timestamp=1_700_000_000 + index / 1000)
                  for index in range(count)]
        bus.incoming.extend(frames)
        batch_sizes = []
        window.can_panel.received.connect(lambda batch: batch_sizes.append(len(batch)))
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
        self.assertLess(longest, 0.5, "CAN receive starved a 10 ms Qt heartbeat")
        self.assertLessEqual(max(batch_sizes), window.can_panel.MAX_RX_BATCH)
        self.assertEqual(sum(batch_sizes), count)
        self.assertEqual(window.can_panel.rx_bytes, count * 8)
        self.assert_display_bounded()
        writer = window.log_file
        window.can_panel.stop()
        self.assertTrue(writer.closed)
        self.assertIsNone(writer.error)
        records = [line for line in self.read_log().splitlines() if " RX [CAN " in line]
        self.assertEqual(len(records), count)
        for record, frame in zip(records, frames):
            self.assertTrue(record.endswith("DATA=" + frame.data.hex(" ").upper()))
        print(f"\nCAN fake-endpoint burst: {count:,} frames, "
              f"{len(batch_sizes)} batches, max Qt heartbeat {longest * 1000:.1f} ms", flush=True)


if __name__ == "__main__":
    unittest.main()
