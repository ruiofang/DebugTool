"""Read-only USB candidate discovery against a temporary sysfs inventory."""

from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import controlcanfd_bus as native


class ControlCanFdDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(native, 'USB_SYSFS_ROOT', self.root))
        self.usb_nodes = self.root / 'nodes'
        self.stack.enter_context(patch.object(native, 'USB_DEVICE_ROOT', self.usb_nodes))
        self.stack.enter_context(patch.object(sys, 'platform', 'linux'))
        self.loader = self.stack.enter_context(patch.object(native, '_load_library',
            side_effect=AssertionError('Refreshing candidates must never load/open an adapter')))

    def device(self, name, vendor='04d8', product='0053'):
        directory = self.root / name
        directory.mkdir()
        (directory / 'idVendor').write_text(vendor + '\n', encoding='ascii')
        (directory / 'idProduct').write_text(product + '\n', encoding='ascii')
        return directory

    def test_open_diagnostic_distinguishes_missing_nodes_and_denied_access(self):
        adapter = self.device('2-5')
        (adapter / 'busnum').write_text('2\n')
        (adapter / 'devnum').write_text('5\n')
        node = self.usb_nodes / '002/005'
        self.assertIn('未提供 USB 设备节点', native._usb_open_error())
        node.parent.mkdir(parents=True)
        node.touch()
        with patch.object(native.os, 'access', return_value=False) as access:
            self.assertIn('--canfd-permissions', native._usb_open_error())
            access.assert_called_once_with(node, native.os.R_OK | native.os.W_OK)
        with patch.object(native.os, 'access', return_value=True):
            self.assertIn('设备索引', native._usb_open_error())

    def test_only_supported_vid_pid_contribute_index_candidates(self):
        self.device('2-5')
        self.device('1-3', vendor='04D8')
        self.device('1-4', product='0054')
        self.device('1-5', vendor='1d50')
        (self.root / '2-5:1.0').mkdir()  # USB interface, not a second device.
        self.assertEqual(native.discover_channels(), ['0:0', '0:1', '1:0', '1:1'])
        self.loader.assert_not_called()

    def test_empty_inventory_returns_no_candidates(self):
        self.assertEqual(native.discover_channels(), [])

    def test_candidates_respect_sdk_eight_device_limit(self):
        for index in range(10):
            self.device(f'1-{index}')
        channels = native.discover_channels()
        self.assertEqual(len(channels), 16)
        self.assertEqual(channels[-1], '7:1')

    def test_unreadable_inventory_is_reported(self):
        with patch.object(native, 'USB_SYSFS_ROOT', self.root / 'absent'), \
                self.assertRaisesRegex(RuntimeError, '无法读取 USB 设备列表'):
            native.discover_channels()
        self.device('1-1')
        with patch.object(Path, 'read_text', side_effect=PermissionError('permission denied')), \
                self.assertRaisesRegex(RuntimeError, 'permission denied'):
            native.discover_channels()

    def test_non_linux_does_not_enumerate_or_load_sdk(self):
        with patch.object(sys, 'platform', 'win32'), \
                self.assertRaisesRegex(RuntimeError, '仅支持 Linux'):
            native.discover_channels()
        self.loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
