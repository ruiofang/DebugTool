"""Quick-command drag/drop QA using Qt events and fake serial writes only."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, QItemSelectionModel, QPoint, QPointF, Qt
from PyQt5.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QMouseEvent
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLineEdit, QTableWidgetItem

import main
from quick_command_table import QuickCommandTable


class InternalDragEnter(QDragEnterEvent):
    def __init__(self, table, rows, position, source=None):
        mime = table._mime_for_rows(rows)
        super().__init__(position, Qt.MoveAction, mime,
                         Qt.LeftButton, Qt.NoModifier)
        self.mime = mime
        self.origin = table if source is None else source

    def source(self):
        return self.origin


class InternalDragMove(QDragMoveEvent):
    def __init__(self, table, rows, position, source=None):
        mime = table._mime_for_rows(rows)
        super().__init__(position, Qt.MoveAction, mime,
                         Qt.LeftButton, Qt.NoModifier)
        self.mime = mime
        self.origin = table if source is None else source

    def source(self):
        return self.origin


class InternalDrop(QDropEvent):
    def __init__(self, table, rows, position, source=None):
        mime = table._mime_for_rows(rows)
        super().__init__(QPointF(position), Qt.MoveAction, mime,
                         Qt.LeftButton, Qt.NoModifier)
        self.mime = mime
        self.origin = table if source is None else source

    def source(self):
        return self.origin


def insertion_point(table, destination):
    if destination == table.rowCount():
        last = table.rowCount() - 1
        return QPoint(8, table.rowViewportPosition(last) + table.rowHeight(last) + 2)
    return QPoint(8, table.rowViewportPosition(destination) + 1)


def drop_rows(table, rows, destination, header=False, source=None):
    """Deliver actual Qt drag/drop events, including through the row header."""
    point = insertion_point(table, destination)
    enter = InternalDragEnter(table, rows, point, source=source)
    move = InternalDragMove(table, rows, point, source=source)
    drop = InternalDrop(table, rows, point, source=source)
    if header:
        surface = table.verticalHeader().viewport()
        QApplication.sendEvent(surface, enter)
        QApplication.sendEvent(surface, move)
        QApplication.sendEvent(surface, drop)
    else:
        table.dragEnterEvent(enter)
        table.dragMoveEvent(move)
        table.dropEvent(drop)
    return enter, move, drop


class QuickTableDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.table = QuickCommandTable(5, 6)
        for row in range(5):
            self.table.setItem(row, 1, QTableWidgetItem(f"name {row}"))
            self.table.setItem(row, 2, QTableWidgetItem(f"content {row}"))
        self.table.resize(640, 260)
        self.table.show()
        self.app.processEvents()
        self.addCleanup(self.close_table)

    def close_table(self):
        self.table.close()
        self.table.deleteLater()
        self.app.processEvents()

    def select_rows(self, rows):
        self.table.clearSelection()
        for row in rows:
            self.table.selectionModel().select(self.table.model().index(row, 1),
                                              QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def test_header_mouse_drag_preserves_multiple_selected_rows(self):
        self.select_rows([1, 3])
        header = self.table.verticalHeader().viewport()
        origin = QPoint(8, self.table.rowViewportPosition(1) + 8)
        with patch.object(self.table, "startDrag", return_value=None) as start:
            QTest.mousePress(header, Qt.LeftButton, Qt.NoModifier, origin)
            move = QMouseEvent(QEvent.MouseMove, QPointF(origin + QPoint(0, 40)),
                               Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
            self.app.sendEvent(header, move)
            start.assert_called_once_with(Qt.MoveAction)
        QTest.mouseRelease(header, Qt.LeftButton, Qt.NoModifier, origin)
        self.assertEqual(sorted({index.row() for index in self.table.selectedIndexes()}), [1, 3])

    def test_selected_content_cell_starts_a_row_drag(self):
        self.select_rows([1])
        origin = self.table.visualRect(self.table.model().index(1, 2)).center()
        with patch.object(self.table, "startDrag", return_value=None) as start:
            QTest.mousePress(self.table.viewport(), Qt.LeftButton, Qt.NoModifier, origin)
            # Qt first enters DraggingState, then starts the native drag after
            # the next move crosses QApplication.startDragDistance().
            for offset in (2, 45):
                point = origin + QPoint(0, offset)
                move = QMouseEvent(QEvent.MouseMove, QPointF(point),
                                   QPointF(self.table.viewport().mapToGlobal(point)),
                                   Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
                self.app.sendEvent(self.table.viewport(), move)
            start.assert_called_once()
        QTest.mouseRelease(self.table.viewport(), Qt.LeftButton, Qt.NoModifier, origin)

    def test_row_header_accepts_internal_drop_without_losing_command_cells(self):
        spy = QSignalSpy(self.table.move_rows_requested)
        events = drop_rows(self.table, [3, 1], 5, header=True)
        self.assertTrue(all(event.isAccepted() for event in events))
        self.assertEqual(list(spy), [[[1, 3], 5]])
        self.assertIsNone(self.table._drop_row)
        self.assertFalse(self.table._scroll_timer.isActive())
        # Moving is delegated to the owner, so Qt has not removed any items.
        self.assertEqual(self.table.item(1, 1).text(), "name 1")

    def test_external_cross_table_and_disabled_drops_are_rejected(self):
        spy = QSignalSpy(self.table.move_rows_requested)
        foreign = QuickCommandTable(1, 6)
        self.addCleanup(foreign.deleteLater)
        events = drop_rows(self.table, [0], 5, source=foreign)
        self.assertFalse(any(event.isAccepted() for event in events))
        self.table.setEnabled(False)
        events = drop_rows(self.table, [0], 5)
        self.assertFalse(any(event.isAccepted() for event in events))
        self.assertEqual(list(spy), [])

    def test_drop_into_selected_block_is_a_noop_and_invalid_rows_are_rejected(self):
        spy = QSignalSpy(self.table.move_rows_requested)
        events = drop_rows(self.table, [1, 2], 2)
        self.assertTrue(events[-1].isAccepted())
        self.assertEqual(list(spy), [])
        for rows in ([5], [-1], [1, 1], [True], []):
            with self.subTest(rows=rows):
                events = drop_rows(self.table, rows, 5)
                self.assertFalse(events[-1].isAccepted())
        self.assertEqual(list(spy), [])


class RecordingSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.is_open = False


class QuickDragIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config_file = str(folder / "serial_tool.ini")
        self.stack.enter_context(patch("main.config_path", return_value=self.config_file))
        self.stack.enter_context(patch.object(main.DebugTool, "refresh_ports"))
        for name in ("information", "warning", "critical"):
            self.stack.enter_context(patch(f"main.QMessageBox.{name}"))
        self.windows = []
        self.addCleanup(self.close_windows)

    def open_window(self, rows=True):
        window = main.DebugTool()
        self.windows.append(window)
        window.transport_mode.setCurrentIndex(0)
        window.ser = RecordingSerial()
        if rows:
            for entry in self.entries():
                window._add_quick_row(entry["name"], entry["content"], entry["hex"],
                                      entry["newline"], entry["loop"])
        return window

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

    @staticmethod
    def entries():
        return [
            {"name": "A", "content": "A", "hex": False, "newline": True, "loop": True},
            {"name": "B", "content": "42", "hex": True, "newline": False, "loop": False},
            {"name": "C", "content": "C", "hex": False, "newline": False, "loop": True},
            {"name": "D", "content": "44", "hex": True, "newline": True, "loop": True},
        ]

    def test_single_row_header_drop_preserves_options_and_manual_send_buttons(self):
        window = self.open_window()
        self.assertIsInstance(window.quick_table, QuickCommandTable)
        events = drop_rows(window.quick_table, [0], 4, header=True)
        self.assertTrue(events[-1].isAccepted())
        expected = self.entries()[1:] + self.entries()[:1]
        self.assertEqual(window._quick_entries(), expected)
        for row in range(4):
            window.quick_table.cellWidget(row, window.QUICK_COL_SEND).click()
        self.assertEqual(window.ser.writes, [b"B", b"C", b"D", b"A\r\n"])

    def test_noncontiguous_rows_keep_relative_order_selection_and_restart_settings(self):
        window = self.open_window()
        drop_rows(window.quick_table, [0, 2], 4)
        original = self.entries()
        expected = [original[row] for row in (1, 3, 0, 2)]
        self.assertEqual(window._quick_entries(), expected)
        self.assertEqual(sorted({index.row() for index in window.quick_table.selectedIndexes()}), [2, 3])
        self.assertEqual(json.loads(window.settings.value("quick_buttons")), expected)
        drop_rows(window.quick_table, [2, 3], 0)
        expected = [original[row] for row in (0, 2, 1, 3)]
        self.assertEqual(window._quick_entries(), expected)
        self.close_window(window)
        restarted = self.open_window(rows=False)
        self.assertEqual(restarted._quick_entries(), expected)
        for row in range(4):
            restarted.quick_table.cellWidget(row, restarted.QUICK_COL_SEND).click()
        self.assertEqual(restarted.ser.writes, [b"A\r\n", b"C", b"B", b"D"])

    def test_uncommitted_cell_edit_survives_a_row_header_drop(self):
        window = self.open_window()
        table = window.quick_table
        tabs = window.findChildren(main.QTabWidget)
        for tab in tabs:
            for index in range(tab.count()):
                if tab.widget(index).isAncestorOf(table):
                    tab.setCurrentIndex(index)
        window.show()
        self.app.processEvents()
        for column, text in ((window.QUICK_COL_NAME, "renamed before drop"),
                             (window.QUICK_COL_CONTENT, "fresh content")):
            with self.subTest(column=column):
                table.setCurrentCell(0, column)
                table.editItem(table.item(0, column))
                self.app.processEvents()
                editor = QApplication.focusWidget()
                self.assertIsInstance(editor, QLineEdit)
                editor.selectAll()
                QTest.keyClicks(editor, text)
                # No Enter or focus change until drop invokes the move owner.
                drop_rows(table, [0], 4, header=True)
                self.assertEqual(table.item(3, column).text(), text)
                drop_rows(table, [3], 0, header=True)
        self.assertEqual(window._quick_entries()[0]["name"], "renamed before drop")
        self.assertEqual(window._quick_entries()[0]["content"], "fresh content")

    def test_loop_uses_reordered_selected_commands_and_locks_dragging(self):
        window = self.open_window()
        drop_rows(window.quick_table, [3], 0)
        expected = window._quick_entries()
        window.spn_quick_interval.setValue(1)
        window.spn_quick_rounds.setValue(2)
        window.chk_quick_loop.setChecked(True)
        self.assertTrue(window._quick_loop_running)
        self.assertFalse(window.quick_table.isEnabled())
        events = drop_rows(window.quick_table, [0], 4)
        self.assertFalse(events[-1].isAccepted())
        window._move_quick_rows([0], 4)
        self.assertEqual(window._quick_entries(), expected)
        deadline = time.monotonic() + 1
        while window._quick_loop_running and time.monotonic() < deadline:
            QTest.qWait(2)
        self.assertFalse(window._quick_loop_running)
        self.assertEqual(window.ser.writes, [b"D", b"A\r\n", b"C"] * 2)
        self.assertTrue(window.quick_table.isEnabled())


if __name__ == "__main__":
    unittest.main()
