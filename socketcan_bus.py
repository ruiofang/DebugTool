"""Small, nonblocking Linux SocketCAN transport without extra dependencies.

Interface bitrate and link state belong to the operating system. This module
only opens already configured CAN interfaces; it never changes a network link.
``CanFrame.dlc`` is the payload byte count, including for CAN FD, rather than the
encoded four-bit DLC value used on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
from pathlib import Path
import re
import socket
import struct
import sys
import time


CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF
CAN_ERR_MASK = CAN_EFF_MASK
CANFD_BRS = 0x01
CANFD_ESI = 0x02
CANFD_FDF = 0x04
CAN_MTU = 16
CANFD_MTU = 72
FD_LENGTHS = frozenset((*range(9), 12, 16, 20, 24, 32, 48, 64))

_CLASSIC = struct.Struct("=IBBBB8s")
_FD = struct.Struct("=IBBBB64s")
_UINT32 = struct.Struct("=I")
_TIMESPEC = struct.Struct("@ll")
_SYS_CLASS_NET = Path("/sys/class/net")
_SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
_CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
_CAN_RAW_ERR_FILTER = getattr(socket, "CAN_RAW_ERR_FILTER", 2)
_CAN_RAW_RECV_OWN_MSGS = getattr(socket, "CAN_RAW_RECV_OWN_MSGS", 4)
_CAN_RAW_FD_FRAMES = getattr(socket, "CAN_RAW_FD_FRAMES", 5)
_SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
_SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    data: bytes = b""
    extended: bool = False
    remote: bool = False
    fd: bool = False
    bitrate_switch: bool = False
    error_state_indicator: bool = False
    dlc: int | None = None
    error: bool = False
    timestamp: float | None = None

    def __post_init__(self):
        _validate_frame(self)


def _check_id(can_id, extended=False):
    maximum = CAN_EFF_MASK if extended else CAN_SFF_MASK
    if type(can_id) is not int or not 0 <= can_id <= maximum:
        bits = 29 if extended else 11
        raise ValueError(f"CAN ID 必须是 {bits} 位无符号整数（0..0x{maximum:X}）")


def _validate_frame(frame, *, transmit=False):
    if not isinstance(frame, CanFrame):
        raise TypeError("frame must be a CanFrame")
    _check_id(frame.can_id, frame.extended or frame.error)
    if not isinstance(frame.data, bytes):
        raise TypeError("CAN 数据必须是 bytes")
    length = len(frame.data) if frame.dlc is None else frame.dlc
    if type(length) is not int:
        raise ValueError("CAN 数据长度必须是整数")
    if frame.remote:
        if frame.fd:
            raise ValueError("CAN FD 不支持远程帧（RTR）")
        if frame.data:
            raise ValueError("远程帧不携带数据，请只设置请求长度 DLC")
        if not 0 <= length <= 8:
            raise ValueError("远程帧 DLC 必须在 0..8 之间")
    else:
        if length != len(frame.data):
            raise ValueError("DLC 必须与数据字节数一致")
        if frame.fd:
            if length not in FD_LENGTHS:
                raise ValueError("CAN FD 数据长度必须为 0..8、12、16、20、24、32、48 或 64 字节；不会自动补零")
        elif not 0 <= length <= 8:
            raise ValueError("经典 CAN 数据长度必须在 0..8 字节之间")
    if not frame.fd and (frame.bitrate_switch or frame.error_state_indicator):
        raise ValueError("BRS / ESI 仅适用于 CAN FD")
    if frame.error and (frame.fd or frame.remote):
        raise ValueError("SocketCAN 错误帧必须是经典 CAN 数据帧")
    if transmit and frame.error:
        raise ValueError("错误帧仅供接收，不能发送")
    if transmit and frame.error_state_indicator:
        raise ValueError("ESI 由 CAN 控制器产生，不能手动发送")


def encode_frame(frame: CanFrame) -> bytes:
    """Validate and encode a TX frame using Linux's native CAN socket ABI."""
    _validate_frame(frame, transmit=True)
    can_id = frame.can_id
    if frame.extended:
        can_id |= CAN_EFF_FLAG
    if frame.remote:
        can_id |= CAN_RTR_FLAG
    length = len(frame.data) if frame.dlc is None else frame.dlc
    if frame.fd:
        flags = CANFD_BRS if frame.bitrate_switch else 0
        return _FD.pack(can_id, length, flags, 0, 0, frame.data)
    return _CLASSIC.pack(can_id, length, 0, 0, 0, frame.data)


