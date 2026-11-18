"""Windows CAN contracts using mocked drivers and real python-can codecs.

No test opens a USB adapter or CAN controller. Optional dependencies are needed
for these tests; the Linux SocketCAN application itself does not require them.
"""

from collections import deque
from contextlib import contextmanager
import importlib.util
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import driver_can_bus as transport
from socketcan_bus import CanFrame

try:
    import can
except ImportError:
    can = None


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("worker did not reach expected state")
        time.sleep(0.002)


class FakeDriver:
    def __init__(self):
        self.rx = deque()
        self.sent = []
        self.calls = []
        self.receive_timeouts = []
        self.recv_gate = None
        self.send_gate = None
        self.close_gate = None
        self.recv_error = None
        self.send_error = None
        self.close_error = None
        self.status_value = 0

    def _record(self, name):
        self.calls.append((name, threading.get_ident()))

    def status(self):
        self._record("status")
        return self.status_value

    def recv(self, timeout):
        self._record("recv")
        self.receive_timeouts.append(timeout)
        assert timeout > 0
        if self.recv_gate:
            self.recv_gate.wait(2)
        if self.rx:
            return self.rx.popleft()
        if self.recv_error:
            raise self.recv_error
        time.sleep(min(timeout, 0.002))
        return None

    def send(self, msg, timeout):
        self._record("send")
        if self.send_gate:
            self.send_gate.wait(2)
        if self.send_error:
            raise self.send_error
        self.sent.append(msg)

    def shutdown(self):
        self._record("shutdown")
        if self.close_gate:
            self.close_gate.wait(2)
        if self.close_error:
            raise self.close_error


class ObservedEvent:
    """Expose idle waits while retaining real event wakeup behavior."""

    def __init__(self):
        self.event = threading.Event()
        self.wait_timeouts = []

    def set(self):
        self.event.set()

    def clear(self):
        self.event.clear()

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        return self.event.wait(timeout)


