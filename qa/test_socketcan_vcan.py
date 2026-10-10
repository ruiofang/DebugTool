"""Opt-in kernel CAN checks; run only on an isolated virtual CAN interface.

Set DEBUGTOOL_VCAN_INTERFACE=vcan_test inside a private network namespace.
Ordinary unittest discovery skips these tests and never sends CAN traffic.
"""
from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from socketcan_bus import CanFrame, SocketCanBus, interface_details, list_can_interfaces


INTERFACE = os.environ.get('DEBUGTOOL_VCAN_INTERFACE', '')


@unittest.skipUnless(INTERFACE, 'requires opt-in isolated vcan interface')
class VirtualCanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not INTERFACE.startswith('vcan'):
            raise RuntimeError('Kernel tests require a virtual vcan interface')
        target = (Path('/sys/class/net') / INTERFACE).resolve()
        if not str(target).startswith('/sys/devices/virtual/net/'):
            raise RuntimeError('Refusing to send on an interface outside virtual sysfs devices')
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def bus(self, **kwargs):
        bus = SocketCanBus(INTERFACE, **kwargs)
        self.stack.callback(bus.close)
        return bus

    def receive(self, bus, timeout=1):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            frame = bus.receive()
            if frame is not None:
                return frame
            self.app.processEvents()
            time.sleep(0.001)
        self.fail('Timed out waiting for a kernel SocketCAN frame')

    def wait_until(self, condition, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.001)
        self.fail('Timed out waiting for asynchronous CAN configuration')

    def set_interface_up(self, up):
        # setUpClass restricts this to an explicitly opted-in virtual device.
        subprocess.run(['/usr/sbin/ip', 'link', 'set', 'dev', INTERFACE,
                        'up' if up else 'down'], check=True, capture_output=True)

    def test_kernel_classic_extended_remote_and_fd(self):
        self.assertIn(INTERFACE, list_can_interfaces())
        tx, rx = self.bus(fd=True), self.bus(fd=True)
        frames = [CanFrame(0), CanFrame(0x7ff, bytes(range(8))),
                  CanFrame(0x1fffffff, b'\xaa\xbb', extended=True),
                  CanFrame(0x321, remote=True, dlc=8),
                  CanFrame(0x456, bytes(range(64)), fd=True, bitrate_switch=True)]
        for frame in frames:
            tx.send(frame)
            received = self.receive(rx)
            self.assertEqual(replace(received, timestamp=None, dlc=frame.dlc), frame)
            self.assertIsNotNone(received.timestamp)
            self.assertAlmostEqual(received.timestamp, time.time(), delta=2)
        self.assertIsNone(tx.receive(), 'Own-message reception must be off by default')

    def test_kernel_filter_and_own_message_option(self):
        rx = self.bus(filter_id=0x123, filter_mask=0x7ff, filter_extended=False)
        tx = self.bus(receive_own=True)
        for frame in (CanFrame(0x124, b'a'), CanFrame(0x123, b'b', extended=True),
                      CanFrame(0x123, b'c')):
            tx.send(frame)
            echoed = self.receive(tx)
            self.assertEqual(echoed.data, frame.data)
        selected = self.receive(rx)
        self.assertEqual(selected.data, b'c')
        self.assertFalse(selected.extended)
        self.assertIsNone(rx.receive())

    def test_full_window_receive_send_cycle_and_logging(self):
        import main
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch('main.config_path', return_value=str(directory / 'settings.ini')))
        self.stack.enter_context(patch.object(main.DebugTool, 'refresh_ports'))
        self.stack.enter_context(patch.object(main.DebugTool, '_save_settings'))
        window = main.DebugTool()
        self.stack.callback(window.close)
        window.settings.setValue('log_dir', str(directory))
        window.transport_mode.setCurrentIndex(window.CAN_MODE)
        window.can_panel.interface.setCurrentText(INTERFACE)
        self.assertNotIn('type can', window.can_panel.config_commands.toPlainText())
        window.chk_log_save.setChecked(True)
        window.show()
        self.app.processEvents()
        window.can_panel.toggle_connection()
        self.wait_until(lambda: window.can_panel.active)
        peer = self.bus(fd=True)
        expected = [CanFrame(0x200 + i, bytes([i, 0xa5])) for i in range(100)]
        for frame in expected:
            peer.send(frame)
        deadline = time.monotonic() + 3
        while window.can_panel.rx_frames < len(expected) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertEqual(window.can_panel.rx_frames, 100)
        self.assertEqual(window.can_panel.rx_bytes, 200)
        window.send_edit.setPlainText('AA 55')
        window.on_send_clicked()
        self.assertEqual(self.receive(peer).data, b'\xaa\x55')
        window.spn_interval.setValue(20)
        window.chk_auto_send.setChecked(True)
        until = time.monotonic() + 0.15
        while time.monotonic() < until:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertGreater(window.can_panel.tx_frames, 2)
        window.transport_mode.setCurrentIndex(0)
        self.assertFalse(window.can_panel.active)
        self.assertFalse(window.auto_send_timer.isActive())
        self.assertIsNone(window.log_file)
        log = next(directory.glob('*.log')).read_text(encoding='utf-8')
        self.assertEqual(log.count(' RX [CAN '), 100)
        self.assertIn('ID=0x200 STD DATA LEN=2 DATA=00 A5', log)
        self.assertIn(' TX [CAN ', log)
        self.assertLessEqual(window.recv_edit.document().characterCount() - 1,
                             window.MAX_DISPLAY_CHARS)

    def test_down_interface_opens_closes_down_and_reopens_from_gui(self):
        from PyQt5.QtCore import QSettings
        from can_panel import CanPanel

        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        panel = CanPanel(QSettings(str(directory / 'settings.ini'), QSettings.IniFormat))
        self.stack.callback(panel.stop)
        self.stack.callback(panel.close)
        panel.interface.setCurrentText(INTERFACE)
        self.set_interface_up(False)
        self.stack.callback(self.set_interface_up, True)
        self.assertFalse(interface_details(INTERFACE)['up'])

        ticks = []
        heartbeat = QTimer()
        heartbeat.setInterval(1)
        heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
        heartbeat.start()
        self.stack.callback(heartbeat.stop)
        panel.toggle_connection()
        self.wait_until(lambda: panel.active)
        self.assertTrue(interface_details(INTERFACE)['up'])
        self.assertTrue(ticks, 'GUI event loop should remain responsive while configuring')

        peer = self.bus()
        peer.send(CanFrame(0x123, b'\x01\x02'))
        self.wait_until(lambda: panel.rx_frames == 1)

        # The explicit Close button must stop the shared Linux interface too.
        closing_ticks = len(ticks)
        panel.toggle_connection()
        self.assertFalse(panel.active)
        self.wait_until(lambda: not panel.closing)
        self.assertFalse(interface_details(INTERFACE)['up'], panel.status.text())
        self.assertIsNone(panel.bus)
        self.assertGreater(len(ticks), closing_ticks,
                           'GUI event loop should remain responsive while stopping')

        panel.toggle_connection()
        self.wait_until(lambda: panel.active)
        self.assertTrue(interface_details(INTERFACE)['up'])
        # Rebind the independent endpoint too: a socket kept across link-down
        # can retain the kernel's ENETDOWN error even after the link returns UP.
        peer.close()
        peer = self.bus()
        peer.send(CanFrame(0x124, b'\x03\x04'))
        self.wait_until(lambda: panel.rx_frames == 2)
        self.assertTrue(panel.send_text('AA 55'))
        self.assertEqual(self.receive(peer).data, b'\xaa\x55')

        # Internal teardown is still distinct from explicitly stopping the bus.
        panel.stop()
        self.assertFalse(panel.active)
        self.assertTrue(interface_details(INTERFACE)['up'],
                        'Local teardown must not disable the shared CAN interface')


if __name__ == '__main__':
    unittest.main()
