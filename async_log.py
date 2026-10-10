"""Bounded, ordered log writing without disk I/O in the caller's thread."""

from collections import deque
import datetime
import threading
import time


class AsyncLogWriter:
    """Append a UTF-8 log using one background writer.

    ``write`` and ``write_received`` return whether the record was accepted.
    The pending budget includes the record currently being written, and uses
    conservative payload/metadata sizes. Overflow stops logging explicitly;
    previously accepted records are still drained. ``error`` retains the first
    failure, including a close timeout. A stuck disk operation cannot be killed
    safely, so the writer is a daemon and close waits at most one second.

    Opening is deliberately synchronous so callers can report path/permission
    errors immediately. All write, flush and close operations run in the worker.
    """

    DEFAULT_MAX_PENDING_BYTES = 8 * 1024 * 1024
    RECORD_OVERHEAD = 256

    def __init__(self, path, *, max_pending_bytes=DEFAULT_MAX_PENDING_BYTES,
                 flush_interval=0.25):
        if max_pending_bytes <= 0:
            raise ValueError("max_pending_bytes must be positive")
        if flush_interval <= 0:
            raise ValueError("flush_interval must be positive")
        self._limit = max_pending_bytes
        self._flush_interval = flush_interval
        self._condition = threading.Condition()
        self._records = deque()
        self._pending_bytes = 0
        self._accepting = True
        self._close_requested = False
        self._closed = False
        self._error = None
        self._file = open(path, "a", encoding="utf-8")
        self._thread = threading.Thread(
            target=self._run, name="serial-log-writer", daemon=True)
        self._thread.start()

    @property
    def error(self):
        """First error as displayable text, or None while logging is healthy."""
        with self._condition:
            return self._error

    @property
    def pending_bytes(self):
        with self._condition:
            return self._pending_bytes

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def write(self, text):
        """Queue an already formatted log entry; never write or flush here."""
        if not isinstance(text, str):
            raise TypeError("text must be str")
        # A Python Unicode string can use up to four bytes per character.
        size = len(text) * 4 + self.RECORD_OVERHEAD
        return self._enqueue(("text", text, None, None), size)

    def write_received(self, data, hex_mode=False, timestamp=None):
        """Queue received bytes; decoding and HEX expansion happen in the worker.

        ``timestamp`` may be a datetime or a preformatted timestamp string.
        UTF-8 decoding falls back to Latin-1 per received chunk, matching the
        existing log format (this is not a streaming UTF-8 decoder).
        """
        if not isinstance(data, bytes):
            data = bytes(data)
        if timestamp is None:
            timestamp = datetime.datetime.now()
        size = len(data) + self.RECORD_OVERHEAD
        if isinstance(timestamp, str):
            size += len(timestamp) * 4
        return self._enqueue(("received", data, bool(hex_mode), timestamp), size)

    def _enqueue(self, record, size):
        with self._condition:
            if not self._accepting:
                return False
            if self._pending_bytes + size > self._limit:
                self._error = (
                    "日志待写数据超过缓冲上限，已停止记录；"
                    "此前接受的数据将继续写入，后续数据未保存。")
                self._accepting = False
                self._close_requested = True
                self._condition.notify_all()
                return False
            self._records.append((record, size))
            self._pending_bytes += size
            self._condition.notify()
            return True

    def close(self, timeout=1.0):
        """Drain accepted records and close; return False on error or timeout.

        The close flag is separate from the bounded queue: even a full queue
        cannot lose the shutdown request. The GUI waits at most one second.
        """
        with self._condition:
            self._accepting = False
            self._close_requested = True
            self._condition.notify_all()
        self._thread.join(min(1.0, max(0.0, timeout)))
        with self._condition:
            if self._thread.is_alive() and self._error is None:
                self._error = (
                    "日志关闭超时，磁盘写入仍未完成；"
                    "部分日志可能尚未保存，已停止接收新日志。")
            return self._closed and self._error is None

    @staticmethod
    def _format_record(record):
        kind, payload, hex_mode, timestamp = record
        if kind == "text":
            return payload
        if hex_mode:
            shown = payload.hex(" ").upper()
        else:
            try:
                shown = payload.decode("utf-8")
            except UnicodeDecodeError:
                shown = payload.decode("latin-1", errors="replace")
        if isinstance(timestamp, datetime.datetime):
            timestamp = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return f"{timestamp} RX <- {shown}\n"

    def _fail(self, operation, exc):
        with self._condition:
            if self._error is None:
                self._error = f"日志{operation}失败，已停止记录，部分数据未保存：{exc}"
            self._accepting = False
            self._close_requested = True
            self._records.clear()
            self._pending_bytes = 0

    def _run(self):
        dirty = False
        next_flush = time.monotonic() + self._flush_interval
        try:
            while True:
                with self._condition:
                    while not self._records and not self._close_requested:
                        wait = next_flush - time.monotonic() if dirty else None
                        if wait is not None and wait <= 0:
                            break
                        self._condition.wait(wait)
                    if self._records:
                        record, size = self._records.popleft()
                    elif self._close_requested:
                        break
                    else:
                        record = None
                if record is not None:
                    text = self._format_record(record)
                    written = self._file.write(text)
                    if written != len(text):
                        raise OSError(f"日志仅写入 {written}/{len(text)} 个字符")
                    dirty = True
                    with self._condition:
                        self._pending_bytes -= size
                if dirty and time.monotonic() >= next_flush:
                    self._file.flush()
                    dirty = False
                    next_flush = time.monotonic() + self._flush_interval
        except Exception as exc:
            self._fail("写入", exc)
        finally:
            try:
                if dirty:
                    self._file.flush()
            except Exception as exc:
                self._fail("刷新", exc)
            try:
                self._file.close()
            except Exception as exc:
                self._fail("关闭", exc)
            with self._condition:
                self._closed = True

