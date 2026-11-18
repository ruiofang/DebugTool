"""Chuang Xin CANFD UI routing with fake drivers; never accesses USB or CAN."""

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

import can_panel
from test_windows_can_panel import FakeDriver


class ControlCanFdPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = QSettings(str(directory / 'can.ini'), QSettings.IniFormat)
        self.stack.enter_context(patch('can_panel.sys.platform', 'linux'))
        self.stack.enter_context(patch('can_panel.list_can_interfaces', return_value=['vcan_qa']))
        self.stack.enter_context(patch('can_panel.interface_details', return_value={'mtu': 16}))
        self.discover = self.stack.enter_context(patch(
            'can_panel.discover_channels', return_value=['0:0', '0:1']))
        self.linux_config = self.stack.enter_context(patch('can_panel.CanConfigurator'))
        self.linux_bus = self.stack.enter_context(patch('can_panel.SocketCanBus'))
        self.factory = self.stack.enter_context(patch('can_panel.DriverCanBus', side_effect=FakeDriver))
        self.panel = can_panel.CanPanel(self.settings)
        self.addCleanup(lambda: self.close_panel(self.panel))

    def close_panel(self, panel):
        panel.stop()
        if panel.bus is not None:
            panel.bus.closed = True
            panel.drain()
        panel.config_dialog.close()
        panel.close()
        panel.deleteLater()
        self.app.processEvents()

    def select(self, name='controlcanfd'):
        self.panel.backend.setCurrentIndex(self.panel.backend.findData(name))
        self.finish_discovery(self.panel)

    def finish_discovery(self, panel):
        deadline = time.monotonic() + 2
        while panel._discovery_jobs:
            panel._poll_discovery()
            if time.monotonic() >= deadline:
                self.fail('fake discovery did not finish')
            time.sleep(0.001)

    def test_linux_option_refresh_provides_candidates_without_opening(self):
        index = self.panel.backend.findData('controlcanfd')
        self.assertEqual(self.panel.backend.itemText(index), '创芯科技 CANFD（Linux）')
        self.assertTrue(self.panel.backend.model().item(index).isEnabled())
        self.assertFalse(self.panel.active)
        self.factory.assert_not_called()
        self.select()
        self.discover.assert_called_once_with('controlcanfd')
        self.assertEqual(self.panel.interface.currentText(), '0:0')
        self.assertEqual([self.panel.interface.itemText(i) for i in range(self.panel.interface.count())],
                         ['0:0', '0:1', '0:0+1'])
        self.assertIn('候选通道', self.panel.status.text())
        self.assertIn('SDK 设备索引', self.panel.status.text())
        self.factory.assert_not_called()
        self.linux_config.assert_not_called()
        self.linux_bus.assert_not_called()

    def test_fd_connection_send_confirmation_and_close_use_vendor_driver(self):
        self.select()
        self.panel.interface.setCurrentText('0:1')
        self.panel.enable_fd.setChecked(True)
        self.panel.bitrate.setCurrentText('250000')
        self.panel.data_bitrate.setCurrentText('4000000')
        self.panel.rx_format.setCurrentIndex(2)
        self.panel.rx_id.setText('12345')
        self.panel.rx_mask.setText('1FFFFFFF')
        self.panel.toggle_connection()
        bus = self.panel.bus
        self.assertEqual((bus.backend, bus.interface, bus.bitrate), ('controlcanfd', '0:1', 250000))
        self.assertEqual(bus.options['data_bitrate'], 4000000)
        self.assertEqual(bus.options['filter_id'], 0x12345)
        self.assertTrue(bus.options['filter_extended'])
        self.assertTrue(bus.options['fd'])
        self.assertTrue(self.panel.connecting)
        self.assertFalse(self.panel.active)
        bus.state = 'open'
        self.panel.drain()
        self.assertTrue(self.panel.active)
        self.panel.frame_type.setCurrentIndex(2)
        self.panel.brs.setChecked(True)
        self.assertTrue(self.panel.send_text(' '.join(['AA'] * 12)))
        self.assertTrue(bus.sent[0].fd)
        self.assertTrue(bus.sent[0].bitrate_switch)
        self.assertEqual(self.panel.tx_frames, 0)
        bus.confirmed.append(bus.sent[0])
        self.panel.drain()
        self.assertEqual((self.panel.tx_frames, self.panel.tx_bytes), (1, 12))
        self.panel.toggle_connection()
        self.assertTrue(self.panel.closing)
        self.assertTrue(bus.close_requested)
        bus.closed = True
        self.panel.drain()
        self.assertIn('驱动通道已关闭', self.panel.status.text())
        self.assertIsNone(self.panel.bus)
        self.linux_config.assert_not_called()
        self.linux_bus.assert_not_called()

    def test_saved_vendor_and_other_driver_channels_remain_independent(self):
        self.select('pcan')
        self.panel.interface.setCurrentText('PCAN_USBBUS3')
        self.select()
        self.panel.interface.setCurrentText('1:1')
        self.panel.save_settings()
        self.select('gs_usb')
        self.panel.interface.setCurrentText('2')
        self.select()
        self.assertEqual(self.panel.interface.currentText(), '1:1')
        self.panel.save_settings()
        restored = can_panel.CanPanel(self.settings)
        try:
            self.finish_discovery(restored)
            self.assertEqual(restored.backend_name(), 'controlcanfd')
            self.assertEqual(restored.interface.currentText(), '1:1')
            restored.backend.setCurrentIndex(restored.backend.findData('pcan'))
            self.finish_discovery(restored)
            self.assertEqual(restored.interface.currentText(), 'PCAN_USBBUS3')
            restored.backend.setCurrentIndex(restored.backend.findData('gs_usb'))
            self.finish_discovery(restored)
            self.assertEqual(restored.interface.currentText(), '2')
        finally:
            self.close_panel(restored)

    def test_dual_session_opens_once_and_switches_tx_without_reconnecting(self):
        self.select()
        panel = self.panel
        panel.interface.setCurrentText('0:0+1')
        self.assertFalse(panel.tx_channel.isHidden())
        panel.enable_fd.setChecked(False)
        panel.can1_settings.setChecked(True)
        panel.can1_bitrate.setCurrentText('250000')
        panel.can1_data_bitrate.setCurrentText('4000000')
        panel.can1_fd.setChecked(True)
        panel.toggle_connection()
        bus = panel.bus
        self.assertEqual(bus.interface, '0:0+1')
        self.assertEqual(bus.options['can1_config'], (250000, 4000000, True))
        self.assertFalse(bus.options['fd'])
        bus.state = 'open'
        panel.drain()
        self.assertIn('CAN0 + CAN1', panel.status.text())
        self.assertTrue(panel.send_text('AA'))
        stopped = []
        panel.stop_sending.connect(lambda: stopped.append(True))
        panel.tx_channel.setCurrentIndex(1)
        self.assertEqual(stopped, [True])
        panel.frame_type.setCurrentIndex(2)
        self.assertTrue(panel.send_text(' '.join(['BB'] * 12)))
        self.assertEqual([(frame.channel, frame.fd) for frame in bus.sent],
                         [('0:0', False), ('0:1', True)])
        self.assertEqual([frame.data for frame in bus.sent], [b'\xAA' + bytes(7), b'\xBB' * 12])
        self.factory.assert_called_once()

    def test_sdk_rate_choices_help_and_other_driver_rates_restore(self):
        self.select()
        self.assertEqual([self.panel.bitrate.itemText(i) for i in range(self.panel.bitrate.count())],
                         ['50000', '100000', '125000', '250000', '500000', '800000', '1000000'])
        self.assertNotIn('8000000', [self.panel.data_bitrate.itemText(i)
                                   for i in range(self.panel.data_bitrate.count())])
        self.assertIn('5000000', [self.panel.data_bitrate.itemText(i)
                                for i in range(self.panel.data_bitrate.count())])
        self.assertIn('不支持 8M', self.panel.data_bitrate.toolTip())
        self.panel.update_config_commands()
        help_text = self.panel.config_commands.toPlainText()
        for term in ('libcontrolcanfd.so', 'USB', 'ISO', '外部 120Ω', '现有终端电阻', '0:1'):
            self.assertIn(term, help_text)
        self.select('pcan')
        self.assertIn('8000000', [self.panel.data_bitrate.itemText(i)
                                for i in range(self.panel.data_bitrate.count())])
        self.assertIn('10000', [self.panel.bitrate.itemText(i)
                               for i in range(self.panel.bitrate.count())])

    def test_windows_saved_vendor_backend_falls_back_and_cannot_be_forced_open(self):
        self.settings.setValue('can/backend', 'controlcanfd')
        with patch('can_panel.sys.platform', 'win32'):
            panel = can_panel.CanPanel(self.settings)
            try:
                self.finish_discovery(panel)
                self.assertEqual(panel.backend_name(), 'pcan')
                index = panel.backend.findData('controlcanfd')
                self.assertFalse(panel.backend.model().item(index).isEnabled())
                self.discover.reset_mock()
                panel.backend.setCurrentIndex(index)
                panel.toggle_connection()
                self.assertIn('创芯科技 CANFD 仅支持 Linux', panel.status.text())
                self.discover.assert_not_called()
                self.factory.assert_not_called()
            finally:
                self.close_panel(panel)

    def test_sdk_open_error_is_reported_without_activating_or_configuring_socketcan(self):
        self.select()
        self.panel.toggle_connection()
        bus = self.panel.bus
        bus.error = '创芯 CANFD：USB 访问失败，请检查设备权限'
        bus.closed = True
        self.panel.drain()
        self.assertFalse(self.panel.active)
        self.assertFalse(self.panel.connecting)
        self.assertIn('CAN 打开失败', self.panel.status.text())
        self.assertIn('设备权限', self.panel.status.text())
        self.assertIsNone(self.panel.bus)
        self.linux_config.assert_not_called()
        self.linux_bus.assert_not_called()


if __name__ == '__main__':
    unittest.main()
