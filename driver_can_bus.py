"""Queued USB CAN transports; driver calls never run on the UI thread.

python-can is imported lazily, so Linux SocketCAN does not need USB libraries.
One worker owns a driver from initialization through shutdown. A successful send
means the driver accepted the frame, not that a remote node acknowledged it.
"""

from collections import deque
from dataclasses import replace
import importlib
import re
import struct
import sys
import threading
import time

from socketcan_bus import CanFrame, _check_id, encode_frame


PCAN_BITRATES = frozenset((5000, 10000, 20000, 33000, 47000, 50000, 83000,
                         95000, 100000, 125000, 250000, 500000, 800000, 1000000))
_CHANNEL_LOCK = threading.Lock()
_CHANNEL_OWNERS = set()


def _load_can():
    return importlib.import_module("can")


def _driver_error(backend, exc):
    detail = str(exc) or type(exc).__name__
    if backend == "controlcanfd":
        return f"创芯科技 CANFD（Linux）：{detail}"
    if isinstance(exc, ImportError):
        return f"CAN 依赖缺失：{detail}；请安装 requirements.txt 后重新打包"
    if backend == "pcan":
        return (f"PCAN：{detail}。请确认 PEAK 驱动、PCANBasic.dll 与程序位数一致，"
                "并检查通道和设备是否被其他程序占用")
    return (f"candleLight：{detail}。请确认设备使用 WinUSB 驱动，且 libusb 可用；"
            "检查设备连接和通道序号")


def _prepare_usb_backend():
    # PyUSB caches this backend; GsUsb.scan() subsequently uses the same library.
    # libusb-package supplies the correctly sized DLL in a frozen Windows build.
    if sys.platform == "win32":
        backend = importlib.import_module("libusb_package").get_libusb1_backend()
        if backend is None:
            raise RuntimeError("无法加载 libusb-1.0.dll")


def discover_channels(backend):
    """Enumerate channels without opening/starting a CAN controller.

    This may call a vendor library or enumerate USB, so call it in a worker.
    candleLight indices are zero based and can change after unplug/replug.
    """
    try:
        if backend == "controlcanfd":
            from controlcanfd_bus import discover_channels as discover_controlcanfd
            return discover_controlcanfd()
        if backend == "pcan":
            can = _load_can()
            # The stock detector suppresses DLL loading errors. Probe explicitly
            # first to give the UI a useful dependency error rather than no ports.
            basic = importlib.import_module("can.interfaces.pcan.basic")
            basic.PCANBasic()
            return list(dict.fromkeys(str(item["channel"]) for item in
                        can.detect_available_configs(interfaces=["pcan"])))
        if backend == "gs_usb":
            _prepare_usb_backend()
            gs = importlib.import_module("gs_usb.gs_usb")
            return [str(index) for index, _ in enumerate(gs.GsUsb.scan())]
        raise ValueError("未知 CAN 后端")
    except Exception as exc:
        raise RuntimeError(_driver_error(backend, exc)) from exc


def _pcan_fd_timing(can, bitrate, data_bitrate):
    """Choose exact rates and enforce PEAK's narrower data-phase limits."""
    for clock in (80000000, 60000000, 40000000, 30000000, 24000000, 20000000):
        arguments = dict(f_clock=clock, nom_bitrate=bitrate,
                         nom_sample_point=80.0 if bitrate >= 800000 else 87.5,
                         data_bitrate=data_bitrate, data_sample_point=80.0)
        try:
            preferred = can.BitTimingFd.from_sample_point(**arguments)
        except ValueError:
            continue
        candidates = (preferred, *can.BitTimingFd.iterate_from_sample_point(**arguments))
        for timing in candidates:
            if (timing.nom_bitrate == bitrate and timing.data_bitrate == data_bitrate
                    and 1 <= timing.nom_brp <= 1024
                    and 1 <= timing.data_brp <= 1024
                    and 1 <= timing.nom_tseg1 <= 256
                    and 1 <= timing.nom_tseg2 <= 128
                    and 1 <= timing.nom_sjw <= 128
                    and 1 <= timing.data_tseg1 <= 32
                    and 1 <= timing.data_tseg2 <= 16
                    and 1 <= timing.data_sjw <= 16):
                return timing
    raise ValueError("PCAN FD 无法生成所选比特率的有效时序，请更换仲裁/数据比特率组合")


