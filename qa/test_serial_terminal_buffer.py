"""Serial VT buffering tests; no physical port or persisted settings are used."""
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication

from serial_terminal import SerialTerminalPanel


class SerialTerminalBufferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sent = []
        self.panel = SerialTerminalPanel(
            lambda data, sensitive=False: self.sent.append(data), lambda: None)
        self.panel.set_connected(True, 'test-port @ 115200')

    def tearDown(self):
        self.panel.clear_pending()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def show_and_drain(self):
        self.panel.show()
        self.app.processEvents()
        for _ in range(1000):
            if not self.panel._pending_bytes and not self.panel._gap_bytes:
                break
            self.panel._drain_pending()
        self.assertEqual(self.panel._pending_bytes, 0)

    def test_hidden_terminal_retains_first_prompt_without_parsing(self):
        with patch.object(self.panel.output, 'feed', wraps=self.panel.output.feed) as feed:
            self.panel.feed(b'login: ')
            self.panel._drain_pending()
            self.assertFalse(feed.called)
            self.assertFalse(self.panel._flush_timer.isActive())
            self.assertEqual(self.panel._pending_bytes, 7)
            self.show_and_drain()
            self.assertTrue(feed.called)
        self.assertIn('login:', self.panel.output.toPlainText())

    def test_overflow_keeps_a_bounded_tail_and_reports_omitted_bytes(self):
        data = b'old-data\r\n' * 10000 + b'latest-prompt> '
        self.panel.feed(data)
        self.assertEqual(self.panel._pending_bytes, self.panel.MAX_PENDING_BYTES)
        self.assertEqual(b''.join(self.panel._pending), data[-self.panel.MAX_PENDING_BYTES:])
        self.assertEqual(self.panel._gap_bytes, len(data) - self.panel.MAX_PENDING_BYTES)
        self.show_and_drain()
        text = self.panel.output.toPlainText()
        self.assertIn('latest-prompt>', text)
        # The stream may exceed the terminal's separate 2000-line history limit;
        # check the notice before a smaller second gap is replayed.
        self.panel.note_gap(123)
        self.show_and_drain()
        self.assertIn('123', self.panel.output.toPlainText())
        self.assertFalse(self.panel._flush_timer.isActive())

    def test_gap_resets_partial_sequences_and_suppresses_replayed_queries(self):
        self.panel.output.responses_enabled = True
        self.panel.output.feed(b'\x1b[')
        self.panel.feed(b'outdated')
        self.panel.note_gap(5)
        self.assertEqual(self.panel._gap_bytes, 13)
        self.panel.feed(b'6n\r\nnew-tail\x1b[6n\x1b[')
        self.show_and_drain()
        self.assertEqual(self.sent, [])
        self.assertIn('new-tail', self.panel.output.toPlainText())
        self.panel.feed(b'6n')
        self.show_and_drain()
        self.assertEqual(self.sent, [])
        self.panel.feed(b'\x1b[6n')
        self.show_and_drain()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith(b'\x1b['))

    def test_parse_time_budget_leaves_work_for_later_ticks(self):
        self.panel.show()
        self.app.processEvents()
        self.panel.feed(b'x' * 8192)
        calls = []

        def slow_feed(data):
            calls.append(data)
            time.sleep(self.panel.PARSE_BUDGET_SECONDS + 0.001)

        with patch.object(self.panel.output, 'feed', side_effect=slow_feed):
            self.panel._drain_pending()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), self.panel.PARSE_SLICE_BYTES)
        self.assertEqual(self.panel._pending_bytes, 8192 - self.panel.PARSE_SLICE_BYTES)
        self.assertTrue(self.panel._flush_timer.isActive())

    def test_timer_replays_hidden_prompt_when_shown(self):
        self.panel.feed(b'boot complete\r\nlogin: ')
        self.panel.show()
        deadline = time.monotonic() + 1
        while self.panel._pending_bytes and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertEqual(self.panel._pending_bytes, 0)
        self.assertIn('login:', self.panel.output.toPlainText())

    def test_clear_discards_pending_output(self):
        self.panel.feed(b'stale')
        self.panel._clear_output()
        self.show_and_drain()
        self.assertEqual(self.panel.output.toPlainText(), '')
        self.assertEqual(self.panel._gap_bytes, 0)

    def test_same_port_reopen_keeps_tail_without_replying_to_old_queries(self):
        self.panel.output.responses_enabled = True
        self.panel.feed(b'last screen\r\n\x1b[6n')
        self.panel.set_connected(False)
        self.panel.set_connected(True, 'test-port @ 115200')
        self.show_and_drain()
        self.assertIn('last screen', self.panel.output.toPlainText())
        self.assertEqual(self.sent, [])

    def test_hidden_query_is_not_answered_when_tab_is_opened(self):
        self.panel.feed(b'login: \x1b[6n')
        self.panel.output.responses_enabled = True
        self.show_and_drain()
        self.assertEqual(self.sent, [])
        self.panel.feed(b'\x1b[6n')
        self.show_and_drain()
        self.assertEqual(len(self.sent), 1)

    def test_hidden_utf8_prefix_is_preserved_into_live_data(self):
        data = '中文'.encode('utf-8')
        self.panel.feed(data[:2])
        self.show_and_drain()
        self.panel.feed(data[2:])
        self.show_and_drain()
        self.assertIn('中文', self.panel.output.toPlainText())

    def test_different_port_reopen_discards_old_tail_and_screen(self):
        self.panel.output.feed(b'old screen')
        self.panel.feed(b'stale tail')
        self.panel.set_connected(False)
        self.panel.set_connected(True, 'another-port @ 115200')
        self.panel.feed(b'new screen')
        self.show_and_drain()
        text = self.panel.output.toPlainText()
        self.assertIn('new screen', text)
        self.assertNotIn('old screen', text)
        self.assertNotIn('stale tail', text)


if __name__ == '__main__':
    unittest.main()
