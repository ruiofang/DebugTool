"""Serial lock/retry and actionable error regressions using temporary PTYs."""

from contextlib import ExitStack
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
import serial
from serial.tools.list_ports_common import ListPortInfo

import main
from serial_access import describe_serial_open_error


class SerialOpenErrorTests(unittest.TestCase):
    def test_busy_is_not_reported_as_permission_failure(self):
        guidance, details = describe_serial_open_error(
            "/dev/ttyACM2", serial.SerialException(
                errno.EAGAIN, "Could not exclusively lock port /dev/ttyACM2"))
        self.assertIn("占用", guidance)
        self.assertNotIn("没有", guidance)
        self.assertIn("/dev/ttyACM2", guidance)
        self.assertIn("原始错误", details)
        if sys.platform.startswith("linux"):
            self.assertIn("fuser -v -- /dev/ttyACM2", details)

    def test_permission_disconnect_and_invalid_parameter_have_distinct_advice(self):
        for code, expected in ((errno.EACCES, "没有串口"),
                               (errno.ENOENT, "已断开"),
                               (errno.EIO, "设备无法响应"),
                               (errno.EINVAL, "串口参数")):
            with self.subTest(code=code):
                guidance, _ = describe_serial_open_error(
                    "/dev/ttyUSB0", serial.SerialException(code, "failure"))
                self.assertIn(expected, guidance)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"),
                     "POSIX pseudo-terminals are required")
class SerialOpenWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.devices = []
        for _ in range(2):
            master, slave = os.openpty()
            self.stack.callback(os.close, master)
            self.stack.callback(os.close, slave)
            self.devices.append(os.ttyname(slave))
        ports = [ListPortInfo(device) for device in self.devices]
        self.stack.enter_context(patch("main.enumerate_serial_ports", return_value=ports))
        # PTYs are intentionally not offered by the hardware enumeration UI.
        self.stack.enter_context(patch("main.serial_port_usability",
                                       return_value=(True, "可用")))
        self.stack.enter_context(patch("main.config_path", return_value=str(
            directory / "serial_tool.ini")))
        self.stack.enter_context(patch.object(main.DebugTool, "_save_settings"))
        self.errors = self.stack.enter_context(patch.object(
            main.DebugTool, "_show_serial_open_error"))
        self.window = main.DebugTool()
        self.addCleanup(self.close_window)
        self.window.cmb_port.setCurrentIndex(1)

    def close_window(self):
        self.window.close_port()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_busy_port_stays_selected_and_reopens_after_owner_closes(self):
        selected = self.devices[1]
        holder = main.open_serial_connection(port=selected, timeout=0)
        self.addCleanup(holder.close)
        self.window.btn_open.click()
        self.errors.assert_called_once()
        attempted, error = self.errors.call_args.args
        self.assertEqual(attempted, selected)
        self.assertEqual(error.errno, errno.EAGAIN)
        self.assertIsNone(self.window.ser)
        self.assertFalse(self.window.btn_open.isChecked())
        self.assertEqual(self.window._current_port_device(), selected)
        # Explicit refresh must not blacklist a device after a transient lock.
        self.window.refresh_ports()
        self.assertEqual(self.window.cmb_port.count(), 2)
        self.assertEqual(self.window._current_port_device(), selected)
        self.assertTrue(self.window.cmb_port.currentData(Qt.UserRole + 1))
        holder.close()
        self.window.btn_open.click()
        self.assertTrue(self.window.ser.is_open)
        self.assertEqual(self.window.ser.port, selected)
        self.assertEqual(self.errors.call_count, 1)

    def test_duplicate_open_preserves_live_reader_and_close_releases_lock(self):
        self.window.btn_open.click()
        connection, reader = self.window.ser, self.window.reader
        self.window.open_port()
        self.assertIs(self.window.ser, connection)
        self.assertIs(self.window.reader, reader)
        self.assertTrue(self.window.btn_open.isChecked())
        self.errors.assert_not_called()
        with self.assertRaises(serial.SerialException):
            main.open_serial_connection(port=self.devices[1], timeout=0)
        self.window.close_port()
        self.assertFalse(connection.is_open)
        self.assertFalse(reader.isRunning())
        with main.open_serial_connection(port=self.devices[1], timeout=0) as reopened:
            self.assertTrue(reopened.is_open)

    def test_other_device_can_open_while_first_device_is_busy(self):
        with main.open_serial_connection(port=self.devices[0], timeout=0):
            self.window.btn_open.click()
            self.assertTrue(self.window.ser.is_open)
            self.assertEqual(self.window.ser.port, self.devices[1])
            self.errors.assert_not_called()


if __name__ == "__main__":
    unittest.main()