@unittest.skipIf(can is None, "optional python-can dependency is not installed")
class DriverWorkerTests(unittest.TestCase):
    @contextmanager
    def opened(self, driver=None, **kwargs):
        driver = driver or FakeDriver()
        def opener(bus, api):
            driver._record("open")
            return driver
        with patch.object(transport.DriverCanBus, "_open", opener):
            bus = transport.DriverCanBus(kwargs.pop("backend", "pcan"),
                                         kwargs.pop("channel", "PCAN_USBBUS1"),
                                         kwargs.pop("bitrate", 500000), **kwargs)
            try:
                wait_for(lambda: bus.state != "opening")
                yield bus, driver
            finally:
                for gate in (driver.recv_gate, driver.send_gate, driver.close_gate):
                    if gate:
                        gate.set()
                bus.close()
                wait_for(lambda: bus.closed)

    def test_driver_lifetime_all_uses_one_non_ui_thread(self):
        with self.opened() as (bus, driver):
            bus.send(CanFrame(0x123, b"\x01"))
            wait_for(lambda: driver.sent)
            wait_for(lambda: bus._sent)
            sent = bus.drain_sent()
            self.assertEqual(sent[0].data, b"\x01")
            self.assertGreater(sent[0].timestamp, 1000000000)
            bus.close()
            wait_for(lambda: bus.closed)
            self.assertEqual({name for name, _ in driver.calls}, {"open", "recv", "send", "status", "shutdown"})
            threads = {ident for _, ident in driver.calls}
            self.assertEqual(len(threads), 1)
            self.assertNotIn(threading.get_ident(), threads)

    def test_receive_poll_uses_one_millisecond_timeout(self):
        with self.opened() as (_, driver):
            wait_for(lambda: driver.receive_timeouts)
            self.assertEqual(transport.DriverCanBus.RECEIVE_TIMEOUT, 0.001)
            self.assertTrue(all(timeout == 0.001 for timeout in driver.receive_timeouts))

    def test_pending_send_and_close_wake_idle_wait(self):
        driver = FakeDriver()
        driver.recv_gate = threading.Event()
        # Stretch only this test's idle window to make a missing wakeup visible
        # without asserting sub-millisecond wall-clock scheduling guarantees.
        with patch.object(transport.DriverCanBus, "RECEIVE_TIMEOUT", 0.5), self.opened(driver) as (bus, _):
            idle = ObservedEvent()
            bus._work_ready = idle
            driver.recv_gate.set()
            wait_for(lambda: idle.wait_timeouts)
            self.assertGreater(idle.wait_timeouts[0], 0.4)
            bus.send(CanFrame(0x123))
            wait_for(lambda: driver.sent, timeout=0.2)
            self.assertEqual(driver.sent[0].arbitration_id, 0x123)
            wait_for(lambda: len(idle.wait_timeouts) >= 2)
            bus.close()
            wait_for(lambda: bus.closed, timeout=0.2)

    def test_full_receive_timeout_has_no_extra_fixed_wait(self):
        driver = FakeDriver()
        driver.recv_gate = threading.Event()

        def receive(timeout):
            driver._record("recv")
            driver.receive_timeouts.append(timeout)
            driver.recv_gate.wait(2)
            time.sleep(timeout)
            return None

        driver.recv = receive
        with patch.object(transport.DriverCanBus, "RECEIVE_TIMEOUT", 0.02), self.opened(driver) as (bus, _):
            idle = ObservedEvent()
            bus._work_ready = idle
            driver.recv_gate.set()
            wait_for(lambda: len(driver.receive_timeouts) >= 3)
            bus.close()
            wait_for(lambda: bus.closed)
            self.assertEqual(idle.wait_timeouts, [],
                             "A receive that consumed its timeout must not add another idle delay")

    def test_transmit_confirmation_only_after_driver_accepts(self):
        driver = FakeDriver()
        driver.send_gate = threading.Event()
        with self.opened(driver) as (bus, _):
            bus.send(CanFrame(0x123))
            wait_for(lambda: any(name == "send" for name, _ in driver.calls))
            self.assertEqual(bus.drain_sent(), [])
            driver.send_gate.set()
            wait_for(lambda: bus._sent)
            self.assertEqual(len(bus.drain_sent()), 1)

    def test_send_error_not_counted_and_cancels_pending(self):
        driver = FakeDriver()
        driver.send_error = OSError("write failed")
        with self.opened(driver) as (bus, _):
            bus.send(CanFrame(0x123))
            bus.send(CanFrame(0x124))
            wait_for(lambda: bus.closed)
            self.assertEqual(bus.state, "error")
            self.assertIn("write failed", bus.error)
            self.assertEqual(bus.drain_sent(), [])
            self.assertEqual(len(bus._tx), 0)

    def test_close_does_not_block_and_keeps_inflight_confirmation(self):
        driver = FakeDriver()
        driver.send_gate = threading.Event()
        with self.opened(driver) as (bus, _):
            bus.send(CanFrame(0x123))
            wait_for(lambda: any(name == "send" for name, _ in driver.calls))
            bus.send(CanFrame(0x124))
            started = time.monotonic()
            bus.close()
            self.assertLess(time.monotonic() - started, 0.05)
            self.assertEqual(bus.state, "closing")
            self.assertFalse(bus.closed)
            driver.send_gate.set()
            wait_for(lambda: bus.closed)
            self.assertEqual([msg.arbitration_id for msg in driver.sent], [0x123])
            self.assertEqual([frame.can_id for frame in bus.drain_sent()], [0x123])

    def test_close_pending_open_never_becomes_open_and_prevents_reopen(self):
        gate = threading.Event()
        entered = threading.Event()
        driver = FakeDriver()
        def open_slow(bus, api):
            entered.set()
            gate.wait(2)
            return driver
        with patch.object(transport.DriverCanBus, "_open", open_slow):
            bus = transport.DriverCanBus("pcan", "PCAN_USBBUS1", 500000)
            try:
                self.assertTrue(entered.wait(1))
                bus.close()
                with self.assertRaisesRegex(RuntimeError, "等待"):
                    transport.DriverCanBus("pcan", "PCAN_USBBUS1", 500000)
                gate.set()
                wait_for(lambda: bus.closed)
                self.assertEqual(bus.state, "closed")
                self.assertFalse(any(name == "recv" for name, _ in driver.calls))
                other = transport.DriverCanBus("pcan", "PCAN_USBBUS1", 500000)
                other.close()
                wait_for(lambda: other.closed)
            finally:
                gate.set()
                bus.close()
                wait_for(lambda: bus.closed)

    def test_channel_remains_reserved_until_shutdown_returns(self):
        driver = FakeDriver()
        driver.close_gate = threading.Event()
        with self.opened(driver) as (bus, _):
            bus.close()
            wait_for(lambda: any(name == "shutdown" for name, _ in driver.calls))
            self.assertFalse(bus.closed)
            with self.assertRaises(RuntimeError):
                transport.DriverCanBus("pcan", "PCAN_USBBUS1", 500000)
            driver.close_gate.set()
            wait_for(lambda: bus.closed)

    def test_close_error_is_preserved(self):
        driver = FakeDriver()
        driver.close_error = OSError("release failed")
        with self.opened(driver) as (bus, _):
            bus.close()
            wait_for(lambda: bus.closed)
            self.assertIn("release failed", bus.close_error)
            self.assertEqual(bus.state, "error")

    def test_receive_queue_is_bounded_and_reports_exact_drops(self):
        driver = FakeDriver()
        driver.rx.extend(can.Message(arbitration_id=index, is_extended_id=False, data=b"\x01") for index in range(100))
        with patch.object(transport.DriverCanBus, "MAX_RX_FRAMES", 16), self.opened(driver) as (bus, _):
            wait_for(lambda: not driver.rx)
            wait_for(lambda: bus.rx_dropped == 84)
            frames = []
            while (frame := bus.receive()) is not None:
                frames.append(frame.can_id)
            self.assertEqual(frames, list(range(84, 100)))

    def test_tx_and_acknowledgement_queue_share_one_bound(self):
        with patch.object(transport.DriverCanBus, "MAX_PENDING_TX", 3), self.opened() as (bus, driver):
            for identifier in range(3):
                bus.send(CanFrame(identifier))
            wait_for(lambda: len(driver.sent) == 3)
            with self.assertRaisesRegex(BufferError, "队列已满"):
                bus.send(CanFrame(4))
            self.assertEqual(len(bus.drain_sent(1)), 1)
            bus.send(CanFrame(4))
            wait_for(lambda: len(driver.sent) == 4)

    def test_rx_before_error_is_delivered_before_error(self):
        driver = FakeDriver()
        driver.rx.append(can.Message(arbitration_id=0x123, data=b"\x02"))
        driver.recv_error = OSError("device removed")
        with self.opened(driver) as (bus, _):
            wait_for(lambda: bus.closed)
            self.assertEqual(bus.receive().data, b"\x02")
            with self.assertRaisesRegex(RuntimeError, "device removed"):
                bus.receive()

    def test_filter_standard_extended_error_and_echo(self):
        driver = FakeDriver()
        driver.rx.extend([
            can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01"),
            can.Message(arbitration_id=0x123, is_extended_id=True, data=b"\x02"),
            can.Message(arbitration_id=0x124, is_extended_id=False, data=b"\x03"),
            can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x04", is_rx=False),
            can.Message(arbitration_id=0x40, is_error_frame=True, data=b"\x05"),
        ])
        with self.opened(driver, filter_id=0x123, filter_mask=0x7FF, filter_extended=False) as (bus, _):
            wait_for(lambda: not driver.rx)
            wait_for(lambda: len(bus._rx) == 2)
            self.assertEqual(bus.receive().data, b"\x01")
            self.assertTrue(bus.receive().error)
            self.assertIsNone(bus.receive())

    def test_own_echo_can_be_enabled_and_errors_disabled(self):
        driver = FakeDriver()
        driver.rx.extend([can.Message(arbitration_id=1, data=b"\x01", is_rx=False),
                          can.Message(arbitration_id=2, is_error_frame=True)])
        with self.opened(driver, receive_own=True, error_frames=False) as (bus, _):
            wait_for(lambda: not driver.rx)
            self.assertEqual(bus.receive().can_id, 1)
            self.assertIsNone(bus.receive())

    def test_rtr_and_fd_payloads_and_timestamps_survive_conversion(self):
        driver = FakeDriver()
        driver.rx.extend([
            can.Message(arbitration_id=0x321, is_remote_frame=True, dlc=8, timestamp=123.4),
            can.Message(arbitration_id=0x10000, data=bytes(range(64)), is_fd=True,
                        bitrate_switch=True, error_state_indicator=True, timestamp=124.4),
        ])
        with self.opened(driver, fd=True) as (bus, _):
            wait_for(lambda: not driver.rx)
            remote, fd = bus.receive(), bus.receive()
            self.assertEqual((remote.data, remote.dlc, remote.timestamp), (b"", 8, 123.4))
            self.assertEqual(fd.data, bytes(range(64)))
            self.assertTrue(fd.bitrate_switch and fd.error_state_indicator and fd.fd)

    def test_rejects_error_esi_and_disabled_fd_transmissions(self):
        with self.opened() as (bus, _):
            for frame in (CanFrame(1, error=True), CanFrame(1, fd=True, error_state_indicator=True), CanFrame(1, fd=True)):
                with self.assertRaises(ValueError):
                    bus.send(frame)

    def test_pcan_busoff_is_visible_and_stops_worker(self):
        driver = FakeDriver()
        driver.status_value = 0x10
        with self.opened(driver) as (bus, _):
            wait_for(lambda: bus.closed)
            self.assertIn("BUS-OFF", bus.error)

    def test_missing_dependency_is_actionable_and_releases_channel(self):
        with patch.object(transport, "_load_can", side_effect=ModuleNotFoundError("can")):
            bus = transport.DriverCanBus("pcan", "PCAN_USBBUS1", 500000)
            wait_for(lambda: bus.closed)
            self.assertIn("requirements.txt", bus.error)
        with self.opened() as (bus, _):
            self.assertEqual(bus.state, "open")

    def test_pcan_fd_timings_obey_hardware_limits_for_ui_rates(self):
        for nominal in (125000, 250000, 500000, 1000000):
            for data in (1000000, 2000000, 4000000, 5000000, 8000000):
                with self.subTest(nominal=nominal, data=data):
                    timing = transport._pcan_fd_timing(can, nominal, data)
                    self.assertEqual((timing.nom_bitrate, timing.data_bitrate), (nominal, data))
                    self.assertLessEqual(timing.nom_tseg1, 256)
                    self.assertLessEqual(timing.nom_tseg2, 128)
                    self.assertLessEqual(timing.data_tseg1, 32)
                    self.assertLessEqual(timing.data_tseg2, 16)


