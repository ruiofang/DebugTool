"""FocalTech I2C polling fallback for boards with a broken touch IRQ line."""

from __future__ import annotations

import fcntl
import os

from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, QTimer, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication


I2C_SLAVE_FORCE = 0x0706


class FocalTechTouchPoller(QObject):
    """Poll the first FocalTech contact and feed it to the Qt widget tree.

    The T113 board's FocalTech controller is healthy on I2C, but its PD17
    interrupt input is stuck low.  Reading registers 0x01..0x08 bypasses only
    that interrupt path; normal Qt event delivery remains unchanged.
    """

    def __init__(self, window, device="/dev/i2c-1", address=0x38,
                 raw_width=720, raw_height=1280, interval_ms=16):
        super().__init__(window)
        self._window = window
        self._fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        fcntl.ioctl(self._fd, I2C_SLAVE_FORCE, address)
        self._target = None
        self._pressed = False
        self._last_global_pos = None
        self._failures = 0
        self._raw_width = max(1, int(raw_width))
        self._raw_height = max(1, int(raw_height))
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._release()
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def _read_frame(self):
        os.write(self._fd, b"\x01")
        data = os.read(self._fd, 8)
        if len(data) != 8:
            raise OSError("short FocalTech I2C read")
        return data

    def _global_position(self, raw_x, raw_y):
        screen = QApplication.primaryScreen().geometry()
        x = screen.left() + round(raw_x * max(0, screen.width() - 1) /
                                  self._raw_width)
        y = screen.top() + round(raw_y * max(0, screen.height() - 1) /
                                 self._raw_height)
        return QPoint(x, y)

    @staticmethod
    def _send(target, event_type, global_pos, button, buttons):
        local = target.mapFromGlobal(global_pos)
        event = QMouseEvent(event_type, QPointF(local), QPointF(global_pos),
                            button, buttons, Qt.NoModifier)
        QApplication.sendEvent(target, event)

    def _press_or_move(self, global_pos):
        self._last_global_pos = global_pos
        if not self._pressed:
            self._target = QApplication.widgetAt(global_pos) or self._window
            self._send(self._target, QEvent.MouseButtonPress, global_pos,
                       Qt.LeftButton, Qt.LeftButton)
            self._pressed = True
            return
        if self._target is not None:
            self._send(self._target, QEvent.MouseMove, global_pos,
                       Qt.NoButton, Qt.LeftButton)

    def _release(self, global_pos=None):
        if not self._pressed:
            return
        target = self._target or self._window
        if global_pos is None:
            global_pos = (self._last_global_pos or
                          target.mapToGlobal(target.rect().center()))
        self._send(target, QEvent.MouseButtonRelease, global_pos,
                   Qt.LeftButton, Qt.NoButton)
        self._pressed = False
        self._target = None
        self._last_global_pos = None

    def _poll(self):
        try:
            data = self._read_frame()
            self._failures = 0
        except OSError:
            self._failures += 1
            if self._failures >= 3:
                self._release()
            return

        point_count = data[1] & 0x0F
        event = (data[2] >> 6) & 0x03
        if point_count == 0 or event == 1:
            self._release()
            return

        raw_x = ((data[2] & 0x0F) << 8) | data[3]
        raw_y = ((data[4] & 0x0F) << 8) | data[5]
        if raw_x > self._raw_width or raw_y > self._raw_height:
            return
        self._press_or_move(self._global_position(raw_x, raw_y))

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
