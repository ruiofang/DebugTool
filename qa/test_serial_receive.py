"""Bounded serial receive and GUI responsiveness checks using POSIX PTYs.

Run from the project root with:
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest qa.test_serial_receive -v

All settings, logs and ports are temporary. No physical device is enumerated or
opened. The throughput checks include real pyserial reads and Qt timers.
"""

from contextlib import ExitStack
import os
from pathlib import Path
import select
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

import main


class ReaderQueueTests(unittest.TestCase):
    def test_partial_drain_counts_every_received_byte_once(self):
        reader = main.SerialReader(None)
        reader._publish(b"abcdef")
        reader._publish(b"ghij")
        chunks, received, dropped = reader.take_received(3)
        self.assertEqual((b"".join(chunks), received, dropped), (b"abc", 10, 0))
        self.assertEqual(reader._pending_bytes, 7)
        chunks, received, dropped = reader.take_received(100)
        self.assertEqual((b"".join(chunks), received, dropped), (b"defghij", 0, 0))
        self.assertEqual(reader._pending_bytes, 0)
        self.assertEqual(reader.take_received(1), ([], 0, 0))

    def test_overflow_retains_latest_tail_and_logs_all_received_bytes(self):
        reader = main.SerialReader(None)
        reader.MAX_PENDING_BYTES = 32
        reader.log_writer = Mock()
        payload = bytes(range(100))
        for offset in range(0, len(payload), 4):
            reader._publish(payload[offset:offset + 4])
            self.assertLessEqual(reader._pending_bytes, reader.MAX_PENDING_BYTES)
        chunks, received, dropped = reader.take_received(100)
        self.assertEqual(b"".join(chunks), payload[-32:])
        self.assertEqual((received, dropped), (100, 68))
        logged = b"".join(call.args[0] for call in
                          reader.log_writer.write_received.call_args_list)
        self.assertEqual(logged, payload)
        self.assertEqual(reader.take_received(1), ([], 0, 0))

    def test_single_oversized_chunk_keeps_the_newest_bytes(self):
        reader = main.SerialReader(None)
        reader.MAX_PENDING_BYTES = 16
        reader._publish(b"old")
        reader._publish(bytes(range(32)))
        chunks, received, dropped = reader.take_received(100)
        self.assertEqual(b"".join(chunks), bytes(range(16, 32)))
        self.assertEqual((received, dropped), (35, 19))


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"),
                     "POSIX pseudo-terminals are required")
class SerialWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("main.config_path", return_value=str(
            self.directory / "serial_tool.ini")))
        self.stack.enter_context(patch.object(main.DebugTool, "refresh_ports"))
        self.stack.enter_context(patch.object(main.DebugTool, "_save_settings"))
        for method in ("warning", "critical", "information"):
            self.stack.enter_context(patch.object(
                main.QMessageBox, method,
                side_effect=lambda *args, **kwargs: self.fail(
                    "Unexpected modal dialog: " + repr(args[1:]))))
        self.window = main.DebugTool()
        self.addCleanup(self.close_window)
        self.window.settings.setValue("log_dir", str(self.directory / "logs"))
        self.window.chk_show_time.setChecked(False)
        self.window.show()
        self.app.processEvents()
        self.master, self.slave = os.openpty()
        # The ExitStack closes last, after readers and windows are stopped.
        self.stack.callback(os.close, self.slave)
        self.stack.callback(os.close, self.master)
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self.slave)
        self.window.cmb_port.addItem(self.device, self.device)
        self.window.cmb_port.setCurrentIndex(0)

    def close_window(self):
        self.window.close_port()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def pump_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertTrue(predicate(), "Timed out waiting for serial/Qt activity")

    def open_port(self, *, logging=False):
        self.window.chk_log_save.setChecked(logging)
        self.window.btn_open.click()
        self.assertIsNotNone(self.window.reader)
        self.assertTrue(self.window.ser.is_open)

    def start_writer(self, payload, bytes_per_second=None):
        done, stop = threading.Event(), threading.Event()
        errors = []

        def write():
            started = time.monotonic()
            offset = 0
            deadline = started + 15
            try:
                while offset < len(payload) and not stop.is_set():
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"PTY producer timed out after {offset}/{len(payload)} bytes")
                    if bytes_per_second:
                        delay = started + offset / bytes_per_second - time.monotonic()
                        if delay > 0 and stop.wait(delay):
                            break
                    if not select.select([], [self.master], [], 0.02)[1]:
                        continue
                    try:
                        offset += os.write(self.master, payload[offset:offset + 4096])
                    except BlockingIOError:
                        continue
                if offset != len(payload) and not stop.is_set():
                    raise AssertionError(f"Only wrote {offset}/{len(payload)} bytes")
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=write, name="qa-pty-producer", daemon=True)
        thread.start()

        def stop_writer():
            stop.set()
            thread.join(1)

        self.addCleanup(stop_writer)
        return done, errors

    def read_master(self, count, timeout=2):
        result = bytearray()
        deadline = time.monotonic() + timeout
        while len(result) < count and time.monotonic() < deadline:
            self.app.processEvents()
            if select.select([self.master], [], [], 0.005)[0]:
                try:
                    result.extend(os.read(self.master, count - len(result)))
                except BlockingIOError:
                    pass
        self.assertEqual(len(result), count, "Expected serial output was not received")
        return bytes(result)

    def assert_display_bounded(self):
        document = self.window.recv_edit.document()
        self.assertLessEqual(document.characterCount() - 1,
                             self.window.MAX_DISPLAY_CHARS)
        self.assertLessEqual(document.blockCount(), self.window.MAX_DISPLAY_BLOCKS)
        block = document.begin()
        while block.isValid():
            self.assertLessEqual(len(block.text()), self.window.MAX_DISPLAY_LINE_CHARS)
            block = block.next()

    def receive_log_payload(self, hex_mode):
        logs = list((self.directory / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        records = []
        for line in logs[0].read_text(encoding="utf-8").splitlines():
            if " RX <- " in line:
                shown = line.split(" RX <- ", 1)[1]
                records.append(bytes.fromhex(shown) if hex_mode else shown.encode("ascii"))
        return b"".join(records)

    def throughput(self, *, hex_mode, terminal_visible=False):
        self.window.chk_rx_hex.setChecked(hex_mode)
        if terminal_visible:
            self.window.io_tabs.setCurrentWidget(self.window.serial_terminal)
            self.app.processEvents()
            self.assertTrue(self.window.serial_terminal.isVisible())
        self.open_port(logging=True)
        payload = (b"0123456789ABCDEF" * (2 * 1024 * 1024 // 16))
        beats = [time.monotonic()]
        maxima = {"reader": 0, "terminal": 0, "log": 0}
        heartbeat = QTimer()
        heartbeat.setInterval(10)

        def tick():
            beats.append(time.monotonic())
            reader = self.window.reader
            if reader is not None:
                with reader._lock:
                    maxima["reader"] = max(maxima["reader"], reader._pending_bytes)
            maxima["terminal"] = max(maxima["terminal"],
                                     self.window.serial_terminal._pending_bytes)
            if self.window.log_file is not None:
                maxima["log"] = max(maxima["log"], self.window.log_file.pending_bytes)

        heartbeat.timeout.connect(tick)
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        started = time.monotonic()
        done, errors = self.start_writer(payload, bytes_per_second=1024 * 1024)
        try:
            self.pump_until(lambda: done.is_set() and self.window.rx_bytes == len(payload)
                            and self.window.reader._pending_bytes == 0, timeout=15)
        except AssertionError:
            print(f"\nPTY timeout: writer_done={done.is_set()}, errors={errors!r}, "
                  f"rx={self.window.rx_bytes}, reader_pending={self.window.reader._pending_bytes}, "
                  f"dropped={self.window.rx_display_dropped}, "
                  f"state={self.window.lbl_state.text()}, peaks={maxima}", flush=True)
            raise
        self.assertEqual(errors, [])
        self.assertEqual(self.window.rx_bytes, len(payload))
        self.assertEqual(self.window.rx_display_dropped, 0)
        self.assertLessEqual(maxima["reader"], self.window.reader.MAX_PENDING_BYTES)
        self.assertLessEqual(maxima["terminal"], self.window.serial_terminal.MAX_PENDING_BYTES)
        self.assertLessEqual(maxima["log"], self.window.log_file.DEFAULT_MAX_PENDING_BYTES)
        writer = self.window.log_file
        self.window.close_port()
        self.assertIsNone(writer.error)
        self.assertTrue(writer.closed)
        self.app.processEvents()
        beats.append(time.monotonic())
        heartbeat.stop()
        max_gap = max(b - a for a, b in zip(beats, beats[1:]))
        self.assertGreater(len(beats), 50)
        self.assertLess(max_gap, 0.250, f"GUI heartbeat stalled for {max_gap:.3f}s")
        self.assertEqual(self.receive_log_payload(hex_mode), payload)
        self.assert_display_bounded()
        label = "HEX" if hex_mode else "ASCII"
        print(f"\nPTY {label}, terminal={'visible' if terminal_visible else 'hidden'}: "
              f"RX={len(payload)} B, elapsed={time.monotonic() - started:.3f}s, "
              f"max GUI gap={max_gap * 1000:.1f}ms, pending peaks={maxima}", flush=True)

    def test_continuous_two_mib_ascii_and_complete_log(self):
        self.throughput(hex_mode=False)

    def test_continuous_two_mib_hex_and_complete_log(self):
        self.throughput(hex_mode=True)

    def test_continuous_two_mib_with_visible_terminal(self):
        self.throughput(hex_mode=False, terminal_visible=True)

    def test_oversized_single_line_remains_bounded(self):
        self.window.append_log("x" * (3 * self.window.MAX_DISPLAY_CHARS) + "LATEST-END")
        self.assertTrue(self.window.recv_edit.toPlainText().endswith("LATEST-END\n"))
        self.assert_display_bounded()
        for _ in range(5):
            self.window.append_log("y" * (128 * 1024))
        self.assert_display_bounded()

    def test_disconnect_drains_counts_and_reopen_ignores_old_reader(self):
        self.open_port(logging=True)
        old_reader = self.window.reader
        self.window.serial_receive_timer.stop()
        payload = b"old-data-" * 16384 + b"FINAL-OLD-TAIL"
        done, errors = self.start_writer(payload)
        self.pump_until(lambda: done.is_set() and old_reader._pending_bytes == len(payload))
        self.assertEqual(errors, [])
        self.window.close_port()
        self.assertEqual(self.window.rx_bytes, len(payload))
        self.assertEqual(self.window.rx_display_dropped,
                         len(payload) - self.window.SERIAL_UI_BATCH_BYTES)
        self.assertIn("FINAL-OLD-TAIL", self.window.recv_edit.toPlainText().replace("\n", ""))
        self.assertEqual(old_reader._pending_bytes, 0)
        self.assertFalse(old_reader.isRunning())
        self.assertEqual(self.receive_log_payload(False), payload)
        self.window.recv_edit.clear()
        before = self.window.rx_bytes
        self.open_port()
        new_reader = self.window.reader
        old_reader.error.emit("stale-session-error")
        old_reader._publish(b"STALE-OLD-READER-DATA")
        os.write(self.master, b"NEW-SESSION")
        self.pump_until(lambda: self.window.rx_bytes == before + len(b"NEW-SESSION"))
        self.assertIs(self.window.reader, new_reader)
        self.assertTrue(self.window.ser.is_open)
        self.assertIn("NEW-SESSION", self.window.recv_edit.toPlainText())
        self.assertNotIn("STALE", self.window.recv_edit.toPlainText())
        self.assertNotIn("FINAL-OLD-TAIL", self.window.recv_edit.toPlainText())
        self.assertEqual(self.window.rx_display_dropped, 0)

    def test_receive_overflow_stops_protocols_and_reports_gap(self):
        self.open_port()
        self.window.serial_receive_timer.stop()
        reader = self.window.reader
        reader.MAX_PENDING_BYTES = 16 * 1024
        self.window.chk_auto_reply.setChecked(True)
        self.window.chk_modbus_poll.blockSignals(True)
        self.window.chk_modbus_poll.setChecked(True)
        self.window.chk_modbus_poll.blockSignals(False)
        self.window.modbus_poll_timer.start(5000)
        self.window.modbus_timeout.start(5000)
        self.window.pending_modbus_request = {"slave_id": 1, "function_code": 3}
        self.window.modbus_rx_buffer.extend(b"partial-frame")
        payload = b"X" * (32 * 1024)
        done, errors = self.start_writer(payload)
        self.pump_until(lambda: done.is_set() and reader._received_bytes == len(payload))
        self.assertEqual(errors, [])
        self.window._drain_serial_data()
        self.assertEqual(self.window.rx_bytes, len(payload))
        self.assertEqual(self.window.rx_display_dropped, 16 * 1024)
        self.assertIn("16,384", self.window.lbl_rx_overflow.text())
        self.assertFalse(self.window.chk_auto_reply.isChecked())
        self.assertFalse(self.window.chk_modbus_poll.isChecked())
        self.assertFalse(self.window.modbus_poll_timer.isActive())
        self.assertIsNone(self.window.pending_modbus_request)
        self.assertFalse(self.window.modbus_timeout.isActive())
        self.assertEqual(self.window.modbus_rx_buffer, b"")
        self.assertIn("不连续", self.window.modbus_result.toPlainText())

    def test_auto_reply_and_split_modbus_response(self):
        self.open_port()
        self.window._add_reply_row("PING", "PONG", True)
        self.window.chk_auto_reply.setChecked(True)
        os.write(self.master, b"PING")
        self.assertEqual(self.read_master(4), b"PONG")
        self.assertEqual(self.window.rx_bytes, 4)
        self.assertEqual(self.window.tx_bytes, 4)
        self.window.chk_auto_reply.setChecked(False)
        config = {"slave_id": 1, "function_code": 3, "address": 10,
                  "quantity": 2, "values": [], "signed": False}
        request = self.window._send_modbus_request(config)
        self.assertEqual(self.read_master(len(request)), request)
        body = bytes.fromhex("01 03 04 00 2A 12 34")
        response = body + main.calc_crc16_modbus(body)
        os.write(self.master, response[:4])
        self.pump_until(lambda: self.window.rx_bytes == 8)
        self.assertIsNotNone(self.window.pending_modbus_request)
        os.write(self.master, response[4:])
        self.pump_until(lambda: self.window.pending_modbus_request is None)
        self.assertEqual([entry["value"] for entry in self.window.modbus_entries], [42, 0x1234])
        self.assertEqual(self.window.rx_bytes, 4 + len(response))
        self.assertFalse(self.window.modbus_timeout.isActive())


if __name__ == "__main__":
    unittest.main()