def decode_frame(packet: bytes, timestamp=None) -> CanFrame:
    """Decode one 16-byte classical or 72-byte FD SocketCAN datagram."""
    if len(packet) == CAN_MTU:
        can_id, length, _padding, _reserved, _raw_dlc, payload = _CLASSIC.unpack(packet)
        fd, flags = False, 0
        if length > 8:
            raise ValueError("收到无效的经典 CAN 数据长度")
    elif len(packet) == CANFD_MTU:
        can_id, length, flags, _reserved0, _reserved1, payload = _FD.unpack(packet)
        fd = True
        if length not in FD_LENGTHS:
            raise ValueError("收到非规范 CAN FD 数据长度")
        if flags & ~(CANFD_BRS | CANFD_ESI | CANFD_FDF):
            raise ValueError("收到未知 CAN FD 标志")
    else:
        raise ValueError(f"SocketCAN 帧长度必须为 16 或 72 字节，实际为 {len(packet)}")
    remote = bool(can_id & CAN_RTR_FLAG)
    return CanFrame(
        can_id=can_id & CAN_EFF_MASK,
        data=b"" if remote else payload[:length],
        extended=bool(can_id & CAN_EFF_FLAG),
        remote=remote,
        fd=fd,
        bitrate_switch=bool(flags & CANFD_BRS),
        error_state_indicator=bool(flags & CANFD_ESI),
        dlc=length,
        error=bool(can_id & CAN_ERR_FLAG),
        timestamp=timestamp,
    )


def parse_hex_payload(text: str) -> bytes:
    """Accept contiguous even HEX or whitespace-separated two-digit bytes.

    Examples: ``AABB00``, ``AA BB 00``, ``0xAA 0xBB 00`` and ``0xAABB00``.
    Separators other than whitespace and incomplete bytes are rejected.
    """
    if not isinstance(text, str):
        raise TypeError("HEX 数据必须是字符串")
    tokens = text.split()
    if not tokens:
        return b""
    values = [token[2:] if token.lower().startswith("0x") else token
              for token in tokens]
    if any(not re.fullmatch(r"[0-9a-fA-F]+", value) or len(value) % 2
           for value in values):
        raise ValueError("HEX 数据须由完整的两位十六进制字节组成")
    if len(values) > 1 and any(len(value) != 2 for value in values):
        raise ValueError("使用空格分隔时，每项必须是一个两位 HEX 字节")
    return bytes.fromhex("".join(values))


def parse_can_id(text: str, extended=False) -> int:
    """Parse an ID as hexadecimal, with or without an explicit 0x prefix."""
    if not isinstance(text, str):
        raise TypeError("CAN ID 必须是字符串")
    value = text.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    if not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ValueError("CAN ID 必须是十六进制数，例如 123 或 0x123")
    result = int(value, 16)
    _check_id(result, extended)
    return result


def _validate_interface_name(name):
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 15
            or name in (".", "..") or any(c in name for c in "/\x00:")
            or any(c.isspace() for c in name)):
        raise ValueError("请输入有效的 CAN 接口名，例如 can0 或 vcan0")


def interface_details(name: str) -> dict:
    """Read sysfs link information; missing values are None / 'unknown'.

    ``up`` reflects IFF_UP, not operstate: working vcan links commonly have
    operstate ``unknown``. No privileged operations or subprocesses are used.
    """
    _validate_interface_name(name)
    base = _SYS_CLASS_NET / name
    result = {"name": name, "state": "unknown", "mtu": None,
              "up": None, "type": None}
    for filename, key in (("operstate", "state"), ("mtu", "mtu"),
                          ("type", "type"), ("flags", "up")):
        try:
            value = (base / filename).read_text(encoding="ascii").strip()
            if filename == "flags":
                result[key] = bool(int(value, 0) & 1)
            else:
                result[key] = value if filename == "operstate" else int(value)
        except (OSError, ValueError, UnicodeError):
            pass
    return result


def list_can_interfaces() -> list[str]:
    """Return physical CAN and vcan interface names (ARPHRD_CAN == 280)."""
    try:
        interfaces = list(_SYS_CLASS_NET.iterdir())
    except OSError:
        return []
    names = []
    for interface in interfaces:
        try:
            if (interface / "type").read_text(encoding="ascii").strip() == "280":
                names.append(interface.name)
        except (OSError, UnicodeError):
            pass
    return sorted(names)


