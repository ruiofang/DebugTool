"""Disk isolation and loss reporting for the serial log writer."""

import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from async_log import AsyncLogWriter


class ControlledFile:
    def __init__(self, *, block=False, write_error=None, flush_error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.write_error = write_error
        self.flush_error = flush_error
        self.parts = []
        self.flushes = 0
        self.closed = False
        self.thread_ids = []

    def write(self, text):
        self.thread_ids.append(threading.get_ident())
        self.entered.set()
        if not self.release.wait(3):
            raise OSError("test writer was not released")
        if self.write_error:
            raise self.write_error
        self.parts.append(text)
        return len(text)

    def flush(self):
        self.thread_ids.append(threading.get_ident())
        self.flushes += 1
        if self.flush_error:
            raise self.flush_error

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.closed = True


class AsyncLogTests(unittest.TestCase):
    def make_writer(self, stream, **kwargs):
        with patch("async_log.open", return_value=stream, create=True):
            return AsyncLogWriter("unused-test-log", **kwargs)

    def test_complete_ordered_text_and_received_records(self):
        stamp = datetime.datetime(2026, 10, 10, 12, 34, 56, 123000)
        prefix = "2026-10-10 12:34:56.123 RX <- "
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serial.log"
            writer = AsyncLogWriter(path)
            expected = ["session start\n"]
            self.assertTrue(writer.write(expected[0]))
            for index in range(1500):
                payload = f"{index}:串口数据\n".encode()
                self.assertTrue(writer.write_received(payload, False, stamp))
                expected.append(prefix + payload.decode() + "\n")
            self.assertTrue(writer.write_received(b"\x00\x80\xff", True, stamp))
            expected.append(prefix + "00 80 FF\n")
            self.assertTrue(writer.write_received(b"\xff\x00", False, stamp))
            expected.append(prefix + "\xff\x00\n")
            self.assertTrue(writer.write("session end\n"))
            expected.append("session end\n")
            self.assertTrue(writer.close())
            self.assertIsNone(writer.error)
            self.assertEqual(path.read_text(encoding="utf-8"), "".join(expected))
            self.assertEqual(writer.pending_bytes, 0)

    def test_slow_write_does_not_block_producers_and_close_is_bounded(self):
        stream = ControlledFile(block=True)
        writer = self.make_writer(stream)
        self.addCleanup(stream.release.set)
        self.assertTrue(writer.write("first\n"))
        self.assertTrue(stream.entered.wait(1))
        started = time.monotonic()
        for _ in range(1000):
            self.assertTrue(writer.write_received(b"abc" * 100, True, "stamp"))
        self.assertLess(time.monotonic() - started, 0.5)
        started = time.monotonic()
        self.assertFalse(writer.close(timeout=0.02))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn("超时", writer.error)
        self.assertFalse(writer.write("not accepted"))
        stream.release.set()
        writer.close()
        self.assertTrue(writer.closed)
        self.assertEqual(len(stream.parts), 1001)
        self.assertTrue(stream.closed)
        self.assertNotIn(threading.get_ident(), stream.thread_ids)

    def test_overflow_reports_error_and_drains_previously_accepted_data(self):
        stream = ControlledFile(block=True)
        writer = self.make_writer(stream, max_pending_bytes=1024)
        self.addCleanup(stream.release.set)
        self.assertTrue(writer.write("first"))
        self.assertTrue(stream.entered.wait(1))
        self.assertTrue(writer.write("second"))
        self.assertFalse(writer.write("z" * 200))
        self.assertIn("超过缓冲上限", writer.error)
        self.assertFalse(writer.write("third"))
        self.assertLessEqual(writer.pending_bytes, 1024)
        stream.release.set()
        self.assertFalse(writer.close())
        self.assertTrue(writer.closed)
        self.assertEqual(stream.parts, ["first", "second"])
        self.assertEqual(writer.pending_bytes, 0)

    def test_write_failure_is_visible_and_stops_future_records(self):
        stream = ControlledFile(write_error=OSError("disk full"))
        writer = self.make_writer(stream)
        self.assertTrue(writer.write("one"))
        self.assertFalse(writer.close())
        self.assertIn("disk full", writer.error)
        self.assertFalse(writer.write("two"))
        self.assertEqual(writer.pending_bytes, 0)
        self.assertTrue(stream.closed)

    def test_flush_failure_is_visible(self):
        stream = ControlledFile(flush_error=OSError("flush failed"))
        writer = self.make_writer(stream)
        self.assertTrue(writer.write("one"))
        self.assertFalse(writer.close())
        self.assertIn("flush failed", writer.error)
        self.assertFalse(writer.write("two"))

    def test_idle_writer_flushes_without_another_record(self):
        stream = ControlledFile()
        writer = self.make_writer(stream, flush_interval=0.02)
        self.assertTrue(writer.write("one"))
        deadline = time.monotonic() + 1
        while not stream.flushes and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreater(stream.flushes, 0)
        self.assertTrue(writer.close())

    def test_open_failure_remains_synchronous(self):
        with patch("async_log.open", side_effect=PermissionError("denied"), create=True):
            with self.assertRaises(PermissionError):
                AsyncLogWriter("unused-test-log")


if __name__ == "__main__":
    unittest.main()
