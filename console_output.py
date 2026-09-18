"""Bounded plain-text console shared by serial and SSH sessions."""
import codecs
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import QPlainTextEdit


class ConsoleOutput(QPlainTextEdit):
    MAX_CHARS = 512 * 1024

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = self
        self.escape = ''
        self.column = 0
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setMaximumBlockCount(2000)
        self.setFont(QFont('Monospace', 10))

    def reset_stream(self):
        self.decoder.reset()
        self.escape = ''
        self.column = 0

    def clear(self):
        super().clear()
        self.column = 0

    def feed(self, data):
        self.append_output(self.decoder.decode(data))

    def append_output(self, text):
        # Strip CSI/OSC sequences across chunks. This is a line console, not a VT emulator.
        clean = []
        for ch in text:
            if self.escape:
                self.escape += ch
                if self.escape.startswith('\x1b['):
                    if len(self.escape) > 2 and '@' <= ch <= '~':
                        self.escape = ''
                elif self.escape.startswith('\x1b]'):
                    if ch == '\x07' or self.escape.endswith('\x1b\\'):
                        self.escape = ''
                elif len(self.escape) >= 2:
                    self.escape = ''
                if len(self.escape) > 4096:
                    self.escape = ''
                continue
            if ch == '\x1b':
                self.escape = ch
            elif ch in '\n\t' or ch >= ' ':
                if ch != '\n' and self.column >= 4096:
                    # Bound each QTextDocument block as well as total history:
                    # a continuous no-newline stream otherwise becomes quadratic.
                    clean.append('\n')
                    self.column = 0
                clean.append(ch)
                self.column = 0 if ch == '\n' else self.column + 1
        scroll = self.output.verticalScrollBar()
        at_bottom, old = scroll.value() >= scroll.maximum() - 2, scroll.value()
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(''.join(clean))
        excess = self.output.document().characterCount() - 1 - self.MAX_CHARS
        if excess > 0:
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor, excess)
            cursor.removeSelectedText()
        scroll.setValue(scroll.maximum() if at_bottom else old)