class SocketCanBus:
    """An already-open, bounded, nonblocking raw CAN socket.

    ``receive`` returns None when no datagram is ready. ``rx_dropped`` counts
    kernel receive-queue overflows reported via SO_RXQ_OVFL; this notification
    arrives on a subsequent received packet. It excludes GUI display drops.
    """

    RECEIVE_BUFFER_BYTES = 256 * 1024

    def __init__(self, interface, fd=False, receive_own=False, error_frames=True,
                 filter_id=0, filter_mask=0, filter_extended=None):
        _validate_interface_name(interface)
        _check_id(filter_id, True)
        _check_id(filter_mask, True)
        if filter_extended is not None and type(filter_extended) is not bool:
            raise ValueError("filter_extended must be None, False or True")
        if filter_extended is False:
            _check_id(filter_id, False)
        self.interface = interface
        self.fd = bool(fd)
        self.receive_own = bool(receive_own)
        self.error_frames = bool(error_frames)
        self.filter_id = filter_id
        self.filter_mask = filter_mask
        self.filter_extended = filter_extended
        self.rx_dropped = 0
        self._last_kernel_drops = 0
        self._socket = None
        self._use_recvmsg = False
        self.open()

    @property
    def is_open(self):
        return self._socket is not None

    def open(self):
        if self.is_open:
            return
        if not sys.platform.startswith("linux") or not hasattr(socket, "AF_CAN"):
            raise RuntimeError("SocketCAN 仅支持带有 CAN 驱动的 Linux 系统")
        details = interface_details(self.interface)
        if details["type"] is not None and details["type"] != 280:
            raise ValueError(f"{self.interface} 不是 CAN 网络接口")
        if details["up"] is False:
            raise OSError(errno.ENETDOWN, f"CAN 接口 {self.interface} 尚未启用，请先在系统中配置波特率并设置为 UP")
        if self.fd and details["mtu"] is not None and details["mtu"] < CANFD_MTU:
            raise ValueError(f"{self.interface} 尚未启用 CAN FD（MTU 应为 72），请先在系统中配置 CAN FD")
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW,
                             getattr(socket, "CAN_RAW", 1))
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                            self.RECEIVE_BUFFER_BYTES)
            sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_RECV_OWN_MSGS,
                            int(self.receive_own))
            sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_ERR_FILTER,
                            _UINT32.pack(CAN_ERR_MASK if self.error_frames else 0))
            if self.fd:
                sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_FD_FRAMES, 1)
            can_id, can_mask = self.filter_id, self.filter_mask
            if self.filter_extended is not None:
                can_mask |= CAN_EFF_FLAG
                if self.filter_extended:
                    can_id |= CAN_EFF_FLAG
            sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_FILTER,
                            struct.pack("=II", can_id, can_mask))
            self._use_recvmsg = callable(getattr(sock, "recvmsg", None))
            if self._use_recvmsg:
                for option in (_SO_RXQ_OVFL, _SO_TIMESTAMPNS):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, option, 1)
                    except OSError:
                        # Older kernels may not support optional ancillary data.
                        pass
            sock.bind((self.interface,))
        except BaseException:
            sock.close()
            raise
        self.rx_dropped = 0
        self._last_kernel_drops = 0
        self._socket = sock

    def fileno(self):
        return self._socket.fileno() if self._socket is not None else -1

    def close(self):
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def send(self, frame: CanFrame) -> None:
        packet = encode_frame(frame)
        if frame.fd and not self.fd:
            raise ValueError("当前连接未启用 CAN FD")
        sock = self._socket
        if sock is None:
            raise OSError(errno.EBADF, "CAN 接口未连接")
        if sock.send(packet) != len(packet):
            raise OSError(errno.EIO, "CAN 帧未完整写入内核发送队列")

    def receive(self) -> CanFrame | None:
        sock = self._socket
        if sock is None:
            raise OSError(errno.EBADF, "CAN 接口未连接")
        timestamp = None
        try:
            if self._use_recvmsg:
                ancillary_size = (socket.CMSG_SPACE(_UINT32.size)
                                  + socket.CMSG_SPACE(_TIMESPEC.size))
                packet, ancillary, flags, _address = sock.recvmsg(CANFD_MTU,
                                                                ancillary_size)
                if flags & getattr(socket, "MSG_TRUNC", 0x20):
                    raise ValueError("收到被截断的 CAN 数据报")
                for level, kind, value in ancillary:
                    if level != socket.SOL_SOCKET:
                        continue
                    if kind == _SO_RXQ_OVFL and len(value) >= _UINT32.size:
                        drops = _UINT32.unpack_from(value)[0]
                        self.rx_dropped += (drops - self._last_kernel_drops) & 0xFFFFFFFF
                        self._last_kernel_drops = drops
                    elif kind == _SO_TIMESTAMPNS and len(value) >= _TIMESPEC.size:
                        seconds, nanoseconds = _TIMESPEC.unpack_from(value)
                        timestamp = seconds + nanoseconds / 1_000_000_000
            else:
                packet = sock.recv(CANFD_MTU)
        except BlockingIOError:
            return None
        if timestamp is None:
            timestamp = time.time()
        return decode_frame(packet, timestamp=timestamp)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
