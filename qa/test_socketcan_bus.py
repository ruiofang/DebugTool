"""SocketCAN codec and transport checks; no hardware or privileges required.

Run: python3 -m unittest qa.test_socketcan_bus -v
"""

from collections import deque
from dataclasses import FrozenInstanceError
import errno
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch

import socketcan_bus as can


class FrameTests(unittest.TestCase):
    def test_standard_and_extended_have_exact_linux_abi(self):
        for extended, can_id in ((False, 0x7FF), (True, 0x1FFFFFFF)):
            with self.subTest(extended=extended):
                frame = can.CanFrame(can_id, b"\x11\x22", extended=extended)
                expected_id = can_id | (can.CAN_EFF_FLAG if extended else 0)
                expected = struct.pack("=IBBBB8s", expected_id, 2, 0, 0, 0,
                                       b"\x11\x22")
                self.assertEqual(can.encode_frame(frame), expected)
                decoded = can.decode_frame(expected, timestamp=123.5)
                self.assertEqual(decoded.can_id, can_id)
                self.assertEqual(decoded.data, frame.data)
                self.assertEqual(decoded.extended, extended)
                self.assertEqual(decoded.dlc, 2)
                self.assertEqual(decoded.timestamp, 123.5)
                self.assertFalse(decoded.fd)

    def test_frame_is_immutable_and_only_accepts_immutable_payload(self):
        frame = can.CanFrame(0x123, b"\x11")
        with self.assertRaises(FrozenInstanceError):
            frame.data = b"\x22"
        with self.assertRaises(TypeError):
            can.CanFrame(0x123, bytearray(b"\x11"))

    def test_rtr_carries_requested_length_and_no_data(self):
        frame = can.CanFrame(0x123, remote=True, dlc=8)
        encoded = can.encode_frame(frame)
        self.assertEqual(encoded, struct.pack("=IBBBB8s",
                                             can.CAN_RTR_FLAG | 0x123,
                                             8, 0, 0, 0, b""))
        decoded = can.decode_frame(encoded)
        self.assertEqual(decoded.data, b"")
        self.assertEqual(decoded.dlc, 8)
        self.assertTrue(decoded.remote)

    def test_all_canonical_fd_lengths_round_trip_without_changing_payload(self):
        for length in sorted(can.FD_LENGTHS):
            with self.subTest(length=length):
                data = bytes(range(length))
                frame = can.CanFrame(0x123, data, fd=True, bitrate_switch=True)
                encoded = can.encode_frame(frame)
                self.assertEqual(len(encoded), 72)
                self.assertEqual(encoded[4:8], bytes((length, 1, 0, 0)))
                decoded = can.decode_frame(encoded)
                self.assertEqual(decoded.data, data)
                self.assertEqual(decoded.dlc, length)
                self.assertTrue(decoded.fd)
                self.assertTrue(decoded.bitrate_switch)

    def test_fd_rejects_noncanonical_lengths_instead_of_padding(self):
        for length in (9, 10, 11, 13, 31, 63, 65):
            with self.subTest(length=length):
                with self.assertRaises(ValueError):
                    can.CanFrame(0x123, bytes(length), fd=True)
                packet = struct.pack("=IBBBB64s", 0x123, length, 0, 0, 0, bytes(64))
                with self.assertRaises(ValueError):
                    can.decode_frame(packet)

    def test_frame_validation_rejects_invalid_ids_and_flag_combinations(self):
        invalid = [
            {"can_id": -1}, {"can_id": 0x800}, {"can_id": True},
            {"can_id": 0x20000000, "extended": True},
            {"data": bytes(9)}, {"data": b"\x11", "dlc": 2},
            {"data": b"\x11", "remote": True},
            {"remote": True, "dlc": -1}, {"remote": True, "dlc": 9},
            {"remote": True, "dlc": 1.5}, {"remote": True, "fd": True},
            {"bitrate_switch": True}, {"error_state_indicator": True},
            {"error": True, "remote": True}, {"error": True, "fd": True},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                can.CanFrame(**dict({"can_id": 0x123}, **fields))

    def test_rx_error_class_uses_all_29_bits_and_cannot_be_transmitted(self):
        packet = struct.pack("=IBBBB8s", can.CAN_ERR_FLAG | 0x10000,
                             8, 0, 0, 0, bytes(range(8)))
        frame = can.decode_frame(packet)
        self.assertEqual(frame.can_id, 0x10000)
        self.assertTrue(frame.error)
        self.assertFalse(frame.extended)
        with self.assertRaisesRegex(ValueError, "错误帧"):
            can.encode_frame(frame)

    def test_rx_esi_and_fdf_are_understood_but_esi_is_not_user_transmitted(self):
        packet = struct.pack("=IBBBB64s", 0x123, 12,
                             can.CANFD_BRS | can.CANFD_ESI | can.CANFD_FDF,
                             0, 0, bytes(range(12)))
        frame = can.decode_frame(packet)
        self.assertTrue(frame.error_state_indicator)
        with self.assertRaisesRegex(ValueError, "ESI"):
            can.encode_frame(frame)

    def test_decode_rejects_bad_size_length_identifier_and_flags(self):
        packets = [bytes(15), bytes(17), bytes(73),
                   struct.pack("=IBBBB8s", 0x123, 9, 0, 0, 0, bytes(8)),
                   struct.pack("=IBBBB8s", 0x800, 0, 0, 0, 0, bytes(8)),
                   struct.pack("=IBBBB64s", 0x123, 8, 0x80, 0, 0, bytes(8)),
                   struct.pack("=IBBBB64s", 0x123 | can.CAN_RTR_FLAG,
                               8, 0, 0, 0, bytes(8))]
        for packet in packets:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                can.decode_frame(packet)


class ParserTests(unittest.TestCase):
    def test_hex_payload_formats(self):
        for text in ("AAbB00", "0xAABB00", "AA BB 00", "0xAA 0XbB 00",
                     " AA\nBB\t00 "):
            with self.subTest(text=text):
                self.assertEqual(can.parse_hex_payload(text), b"\xaa\xbb\x00")
        self.assertEqual(can.parse_hex_payload(" \t"), b"")

    def test_hex_payload_rejects_odd_partial_and_ambiguous_tokens(self):
        for text in ("A", "ABC", "0x", "AA B", "AA,BB", "AA_BB",
                     "GG", "-1", "AABB CC", "AA 0xB", "0x0xAA"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                can.parse_hex_payload(text)

    def test_can_ids_default_to_hex_and_enforce_selected_format(self):
        self.assertEqual(can.parse_can_id("123"), 0x123)
        self.assertEqual(can.parse_can_id(" 0X7ff "), 0x7FF)
        self.assertEqual(can.parse_can_id("1fffffff", extended=True), 0x1FFFFFFF)
        for text in ("", "-1", "+1", "12 3", "0x", "800", "123_456"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                can.parse_can_id(text)
        with self.assertRaises(ValueError):
            can.parse_can_id("20000000", extended=True)


class FakeSocket:
    def __init__(self):
        self.options = []
        self.incoming = deque()
        self.closed = 0
        self.sent = []
        self.send_result = None
        self.send_error = None
        self.bind_error = None
        self.unsupported_options = set()

    def setblocking(self, enabled):
        self.blocking = enabled

    def setsockopt(self, level, option, value):
        if option in self.unsupported_options:
            raise OSError(errno.ENOPROTOOPT, "not available")
        self.options.append((level, option, value))

    def bind(self, address):
        if self.bind_error:
            raise self.bind_error
        self.bound = address

    def close(self):
        self.closed += 1

    def fileno(self):
        return 42

    def send(self, packet):
        if self.send_error:
            raise self.send_error
        self.sent.append(packet)
        return len(packet) if self.send_result is None else self.send_result

    def recvmsg(self, size, ancillary_size):
        if not self.incoming:
            raise BlockingIOError(errno.EAGAIN, "empty")
        result = self.incoming.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def recv(self, size):
        if not self.incoming:
            raise BlockingIOError(errno.EAGAIN, "empty")
        return self.incoming.popleft()


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSocket()
        factory = patch.object(can.socket, "socket", return_value=self.fake)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        platform = patch.object(can.sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        family = patch.object(can.socket, "AF_CAN", 29, create=True)
        family.start()
        self.addCleanup(family.stop)
        details = patch.object(can, "interface_details", return_value={
            "name": "vcan0", "state": "unknown", "up": True,
            "type": 280, "mtu": 72,
        })
        self.details = details.start()
        self.addCleanup(details.stop)

    def bus(self, **kwargs):
        bus = can.SocketCanBus("vcan0", **kwargs)
        self.addCleanup(bus.close)
        return bus

    def test_open_configures_nonblocking_bounded_socket_and_default_filters(self):
        bus = self.bus()
        self.assertFalse(self.fake.blocking)
        self.assertEqual(self.fake.bound, ("vcan0",))
        self.assertTrue(bus.is_open)
        self.assertEqual(bus.fileno(), 42)
        self.assertIn((socket.SOL_SOCKET, socket.SO_RCVBUF,
                       can.SocketCanBus.RECEIVE_BUFFER_BYTES), self.fake.options)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_FILTER,
                       struct.pack("=II", 0, 0)), self.fake.options)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_ERR_FILTER,
                       struct.pack("=I", can.CAN_ERR_MASK)), self.fake.options)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_RECV_OWN_MSGS, 0),
                      self.fake.options)
        self.assertNotIn((can._SOL_CAN_RAW, can._CAN_RAW_FD_FRAMES, 1),
                         self.fake.options)

    def test_extended_and_standard_filters_include_format_mask(self):
        for extended in (False, True, None):
            with self.subTest(extended=extended):
                self.fake.options.clear()
                bus = self.bus(filter_id=0x123, filter_mask=0x7FF,
                               filter_extended=extended)
                can_id = 0x123 | (can.CAN_EFF_FLAG if extended else 0)
                mask = 0x7FF | (can.CAN_EFF_FLAG if extended is not None else 0)
                self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_FILTER,
                               struct.pack("=II", can_id, mask)), self.fake.options)
                bus.close()

    def test_fd_own_echo_and_error_frames_options(self):
        self.bus(fd=True, receive_own=True, error_frames=False)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_FD_FRAMES, 1), self.fake.options)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_RECV_OWN_MSGS, 1), self.fake.options)
        self.assertIn((can._SOL_CAN_RAW, can._CAN_RAW_ERR_FILTER,
                       struct.pack("=I", 0)), self.fake.options)

    def test_receive_reports_kernel_drops_and_timestamp(self):
        bus = self.bus()
        frame = can.encode_frame(can.CanFrame(0x123, b"\x11"))
        for drops in (7, 7, 11):
            self.fake.incoming.append((frame, [
                (socket.SOL_SOCKET, can._SO_RXQ_OVFL, struct.pack("=I", drops)),
                (socket.SOL_SOCKET, can._SO_TIMESTAMPNS, can._TIMESPEC.pack(123, 500000000)),
            ], 0, ("vcan0",)))
            received = bus.receive()
            self.assertEqual(received.timestamp, 123.5)
            self.assertEqual(bus.rx_dropped, drops)
        self.assertIsNone(bus.receive())

    def test_drop_counter_handles_uint32_wrap(self):
        bus = self.bus()
        bus._last_kernel_drops = 0xFFFFFFFE
        bus.rx_dropped = 0xFFFFFFFE
        self.fake.incoming.append((can.encode_frame(can.CanFrame(0x123)), [
            (socket.SOL_SOCKET, can._SO_RXQ_OVFL, struct.pack("=I", 2)),
        ], 0, ("vcan0",)))
        bus.receive()
        self.assertEqual(bus.rx_dropped, 0x100000002)

    def test_optional_ancillary_options_can_be_unavailable(self):
        self.fake.unsupported_options.update((can._SO_RXQ_OVFL, can._SO_TIMESTAMPNS))
        bus = self.bus()
        self.fake.incoming.append((can.encode_frame(can.CanFrame(0x123)), [], 0, ("vcan0",)))
        with patch.object(can.time, "time", return_value=456.0):
            self.assertEqual(bus.receive().timestamp, 456.0)

    def test_receive_falls_back_to_recv_when_recvmsg_is_unavailable(self):
        self.fake.recvmsg = None
        bus = self.bus()
        self.assertIsNone(bus.receive())
        self.fake.incoming.append(can.encode_frame(can.CanFrame(0x123, b"\x22")))
        self.assertEqual(bus.receive().data, b"\x22")

    def test_receive_propagates_real_device_failure_and_rejects_truncation(self):
        bus = self.bus()
        self.fake.incoming.append(OSError(errno.ENETDOWN, "link down"))
        with self.assertRaises(OSError) as error:
            bus.receive()
        self.assertEqual(error.exception.errno, errno.ENETDOWN)
        self.fake.incoming.append((bytes(72), [], socket.MSG_TRUNC, ("vcan0",)))
        with self.assertRaisesRegex(ValueError, "截断"):
            bus.receive()

    def test_send_records_exact_packet_and_rejects_fd_on_classic_socket(self):
        bus = self.bus()
        frame = can.CanFrame(0x123, b"\x11\x22")
        self.assertIsNone(bus.send(frame))
        self.assertEqual(self.fake.sent, [can.encode_frame(frame)])
        with self.assertRaisesRegex(ValueError, "未启用 CAN FD"):
            bus.send(can.CanFrame(0x123, bytes(12), fd=True))

    def test_short_send_and_full_kernel_queue_are_not_reported_as_success(self):
        bus = self.bus()
        frame = can.CanFrame(0x123)
        self.fake.send_result = 1
        with self.assertRaises(OSError) as error:
            bus.send(frame)
        self.assertEqual(error.exception.errno, errno.EIO)
        self.fake.send_error = BlockingIOError(errno.EAGAIN, "queue full")
        with self.assertRaises(BlockingIOError):
            bus.send(frame)

    def test_close_is_idempotent_and_closed_operations_fail(self):
        bus = self.bus()
        bus.open()
        self.factory.assert_called_once()
        bus.close()
        bus.close()
        self.assertEqual(self.fake.closed, 1)
        self.assertFalse(bus.is_open)
        self.assertEqual(bus.fileno(), -1)
        for action in (bus.receive, lambda: bus.send(can.CanFrame(0x123))):
            with self.assertRaises(OSError) as error:
                action()
            self.assertEqual(error.exception.errno, errno.EBADF)

    def test_failed_bind_closes_socket(self):
        self.fake.bind_error = OSError(errno.ENODEV, "missing interface")
        with self.assertRaises(OSError):
            self.bus()
        self.assertEqual(self.fake.closed, 1)

    def test_unsupported_platform_fails_cleanly_before_socket_creation(self):
        with patch.object(can.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "Linux"):
                self.bus()
        self.factory.assert_not_called()

    def test_down_non_can_and_classic_only_interfaces_are_rejected(self):
        for changes, kwargs in (({"up": False}, {}), ({"type": 1}, {}),
                                ({"mtu": 16}, {"fd": True})):
            with self.subTest(changes=changes):
                self.details.return_value.update({"up": True, "type": 280, "mtu": 72})
                self.details.return_value.update(changes)
                with self.assertRaises((ValueError, OSError)):
                    self.bus(**kwargs)
        self.factory.assert_not_called()

    def test_bad_interface_names_and_filters_are_rejected(self):
        for name in ("", "../can0", "can 0", "can\x000", "c" * 16):
            with self.subTest(name=name), self.assertRaises(ValueError):
                can.SocketCanBus(name)
        for kwargs in ({"filter_mask": -1}, {"filter_id": 0x20000000},
                       {"filter_extended": "yes"},
                       {"filter_id": 0x800, "filter_extended": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.bus(**kwargs)
        self.factory.assert_not_called()


class InterfaceTests(unittest.TestCase):
    def test_enumeration_and_details_distinguish_vcan_unknown_from_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, kind, flags, state, mtu in (
                ("can0", 280, "0x0", "down", 16),
                ("vcan0", 280, "0x41", "unknown", 72),
                ("eth0", 1, "0x1", "up", 1500),
            ):
                directory = root / name
                directory.mkdir()
                for filename, value in (("type", kind), ("flags", flags),
                                        ("operstate", state), ("mtu", mtu)):
                    (directory / filename).write_text(str(value) + "\n", encoding="ascii")
            (root / "missing").mkdir()
            with patch.object(can, "_SYS_CLASS_NET", root):
                self.assertEqual(can.list_can_interfaces(), ["can0", "vcan0"])
                self.assertEqual(can.interface_details("vcan0"), {
                    "name": "vcan0", "state": "unknown", "up": True,
                    "mtu": 72, "type": 280,
                })
                self.assertFalse(can.interface_details("can0")["up"])
                self.assertIsNone(can.interface_details("missing")["up"])
                self.assertIsNone(can.interface_details("absent")["mtu"])

    def test_missing_sysfs_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(can, "_SYS_CLASS_NET", Path(temporary) / "absent"):
                self.assertEqual(can.list_can_interfaces(), [])


if __name__ == "__main__":
    unittest.main()