def _open_pcan(can, options):
    module = importlib.import_module("can.interfaces.pcan.pcan")

    class CheckedPcanBus(module.PcanBus):
        def __init__(self, **kwargs):
            try:
                super().__init__(**kwargs)
            except Exception:
                # PcanBus can fail after Initialize but before BusABC.__init__.
                if hasattr(self, "m_PcanHandle"):
                    self.m_objPCANBasic.Uninitialize(self.m_PcanHandle)
                self._is_shutdown = True
                raise

        def shutdown(self):
            if self._is_shutdown:
                return
            can.BusABC.shutdown(self)
            if module.HAS_EVENTS and module.IS_LINUX:
                self.m_objPCANBasic.SetValue(self.m_PcanHandle, module.PCAN_RECEIVE_EVENT, 0)
            result = self.m_objPCANBasic.Uninitialize(self.m_PcanHandle)
            if result != module.PCAN_ERROR_OK:
                raise RuntimeError(f"PCAN 通道释放失败，状态码 0x{result:X}")

    return CheckedPcanBus(**options)


def _open_gs_usb(can, options):
    """Keep python-can initialization, with bounded I/O and visible USB errors.

    gs_usb 0.3.1 read()/stop() swallow all USBError instances. In particular a
    removed device looks like an idle healthy bus. Use its frame codec directly
    for receive/reset so only genuine read timeouts are ignored.
    """
    _prepare_usb_backend()
    module = importlib.import_module("can.interfaces.gs_usb")
    frame_module = importlib.import_module("gs_usb.gs_usb_frame")
    constants = importlib.import_module("gs_usb.constants")
    usb = importlib.import_module("usb.core")
    usb_util = importlib.import_module("usb.util")

    class CheckedGsUsbBus(module.GsUsbBus):
        def __init__(self, **kwargs):
            try:
                super().__init__(**kwargs)
                self.gs_usb.gs_usb.default_timeout = 100
                self._timestamp_epoch = None
                self._timestamp_raw_start = 0
                self._timestamp_mono_start = 0
            except Exception:
                if hasattr(self, "gs_usb"):
                    try:
                        self._reset_usb()
                    except Exception:
                        pass
                self._is_shutdown = True
                raise

        def send(self, msg, timeout=None):
            can_id = msg.arbitration_id
            if msg.is_extended_id:
                can_id |= constants.CAN_EFF_FLAG
            if msg.is_remote_frame:
                can_id |= constants.CAN_RTR_FLAG
            frame = frame_module.GsUsbFrame(can_id=can_id, data=bytes(msg.data))
            frame.can_dlc = msg.dlc  # Remote frames carry DLC, with no payload.
            timestamps = bool(self.gs_usb.device_flags & constants.GS_CAN_MODE_HW_TIMESTAMP)
            packet = frame.pack(timestamps)
            timeout_ms = min(100, max(1, round((0.1 if timeout is None else timeout) * 1000)))
            written = self.gs_usb.gs_usb.write(0x02, packet, timeout=timeout_ms)
            if written != len(packet):
                raise can.CanOperationError(f"USB 帧写入不完整：{written}/{len(packet)} 字节")

        def _recv_internal(self, timeout):
            frame = frame_module.GsUsbFrame()
            timestamps = bool(self.gs_usb.device_flags & constants.GS_CAN_MODE_HW_TIMESTAMP)
            timeout_ms = max(1, round((timeout if timeout is not None else 0.01) * 1000))
            try:
                packet = self.gs_usb.gs_usb.read(0x81, frame.__sizeof__(timestamps), timeout_ms)
            except usb.USBTimeoutError:
                return None, False
            frame_module.GsUsbFrame.unpack_into(frame, packet, timestamps)
            timestamp = time.time()
            if timestamps:
                # The firmware's clock is a wrapping 32-bit microsecond counter,
                # not UTC. Use monotonic elapsed time to also handle multiple
                # wraps while the bus has no traffic (one wrap is 71.6 minutes).
                if self._timestamp_epoch is None:
                    self._timestamp_raw_start = frame.timestamp_us
                    self._timestamp_mono_start = time.monotonic()
                    self._timestamp_epoch = timestamp - frame.timestamp_us / 1000000
                expected = self._timestamp_raw_start + (time.monotonic() - self._timestamp_mono_start) * 1000000
                wraps = max(0, round((expected - frame.timestamp_us) / 2**32))
                timestamp = self._timestamp_epoch + (frame.timestamp_us + wraps * 2**32) / 1000000
            return can.Message(
                timestamp=timestamp, arbitration_id=frame.arbitration_id,
                is_extended_id=frame.is_extended_id,
                is_remote_frame=frame.is_remote_frame, is_error_frame=frame.is_error_frame,
                dlc=frame.can_dlc, data=bytes(frame.data[:frame.can_dlc]),
                is_rx=frame.echo_id == frame_module.GS_USB_NONE_ECHO_ID,
                channel=self.channel_info), False

        def _reset_usb(self):
            device = self.gs_usb.gs_usb
            try:
                # GS_USB_BREQ_MODE, GS_CAN_MODE_RESET, no mode flags.
                device.ctrl_transfer(0x41, 2, 0, 0, struct.pack("<II", 0, 0), timeout=1000)
            finally:
                usb_util.dispose_resources(device)

        def shutdown(self):
            if self._is_shutdown:
                return
            can.BusABC.shutdown(self)
            self._reset_usb()

    return CheckedGsUsbBus(**options)


