"""Qt VT terminal: pyte screen model, bounded history and direct keyboard input."""
import copy
import codecs
import math
import re

import pyte
from wcwidth import wcswidth
from PyQt5.QtCore import Qt, QRect, QPoint, QPointF, QEvent, QTimer, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QInputMethodEvent,
                         QTextLayout, QTextCharFormat, QTextFormat)
from PyQt5.QtWidgets import QAbstractScrollArea, QApplication, QMenu


class TerminalScreen(pyte.HistoryScreen):
    """Add xterm's alternate screen and terminal response callback to pyte."""
    STATE = ('savepoints', 'columns', 'lines', 'buffer', 'dirty', 'margins',
             'mode', 'title', 'icon_name', 'charset', 'g0_charset', 'g1_charset',
             'tabstops', 'cursor', 'saved_columns', 'history', 'backarrow_mode')

    def __init__(self, columns, lines, reply):
        self.primary = None
        self.reply = reply
        super().__init__(columns, lines, history=2000)

    def write_process_input(self, data):
        self.reply(data.encode('utf-8'))

    def reset(self):
        self.backarrow_mode = None
        super().reset()

    def resize(self, lines=None, columns=None):
        lines, columns = lines or self.lines, columns or self.columns
        if (lines, columns) == (self.lines, self.columns):
            return
        # Remove unused bottom rows before scrolling away text above the cursor.
        # This also preserves the primary prompt when leaving a resized alt screen.
        if lines < self.lines:
            drop = max(0, self.cursor.y - lines + 1)
            old = dict(self.buffer)
            if self.primary is None:
                for y in range(drop):
                    self.history.top.append(self.buffer[y])
            self.buffer.clear()
            for y in range(lines):
                if y + drop in old:
                    self.buffer[y] = old[y + drop]
            self.cursor.y -= drop
        if columns < self.columns:
            for row in self.buffer.values():
                for x in list(row):
                    if x >= columns:
                        del row[x]
        self.lines, self.columns = lines, columns
        self.cursor.x = min(self.cursor.x, columns - 1)
        self.cursor.y = min(self.cursor.y, lines - 1)
        self.set_margins()
        self.dirty.update(range(lines))

    def set_mode(self, *modes, **kwargs):
        if kwargs.get('private') and any(m in (47, 1047, 1049) for m in modes):
            if self.primary is None:
                self.primary = {name: copy.deepcopy(getattr(self, name)) for name in self.STATE}
                # Reset the alternate screen without mutating the saved primary.
                super().reset()
        super().set_mode(*modes, **kwargs)
        if kwargs.get('private') and 67 in modes:
            self.backarrow_mode = True

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get('private') and any(m in (47, 1047, 1049) for m in modes):
            if self.primary is not None:
                columns, lines = self.columns, self.lines
                for name, value in self.primary.items():
                    setattr(self, name, value)
                self.primary = None
                self.resize(lines=lines, columns=columns)
                self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)
        if kwargs.get('private') and 67 in modes:
            self.backarrow_mode = False


