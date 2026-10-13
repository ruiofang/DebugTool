"""Long send history must not prevent resizing the window or its splitter.

Run from the project root with QT_QPA_PLATFORM=offscreen and unittest.
Settings live in a temporary directory; no device is enumerated or opened.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QSplitter

import main


SCREENSHOT_HISTORY = (
    '{"type":"omnipicker","id":1,"cmd":"move","position":0,"speed":255,'
    '"force":255,"acceleration":255,"deceleration":255}'
)
LONG_HISTORY = json.dumps({"command": "发送完整数据", "data": "0123456789abcdef" * 256},
                          ensure_ascii=False)


class HistoryLayoutTests(unittest.TestCase):
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
        self.stack.enter_context(patch.dict(os.environ, {"QT5COM_PORTRAIT": "0"}))
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in list(self.windows):
            self.close_window(window)

    def close_window(self, window):
        window.close()
        window.settings.sync()
        self.windows.remove(window)
        window.deleteLater()
        self.app.processEvents()

    def seed_settings(self, theme, history):
        settings = QSettings(self.config_file, QSettings.IniFormat)
        settings.clear()
        settings.setValue("theme", theme)
        settings.setValue("history", json.dumps(history, ensure_ascii=False))
        settings.sync()

    def open_window(self):
        window = main.DebugTool()
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def main_splitter(self, window):
        splitter = window.centralWidget().layout().itemAt(0).widget()
        self.assertIsInstance(splitter, QSplitter)
        self.assertEqual(splitter.orientation(), Qt.Horizontal)
        return splitter

    def drag_handle(self, splitter, distance):
        handle = splitter.handle(1)
        start = handle.rect().center()
        QTest.mousePress(handle, Qt.LeftButton, Qt.NoModifier, start)
        target = start + QPoint(distance, 0)
        # An explicit held-button move works with offscreen Qt as well as X11.
        event = QMouseEvent(QEvent.MouseMove, QPointF(target),
                            QPointF(handle.mapToGlobal(target)), Qt.NoButton,
                            Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(handle, event)
        QTest.mouseRelease(handle, Qt.LeftButton, Qt.NoModifier,
                           handle.rect().center())
        self.app.processEvents()

    def assert_draggable_both_ways(self, window, width):
        window.resize(width, 900)
        self.app.processEvents()
        self.assertEqual(window.width(), width,
                         "History forced the window wider than requested")
        splitter = self.main_splitter(window)
        original = splitter.sizes()
        self.drag_handle(splitter, 140)
        wider = splitter.sizes()
        self.assertGreater(wider[0], original[0] + 100,
                           f"Left panel could not widen: {original} -> {wider}")
        self.assertLess(wider[1], original[1] - 100)
        self.drag_handle(splitter, -140)
        narrower = splitter.sizes()
        self.assertLess(narrower[0], wider[0] - 100,
                        f"Left panel could not shrink: {wider} -> {narrower}")
        self.assertAlmostEqual(narrower[0], original[0], delta=2)

    def assert_complete_selection(self, window, history):
        self.assertEqual([window.cmb_history.itemText(i)
                          for i in range(window.cmb_history.count())], history)
        for index, text in enumerate(history):
            window.cmb_history.setCurrentIndex(index)
            window.cmb_history.activated[int].emit(index)
            self.assertEqual(window.send_edit.toPlainText(), text)

    def baseline_width(self, theme):
        self.seed_settings(theme, [])
        window = self.open_window()
        minimum = window.minimumSizeHint().width()
        self.close_window(window)
        return minimum

    def test_persisted_json_remains_resizable_after_restart_in_both_themes(self):
        for theme in ("浅色", "深色"):
            with self.subTest(theme=theme):
                baseline = self.baseline_width(theme)
                history = [SCREENSHOT_HISTORY, "AT"]
                self.seed_settings(theme, history)
                for restart in range(2):
                    with self.subTest(restart=restart):
                        window = self.open_window()
                        self.assertLessEqual(window.minimumSizeHint().width(), baseline,
                                             "Saved history inflated the minimum width")
                        self.assert_draggable_both_ways(window, baseline + 400)
                        self.assert_complete_selection(window, history)
                        self.close_window(window)

    def test_runtime_history_and_full_selection_survive_restart_in_both_themes(self):
        for theme in ("浅色", "深色"):
            with self.subTest(theme=theme):
                self.seed_settings(theme, [])
                window = self.open_window()
                baseline = window.minimumSizeHint().width()
                window._push_history(SCREENSHOT_HISTORY)
                window._push_history(LONG_HISTORY)
                self.app.processEvents()
                history = [LONG_HISTORY, SCREENSHOT_HISTORY]
                self.assertLessEqual(window.minimumSizeHint().width(), baseline)
                self.assert_draggable_both_ways(window, baseline + 400)
                self.assert_complete_selection(window, history)
                self.close_window(window)

                restarted = self.open_window()
                self.assertLessEqual(restarted.minimumSizeHint().width(), baseline,
                                     "Runtime history broke resizing after restart")
                self.assert_draggable_both_ways(restarted, baseline + 400)
                self.assert_complete_selection(restarted, history)
                self.close_window(restarted)


if __name__ == "__main__":
    unittest.main()
