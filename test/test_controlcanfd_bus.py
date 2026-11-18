"""Exercise the Chuangxin ctypes ABI with a compiled, hardware-free SDK.

The C shim deliberately uses 64-bit handles and packed records. No test loads
the vendor library, enumerates USB, starts a real controller, or sends on CAN.
"""

from collections import deque
from contextlib import contextmanager
import ctypes
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import controlcanfd_bus as native
import driver_can_bus as transport
from socketcan_bus import CanFrame


_SDK_SOURCE = r"""
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#pragma pack(push, 1)
typedef struct { uint32_t id; uint8_t len, pad[3], data[8]; } Can;
typedef struct { uint32_t id; uint8_t len, flags, pad[2], data[64]; } Fd;
typedef struct { Can frame; uint32_t type; } Tx;
typedef struct { Fd frame; uint32_t type; } TxFd;
typedef struct { Can frame; uint64_t timestamp; } Rx;
typedef struct { Fd frame; uint64_t timestamp; } RxFd;
typedef struct { uint32_t code, mask, reserved; uint8_t filter,t0,t1,mode; } ClassicConfig;
typedef struct { uint32_t code,mask,abit,dbit,brp; uint8_t filter,mode;
                 uint16_t pad; uint32_t reserved; } FdConfig;
typedef struct { uint32_t type; union { ClassicConfig can; FdConfig fd; } config; } Init;
#pragma pack(pop)

static const uintptr_t DEV = sizeof(void*) == 8 ? (uintptr_t)UINT64_C(0x12345678ABCDEF01) : 0x12345671;
static const uintptr_t CH = sizeof(void*) == 8 ? (uintptr_t)UINT64_C(0x23456789ABCDEF02) : 0x23456782;
static int events[4096], event_count, fail_stage, bad_handle, online = 2;
static uint32_t last_device_type, last_device_index, last_channel, last_abit, last_dbit, last_standard;
static uint32_t last_id, last_type, last_init_type, sent_count = 1;
static uint8_t last_length, last_flags, last_mode, last_data[64];
static Rx rx[2];
static RxFd rx_fd[2];
static int has_rx[2], has_rx_fd[2], receive_wait, fail_start_port = -1, tx_port;
static uint32_t abit[2], dbit[2];

static int event(int code) {
    if (event_count < 4096) events[event_count++] = code;
    return fail_stage != code;
}
static void check_device(void *h) { if ((uintptr_t)h != DEV) bad_handle = 1; }
static int check_channel(void *h) {
    if ((uintptr_t)h == CH + 1) return 1;
    if ((uintptr_t)h != CH) bad_handle = 1;
    return 0;
}

void test_reset(void) {
    memset(events,0,sizeof events); event_count = fail_stage = bad_handle = 0;
    online = 2; sent_count = 1; receive_wait = 0; fail_start_port = -1; tx_port = -1;
    memset(has_rx,0,sizeof has_rx); memset(has_rx_fd,0,sizeof has_rx_fd);
    memset(abit,0,sizeof abit); memset(dbit,0,sizeof dbit);
    last_device_type = last_device_index = last_channel = 0;
    last_abit = last_dbit = last_standard = 0;
    last_id = last_type = last_init_type = 0;
    last_length = last_flags = last_mode = 0; memset(last_data,0,sizeof last_data);
}
void test_fail(int stage) { fail_stage = stage; }
void test_fail_start_port(int port) { fail_start_port = port; }
void test_set_online(int value) { online = value; }
void test_set_sent_count(uint32_t value) { sent_count = value; }
int test_event_count(void) { return event_count; }
int test_event(int index) { return events[index]; }
int test_bad_handle(void) { return bad_handle; }
int test_receive_wait(void) { return receive_wait; }
int test_tx_port(void) { return tx_port; }
uint32_t test_port_abit(int index) { return abit[index]; }
uint32_t test_port_dbit(int index) { return dbit[index]; }
uint32_t test_device_type(void) { return last_device_type; }
uint32_t test_device_index(void) { return last_device_index; }
uint32_t test_channel(void) { return last_channel; }
uint32_t test_abit(void) { return last_abit; }
uint32_t test_dbit(void) { return last_dbit; }
uint32_t test_standard(void) { return last_standard; }
uint32_t test_id(void) { return last_id; }
uint32_t test_transmit_type(void) { return last_type; }
uint32_t test_init_type(void) { return last_init_type; }
uint32_t test_length(void) { return last_length; }
uint32_t test_flags(void) { return last_flags; }
uint32_t test_mode(void) { return last_mode; }
uint32_t test_data(int index) { return last_data[index]; }
uint32_t test_size(int index) {
    switch(index) { case 0: return sizeof(Init); case 1: return sizeof(Tx);
    case 2: return sizeof(TxFd); case 3: return sizeof(Rx); default: return sizeof(RxFd); }
}
void test_queue_classic_on(int port, uint32_t id, uint8_t length, uint64_t timestamp, const uint8_t *data) {
    Rx *msg = &rx[port];
    memset(msg,0,sizeof *msg); msg->frame.id=id; msg->frame.len=length; msg->timestamp=timestamp;
    if (data) memcpy(msg->frame.data,data,length <= 8 ? length : 8); has_rx[port]=1;
}
void test_queue_classic(uint32_t id, uint8_t length, uint64_t timestamp, const uint8_t *data) {
    test_queue_classic_on(0, id, length, timestamp, data);
}
void test_queue_fd(uint32_t id, uint8_t length, uint8_t flags, uint64_t timestamp, const uint8_t *data) {
    RxFd *msg = &rx_fd[0];
    memset(msg,0,sizeof *msg); msg->frame.id=id; msg->frame.len=length;
    msg->frame.flags=flags; msg->timestamp=timestamp;
    if (data) memcpy(msg->frame.data,data,length <= 64 ? length : 64); has_rx_fd[0]=1;
}

void *ZCAN_OpenDevice(uint32_t type, uint32_t index, uint32_t reserved) {
    last_device_type=type; last_device_index=index;
    if (reserved || !event(1)) return NULL; return (void*)DEV;
}
uint32_t ZCAN_SetAbitBaud(void *h, uint32_t ch, uint32_t rate) {
    check_device(h); last_channel=ch; last_abit=abit[ch]=rate; return event(2);
}
uint32_t ZCAN_SetDbitBaud(void *h, uint32_t ch, uint32_t rate) {
    check_device(h); last_channel=ch; last_dbit=dbit[ch]=rate; return event(3);
}
uint32_t ZCAN_SetCANFDStandard(void *h, uint32_t ch, uint32_t standard) {
    check_device(h); last_channel=ch; last_standard=standard; return event(4);
}
uint32_t ZCAN_SetResistanceEnable(void *h, uint32_t ch, uint32_t value) {
    check_device(h); return 1;
}
void *ZCAN_InitCAN(void *h, uint32_t ch, const Init *config) {
    check_device(h); last_channel=ch; last_init_type=config->type; last_mode=config->config.fd.mode;
    if (!event(5)) return NULL; return (void*)(CH + ch);
}
uint32_t ZCAN_ClearFilter(void *h) { check_channel(h); return event(6); }
uint32_t ZCAN_StartCAN(void *h) { int p=check_channel(h); return event(7) && p != fail_start_port; }
uint32_t ZCAN_ResetCAN(void *h) { check_channel(h); return event(8); }
uint32_t ZCAN_CloseDevice(void *h) { check_device(h); return event(9); }
uint32_t ZCAN_ClearBuffer(void *h) { check_channel(h); return 1; }
uint32_t ZCAN_IsDeviceOnLine(void *h) { check_device(h); event(14); return online; }
uint32_t ZCAN_GetDeviceInf(void *h, void *info) { check_device(h); return 1; }
uint32_t ZCAN_GetReceiveNum(void *h, uint8_t type) {
    int port=check_channel(h); return type ? has_rx_fd[port] : has_rx[port];
}
uint32_t ZCAN_Transmit(void *h, const Tx *frames, uint32_t count) {
    tx_port=check_channel(h); event(10);
    if (!count) return 0;
    last_id=frames[0].frame.id; last_length=frames[0].frame.len;
    last_type=frames[0].type; memcpy(last_data,frames[0].frame.data,8); return sent_count;
}
uint32_t ZCAN_TransmitFD(void *h, const TxFd *frames, uint32_t count) {
    tx_port=check_channel(h); event(11);
    if (!count) return 0;
    last_id=frames[0].frame.id; last_length=frames[0].frame.len;
    last_flags=frames[0].frame.flags; last_type=frames[0].type;
    memcpy(last_data,frames[0].frame.data,64); return sent_count;
}
uint32_t ZCAN_Receive(void *h, Rx *frames, uint32_t count, int wait) {
    int port=check_channel(h); event(12); receive_wait=wait;
    if (count && has_rx[port]) { frames[0]=rx[port]; has_rx[port]=0; return 1; } return 0;
}
uint32_t ZCAN_ReceiveFD(void *h, RxFd *frames, uint32_t count, int wait) {
    int port=check_channel(h); event(13); receive_wait=wait;
    if (count && has_rx_fd[port]) { frames[0]=rx_fd[port]; has_rx_fd[port]=0; return 1; } return 0;
}
"""


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("CANFD worker did not reach expected state")
        time.sleep(0.002)


