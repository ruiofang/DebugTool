"""Drag whole quick-command rows without Qt discarding their cell widgets.

The owner handles ``move_rows_requested(rows, destination)`` by rebuilding its
command records. ``destination`` is an insertion position in the *old* table,
from zero (before the first row) through ``rowCount()`` (after the last row).
Rows are logical row indices in their current table order.
"""

import json
import uuid

from PyQt5.QtCore import QEvent, QItemSelectionModel, QMimeData, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QDrag, QPainter, QPen, QPolygon
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QTableWidget


class QuickCommandTable(QTableWidget):
    """A command table supporting internal, single- and multiple-row dragging."""

    move_rows_requested = pyqtSignal(object, int)
    _MIME_TYPE = "application/x-debugtool-quick-command-rows"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        # QTableWidget's built-in indicator and internal move operate on items,
        # not on checkbox/button widgets. Draw and perform the move ourselves.
        self.setDropIndicatorShown(False)
        self._drag_token = uuid.uuid4().hex
        self._header_press = None
        self._drop_row = None
        self._drop_position = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(50)
        self._scroll_timer.timeout.connect(self._scroll_while_dragging)
        header = self.verticalHeader()
        header.setSectionsMovable(False)
        header.setAcceptDrops(True)
        header.viewport().setAcceptDrops(True)
        header.viewport().installEventFilter(self)
        self.setToolTip("拖动左侧行号，或拖动已选中的名称/内容，可上下调整指令顺序；Ctrl/Shift 可多选。")
        header.setToolTip("上下拖动行号调整顺序；Ctrl/Shift 可多选后一起拖动。")

    def eventFilter(self, watched, event):
        if watched is self.verticalHeader().viewport():
            # The row-number strip shares the table viewport's vertical
            # coordinates. A drag can stay entirely within that strip.
            handlers = {QEvent.DragEnter: self.dragEnterEvent,
                        QEvent.DragMove: self.dragMoveEvent,
                        QEvent.DragLeave: self.dragLeaveEvent,
                        QEvent.Drop: self.dropEvent}
            if event.type() in handlers:
                handlers[event.type()](event)
                return True
        if watched is self.verticalHeader().viewport() and self.isEnabled():
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                row = self.verticalHeader().logicalIndexAt(event.pos())
                self._header_press = QPoint(event.pos()) if row >= 0 else None
                if row >= 0 and not (event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                    selected = self.selectionModel().isRowSelected(row, self.rootIndex())
                    if not selected:
                        self.selectRow(row)
                    self.selectionModel().setCurrentIndex(
                        self.model().index(row, 0), QItemSelectionModel.NoUpdate)
                    # Preserve a multi-row selection when starting a drag from
                    # one of its row numbers.
                    return True
            elif event.type() == QEvent.MouseMove and self._header_press is not None:
                if event.buttons() & Qt.LeftButton:
                    distance = (event.pos() - self._header_press).manhattanLength()
                    if distance >= QApplication.startDragDistance():
                        self._header_press = None
                        self.startDrag(Qt.MoveAction)
                    return True
            elif event.type() == QEvent.MouseButtonRelease:
                self._header_press = None
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange and not self.isEnabled():
            self._header_press = None
            self._clear_drop_indicator()

    def _mime_for_rows(self, rows):
        mime = QMimeData()
        mime.setData(self._MIME_TYPE, json.dumps({"token": self._drag_token, "rows": rows}).encode("utf-8"))
        return mime

    def startDrag(self, supported_actions):
        if not self.isEnabled():
            return
        rows = sorted({index.row() for index in self.selectedIndexes()})
        if not rows:
            return
        drag = QDrag(self)
        drag.setMimeData(self._mime_for_rows(rows))
        try:
            drag.exec_(Qt.MoveAction, Qt.MoveAction)
        finally:
            self._clear_drop_indicator()

    def _rows_from_event(self, event):
        if not self.isEnabled() or event.source() is not self:
            return None
        mime = event.mimeData()
        if not mime.hasFormat(self._MIME_TYPE):
            return None
        try:
            payload = json.loads(bytes(mime.data(self._MIME_TYPE)).decode("utf-8"))
            rows = payload["rows"]
            if payload["token"] != self._drag_token or not isinstance(rows, list) or not rows:
                return None
            if any(type(row) is not int or row < 0 or row >= self.rowCount() for row in rows):
                return None
            if len(set(rows)) != len(rows):
                return None
            return sorted(rows)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def _drop_destination(self, position):
        row = self.rowAt(position.y())
        if row < 0:
            return 0 if position.y() < 0 else self.rowCount()
        top = self.rowViewportPosition(row)
        return row + (position.y() >= top + self.rowHeight(row) / 2)

    def dragEnterEvent(self, event):
        if self._rows_from_event(event) is None:
            event.ignore()
            return
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def dragMoveEvent(self, event):
        if self._rows_from_event(event) is None:
            self._clear_drop_indicator()
            event.ignore()
            return
        self._drop_position = QPoint(event.pos())
        self._drop_row = self._drop_destination(event.pos())
        self.viewport().update()
        self._scroll_timer.start()
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event):
        self._clear_drop_indicator()
        event.accept()

    def dropEvent(self, event):
        rows = self._rows_from_event(event)
        destination = self._drop_destination(event.pos())
        self._clear_drop_indicator()
        if rows is None:
            event.ignore()
            return
        # Do not call QTableWidget.dropEvent: it can move only QTableWidgetItems
        # and lose the loop/HEX/newline checkboxes and send buttons.
        remaining = [row for row in range(self.rowCount()) if row not in rows]
        insertion = destination - sum(row < destination for row in rows)
        reordered = remaining[:insertion] + rows + remaining[insertion:]
        if reordered != list(range(self.rowCount())):
            self.move_rows_requested.emit(rows, destination)
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def _clear_drop_indicator(self):
        self._scroll_timer.stop()
        self._drop_row = self._drop_position = None
        self.viewport().update()

    def _scroll_while_dragging(self):
        if not self.isEnabled() or self._drop_position is None:
            self._clear_drop_indicator()
            return
        margin = 20
        y = self._drop_position.y()
        direction = -1 if y < margin else (1 if y > self.viewport().height() - margin else 0)
        if direction:
            scroll = self.verticalScrollBar()
            scroll.setValue(scroll.value() + direction * scroll.singleStep())
            self._drop_row = self._drop_destination(self._drop_position)
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_row is None:
            return
        if self._drop_row < self.rowCount():
            y = self.rowViewportPosition(self._drop_row)
        elif self.rowCount():
            last = self.rowCount() - 1
            y = self.rowViewportPosition(last) + self.rowHeight(last)
        else:
            y = 0
        y = max(1, min(y, self.viewport().height() - 2))
        painter = QPainter(self.viewport())
        color = self.palette().highlight().color()
        painter.setPen(QPen(color, 2))
        painter.drawLine(0, y, self.viewport().width(), y)
        painter.setBrush(color)
        painter.drawPolygon(QPolygon([QPoint(0, y - 4), QPoint(6, y), QPoint(0, y + 4)]))