class SSHTerminal(QAbstractScrollArea):
    data_ready = pyqtSignal(bytes)
    size_changed = pyqtSignal(int, int)
    SELECTION_FG = '#101820'
    SELECTION_BG = '#b8d8ff'
    COLORS = {
        'black': '#1b1d23', 'red': '#e06c75', 'green': '#98c379',
        'brown': '#e5c07b', 'blue': '#61afef', 'magenta': '#c678dd',
        'cyan': '#56b6c2', 'white': '#abb2bf', 'brightblack': '#5c6370',
        'brightred': '#ff7a85', 'brightgreen': '#b5e890', 'brightbrown': '#ffe19a',
        'brightblue': '#8bcaff', 'brightmagenta': '#dfa3ff',
        'brightcyan': '#83e4ee', 'brightwhite': '#ffffff',
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont('monospace', 11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        metrics = QFontMetrics(font)
        self.cell_width = max(1, metrics.horizontalAdvance('M'))
        self.cell_height = max(1, math.ceil(metrics.height() * 1.12))
        self.ascent = metrics.ascent()
        self.connected = False
        self.waiting_for_output = True
        self.preedit = ''
        self._resetting_input_method = False
        self.preedit_cursor = 0
        self.preedit_cursor_visible = True
        self.preedit_formats = []
        self.cursor_on = True
        self.responses_enabled = True
        self.return_bytes = b'\r'
        self.backspace_bytes = b'\x7f'
        self.backspace_override = False
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.selection = None
        self.dragging_selection = False
        self.drag_position = QPoint()
        self.selection_scroll_timer = QTimer(self)
        self.selection_scroll_timer.setInterval(40)
        self.selection_scroll_timer.timeout.connect(self._scroll_selection)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.viewport().setAttribute(Qt.WA_InputMethodEnabled, True)
        self.viewport().setFocusProxy(self)
        self.viewport().setCursor(Qt.IBeamCursor)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumSize(300, 160)
        self.screen = TerminalScreen(80, 24, self._reply)
        self.stream = pyte.Stream(self.screen)
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.setToolTip('直接输入；Ctrl+C 中断；Ctrl+Shift+C 复制；Ctrl+Shift+V 粘贴')
        self.blink_timer = QTimer(self)
        self.blink_timer.setInterval(max(200, QApplication.cursorFlashTime() // 2))
        self.blink_timer.timeout.connect(self._blink_cursor)

    def set_connected(self, connected):
        self.connected = connected
        if not connected:
            self._cancel_preedit()
        self._update_input_method()
        self.viewport().update()

    def reset_stream(self):
        self._stop_selection_drag()
        self._cancel_preedit()
        self.decoder.reset()
        self.waiting_for_output = True
        self.screen = TerminalScreen(self.screen.columns, self.screen.lines, self._reply)
        self.stream = pyte.Stream(self.screen)
        self.selection = None
        self._refresh()

    def resume_stream(self):
        # Discard incomplete UTF-8/escape sequences across a disconnect while
        # preserving the screen, history and terminal modes for this device.
        self._stop_selection_drag()
        self._cancel_preedit()
        self.decoder.reset()
        self.stream = pyte.Stream(self.screen)
        self.selection = None
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self._refresh()

    def clear(self):
        self._stop_selection_drag()
        # Local clear must preserve remote terminal modes (e.g. application keys).
        self.screen.erase_in_display(2)
        self.screen.cursor_position()
        self.screen.history.top.clear()
        self.screen.history.bottom.clear()
        self.selection = None
        self._refresh()

    def append_output(self, text):
        if text:
            self.waiting_for_output = False
        self.stream.feed(text)
        self._refresh()

    def feed(self, data):
        self.append_output(self.decoder.decode(data))

    def _reply(self, data):
        if self.responses_enabled:
            self._send(data)

    def append_notice(self, text):
        # Application diagnostics are text, never terminal control sequences.
        self.append_output('\r\n' + ''.join(c for c in text if c >= ' ') + '\r\n')

    def _refresh(self):
        scroll = self.verticalScrollBar()
        bottom = scroll.value() == scroll.maximum() and not self.dragging_selection
        scroll.setRange(0, 0 if self.screen.primary is not None else len(self.screen.history.top))
        scroll.setPageStep(self.screen.lines)
        if bottom:
            scroll.setValue(scroll.maximum())
        self.screen.dirty.clear()
        self.viewport().update()
        self._update_input_method()

    def _lines(self):
        history = [] if self.screen.primary is not None else list(self.screen.history.top)
        return history + [self.screen.buffer[y] for y in range(self.screen.lines)]

    def toPlainText(self):
        return '\n'.join(''.join(line[x].data for x in range(self.screen.columns)).rstrip()
                         for line in self._lines()).rstrip()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'screen'):
            return
        columns = max(20, min(240, (self.viewport().width() - 8) // self.cell_width))
        lines = max(4, min(120, (self.viewport().height() - 8) // self.cell_height))
        if (columns, lines) != (self.screen.columns, self.screen.lines):
            self.screen.resize(lines=lines, columns=columns)
            self.screen.cursor.x = min(self.screen.cursor.x, columns - 1)
            self.screen.cursor.y = min(self.screen.cursor.y, lines - 1)
            self.selection = None
            self._refresh()
            self.size_changed.emit(columns, lines)

    def _color(self, name, background=False, bold=False):
        if name == 'default':
            return QColor('#171a21' if background else '#d8dee9')
        if bold and not background and 'bright' + name in self.COLORS:
            name = 'bright' + name
        if name in self.COLORS:
            return QColor(self.COLORS[name])
        return QColor('#' + name) if re.fullmatch('[0-9a-fA-F]{6}', name) else QColor('#d8dee9')

    def _selected(self, row, column):
        if not self.selection:
            return False
        start, end = sorted(self.selection)
        return start <= (row, column) < end

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor('#171a21'))
        rows = self._lines()
        offset = self.verticalScrollBar().value()
        default = self.screen.default_char
        base_bg = QColor('#171a21')
        fonts = {}
        colors = {}
        for y in range(self.screen.lines):
            row = rows[offset + y] if offset + y < len(rows) else {}
            x = 0
            while x < self.screen.columns:
                char = row.get(x, default)
                if char.data == '':
                    x += 1
                    continue
                start = x
                selected = self._selected(offset + y, x)
                text = char.data
                # Coalesce fixed-width ASCII with identical attributes. CJK,
                # combining characters and wide cells retain explicit placement.
                if len(text) == 1 and ' ' <= text <= '~':
                    run = [text]
                    x += 1
                    while x < self.screen.columns:
                        next_char = row.get(x, default)
                        if (len(next_char.data) != 1 or not ' ' <= next_char.data <= '~'
                                or next_char[1:] != char[1:]
                                or self._selected(offset + y, x) != selected):
                            break
                        run.append(next_char.data)
                        x += 1
                    text = ''.join(run)
                else:
                    x += max(1, wcswidth(text))
                style = (char.fg, char.bg, char.bold, char.reverse, selected)
                if style not in colors:
                    fg, bg = self._color(char.fg, bold=char.bold), self._color(char.bg, True)
                    if char.reverse:
                        fg, bg = bg, fg
                    if selected:
                        fg, bg = QColor(self.SELECTION_FG), QColor(self.SELECTION_BG)
                    colors[style] = (fg, bg)
                fg, bg = colors[style]
                rect = QRect(4 + start * self.cell_width, 4 + y * self.cell_height,
                             self.cell_width * (x - start), self.cell_height)
                if bg != base_bg:
                    painter.fillRect(rect, bg)
                if text.strip() or char.underscore or char.strikethrough:
                    font_style = (char.bold, char.italics, char.underscore, char.strikethrough)
                    if font_style not in fonts:
                        font = self.font()
                        font.setBold(char.bold)
                        font.setItalic(char.italics)
                        font.setUnderline(char.underscore)
                        font.setStrikeOut(char.strikethrough)
                        font.setKerning(False)
                        font.setLetterSpacing(QFont.AbsoluteSpacing,
                                              self.cell_width - QFontMetricsF(font).horizontalAdvance('M'))
                        fonts[font_style] = font
                    painter.setFont(fonts[font_style])
                    painter.setPen(fg)
                    painter.drawText(rect.x(), rect.y() + self.ascent, text)
        if self.connected and self.waiting_for_output:
            painter.setPen(QColor('#abb2bf'))
            painter.drawText(self.viewport().rect().adjusted(12, 32, -12, -12),
                             Qt.AlignTop | Qt.TextWordWrap,
                             '等待设备输出。Linux shell 可按 Ctrl+L 刷新提示符。')
        at_bottom = offset == self.verticalScrollBar().maximum()
        if self.preedit and at_bottom:
            rect = self._cursor_rect(include_preedit=False)
            layout = self._preedit_layout()
            painter.fillRect(QRect(rect.x(), rect.y(), max(2, int(layout.boundingRect().width()) + 2),
                                  self.cell_height), QColor('#171a21'))
            layout.draw(painter, QPointF(rect.x(), rect.y()))
        if (at_bottom and not self.screen.cursor.hidden and self.cursor_on
                and (not self.preedit or self.preedit_cursor_visible)):
            painter.fillRect(self._cursor_rect(), QColor('#88c0d0' if self.connected else '#5c6370'))

    def _preedit_layout(self):
        layout = QTextLayout(self.preedit, self.font())
        default = QTextLayout.FormatRange()
        default.start = 0
        default.length = len(self.preedit.encode('utf-16-le')) // 2
        default.format = QTextCharFormat()
        default.format.setForeground(QColor('#d8dee9'))
        default.format.setFontUnderline(True)
        layout.setFormats([default] + self.preedit_formats)
        layout.beginLayout()
        line = layout.createLine()
        if line.isValid():
            line.setLineWidth(100000)
        layout.endLayout()
        return layout

    def _cursor_rect(self, include_preedit=True):
        cursor = self.screen.cursor
        x = 4 + min(cursor.x, self.screen.columns - 1) * self.cell_width
        if include_preedit and self.preedit:
            layout = self._preedit_layout()
            line = layout.lineAt(0)
            x += int(line.cursorToX(self.preedit_cursor)[0])
        return QRect(x, 4 + cursor.y * self.cell_height, 2, self.cell_height)

    def _update_input_method(self):
        if self.hasFocus():
            QApplication.inputMethod().update(Qt.ImEnabled | Qt.ImCursorRectangle | Qt.ImCursorPosition
                                              | Qt.ImSurroundingText | Qt.ImAnchorPosition)

    def _cancel_preedit(self):
        # Some native IMEs synchronously deliver a commit during reset().
        # Cancellation must never transmit that pending text to a remote vi.
        if self._resetting_input_method:
            return
        self._resetting_input_method = True
        try:
            if self.hasFocus():
                QApplication.inputMethod().reset()
        finally:
            self.preedit = ''
            self.preedit_formats = []
            self.preedit_cursor = 0
            self._resetting_input_method = False

    def reset_input(self):
        """Recover local input without sending commands or changing remote data."""
        self.setFocus(Qt.OtherFocusReason)
        self._stop_selection_drag()
        self._cancel_preedit()
        self.preedit_cursor_visible = True
        self.cursor_on = True
        self._update_input_method()
        self.viewport().update()

    def _blink_cursor(self):
        self.cursor_on = not self.cursor_on
        self.viewport().update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.cursor_on = True
        if QApplication.cursorFlashTime() > 0:
            self.blink_timer.start()
        self._update_input_method()
        self.viewport().update()

    def focusOutEvent(self, event):
        self._stop_selection_drag()
        self._cancel_preedit()
        self.blink_timer.stop()
        self.cursor_on = True
        super().focusOutEvent(event)
        self.viewport().update()

    def viewportEvent(self, event):
        if event.type() == QEvent.InputMethod:
            self.inputMethodEvent(event)
            return True
        if event.type() == QEvent.InputMethodQuery:
            for query in (Qt.ImEnabled, Qt.ImCursorRectangle, Qt.ImFont, Qt.ImHints,
                          Qt.ImCursorPosition, Qt.ImAnchorPosition, Qt.ImSurroundingText,
                          Qt.ImCurrentSelection):
                if event.queries() & query:
                    value = self._cursor_rect() if query == Qt.ImCursorRectangle else self.inputMethodQuery(query)
                    event.setValue(query, value)
            return True
        return super().viewportEvent(event)

    def _send(self, data):
        if self.connected:
            self._stop_selection_drag()
            self.cursor_on = True
            self.selection = None
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self.data_ready.emit(data)

    def event(self, event):
        # QWidget otherwise consumes Tab for focus navigation.
        if event.type() == event.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:
                self.copy_selection()
                return
            if key == Qt.Key_V:
                self.paste()
                return
            if key == Qt.Key_A:
                self.select_all()
                return
        if self.preedit:
            # IMEs normally consume composition keys before they reach us.
            # A stale preedit must not trap Esc or terminal control shortcuts.
            if key == Qt.Key_Escape or (mods & Qt.ControlModifier and not mods & Qt.AltModifier):
                self._cancel_preedit()
            else:
                event.accept()
                return
        if mods & Qt.ShiftModifier and key in (Qt.Key_PageUp, Qt.Key_PageDown):
            delta = self.screen.lines * (-1 if key == Qt.Key_PageUp else 1)
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + delta)
            return
        app_cursor = (1 << 5) in self.screen.mode
        prefix = b'\x1bO' if app_cursor else b'\x1b['
        backspace = self.backspace_bytes
        if not self.backspace_override and self.screen.backarrow_mode is not None:
            backspace = b'\x08' if self.screen.backarrow_mode else b'\x7f'
        keys = {Qt.Key_Return: self.return_bytes, Qt.Key_Enter: self.return_bytes, Qt.Key_Backspace: backspace,
                Qt.Key_Tab: b'\t', Qt.Key_Backtab: b'\x1b[Z', Qt.Key_Escape: b'\x1b',
                Qt.Key_Up: prefix + b'A', Qt.Key_Down: prefix + b'B',
                Qt.Key_Right: prefix + b'C', Qt.Key_Left: prefix + b'D',
                Qt.Key_Home: prefix + b'H', Qt.Key_End: prefix + b'F',
                Qt.Key_Insert: b'\x1b[2~', Qt.Key_Delete: b'\x1b[3~',
                Qt.Key_PageUp: b'\x1b[5~', Qt.Key_PageDown: b'\x1b[6~'}
        for i, suffix in enumerate((b'OP', b'OQ', b'OR', b'OS', b'[15~', b'[17~',
                                    b'[18~', b'[19~', b'[20~', b'[21~', b'[23~', b'[24~')):
            keys[Qt.Key_F1 + i] = b'\x1b' + suffix
        if mods & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
            data = bytes([key - Qt.Key_A + 1])
        elif mods & Qt.ControlModifier and key in (Qt.Key_Space, Qt.Key_At):
            data = b'\x00'
        elif mods & Qt.ControlModifier and Qt.Key_BracketLeft <= key <= Qt.Key_Underscore:
            data = bytes([key - 64])
        else:
            data = keys.get(key, event.text().encode('utf-8'))
            if mods & Qt.AltModifier:
                data = b'\x1b' + data
        if data:
            self._send(data)
        event.accept()

    def inputMethodEvent(self, event):
        if self._resetting_input_method:
            event.accept()
            return
        if not self.connected:
            event.ignore()
            return
        if event.commitString():
            self._send(event.commitString().encode('utf-8'))
        self.preedit = event.preeditString()
        self.preedit_cursor = len(self.preedit.encode('utf-16-le')) // 2
        self.preedit_cursor_visible = True
        self.preedit_formats = []
        for attribute in event.attributes():
            if attribute.type == QInputMethodEvent.Cursor:
                self.preedit_cursor = attribute.start
                self.preedit_cursor_visible = attribute.length != 0
            elif attribute.type == QInputMethodEvent.TextFormat:
                # Native IMEs deliver a base QTextFormat, unlike manually
                # constructed events which may contain QTextCharFormat.
                value = attribute.value
                if not isinstance(value, QTextFormat) or not value.isCharFormat():
                    continue
                fmt = QTextLayout.FormatRange()
                fmt.start, fmt.length = attribute.start, attribute.length
                fmt.format = value.toCharFormat()
                self.preedit_formats.append(fmt)
        self.cursor_on = True
        self._update_input_method()
        self.viewport().update()
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.ImEnabled:
            return self.connected
        if query == Qt.ImCursorRectangle:
            return self._cursor_rect().translated(self.viewport().pos())
        if query == Qt.ImFont:
            return self.font()
        if query == Qt.ImHints:
            return int(Qt.ImhNone)
        if query in (Qt.ImCursorPosition, Qt.ImAnchorPosition, Qt.ImAbsolutePosition):
            return 0
        if query in (Qt.ImSurroundingText, Qt.ImCurrentSelection, Qt.ImTextBeforeCursor, Qt.ImTextAfterCursor):
            # Remote screen contents may contain secrets and aren't locally editable.
            return ''
        return super().inputMethodQuery(query)

    def paste(self):
        text = QApplication.clipboard().text().replace('\r\n', '\n').replace('\r', '\n')
        if not text:
            return
        # Do not turn clipboard control bytes into injected terminal key sequences.
        text = ''.join(c for c in text if c in '\n\t' or c >= ' ')
        data = text.encode('utf-8').replace(b'\n', self.return_bytes)
        if (2004 << 5) in self.screen.mode:
            data = b'\x1b[200~' + data + b'\x1b[201~'
        self._send(data)

    def _position(self, point):
        return (max(0, min(self.screen.lines - 1, (point.y() - 4) // self.cell_height))
                + self.verticalScrollBar().value(),
                max(0, min(self.screen.columns, (point.x() - 4) // self.cell_width)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.reset_input()
            self.dragging_selection = True
            self.drag_position = event.pos()
            pos = self._position(event.pos())
            self.selection = (pos, pos)
            self.viewport().update()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.selection and self.dragging_selection:
            self.drag_position = event.pos()
            self.selection = (self.selection[0], self._position(event.pos()))
            if self._selection_scroll_delta():
                self.selection_scroll_timer.start()
            else:
                self.selection_scroll_timer.stop()
            self.viewport().update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.dragging_selection and self.selection:
                self.selection = (self.selection[0], self._position(event.pos()))
            self._stop_selection_drag()
            self.viewport().update()
            event.accept()

    def _stop_selection_drag(self):
        self.dragging_selection = False
        self.selection_scroll_timer.stop()

    def hideEvent(self, event):
        self._stop_selection_drag()
        super().hideEvent(event)

    def _selection_scroll_delta(self):
        y = self.drag_position.y()
        edge = min(self.cell_height, 24)
        if y < edge:
            return -min(8, 1 + (edge - y) // self.cell_height)
        if y >= self.viewport().height() - edge:
            return min(8, 1 + (y - self.viewport().height() + edge) // self.cell_height)
        return 0

    def _scroll_selection(self):
        if not self.dragging_selection or not self.selection:
            self.selection_scroll_timer.stop()
            return
        delta = self._selection_scroll_delta()
        scroll = self.verticalScrollBar()
        scroll.setValue(scroll.value() + delta)

    def _scroll_changed(self, value):
        if self.dragging_selection and self.selection:
            self.selection = (self.selection[0], self._position(self.drag_position))
        self.viewport().update()

    def select_all(self):
        self._stop_selection_drag()
        rows = self._lines()
        self.selection = ((0, 0), (len(rows) - 1, self.screen.columns))
        self.viewport().update()

    def copy_selection(self):
        if not self.selection:
            return
        start, end = sorted(self.selection)
        rows, text = self._lines(), []
        for y in range(start[0], min(end[0] + 1, len(rows))):
            first = start[1] if y == start[0] else 0
            last = end[1] if y == end[0] else self.screen.columns
            text.append(''.join(rows[y][x].data for x in range(first, last)).rstrip())
        QApplication.clipboard().setText('\n'.join(text))

    def contextMenuEvent(self, event):
        self._stop_selection_drag()
        menu = QMenu(self)
        menu.addAction('全选（Ctrl+Shift+A）', self.select_all)
        copy_action = menu.addAction('复制（Ctrl+Shift+C）', self.copy_selection)
        copy_action.setEnabled(bool(self.selection))
        paste_action = menu.addAction('粘贴（Ctrl+Shift+V）', self.paste)
        paste_action.setEnabled(self.connected)
        menu.addSeparator()
        menu.addAction('恢复键盘输入（仅本地）', self.reset_input)
        menu.exec_(event.globalPos())