@unittest.skipUnless(sys.platform == "linux" and shutil.which("cc"),
                     "native SDK ABI shim requires Linux and a C compiler")
class ControlCanFdNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="debugtool-canfd-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        source = root / "sdk.c"
        source.write_text(_SDK_SOURCE, encoding="utf-8")
        cls.library_path = root / "sdk.so"
        subprocess.run([shutil.which("cc"), "-shared", "-fPIC", "-O0", str(source),
                        "-o", str(cls.library_path)], check=True, capture_output=True, text=True)

    def setUp(self):
        self.sdk = ctypes.CDLL(str(self.library_path))
        self.sdk.test_queue_classic.argtypes = (
            ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint64, ctypes.c_void_p)
        self.sdk.test_queue_classic_on.argtypes = (ctypes.c_int, *self.sdk.test_queue_classic.argtypes)
        self.sdk.test_queue_fd.argtypes = (
            ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint64, ctypes.c_void_p)
        self.sdk.test_reset()
        self.loader = patch.object(native, "_load_library", return_value=self.sdk)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def events(self):
        return [self.sdk.test_event(index) for index in range(self.sdk.test_event_count())]

    @contextmanager
    def opened(self, **options):
        driver = native.ControlCanFdDriver(channel=options.pop("channel", "0:0"),
            bitrate=options.pop("bitrate", 500000), fd=options.pop("fd", True),
            data_bitrate=options.pop("data_bitrate", 2000000), **options)
        try:
            yield driver
        finally:
            driver.shutdown()

    def test_open_uses_type41_iso_and_ordered_setup_with_full_width_handles(self):
        with self.opened(channel="3:1", bitrate=1000000, data_bitrate=5000000):
            self.assertEqual(self.events(), [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(self.sdk.test_device_type(), 41)
            self.assertEqual(self.sdk.test_device_index(), 3)
            self.assertEqual(self.sdk.test_channel(), 1)
            self.assertEqual(self.sdk.test_abit(), 1000000)
            self.assertEqual(self.sdk.test_dbit(), 5000000)
            self.assertEqual(self.sdk.test_standard(), 0)
            self.assertEqual(self.sdk.test_init_type(), 1)
            self.assertEqual(self.sdk.test_mode(), 0)
        self.assertEqual(self.events()[-2:], [8, 9])
        self.assertFalse(self.sdk.test_bad_handle(), "ctypes truncated a native pointer")
        self.assertEqual([self.sdk.test_size(i) for i in range(5)], [32, 20, 76, 24, 80])

    def test_classic_operation_still_initializes_the_fd_device(self):
        with self.opened(fd=False) as driver:
            self.assertEqual(self.sdk.test_init_type(), 1)
            self.assertEqual(self.sdk.test_dbit(), 2000000)
            with self.assertRaises((ValueError, RuntimeError)):
                driver.send(CanFrame(0x123, bytes(12), fd=True))
            driver.recv(timeout=0.001)
            self.assertNotIn(13, self.events(), "FD receive must follow the enabled mode")

    def test_failed_open_and_each_setup_stage_release_partial_handles(self):
        for stage in range(1, 8):
            with self.subTest(stage=stage):
                self.sdk.test_reset()
                self.sdk.test_fail(stage)
                with self.assertRaises((OSError, RuntimeError)):
                    native.ControlCanFdDriver(channel="0:0", bitrate=500000,
                                              fd=True, data_bitrate=2000000)
                events = self.events()
                self.assertEqual(events[:stage], list(range(1, stage + 1)))
                self.assertEqual(events[stage:], [] if stage == 1 else
                                 [9] if stage <= 5 else [8, 9])
                self.assertFalse(self.sdk.test_bad_handle())

    def test_shutdown_attempts_close_even_when_reset_fails_and_is_idempotent(self):
        driver = native.ControlCanFdDriver("0:0", 500000, fd=True, data_bitrate=2000000)
        self.sdk.test_fail(8)
        with self.assertRaises((OSError, RuntimeError)):
            driver.shutdown()
        self.assertEqual(self.events()[-2:], [8, 9])
        previous = self.events()
        driver.shutdown()
        self.assertEqual(self.events(), previous)

    def test_send_uses_distinct_packed_classic_rtr_and_fd_records(self):
        with self.opened() as driver:
            driver.send(CanFrame(0x123, b"\x01\x02"), timeout=0.1)
            self.assertEqual(self.sdk.test_id(), 0x123)
            self.assertEqual(self.sdk.test_length(), 2)
            self.assertEqual(self.sdk.test_transmit_type(), 0)
            self.assertEqual([self.sdk.test_data(i) for i in range(2)], [1, 2])
            driver.send(CanFrame(0x123456, extended=True, remote=True, dlc=8), timeout=0.1)
            self.assertEqual(self.sdk.test_id() & 0xFFFFFFFF, 0xC0123456)
            self.assertEqual(self.sdk.test_length(), 8)
            self.assertTrue(all(self.sdk.test_data(i) == 0 for i in range(8)))
            payload = bytes(range(64))
            driver.send(CanFrame(0x1ABCDE, payload, extended=True, fd=True,
                                 bitrate_switch=True), timeout=0.1)
            self.assertEqual(self.sdk.test_id() & 0xFFFFFFFF, 0x801ABCDE)
            self.assertEqual(self.sdk.test_length(), 64)
            self.assertEqual(self.sdk.test_flags(), 1)
            self.assertEqual(bytes(self.sdk.test_data(i) for i in range(64)), payload)
            self.assertEqual(self.events().count(10), 2)
            self.assertEqual(self.events().count(11), 1)

    def test_zero_transmit_count_reports_failure(self):
        with self.opened() as driver:
            self.sdk.test_set_sent_count(0)
            for frame in (CanFrame(0x123, b"\x01"), CanFrame(0x123, bytes(12), fd=True)):
                with self.subTest(fd=frame.fd), self.assertRaises((OSError, RuntimeError)):
                    driver.send(frame, timeout=0.1)

    def test_invalid_channel_and_rates_fail_before_loading_sdk(self):
        invalid = (
            dict(channel="0"), dict(channel="0:2"), dict(channel="8:0"),
            dict(channel="../0:0"), dict(bitrate=10000), dict(bitrate=True),
            dict(data_bitrate=8000000), dict(bitrate=1000000, data_bitrate=500000),
        )
        for options in invalid:
            arguments = dict(channel="0:0", bitrate=500000, fd=True, data_bitrate=2000000)
            arguments.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                native.ControlCanFdDriver(**arguments)
        self.assertEqual(self.events(), [])

    def test_both_receive_queues_decode_flags_rtr_and_microsecond_timestamps(self):
        timestamp = 0x123456789A
        payload = bytes(range(64))
        with self.opened() as driver:
            self.sdk.test_queue_classic(0xC0123456, 8, timestamp, bytes(8))
            self.sdk.test_queue_fd(0x801ABCDE, 64, 3, timestamp + 250000, payload)
            received = [driver.recv(timeout=0.001), driver.recv(timeout=0.001)]
            self.assertTrue(all(frame is not None for frame in received))
            classic = next(frame for frame in received if not frame.fd)
            fd = next(frame for frame in received if frame.fd)
            self.assertEqual((classic.can_id, classic.extended, classic.remote,
                              classic.data, classic.dlc), (0x123456, True, True, b"", 8))
            self.assertEqual((fd.can_id, fd.extended, fd.data, fd.bitrate_switch,
                              fd.error_state_indicator), (0x1ABCDE, True, payload, True, True))
            self.assertAlmostEqual(fd.timestamp - classic.timestamp, 0.25, places=5)
            self.assertEqual(self.sdk.test_receive_wait(), 0,
                             "queued worker must poll SDK receive without a blocking wait")

    def test_mixed_receive_keeps_device_time_order_across_backlogged_batches(self):
        classic = deque(range(1, 129))
        fd = deque([129])

        def receive(source, channel, buffer, count, wait):
            self.assertEqual(channel, 1)
            self.assertEqual(wait, 0)
            actual = min(len(source), count)
            for index in range(actual):
                buffer[index].frame.can_id = 0x123
                buffer[index].frame.length = 0
                buffer[index].timestamp = source.popleft() * 1000000
            return actual

        lib = SimpleNamespace(
            ZCAN_Receive=lambda *args: receive(classic, *args),
            ZCAN_ReceiveFD=lambda *args: receive(fd, *args))
        port = native._CanChannel(lib, 1, '0:0', True)
        with patch.object(native.time, "time", return_value=1700000000):
            frames = [port.recv() for _ in range(129)]
        self.assertEqual([frame.timestamp for frame in frames],
                         [1700000000 + index for index in range(129)])
        self.assertTrue(all(not frame.fd for frame in frames[:-1]))
        self.assertTrue(frames[-1].fd)
        self.assertIsNone(port.recv())

    def test_dual_channel_uses_one_device_and_routes_worker_io(self):
        bus = transport.DriverCanBus('controlcanfd', '0:0+1', 500000, fd=True,
                                     can1_config=(250000, 2000000, False))
        try:
            wait_for(lambda: bus.state != 'opening')
            self.assertEqual(bus.state, 'open', bus.error)
            self.assertEqual([self.sdk.test_port_abit(port) for port in (0, 1)], [500000, 250000])
            self.assertEqual(self.sdk.test_port_dbit(1), 2000000)
            self.assertEqual(self.events().count(1), 1)
            self.assertEqual(self.events().count(7), 2)
            with self.assertRaisesRegex(ValueError, '发送通道'):
                bus.send(CanFrame(0x100))
            with self.assertRaisesRegex(ValueError, '未启用 CAN FD'):
                bus.send(CanFrame(0x100, bytes(12), fd=True, channel='0:1'))
            for port in (1, 0):
                bus.send(CanFrame(0x100 + port, channel=f'0:{port}'))
                wait_for(lambda: bool(bus._sent) or bus.closed)
                self.assertEqual(bus.drain_sent()[0].channel, f'0:{port}')
                self.assertEqual(self.sdk.test_tx_port(), port)
                self.sdk.test_queue_classic_on(port, 0x200 + port, 0, 1000, None)
            wait_for(lambda: len(bus._rx) == 2)
            self.assertEqual({(frame.channel, frame.can_id) for frame in (bus.receive(), bus.receive())},
                             {('0:0', 0x200), ('0:1', 0x201)})
        finally:
            bus.close()
            wait_for(lambda: bus.closed)
        self.assertEqual(self.events()[-3:], [8, 8, 9])
        self.assertFalse(self.sdk.test_bad_handle())

    def test_can1_start_failure_releases_both_channels_and_the_device(self):
        self.sdk.test_fail_start_port(1)
        with self.assertRaisesRegex(RuntimeError, 'CAN1 启动通道'):
            with self.opened(channel='0:0+1'):
                self.fail('dual connection must fail when either port does not start')
        self.assertEqual(self.events()[-5:], [7, 7, 8, 8, 9])
        self.assertEqual(self.events().count(1), 1)

    def test_status_distinguishes_online_code_two_from_boolean_success(self):
        with self.opened() as driver:
            self.sdk.test_set_online(2)
            driver.status()
            for state in (0, 1, 3):
                self.sdk.test_set_online(state)
                with self.subTest(state=state), self.assertRaises((OSError, RuntimeError)):
                    driver.status()

    def test_worker_has_no_python_can_dependency_and_confirms_only_accepted_frames(self):
        with patch.object(transport, "_load_can", side_effect=AssertionError(
                "Chuangxin Linux must not import python-can")):
            bus = transport.DriverCanBus("controlcanfd", "0:0", 500000, fd=True,
                                         data_bitrate=2000000)
            try:
                wait_for(lambda: bus.state != "opening")
                self.assertEqual(bus.state, "open", bus.error)
                accepted = CanFrame(0x123, bytes(range(12)), fd=True, bitrate_switch=True)
                bus.send(accepted)
                wait_for(lambda: bool(bus._sent) or bus.closed)
                self.assertFalse(bus.closed, bus.error)
                self.assertEqual(bus.drain_sent()[0].data, accepted.data)
                self.sdk.test_set_sent_count(0)
                bus.send(CanFrame(0x124, b"\xFF"))
                wait_for(lambda: bus.closed)
                self.assertEqual(bus.state, "error")
                self.assertEqual(bus.drain_sent(), [])
                self.assertEqual(self.events()[-2:], [8, 9])
            finally:
                bus.close()
                wait_for(lambda: bus.closed)

    def test_same_device_is_reserved_across_both_can_channels(self):
        bus = transport.DriverCanBus("controlcanfd", "0:0", 500000, fd=True,
                                     data_bitrate=2000000)
        try:
            wait_for(lambda: bus.state != "opening")
            self.assertEqual(bus.state, "open", bus.error)
            with self.assertRaises((ValueError, RuntimeError)):
                transport.DriverCanBus("controlcanfd", "0:1", 500000, fd=True,
                                       data_bitrate=2000000)
        finally:
            bus.close()
            wait_for(lambda: bus.closed)

    def test_worker_applies_ui_mask_and_format_to_native_frames(self):
        bus = transport.DriverCanBus("controlcanfd", "0:0", 500000, fd=True,
            data_bitrate=2000000, filter_id=0x120, filter_mask=0x7F0,
            filter_extended=False)
        try:
            wait_for(lambda: bus.state != "opening")
            self.assertEqual(bus.state, "open", bus.error)
            self.sdk.test_queue_classic(0x125, 1, 1000, b"\x01")
            wait_for(lambda: bool(bus._rx) or bus.closed)
            self.assertEqual(bus.receive().can_id, 0x125)
            # Both an extended frame with the same ID and a nonmatching ID
            # must be rejected by the common worker, without hardware filters.
            for identifier in (0x80000125, 0x135):
                count_before = self.events().count(12)
                self.sdk.test_queue_classic(identifier, 1, 2000, b"\x02")
                wait_for(lambda: self.events().count(12) > count_before + 1 or bus.closed)
                self.assertIsNone(bus.receive())
        finally:
            bus.close()
            wait_for(lambda: bus.closed)

    def test_software_echo_shares_rx_bound_and_does_not_block_device_replies(self):
        with patch.object(transport.DriverCanBus, "MAX_RX_FRAMES", 8):
            bus = transport.DriverCanBus("controlcanfd", "0:0", 500000, fd=True,
                data_bitrate=2000000, receive_own=True)
            try:
                wait_for(lambda: bus.state != "opening")
                self.assertEqual(bus.state, "open", bus.error)
                for index in range(32):
                    bus.send(CanFrame(0x100 + index, b"\x01"))
                wait_for(lambda: len(bus._sent) == 32 or bus.closed)
                self.assertFalse(bus.closed, bus.error)
                self.sdk.test_queue_classic(0x345, 1, 10000, b"\xA5")
                wait_for(lambda: any(frame.can_id == 0x345 for frame in bus._rx) or bus.closed)
                self.assertFalse(bus.closed, bus.error)
                self.assertEqual(len(bus._rx), 8)
                self.assertEqual(bus.rx_dropped, 25)
                received = []
                while (frame := bus.receive()) is not None:
                    received.append(frame)
                self.assertEqual(received[-1].can_id, 0x345)
                self.assertEqual(received[-1].data, b"\xA5")
                self.assertEqual(self.sdk.test_transmit_type(), 0,
                                 "software echo must not change normal hardware transmit mode")
                self.assertEqual(len(bus.drain_sent(64)), 32)
            finally:
                bus.close()
                wait_for(lambda: bus.closed)

    def test_worker_detects_unplug_and_releases_device(self):
        bus = transport.DriverCanBus("controlcanfd", "0:0", 500000, fd=True,
                                     data_bitrate=2000000)
        try:
            wait_for(lambda: bus.state != "opening")
            self.assertEqual(bus.state, "open", bus.error)
            self.sdk.test_set_online(3)
            wait_for(lambda: bus.closed)
            self.assertEqual(bus.state, "error")
            self.assertIn("离线", bus.error)
            self.assertEqual(self.events()[-2:], [8, 9])
        finally:
            bus.close()
            wait_for(lambda: bus.closed)


if __name__ == "__main__":
    unittest.main()