class ParameterTests(unittest.TestCase):
    def test_invalid_parameters_fail_without_starting_a_worker(self):
        invalid = [dict(backend="bad"), dict(channel="can0"), dict(bitrate=123456),
                   dict(bitrate=True), dict(filter_id=-1), dict(filter_mask=2**29),
                   dict(filter_extended="false"), dict(filter_extended=False, filter_id=0x800),
                   dict(fd=True, data_bitrate=100000), dict(fd=True, data_bitrate=9000000)]
        with patch.object(threading.Thread, "start") as start:
            for change in invalid:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    transport.DriverCanBus(**dict(dict(backend="pcan", channel="PCAN_USBBUS1", bitrate=500000), **change))
            for channel in ("-1", "0x0", "", "name", "1000"):
                with self.assertRaises(ValueError):
                    transport.DriverCanBus("gs_usb", channel, 500000)
            with self.assertRaisesRegex(ValueError, "不支持 CAN FD"):
                transport.DriverCanBus("gs_usb", "0", 500000, fd=True)
            start.assert_not_called()


@unittest.skipIf(can is None, "optional python-can dependency is not installed")
class PcanDriverBoundaryTests(unittest.TestCase):
    def stub_init(self, instance, **kwargs):
        instance.m_objPCANBasic = self.api
        instance.m_PcanHandle = 0x51
        instance._can_protocol = can.CanProtocol.CAN_FD
        can.BusABC.__init__(instance, channel="PCAN_USBBUS1")

    def setUp(self):
        self.module = __import__("can.interfaces.pcan.pcan", fromlist=["PcanBus"])
        self.api = SimpleNamespace(WriteFD=Mock(return_value=0), Uninitialize=Mock(return_value=0), SetValue=Mock(return_value=0))

    def test_real_pcan_writefd_accepts_classic_rtr_with_nonzero_dlc(self):
        with patch.object(self.module.PcanBus, "__init__", lambda instance, **kwargs: self.stub_init(instance, **kwargs)):
            driver = transport._open_pcan(can, {})
        facade = object.__new__(transport.DriverCanBus)
        facade.backend, facade.fd = "pcan", True
        frame = CanFrame(0x123, remote=True, dlc=8)
        driver.send(facade._message(can, frame), timeout=0.1)
        packet = self.api.WriteFD.call_args.args[1]
        self.assertEqual(packet.DLC, 8)
        self.assertTrue(packet.MSGTYPE & 1)  # PCAN_MESSAGE_RTR
        self.assertEqual(frame.data, b"")
        driver.shutdown()
        self.api.Uninitialize.assert_called_once()

    def test_uninitialize_failure_is_not_reported_as_success(self):
        with patch.object(self.module.PcanBus, "__init__", lambda instance, **kwargs: self.stub_init(instance, **kwargs)):
            driver = transport._open_pcan(can, {})
        self.api.Uninitialize.return_value = 0x200
        with self.assertRaisesRegex(RuntimeError, "释放失败"):
            driver.shutdown()


