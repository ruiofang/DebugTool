"""Timed quick-command sequences using Qt timers, fake transports and a PTY.

Run from the project root with QT_QPA_PLATFORM=offscreen and unittest.
Every test uses temporary settings and disables device enumeration. No physical
serial/CAN device or external network is used; elapsed-time waits are bounded.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSettings
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import main


class RecordingSerial:
    """Record exact writes; optionally fail one particular attempted write."""

    def __init__(self, fail_at=None, failure=None):
        self.is_open = True
        self.writes = []
        self.times = []
        self.fail_at = fail_at
        self.failure = failure

    def write(self, data):
        self.writes.append(bytes(data))
        self.times.append(time.monotonic())
        if len(self.writes) == self.fail_at:
            if self.failure == "short":
                return max(0, len(data) - 1)
            raise self.failure
        return len(data)

    def close(self):
        self.is_open = False


class QuickLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config_file = str(directory / "serial_tool.ini")
        self.stack.enter_context(patch("main.config_path", return_value=self.config_file))
        self.stack.enter_context(patch.object(main.DebugTool, "refresh_ports"))
        self.stack.enter_context(patch("main.QMessageBox.information"))
        self.stack.enter_context(patch("main.QMessageBox.warning"))
        self.stack.enter_context(patch("main.QMessageBox.critical"))
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in list(self.windows):
            self.close_window(window)

    def close_window(self, window):
        window.chk_auto_send.setChecked(False)
        window.close()
        window.settings.sync()
        self.windows.remove(window)
        window.deleteLater()
        self.app.processEvents()

    def open_window(self):
        window = main.DebugTool()
        self.windows.append(window)
        return window

    def serial_window(self, **serial_options):
        window = self.open_window()
        window.transport_mode.setCurrentIndex(0)
        serial = RecordingSerial(**serial_options)
        window.ser = serial
        return window, serial

    def add_row(self, window, name, content, selected=True, is_hex=False,
                newline=False):
        window._add_quick_row(name, content, is_hex, newline,
                              loop_enabled=selected)

    def checked(self, window, row, column):
        return window._cell_checkbox(window.quick_table.cellWidget(row, column))

    def start(self, window, interval=40, rounds=1):
        window.spn_quick_interval.setValue(interval)
        window.spn_quick_rounds.setValue(rounds)
        window.chk_quick_loop.setChecked(True)

    def wait_until(self, predicate, timeout=1200):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), "Timed operation did not finish before its deadline")

    def assert_stopped(self, window):
        self.assertFalse(window._quick_loop_running)
        self.assertFalse(window.chk_quick_loop.isChecked())
        self.assertFalse(window.quick_loop_timer.isActive())

    def assert_no_more(self, recorded, milliseconds=100):
        before = len(recorded)
        QTest.qWait(milliseconds)
        self.assertEqual(len(recorded), before, "A stopped sequence sent another command")

    def test_selected_rows_send_in_order_for_exact_rounds_with_real_intervals(self):
        window, serial = self.serial_window()
        self.assertGreaterEqual(window.spn_quick_interval.minimum(), 10)
        self.assertEqual(window.spn_quick_rounds.value(), 1)
        self.assertTrue(window.quick_loop_timer.isSingleShot())
        self.add_row(window, "first", "A")
        self.add_row(window, "skipped", "B", selected=False)
        self.add_row(window, "last", "C")
        self.start(window, interval=40, rounds=2)
        self.assertEqual(serial.writes, [b"A"], "The first command should be immediate")
        self.assertTrue(window._quick_loop_running)
        self.wait_until(lambda: not window._quick_loop_running)
        self.assertEqual(serial.writes, [b"A", b"C", b"A", b"C"])
        self.assertEqual(window._quick_loop_sent, 4)
        self.assertEqual(window._quick_loop_round, 2)
        self.assertEqual(window.tx_bytes, 4)
        for earlier, later in zip(serial.times, serial.times[1:]):
            # A 40 ms interval must not accidentally become one callback per
            # event-loop iteration. Allow 5% coarse-timer scheduling tolerance.
            self.assertGreaterEqual(later - earlier, 0.038)
        self.assert_stopped(window)
        self.assert_no_more(serial.writes)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"),
                         "POSIX pseudo-terminals are required")
    def test_real_pyserial_pty_receives_exact_two_rounds_and_no_extra_bytes(self):
        master, slave = os.openpty()
        self.stack.callback(os.close, master)
        self.stack.callback(os.close, slave)
        os.set_blocking(master, False)
        connection = self.stack.enter_context(main.open_serial_connection(
            port=os.ttyname(slave), baudrate=115200, timeout=0,
            write_timeout=0.5))
        window = self.open_window()
        window.ser = connection
        self.add_row(window, "text", "AT", newline=True)
        self.add_row(window, "ignored", "SHOULD NOT SEND", selected=False)
        self.add_row(window, "hex", "01 FF", is_hex=True, newline=True)
        received = bytearray()

        def drain_master():
            while True:
                try:
                    chunk = os.read(master, 4096)
                except BlockingIOError:
                    return
                if not chunk:
                    return
                received.extend(chunk)

        self.start(window, interval=20, rounds=2)
        expected = b"AT\r\n\x01\xff" * 2

        def finished_and_received():
            drain_master()
            return not window._quick_loop_running and len(received) >= len(expected)

        self.wait_until(finished_and_received)
        self.assertEqual(bytes(received), expected)
        self.assertEqual(window.tx_bytes, len(expected))
        self.assertEqual(window._quick_loop_sent, 4)
        self.assertEqual(window._quick_loop_round, 2)
        self.assert_stopped(window)
        QTest.qWait(80)
        drain_master()
        self.assertEqual(bytes(received), expected,
                         "The completed sequence wrote additional PTY bytes")

    def test_sequence_snapshots_bytes_with_row_formats_and_global_checksum(self):
        window, serial = self.serial_window()
        window.chk_checksum.setChecked(True)
        window.cmb_checksum.setCurrentText("SUM (1B)")
        window.spn_chk_start.setValue(1)
        window.spn_chk_end.setValue(0)
        self.add_row(window, "text", "A", newline=True)
        self.add_row(window, "hex", "01 02", is_hex=True, newline=True)
        self.start(window, rounds=2)
        self.assertFalse(window.quick_table.isEnabled())
        self.assertFalse(window.btn_quick_add.isEnabled())
        self.assertFalse(window.btn_quick_delete.isEnabled())
        # Programmatic edits deliberately bypass disabled UI controls. They
        # must not alter any subsequent bytes in this already-started run.
        window.quick_table.item(1, window.QUICK_COL_CONTENT).setText("FF")
        self.checked(window, 1, window.QUICK_COL_LOOP).setChecked(False)
        window.chk_checksum.setChecked(False)
        self.wait_until(lambda: not window._quick_loop_running)
        self.assertEqual(serial.writes, [b"A\r\nX", b"\x01\x02\x03"] * 2)
        self.assertTrue(window.quick_table.isEnabled())
        self.assertTrue(window.btn_quick_add.isEnabled())
        self.assertTrue(window.btn_quick_delete.isEnabled())

    def test_infinite_rounds_stop_immediately_on_stop_button(self):
        window, serial = self.serial_window()
        self.add_row(window, "first", "A")
        self.add_row(window, "last", "C")
        self.start(window, interval=20, rounds=0)
        self.wait_until(lambda: len(serial.writes) >= 5)
        self.assertTrue(window._quick_loop_running)
        self.assertEqual(serial.writes, [b"A", b"C", b"A", b"C", b"A"])
        self.assertEqual(window._quick_loop_round, 2)
        window.btn_quick_stop.click()
        self.assert_stopped(window)
        self.assert_no_more(serial.writes)

    def test_all_selected_commands_are_validated_before_first_send(self):
        for bad_content, is_hex in (("", False), ("GG", True), ("0", True)):
            with self.subTest(content=bad_content, is_hex=is_hex):
                window, serial = self.serial_window()
                window.quick_table.setRowCount(0)
                self.add_row(window, "valid first", "A")
                self.add_row(window, "invalid second", bad_content, is_hex=is_hex)
                self.start(window)
                self.assert_stopped(window)
                self.assertEqual(serial.writes, [])
                self.assert_no_more(serial.writes, 55)
                self.close_window(window)
        window, serial = self.serial_window()
        window.quick_table.setRowCount(0)
        self.add_row(window, "not selected", "A", selected=False)
        self.start(window)
        self.assert_stopped(window)
        self.assertEqual(serial.writes, [])

    def test_missing_serial_connection_does_not_start_or_send(self):
        window = self.open_window()
        self.add_row(window, "command", "A")
        self.start(window)
        self.assert_stopped(window)
        self.assertEqual(window._quick_loop_sent, 0)
        QTest.qWait(55)
        self.assert_stopped(window)

    def test_failed_or_partial_serial_write_stops_without_counting_success(self):
        for failure in ("short", OSError("fake disconnected serial")):
            with self.subTest(failure=str(failure)):
                window, serial = self.serial_window(fail_at=2, failure=failure)
                window.quick_table.setRowCount(0)
                self.add_row(window, "first", "AA")
                self.add_row(window, "fails", "CC")
                self.start(window, interval=20, rounds=2)
                self.wait_until(lambda: not window._quick_loop_running)
                self.assertEqual(serial.writes, [b"AA", b"CC"])
                self.assertEqual(window._quick_loop_sent, 1)
                self.assertEqual(window._quick_loop_round, 0)
                self.assert_stopped(window)
                self.assert_no_more(serial.writes, 65)
                self.close_window(window)

    def test_network_false_result_stops_the_sequence_after_last_success(self):
        window = self.open_window()
        window.transport_mode.setCurrentIndex(1)
        window.network_panel.active = True
        self.add_row(window, "first", "A")
        self.add_row(window, "fails", "C")
        sent = []

        def send(data):
            sent.append(bytes(data))
            return len(sent) == 1

        with patch.object(window.network_panel, "send_bytes", side_effect=send):
            self.start(window, interval=20, rounds=2)
            self.wait_until(lambda: not window._quick_loop_running)
            self.assertEqual(sent, [b"A", b"C"])
            self.assertEqual(window._quick_loop_sent, 1)
            self.assert_stopped(window)
            self.assert_no_more(sent, 65)

    def test_synchronous_network_stop_signal_cannot_rearm_the_timer(self):
        window = self.open_window()
        window.transport_mode.setCurrentIndex(1)
        window.network_panel.active = True
        self.add_row(window, "first", "A")
        self.add_row(window, "last", "C")
        sent = []

        def send(data):
            sent.append(bytes(data))
            window.network_panel.stop_sending.emit()
            return True

        with patch.object(window.network_panel, "send_bytes", side_effect=send):
            self.start(window, interval=20, rounds=0)
            self.assertEqual(sent, [b"A"])
            self.assert_stopped(window)
            self.assert_no_more(sent, 65)

    def test_disconnect_transport_change_terminal_page_and_close_stop_sending(self):
        for action in ("disconnect", "transport", "terminal", "close"):
            with self.subTest(action=action):
                window, serial = self.serial_window()
                window.quick_table.setRowCount(0)
                self.add_row(window, "command", "A")
                self.start(window, interval=20, rounds=0)
                self.assertEqual(serial.writes, [b"A"])
                if action == "disconnect":
                    window.close_port()
                elif action == "transport":
                    window.transport_mode.setCurrentIndex(1)
                elif action == "terminal":
                    window.io_tabs.setCurrentWidget(window.serial_terminal)
                else:
                    window.close()
                self.assert_stopped(window)
                self.assert_no_more(serial.writes, 65)
                self.close_window(window)

    def test_main_auto_send_and_quick_loop_are_mutually_exclusive(self):
        window, serial = self.serial_window()
        self.add_row(window, "command", "A")
        window.spn_interval.setValue(500)
        window.chk_auto_send.setChecked(True)
        self.assertTrue(window.auto_send_timer.isActive())
        self.start(window, interval=40, rounds=0)
        self.assertTrue(window._quick_loop_running)
        self.assertFalse(window.chk_auto_send.isChecked())
        self.assertFalse(window.auto_send_timer.isActive())
        window.chk_auto_send.setChecked(True)
        self.assert_stopped(window)
        self.assertTrue(window.chk_auto_send.isChecked())
        self.assertTrue(window.auto_send_timer.isActive())
        window.chk_auto_send.setChecked(False)
        self.assert_no_more(serial.writes, 65)

    def test_old_and_new_quick_settings_survive_restart_without_autostart(self):
        old_rows = [{"name": "legacy", "content": "AT", "hex": False,
                     "newline": True}]
        settings = QSettings(self.config_file, QSettings.IniFormat)
        settings.setValue("quick_buttons", json.dumps(old_rows))
        settings.sync()
        window, serial = self.serial_window()
        self.assertEqual(window.quick_table.rowCount(), 1)
        self.assertEqual(window.quick_table.item(0, window.QUICK_COL_NAME).text(), "legacy")
        self.assertEqual(window.quick_table.item(0, window.QUICK_COL_CONTENT).text(), "AT")
        self.assertFalse(self.checked(window, 0, window.QUICK_COL_LOOP).isChecked())
        self.assertTrue(self.checked(window, 0, window.QUICK_COL_NEWLINE).isChecked())
        self.checked(window, 0, window.QUICK_COL_LOOP).setChecked(True)
        self.add_row(window, "hex", "01 02", selected=False, is_hex=True)
        self.start(window, interval=75, rounds=0)
        window._save_settings()
        window.settings.sync()
        saved_rows = json.loads(window.settings.value("quick_buttons"))
        self.assertEqual(saved_rows, [dict(old_rows[0], loop=True),
                                     {"name": "hex", "content": "01 02", "hex": True,
                                      "newline": False, "loop": False}])
        self.close_window(window)
        restarted = self.open_window()
        self.assertEqual(restarted.spn_quick_interval.value(), 75)
        self.assertEqual(restarted.spn_quick_rounds.value(), 0)
        self.assertTrue(self.checked(restarted, 0, restarted.QUICK_COL_LOOP).isChecked())
        self.assertFalse(self.checked(restarted, 1, restarted.QUICK_COL_LOOP).isChecked())
        self.assert_stopped(restarted)
        self.assert_no_more(serial.writes, 90)

    def test_manual_quick_sending_remains_compatible_and_can_disables_quick_tab(self):
        window, serial = self.serial_window()
        self.add_row(window, "text", "AT", selected=False, newline=True)
        self.add_row(window, "hex", "01 FF", selected=False, is_hex=True, newline=True)
        for row in (0, 1):
            window.quick_table.cellWidget(row, window.QUICK_COL_SEND).click()
        self.assertEqual(serial.writes, [b"AT\r\n", b"\x01\xff"])
        self.assertEqual(window.tx_bytes, 6)
        self.assert_stopped(window)
        window.transport_mode.setCurrentIndex(window.CAN_MODE)
        self.assertFalse(window.io_tabs.isTabEnabled(2))
        self.start(window, rounds=0)
        self.assert_stopped(window)
        self.assert_no_more(serial.writes, 65)


if __name__ == "__main__":
    unittest.main()