class DriverCanBus:
    """Nonblocking UI facade. All driver access belongs to one daemon worker."""

    async_tx = True
    MAX_RX_FRAMES = 4096
    MAX_PENDING_TX = 256  # queued + in flight + confirmations awaiting the UI
    RECEIVE_TIMEOUT = 0.001
    SEND_TIMEOUT = 0.1

    def __init__(self, backend, channel, bitrate, fd=False, data_bitrate=2000000,
                 receive_own=False, error_frames=True, filter_id=0, filter_mask=0,
                 filter_extended=None, can1_config=None):
        if backend not in ("pcan", "gs_usb", "controlcanfd"):
            raise ValueError("CAN 后端须为 pcan、gs_usb 或 controlcanfd")
        if not isinstance(channel, str):
            raise ValueError("请选择 CAN 通道")
        channel = channel.strip()
        self._native_ports = None
        self._can1_config = can1_config
        if backend == "pcan":
            channel = channel.upper()
            if not re.fullmatch(r"PCAN_[A-Z]+BUS[1-9][0-9]?", channel):
                raise ValueError("PCAN 通道格式示例：PCAN_USBBUS1")
        elif backend == "controlcanfd":
            if sys.platform != "linux":
                raise ValueError("创芯科技 CANFD 驱动仅支持 Linux")
            from controlcanfd_bus import channel_configs
            _, self._native_ports = channel_configs(channel, bitrate, data_bitrate, fd, can1_config)
        elif not re.fullmatch(r"[0-9]{1,3}", channel):
            raise ValueError("candleLight 通道为从 0 开始的设备序号，例如 0")
        else:
            channel = str(int(channel))
        if type(bitrate) is not int or not 1 <= bitrate <= 1000000:
            raise ValueError("CAN 比特率须为 1..1000000 的整数")
        if backend == "gs_usb" and fd:
            raise ValueError("candleLight (gs_usb) 当前仅支持经典 CAN，不支持 CAN FD")
        if backend == "pcan" and not fd and bitrate not in PCAN_BITRATES:
            raise ValueError("PCAN 不支持此经典 CAN 比特率；请选择列表中的标准比特率")
        if fd and (type(data_bitrate) is not int or not bitrate <= data_bitrate <= 8000000):
            raise ValueError("CAN FD 数据比特率须不低于仲裁比特率，且不超过 8000000")
        _check_id(filter_id, True)
        _check_id(filter_mask, True)
        if filter_extended is not None and type(filter_extended) is not bool:
            raise ValueError("RX 格式须为标准、扩展或任意格式")
        if filter_extended is False:
            _check_id(filter_id, False)
        self.backend, self.channel, self.bitrate = backend, channel, bitrate
        self.fd, self.data_bitrate = bool(fd), data_bitrate
        self.receive_own, self.error_frames = bool(receive_own), bool(error_frames)
        self.filter_id, self.filter_mask = filter_id, filter_mask
        self.filter_extended = filter_extended
        self.state, self.error, self.close_error = "opening", "", ""
        self.closed, self.rx_dropped = False, 0
        self._rx, self._tx, self._sent = deque(), deque(), deque()
        self._inflight = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._work_ready = threading.Event()
        # One worker owns the SDK device, even when it runs both channels.
        self._key = (backend, channel.split(':')[0] if backend == "controlcanfd" else channel)
        with _CHANNEL_LOCK:
            if self._key in _CHANNEL_OWNERS:
                if backend == "controlcanfd":
                    raise RuntimeError("该创芯设备已占用。请关闭连接后选择双通道，再打开一次")
                raise RuntimeError("该 CAN 通道仍在打开或关闭，请等待驱动释放后重试")
            _CHANNEL_OWNERS.add(self._key)
        self._thread = threading.Thread(target=self._run, name=f"CAN-{backend}-{channel}", daemon=True)
        try:
            self._thread.start()
        except Exception:
            with _CHANNEL_LOCK:
                _CHANNEL_OWNERS.discard(self._key)
            raise

    @property
    def is_open(self):
        return self.state == "open"

    def _open(self, can):
        if self.backend == "controlcanfd":
            from controlcanfd_bus import ControlCanFdDriver
            return ControlCanFdDriver(self.channel, self.bitrate, fd=self.fd,
                                      data_bitrate=self.data_bitrate, receive_own=self.receive_own,
                                      can1_config=self._can1_config)
        # Do not inherit python-can's user config: the panel is authoritative.
        options = dict(channel=self.channel, bitrate=self.bitrate,
                       can_filters=None)
        if self.backend == "pcan":
            options.update(receive_own_messages=self.receive_own, auto_reset=False)
            if self.fd:
                options["timing"] = _pcan_fd_timing(can, self.bitrate, self.data_bitrate)
            return _open_pcan(can, options)
        options.update(channel=int(self.channel), index=int(self.channel))
        return _open_gs_usb(can, options)

    def send(self, frame):
        encode_frame(frame)  # reuse the strict TX ID / length / RTR / FD validator
        if self._native_ports is not None:
            from controlcanfd_bus import transmit_channel
            channel = transmit_channel(frame.channel, self._native_ports)
            if frame.fd and not self._native_ports[channel][2]:
                raise ValueError(f"通道 {channel} 未启用 CAN FD")
            if frame.channel is None:
                frame = replace(frame, channel=channel)
        elif frame.fd and not self.fd:
            raise ValueError("请先启用 CAN FD 后再发送 FD 帧")
        with self._lock:
            if self.state != "open" or self._stop.is_set():
                raise RuntimeError(self.error or "CAN 通道未打开")
            if len(self._tx) + len(self._sent) + self._inflight >= self.MAX_PENDING_TX:
                raise BufferError("CAN 发送队列已满，请降低发送速率")
            self._tx.append(frame)
            self._work_ready.set()

    def drain_sent(self, max_count=256):
        with self._lock:
            return [self._sent.popleft() for _ in range(min(max(0, max_count), len(self._sent)))]

    def receive(self):
        with self._lock:
            if self._rx:
                return self._rx.popleft()
            if self.error:
                raise RuntimeError(self.error)
        return None

    def close(self):
        with self._lock:
            self._stop.set()
            self._tx.clear()
            self._work_ready.set()
            if not self.closed and self.state != "error":
                self.state = "closing"

    def _accept(self, msg):
        if self.backend == "controlcanfd":
            if msg.error:
                return self.error_frames
            return ((self.filter_extended is None or msg.extended == self.filter_extended)
                    and (msg.can_id & self.filter_mask) == (self.filter_id & self.filter_mask))
        if not self.receive_own and not msg.is_rx:
            return False
        if msg.is_error_frame:
            return self.error_frames
        return ((self.filter_extended is None or msg.is_extended_id == self.filter_extended)
                and (msg.arbitration_id & self.filter_mask) == (self.filter_id & self.filter_mask))

    def _frame(self, msg):
        if self.backend == "controlcanfd":
            return msg
        return CanFrame(
            can_id=msg.arbitration_id, data=b"" if msg.is_remote_frame else bytes(msg.data),
            dlc=msg.dlc, extended=msg.is_extended_id, remote=msg.is_remote_frame,
            fd=msg.is_fd, bitrate_switch=msg.bitrate_switch,
            error_state_indicator=msg.error_state_indicator,
            error=msg.is_error_frame, timestamp=msg.timestamp)

    def _message(self, can, frame):
        if self.backend == "controlcanfd":
            return frame
        msg = can.Message(arbitration_id=frame.can_id, data=frame.data,
                          is_extended_id=frame.extended, is_remote_frame=frame.remote,
                          is_fd=frame.fd, bitrate_switch=frame.bitrate_switch,
                          dlc=len(frame.data) if frame.dlc is None else frame.dlc,
                          check=True)
        if self.backend == "pcan" and self.fd and frame.remote:
            # python-can 4.6.1 WriteFD assigns DLC bytes even for classic RTR.
            # These ignored driver-buffer bytes are not a remote-frame payload.
            msg.data.extend(bytes(msg.dlc))
        return msg

    def _queue_received(self, frame):
        with self._lock:
            if len(self._rx) >= self.MAX_RX_FRAMES:
                self._rx.popleft()
                self.rx_dropped += 1
            self._rx.append(frame)

    def _run(self):
        driver = None
        try:
            if self._stop.is_set():
                return
            can = None if self.backend == "controlcanfd" else _load_can()
            if self._stop.is_set():
                return
            driver = self._open(can)
            with self._lock:
                if self._stop.is_set():
                    return
                self.state = "open"
            next_status = time.monotonic()
            while not self._stop.is_set():
                # Bound TX work so a busy sender cannot starve receive.
                for _ in range(16):
                    with self._lock:
                        if self._stop.is_set() or not self._tx:
                            break
                        frame = self._tx.popleft()
                        self._inflight = 1
                    driver.send(self._message(can, frame), timeout=self.SEND_TIMEOUT)
                    confirmed = replace(frame, timestamp=time.time())
                    with self._lock:
                        self._sent.append(confirmed)
                        self._inflight = 0
                    if self.backend == "controlcanfd" and self.receive_own and self._accept(confirmed):
                        self._queue_received(confirmed)
                if self._stop.is_set():
                    break
                if self.backend in ("pcan", "controlcanfd") and time.monotonic() >= next_status:
                    status = driver.status()
                    if self.backend == "pcan" and status & 0x10:
                        # PCAN_ERROR_BUSOFF, not SocketCAN CAN_ERR_BUSOFF.
                        raise RuntimeError("PCAN 总线 BUS-OFF，请检查比特率、接线和终端电阻后重新打开")
                    next_status = time.monotonic() + 0.25
                receive_started = time.monotonic()
                msg = driver.recv(timeout=self.RECEIVE_TIMEOUT)
                if msg is None:
                    # Some PCAN warning statuses return immediately despite the
                    # requested timeout. Only wait for its unused portion, and
                    # let queued TX / close wake the worker immediately. Clear
                    # under the queue lock so a concurrent send cannot lose its
                    # wakeup between the empty check and wait.
                    remaining = self.RECEIVE_TIMEOUT - (time.monotonic() - receive_started)
                    if remaining > 0:
                        with self._lock:
                            pending = bool(self._tx) or self._stop.is_set()
                            if not pending:
                                self._work_ready.clear()
                        if not pending:
                            self._work_ready.wait(remaining)
                if msg is not None and self._accept(msg):
                    self._queue_received(self._frame(msg))
        except Exception as exc:
            with self._lock:
                self.error = _driver_error(self.backend, exc)
                self.state = "error"
                self._tx.clear()
                self._inflight = 0
        finally:
            if driver is not None:
                try:
                    driver.shutdown()
                except Exception as exc:
                    with self._lock:
                        self.close_error = _driver_error(self.backend, exc)
            with _CHANNEL_LOCK:
                _CHANNEL_OWNERS.discard(self._key)
            with self._lock:
                self.closed = True
                if not self.error:
                    self.state = "closed" if not self.close_error else "error"