@unittest.skipUnless(can and importlib.util.find_spec("gs_usb") and importlib.util.find_spec("usb"), "optional gs_usb dependency is not installed")
class GsUsbBoundaryTests(unittest.TestCase):
    def setUp(self):
        from gs_usb.gs_usb import GsUsb
        from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP
        from gs_usb.gs_usb_frame import GsUsbFrame, GS_USB_NONE_ECHO_ID
        import usb.core
        import can.interfaces.gs_usb
        self.usb = usb.core
        self.module = can.interfaces.gs_usb
        self.raw = SimpleNamespace(read=Mock(), write=Mock(return_value=24), ctrl_transfer=Mock(), default_timeout=1000)
        self.gs = GsUsb(self.raw)
        self.gs.device_flags = GS_CAN_MODE_HW_TIMESTAMP
        def init(instance, **kwargs):
            instance.gs_usb = self.gs
            instance.channel_info = "0"
            instance._can_protocol = can.CanProtocol.CAN_20
            can.BusABC.__init__(instance, channel=0)
        with patch.object(self.module.GsUsbBus, "__init__", init):
            self.driver = transport._open_gs_usb(can, {})
        frame = GsUsbFrame(can_id=0x123, data=[1, 2])
        frame.echo_id = GS_USB_NONE_ECHO_ID
        frame.timestamp_us = 100
        self.frame = frame
        self.raw.read.return_value = frame.pack(True)
        self.dispose = patch("usb.util.dispose_resources").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.driver.shutdown)

    def test_reads_have_positive_usb_timeout_and_epoch_timestamp(self):
        msg = self.driver.recv(timeout=0.01)
        self.assertEqual(msg.data, b"\x01\x02")
        self.assertTrue(msg.is_rx)
        self.assertGreater(msg.timestamp, 1000000000)
        self.assertEqual(self.raw.read.call_args.args, (0x81, 24, 10))
        self.assertEqual(self.raw.default_timeout, 100)

    def test_usb_timeout_is_idle_but_device_removal_is_an_error(self):
        self.raw.read.side_effect = self.usb.USBTimeoutError("timeout")
        self.assertIsNone(self.driver.recv(timeout=0.001))
        self.raw.read.side_effect = self.usb.USBError("No such device", errno=19)
        with self.assertRaises(self.usb.USBError):
            self.driver.recv(timeout=0.01)

    def test_submillisecond_poll_is_clamped_to_one_ms(self):
        self.driver.recv(timeout=0.0001)
        self.assertEqual(self.raw.read.call_args.args[2], 1)

    def test_gs_rtr_wire_dlc_is_preserved_and_data_does_not_mutate_frame(self):
        facade = object.__new__(transport.DriverCanBus)
        facade.backend, facade.fd = "gs_usb", False
        frame = CanFrame(0x321, remote=True, dlc=8)
        self.driver.send(facade._message(can, frame), timeout=0.1)
        payload = self.raw.write.call_args.args[1]
        fields = struct.unpack("<2I12BI", payload)
        self.assertEqual(fields[1], 0x321 | 0x40000000)
        self.assertEqual(fields[2], 8)
        self.assertEqual(frame.data, b"")
        self.assertEqual(self.raw.write.call_args.kwargs["timeout"], 100)

    def test_short_usb_write_is_not_accepted_as_a_sent_can_frame(self):
        self.raw.write.return_value = 12
        with self.assertRaisesRegex(can.CanOperationError, "写入不完整"):
            self.driver.send(can.Message(arbitration_id=0x123, data=b"\x01"), timeout=0.1)

    def test_timestamp_wrap_preserves_elapsed_interval(self):
        self.frame.timestamp_us = 2**32 - 100
        self.raw.read.return_value = self.frame.pack(True)
        first = self.driver.recv(timeout=0.01)
        self.frame.timestamp_us = 100
        self.raw.read.return_value = self.frame.pack(True)
        second = self.driver.recv(timeout=0.01)
        self.assertAlmostEqual(second.timestamp - first.timestamp, 0.0002, places=6)

    def test_timestamp_handles_multiple_wraps_after_long_idle(self):
        first = self.driver.recv(timeout=0.01)
        silence = 2 * (2**32 / 1000000)
        self.driver._timestamp_mono_start -= silence
        second = self.driver.recv(timeout=0.01)
        self.assertAlmostEqual(second.timestamp - first.timestamp, silence, places=6)

    def test_close_checks_reset_error_and_always_releases_usb_resources(self):
        self.raw.ctrl_transfer.side_effect = self.usb.USBError("gone", errno=19)
        with self.assertRaises(self.usb.USBError):
            self.driver.shutdown()
        self.dispose.assert_called_once_with(self.raw)

    def test_close_resets_only_open_adapter_without_rescanning_or_starting(self):
        with patch("gs_usb.gs_usb.GsUsb.scan") as scan:
            self.driver.shutdown()
        scan.assert_not_called()
        self.raw.ctrl_transfer.assert_called_once_with(0x41, 2, 0, 0, struct.pack("<II", 0, 0), timeout=1000)


if __name__ == "__main__":
    unittest.main()
