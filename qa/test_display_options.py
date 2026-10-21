"""Headless display options checks; settings, logs and transports are isolated.

Run with ``QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest
qa.test_display_options -v``. No physical port or network endpoint is opened.
"""

from contextlib import ExitStack
import datetime
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QApplication

import main
from async_log import AsyncLogWriter
from socketcan_bus import CanFrame


class RecordingSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.is_open = False


class DisplayOptionsTests(unittest.TestCase):
    TIME_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] (?:>>|<<) ")
    FILE_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d{3} (?:TX|RX)")

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("main.config_path", return_value=str(
            self.directory / "display.ini")))
        self.stack.enter_context(patch.object(main.DebugTool, "refresh_ports"))
        self.stack.enter_context(patch("can_panel.list_can_interfaces", return_value=["can0"]))
        self.stack.enter_context(patch("can_panel.interface_details", return_value={
            "name": "can0", "type": 280, "up": True, "mtu": 72}))
        self.stack.enter_context(patch("can_panel.SocketCanBus", side_effect=AssertionError(
            "Display tests must not open a CAN endpoint")))
        self.stack.enter_context(patch("can_panel.CanConfigurator.start", side_effect=AssertionError(
            "Display tests must not configure a CAN interface")))
        self.stack.enter_context(patch("main.open_serial_connection", side_effect=AssertionError(
            "Display tests must not open a serial port")))
        for method in ("warning", "critical", "information"):
            self.stack.enter_context(patch.object(main.QMessageBox, method,
                side_effect=AssertionError("Unexpected modal dialog")))
        self.window = main.DebugTool()
        self.addCleanup(self.close_window)
        self.window.resize(1200, 900)
        self.window.show()
        self.app.processEvents()
        self.window.chk_show_time.setChecked(False)
        self.window.chk_log_save.setChecked(False)
        self.window.recv_edit.clear()
        self.timestamp = datetime.datetime(2026, 10, 21, 16, 18, 24, 933000).timestamp()

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def attach_reader(self, *chunks):
        reader = main.SerialReader(None)
        self.window.reader = reader
        for chunk in chunks:
            reader._publish(chunk)
        return reader

    def assert_display_prefix(self, shown):
        text = self.window.recv_edit.toPlainText()
        self.assertTrue(text.strip(), "The operation did not produce a display log")
        for line in text.splitlines():
            if shown:
                self.assertRegex(line, self.TIME_PREFIX)
            else:
                self.assertNotRegex(line, r"\[\d{2}:\d{2}:\d{2}\.\d{3}\]")
                self.assertNotIn(">>", line)
                self.assertNotIn("<<", line)

    def test_time_option_controls_time_and_direction_across_send_receive_paths(self):
        window = self.window
        window.ser = RecordingSerial()
        window.chk_tx_hex.setChecked(False)
        window.chk_tx_newline.setChecked(False)
        window.chk_checksum.setChecked(False)
        window.reply_table.setRowCount(0)
        window._add_reply_row("TRIGGER", "AUTO-REPLY", True)
        config = {"slave_id": 1, "function_code": 3, "address": 10,
                  "quantity": 1, "values": [], "signed": False}
        with patch("main.now_ms", return_value="16:18:24.933"), \
                patch.object(window, "_write_log_raw") as write_log:
            for shown in (True, False):
                window.chk_show_time.setChecked(shown)
                operations = (
                    ("serial TX", 0, lambda: (
                        window.send_edit.setPlainText("SERIAL-TX"), window.on_send_clicked())),
                    ("quick TX", 0, lambda: window._send_quick_payload({
                        "name": "QUICK", "text": "QUICK-TX", "hex": False, "data": b"QUICK-TX"})),
                    ("terminal TX", 0, lambda: window._send_terminal_data(b"TERMINAL-TX")),
                    ("auto TX", 0, lambda: window._check_auto_reply(b"TRIGGER")),
                    ("Modbus TX", 0, lambda: window._send_modbus_request(config)),
                    ("network RX", 1, lambda: window._on_network_received(b"NET-RX", "test-peer", "NET-RX")),
                    ("network TX", 1, lambda: window._on_network_sent(b"NET-TX", "test-peer")),
                    ("CAN RX", 4, lambda: window._on_can_received([
                        CanFrame(0x300, b"\x01", timestamp=self.timestamp)])),
                    ("CAN TX", 4, lambda: window._on_can_sent(
                        CanFrame(0x301, b"\x02", timestamp=self.timestamp))),
                )
                for name, mode, operation in operations:
                    with self.subTest(time=shown, path=name):
                        window.transport_mode.setCurrentIndex(mode)
                        if mode == 0 and window.ser is None:
                            window.ser = RecordingSerial()
                        window.pending_modbus_request = None
                        window.modbus_timeout.stop()
                        window.recv_edit.clear()
                        write_log.reset_mock()
                        operation()
                        self.assert_display_prefix(shown)
                        self.assertTrue(write_log.called)
                        for record in write_log.call_args_list:
                            for line in record.args[0].splitlines():
                                self.assertRegex(line, self.FILE_RECORD)

    def test_serial_chunks_have_separate_rows_but_terminal_and_parsers_keep_original_bytes(self):
        window = self.window
        for hex_mode in (False, True):
            with self.subTest(hex=hex_mode):
                window.chk_rx_hex.setChecked(hex_mode)
                window.recv_edit.clear()
                self.attach_reader(b"FIRST", b"SECOND")
                window.pending_modbus_request = {"fake": True}
                window.chk_auto_reply.setChecked(True)
                with patch.object(window.serial_terminal, "feed") as terminal, \
                        patch.object(window, "_receive_modbus_data") as modbus, \
                        patch.object(window, "_check_auto_reply") as auto_reply, \
                        patch.object(window, "append_log", wraps=window.append_log) as append:
                    window._drain_serial_data()
                expected = (["46 49 52 53 54", "53 45 43 4F 4E 44"]
                            if hex_mode else ["FIRST", "SECOND"])
                self.assertEqual(window.recv_edit.toPlainText().splitlines(), expected)
                terminal.assert_called_once_with(b"FIRSTSECOND")
                self.assertEqual(b"".join(item.args[0] for item in modbus.call_args_list), b"FIRSTSECOND")
                self.assertEqual(b"".join(item.args[0] for item in auto_reply.call_args_list), b"FIRSTSECOND")
                append.assert_called_once()

    def test_serial_prefix_applies_to_each_reader_chunk(self):
        self.window.chk_show_time.setChecked(True)
        self.attach_reader(b"A", b"B")
        with patch("main.now_ms", return_value="16:18:24.933"):
            self.window._drain_serial_data()
        self.assertEqual(self.window.recv_edit.toPlainText().splitlines(), [
            "[16:18:24.933] << A", "[16:18:24.933] << B"])

    def test_auto_reply_matches_across_display_chunks_without_changing_raw_bytes(self):
        window = self.window
        window.ser = RecordingSerial()
        window.reply_table.setRowCount(0)
        window._add_reply_row("PING", "PONG", True)
        window.chk_reply_hex_match.setChecked(False)
        window.chk_reply_hex_send.setChecked(False)
        window.chk_checksum.setChecked(False)
        window.chk_auto_reply.setChecked(True)
        path = self.directory / "auto-reply.log"
        writer = AsyncLogWriter(str(path))
        window.log_file = writer
        reader = self.attach_reader()
        reader.log_writer = writer
        try:
            reader._publish(b"PI")
            reader._publish(b"NG")
            with patch.object(window.serial_terminal, "feed") as terminal:
                window._drain_serial_data()
            self.assertEqual(window.recv_edit.toPlainText().splitlines(), [
                "PI", "NG", "(auto) PONG"])
            self.assertEqual(window.ser.writes, [b"PONG"])
            terminal.assert_called_once_with(b"PING")
            self.assertTrue(writer.close())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            for line in lines:
                self.assertRegex(line, self.FILE_RECORD)
            received_payloads = [line.split(" RX <- ", 1)[1] for line in lines if " RX <- " in line]
            self.assertEqual(received_payloads, ["PI", "NG"])
            self.assertEqual("".join(received_payloads), "PING")
            self.assertTrue(lines[-1].endswith(" TX(auto) -> PONG"))
        finally:
            writer.close()
            window.log_file = None
            reader.log_writer = None

    def test_disconnect_keeps_retained_chunk_boundaries_and_counts_the_omitted_tail(self):
        self.window.SERIAL_UI_BATCH_BYTES = 6
        self.attach_reader(b"OLD", b"MID", b"NEW")
        with patch.object(self.window.serial_terminal, "feed") as terminal, \
                patch.object(self.window.serial_terminal, "note_gap") as gap:
            self.window._drain_serial_data(final=True)
        self.assertEqual(self.window.recv_edit.toPlainText().splitlines(), ["MID", "NEW"])
        self.assertEqual(self.window.rx_bytes, 9)
        self.assertEqual(self.window.rx_display_dropped, 3)
        terminal.assert_called_once_with(b"MIDNEW")
        gap.assert_called_once_with(3)

    def test_text_decoding_preserves_utf8_split_across_chunks_in_one_gui_batch(self):
        encoded = "串口中文🧪".encode("utf-8")
        chunks = (encoded[:1], encoded[1:5], encoded[5:12], encoded[12:])
        self.attach_reader(*chunks)
        with patch.object(self.window.serial_terminal, "feed") as terminal:
            self.window._drain_serial_data()
        displayed = self.window.recv_edit.toPlainText()
        self.assertEqual("".join(displayed.splitlines()), encoded.decode("utf-8"))
        self.assertNotIn("\ufffd", displayed)
        terminal.assert_called_once_with(encoded)

    def test_text_decoding_uses_one_latin1_fallback_for_the_whole_gui_batch(self):
        chunks = (b"\xc3", b"\xa9", b"\xff")
        self.attach_reader(*chunks)
        self.window._drain_serial_data()
        self.assertEqual("".join(self.window.recv_edit.toPlainText().splitlines()),
                         b"".join(chunks).decode("latin-1"))

    def test_can_batch_keeps_each_frame_on_its_own_row(self):
        self.window.transport_mode.setCurrentIndex(4)
        frames = [CanFrame(0x300 + i, bytes([i]), timestamp=self.timestamp + i / 1000)
                  for i in range(3)]
        for shown in (True, False):
            with self.subTest(time=shown):
                self.window.chk_show_time.setChecked(shown)
                self.window.recv_edit.clear()
                self.window._on_can_received(frames)
                lines = self.window.recv_edit.toPlainText().splitlines()
                self.assertEqual(len(lines), len(frames))
                for frame, line in zip(frames, lines):
                    self.assertEqual(line.count("ID="), 1)
                    self.assertIn(f"ID=0x{frame.can_id:03X}", line)
                self.assert_display_prefix(shown)

    def test_file_logs_retain_time_direction_and_payload_when_display_prefix_is_hidden(self):
        path = self.directory / "capture.log"
        writer = AsyncLogWriter(str(path))
        self.window.log_file = writer
        try:
            reader = self.attach_reader()
            reader.log_writer = writer
            reader._publish(b"FIRST")
            reader._publish(b"SECOND")
            self.window._drain_serial_data()
            self.window._log_can_frames([CanFrame(0x300, b"\x01", timestamp=self.timestamp)], "TX")
            self.assertTrue(writer.close())
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            self.assertEqual(len(lines), 3)
            for line in lines:
                self.assertRegex(line, self.FILE_RECORD)
            self.assertIn("RX <- FIRST", text)
            self.assertIn("RX <- SECOND", text)
            self.assertIn("TX [CAN can0] ID=0x300", text)
            self.assert_display_prefix(False)
        finally:
            writer.close()
            self.window.log_file = None
            self.window.reader.log_writer = None

    @staticmethod
    def contrast(first, second):
        def luminance(color):
            channels = [color.redF(), color.greenF(), color.blueF()]
            channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                        for c in channels]
            return sum(c * weight for c, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
        low, high = sorted((luminance(first), luminance(second)))
        return (high + 0.05) / (low + 0.05)

    def test_theme_switch_keeps_existing_and_new_log_roles_readable(self):
        window = self.window
        window.transport_mode.setCurrentIndex(4)
        window._on_can_status("正在配置 can0；需要权限时请在系统认证窗口中确认…")
        window._on_can_sent(CanFrame(0x300, b"\x01", timestamp=self.timestamp))
        window._on_can_received([CanFrame(0x301, b"\x02", timestamp=self.timestamp)])
        window.append_log("[错误] 错误提示", color="#c0392b")
        window.append_log("[自动回复] 自动回复提示", color="#6a1b9a")
        window.append_log("Unicode 中文🧪")
        window.chk_autoscroll.setChecked(False)
        window.append_log("\n".join(f"history {i}: " + "1234567890" * 40 for i in range(100)))
        selection = window.recv_edit.document().find("Unicode 中文🧪")
        window.recv_edit.setTextCursor(selection)
        vertical, horizontal = window.recv_edit.verticalScrollBar(), window.recv_edit.horizontalScrollBar()
        self.app.processEvents()
        vertical.setValue(vertical.maximum() // 2)
        horizontal.setValue(horizontal.maximum() // 2)
        before = (selection.anchor(), selection.position(), vertical.value(), horizontal.value())
        text = window.recv_edit.toPlainText()
        for theme in ("深色", "浅色", "深色"):
            with self.subTest(theme=theme):
                window.cmb_theme.setCurrentText(theme)
                self.app.processEvents()
                cursor = window.recv_edit.textCursor()
                self.assertEqual((cursor.anchor(), cursor.position(), vertical.value(), horizontal.value()), before)
                self.assertEqual(window.recv_edit.toPlainText(), text)
                background = window.recv_edit.palette().color(QPalette.Base)
                block = window.recv_edit.document().begin()
                while block.isValid():
                    it = block.begin()
                    while not it.atEnd():
                        fragment = it.fragment()
                        if fragment.isValid() and fragment.text().strip():
                            foreground = fragment.charFormat().foreground().color()
                            self.assertGreaterEqual(self.contrast(foreground, background), 4.5,
                                                    (theme, fragment.text()[:40], foreground.name(), background.name()))
                        it += 1
                    block = block.next()
        window.append_log("新增系统提示")
        match = window.recv_edit.document().find("新增系统提示")
        self.assertGreaterEqual(self.contrast(match.charFormat().foreground().color(),
                                            window.recv_edit.palette().color(QPalette.Base)), 4.5)


class ReaderDisplayBoundaryTests(unittest.TestCase):
    def test_reader_publishes_observed_idle_bursts_without_waiting_for_twenty_ms(self):
        class BurstSerial:
            def __init__(self):
                self.events = [b"FIRST", None, b"SECOND", None]
                self.reader = None

            @property
            def in_waiting(self):
                if not self.events:
                    self.reader._running = False
                    return 0
                if self.events[0] is None:
                    self.events.pop(0)
                    return 0
                return len(self.events[0])

            def read(self, size):
                data = self.events.pop(0)
                assert size >= len(data)
                return data

        serial = BurstSerial()
        reader = main.SerialReader(serial)
        serial.reader = reader
        with patch("main.time.monotonic", return_value=0.0), \
                patch.object(reader, "msleep") as sleep, \
                patch.object(reader, "_publish", wraps=reader._publish) as publish:
            reader.run()
        self.assertEqual(publish.call_args_list, [call(b"FIRST"), call(b"SECOND")])
        self.assertTrue(sleep.called)
        self.assertTrue(all(item == call(1) for item in sleep.call_args_list))
        chunks, received, dropped = reader.take_received(100)
        self.assertEqual((chunks, received, dropped), ([b"FIRST", b"SECOND"], 11, 0))


if __name__ == "__main__":
    unittest.main()
