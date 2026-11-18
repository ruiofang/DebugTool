"""Chuang Xin USBCANFD Linux SDK V1.03 transport (device type 41).

Only the driver worker calls this module. It uses ctypes and the vendor SDK,
without python-can, SocketCAN configuration, or changing USB permissions.
Structures and signatures follow the supplied packed C header, not ctypes'
platform-dependent unsigned long defaults in the Python demonstration.
"""

from collections import deque
import ctypes as C
from functools import wraps
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time

from socketcan_bus import (CanFrame, CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG,
                           CAN_EFF_MASK, encode_frame)


DEVICE_TYPE = 41
ARBITRATION_BITRATES = (50000, 100000, 125000, 250000, 500000, 800000, 1000000)
DATA_BITRATES = (100000, 125000, 250000, 500000, 800000, 1000000,
                 2000000, 4000000, 5000000)
SDK_DIRECTORY = Path('lib/controlcanfd')
USB_SYSFS_ROOT = Path('/sys/bus/usb/devices')
USB_DEVICE_ROOT = Path('/dev/bus/usb')
_SDK_LOCK = threading.RLock()


def _matching_usb_devices():
    for entry in USB_SYSFS_ROOT.iterdir():
        try:
            vendor = (entry / 'idVendor').read_text(encoding='ascii').strip().lower()
            product = (entry / 'idProduct').read_text(encoding='ascii').strip().lower()
        except FileNotFoundError:
            # USB interface entries have no device IDs. An adapter may also
            # disappear between listing sysfs and reading its attributes.
            continue
        if (vendor, product) == ('04d8', '0053'):
            yield entry


def discover_channels():
    """List index candidates without opening an adapter or starting a CAN port.

    V1.03's ZCAN_FindUsbDevice internally opens and closes every adapter. Read
    only sysfs USB identifiers here instead. sysfs order is not SDK index order;
    multi-adapter users must confirm the intended SDK device index before open.
    """
    if sys.platform != 'linux':
        raise RuntimeError('创芯科技 CANFD 驱动仅支持 Linux')
    try:
        count = sum(1 for _ in _matching_usb_devices())
    except OSError as exc:
        raise RuntimeError(f'无法读取 USB 设备列表 {USB_SYSFS_ROOT}：{exc}') from exc
    return [f'{device}:{channel}' for device in range(min(count, 8)) for channel in (0, 1)]


def _usb_open_error():
    """Explain a failed SDK open only when sysfs and device nodes agree."""
    try:
        nodes = []
        for device in _matching_usb_devices():
            bus = int((device / 'busnum').read_text(encoding='ascii'))
            address = int((device / 'devnum').read_text(encoding='ascii'))
            nodes.append(USB_DEVICE_ROOT / f'{bus:03d}' / f'{address:03d}')
        if nodes and not any(node.exists() for node in nodes):
            return '系统检测到创芯 USB 设备，但当前运行环境未提供 USB 设备节点。请在宿主机桌面运行。'
        if nodes and all(node.exists() and not os.access(node, os.R_OK | os.W_OK) for node in nodes):
            return ('当前用户没有创芯 USB 设备的读写权限。请执行 sudo ./install.sh --canfd-permissions，'
                    '然后在本机桌面重新打开设备。')
    except (OSError, ValueError):
        pass  # Optional diagnostics must not hide the original SDK failure.
    return '请检查 USB 连接、设备索引和访问权限，并关闭占用设备的厂商软件。'


def _serialized(operation):
    # The vendor library owns process-global USB state. Its API does not
    # promise thread safety, including when different devices are selected.
    @wraps(operation)
    def call(*args, **kwargs):
        with _SDK_LOCK:
            return operation(*args, **kwargs)
    return call


class _Packed(C.Structure):
    _pack_ = 1


class _CanConfig(_Packed):
    _fields_ = [('acc_code', C.c_uint32), ('acc_mask', C.c_uint32),
                ('reserved', C.c_uint32), ('filter', C.c_uint8),
                ('timing0', C.c_uint8), ('timing1', C.c_uint8), ('mode', C.c_uint8)]


class _FdConfig(_Packed):
    _fields_ = [('acc_code', C.c_uint32), ('acc_mask', C.c_uint32),
                ('abit_timing', C.c_uint32), ('dbit_timing', C.c_uint32),
                ('brp', C.c_uint32), ('filter', C.c_uint8), ('mode', C.c_uint8),
                ('pad', C.c_uint16), ('reserved', C.c_uint32)]


class _Config(C.Union):
    _pack_ = 1
    _fields_ = [('can', _CanConfig), ('canfd', _FdConfig)]


class InitConfig(_Packed):
    _fields_ = [('can_type', C.c_uint32), ('config', _Config)]


class ClassicFrame(_Packed):
    _fields_ = [('can_id', C.c_uint32), ('length', C.c_uint8),
                ('pad', C.c_uint8 * 3), ('data', C.c_uint8 * 8)]


class FdFrame(_Packed):
    _fields_ = [('can_id', C.c_uint32), ('length', C.c_uint8),
                ('flags', C.c_uint8), ('pad', C.c_uint8 * 2),
                ('data', C.c_uint8 * 64)]


class ClassicTransmit(_Packed):
    _fields_ = [('frame', ClassicFrame), ('transmit_type', C.c_uint32)]


class FdTransmit(_Packed):
    _fields_ = [('frame', FdFrame), ('transmit_type', C.c_uint32)]


class ClassicReceive(_Packed):
    _fields_ = [('frame', ClassicFrame), ('timestamp', C.c_uint64)]


class FdReceive(_Packed):
    _fields_ = [('frame', FdFrame), ('timestamp', C.c_uint64)]


def parse_channel(channel):
    if not isinstance(channel, str) or not re.fullmatch(r'[0-7]:(?:0\+1|[01])', channel.strip()):
        raise ValueError('创芯 CANFD 通道示例：0:0、0:1 或 0:0+1（双通道）。设备索引为 0–7')
    device, ports = channel.strip().split(':')
    return int(device), tuple(map(int, ports.split('+')))


def validate_bitrates(bitrate, data_bitrate):
    if type(bitrate) is not int or bitrate not in ARBITRATION_BITRATES:
        raise ValueError('创芯 CANFD 仲裁比特率支持 50000、100000、125000、250000、500000、800000、1000000')
    if type(data_bitrate) is not int or data_bitrate not in DATA_BITRATES or data_bitrate < bitrate:
        raise ValueError('创芯 CANFD 数据比特率须不低于仲裁比特率；支持 100000、125000、250000、'
                         '500000、800000、1000000、2000000、4000000、5000000（最高 5M）')


def channel_configs(selection, bitrate, data_bitrate, fd, can1_config=None):
    """Return the device index and settings keyed by physical port name."""
    device, ports = parse_channel(selection)
    if can1_config is not None:
        if len(ports) != 2:
            raise ValueError('CAN1 独立设置仅适用于双通道')
        if not isinstance(can1_config, tuple) or len(can1_config) != 3 or type(can1_config[2]) is not bool:
            raise ValueError('CAN1 独立设置须包含仲裁比特率、数据比特率和 FD 开关')
    configs = {}
    for port in ports:
        config = can1_config if port == 1 and can1_config is not None else (bitrate, data_bitrate, fd)
        try:
            validate_bitrates(*config[:2])
        except ValueError as exc:
            raise ValueError(f'CAN{port}：{exc}') from exc
        configs[f'{device}:{port}'] = config
    return device, configs


def transmit_channel(channel, ports):
    if channel is None and len(ports) == 1:
        return next(iter(ports))
    if channel not in ports:
        raise ValueError('请选择已打开的发送通道')
    return channel


def sdk_arch():
    """Select by process bitness as well as CPU (a 32-bit Python needs 32-bit SDK)."""
    machine = platform.machine().lower()
    bits = C.sizeof(C.c_void_p) * 8
    if sys.byteorder == 'little':
        if machine in ('x86_64', 'amd64', 'i386', 'i486', 'i586', 'i686', 'x86'):
            return 'x86_64' if bits == 64 else 'x86_32'
        if machine in ('aarch64', 'arm64') or machine.startswith('arm'):
            return 'aarch64' if bits == 64 else 'aarch32'
        if machine == 'riscv64' and bits == 64:
            return 'riscv64'
    raise RuntimeError(f'创芯 CANFD SDK 不支持当前架构：{machine}/{bits}bit/{sys.byteorder}')


def library_path():
    arch = sdk_arch()
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    candidate = root / SDK_DIRECTORY / arch / 'libcontrolcanfd.so'
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f'未找到创芯 CANFD {arch} 运行库；请保留 {SDK_DIRECTORY}/{arch}/libcontrolcanfd.so '
                            '并用新版 build.py 重新打包')


def _load_library():
    if sys.platform != 'linux':
        raise RuntimeError('创芯科技 CANFD 驱动仅支持 Linux')
    return C.CDLL(str(library_path()))


def _bind_library(lib):
    # UINT is 32 bits on every SDK target; opaque handles are pointer-sized.
    handle, uint = C.c_void_p, C.c_uint32
    signatures = {
        'ZCAN_OpenDevice': (handle, [uint, uint, uint]),
        'ZCAN_CloseDevice': (uint, [handle]),
        'ZCAN_IsDeviceOnLine': (uint, [handle]),
        'ZCAN_SetAbitBaud': (uint, [handle, uint, uint]),
        'ZCAN_SetDbitBaud': (uint, [handle, uint, uint]),
        'ZCAN_SetCANFDStandard': (uint, [handle, uint, uint]),
        'ZCAN_InitCAN': (handle, [handle, uint, C.POINTER(InitConfig)]),
        'ZCAN_StartCAN': (uint, [handle]),
        'ZCAN_ResetCAN': (uint, [handle]),
        'ZCAN_ClearFilter': (uint, [handle]),
        'ZCAN_Transmit': (uint, [handle, C.POINTER(ClassicTransmit), uint]),
        'ZCAN_TransmitFD': (uint, [handle, C.POINTER(FdTransmit), uint]),
        'ZCAN_Receive': (uint, [handle, C.POINTER(ClassicReceive), uint, C.c_int32]),
        'ZCAN_ReceiveFD': (uint, [handle, C.POINTER(FdReceive), uint, C.c_int32]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    return lib


def _checked(result, operation):
    if result != 1:
        raise RuntimeError(f'{operation} 失败（返回 {result}）')


class _CanChannel:
    """A started SDK channel and its independent receive queues and clock."""
    RECEIVE_BATCH = 64

    def __init__(self, lib, handle, name, fd):
        self._lib, self.handle, self.name, self.fd = lib, handle, name, fd
        self._received = {False: deque(), True: deque()}
        self._timestamp_epoch = None

    def send(self, frame):
        encode_frame(frame)
        if frame.fd and not self.fd:
            raise ValueError(f'通道 {self.name} 未启用 CAN FD')
        packet = FdTransmit() if frame.fd else ClassicTransmit()
        packet.frame.can_id = frame.can_id | (CAN_EFF_FLAG if frame.extended else 0) | (
            CAN_RTR_FLAG if frame.remote else 0)
        packet.frame.length = len(frame.data) if frame.dlc is None else frame.dlc
        if frame.fd:
            packet.frame.flags = 1 if frame.bitrate_switch else 0
        packet.frame.data[:len(frame.data)] = frame.data
        packet.transmit_type = 0  # normal transmit, never enable hardware self-test
        transmit = self._lib.ZCAN_TransmitFD if frame.fd else self._lib.ZCAN_Transmit
        count = transmit(self.handle, C.byref(packet), 1)
        if count != 1:
            raise RuntimeError(f'创芯 CANFD 通道 {self.name} 发送失败：驱动接受 {count}/1 帧')
        # Optional software TX echo belongs to DriverCanBus's bounded RX queue;
        # adding it here could starve both hardware queues under sustained TX.

    def _decode(self, item, fd):
        raw = item.frame
        identifier = raw.can_id
        remote = bool(identifier & CAN_RTR_FLAG)
        if self._timestamp_epoch is None:
            self._timestamp_epoch = time.time() - item.timestamp / 1000000
        return CanFrame(
            can_id=identifier & CAN_EFF_MASK,
            data=b'' if remote else bytes(raw.data[:raw.length]),
            extended=bool(identifier & CAN_EFF_FLAG), remote=remote,
            fd=fd, bitrate_switch=fd and bool(raw.flags & 1),
            error_state_indicator=fd and bool(raw.flags & 2),
            error=bool(identifier & CAN_ERR_FLAG), dlc=raw.length,
            timestamp=self._timestamp_epoch + item.timestamp / 1000000,
            channel=self.name)

    def recv(self):
        for fd in (False, True) if self.fd else (False,):
            queue = self._received[fd]
            if not queue:
                buffer = ((FdReceive if fd else ClassicReceive) * self.RECEIVE_BATCH)()
                receive = self._lib.ZCAN_ReceiveFD if fd else self._lib.ZCAN_Receive
                # Both queues must be polled: ReceiveFD does not return classic
                # frames. wait_time=0 keeps TX/close responsive and never uses -1.
                count = receive(self.handle, buffer, self.RECEIVE_BATCH, 0)
                if count > self.RECEIVE_BATCH:
                    raise RuntimeError(f'创芯 CANFD 通道 {self.name} 接收失败（返回 {count}）')
                queue.extend(buffer[index] for index in range(count))
        available = [fd for fd, queue in self._received.items() if queue]
        if not available:
            return None
        # Refill each empty queue before comparing timestamps. Merging complete
        # batches could deliver FD time 129 ahead of a classic backlog 65..128.
        fd = min(available, key=lambda kind: self._received[kind][0].timestamp)
        return self._decode(self._received[fd].popleft(), fd)


class ControlCanFdDriver:
    """One SDK device handle, owned by one worker, for one or both CAN ports."""

    @_serialized
    def __init__(self, channel, bitrate, fd=False, data_bitrate=2000000, receive_own=False, can1_config=None):
        index, configs = channel_configs(channel, bitrate, data_bitrate, fd, can1_config)
        self._channels = {}
        self._read_order = deque()
        self._device = None
        self._lib = _bind_library(_load_library())
        lib = self._lib
        try:
            self._device = lib.ZCAN_OpenDevice(DEVICE_TYPE, index, 0)
            if not self._device:
                raise RuntimeError('ZCAN_OpenDevice 失败：' + _usb_open_error())
            for name, (arbitration, data, _) in configs.items():
                port = int(name[-1])
                label = f'CAN{port}'
                _checked(lib.ZCAN_SetAbitBaud(self._device, port, arbitration), f'{label} 设置仲裁比特率')
                _checked(lib.ZCAN_SetDbitBaud(self._device, port, data), f'{label} 设置数据比特率')
                _checked(lib.ZCAN_SetCANFDStandard(self._device, port, 0), f'{label} 设置 CAN FD ISO 标准')
            for name, (_, _, use_fd) in configs.items():
                port = int(name[-1])
                label = f'CAN{port}'
                config = InitConfig()
                config.can_type = 1  # The FD adapter also uses TYPE_CANFD for classic traffic.
                handle = lib.ZCAN_InitCAN(self._device, port, C.byref(config))
                if not handle:
                    raise RuntimeError(f'{label} ZCAN_InitCAN 失败')
                self._channels[name] = _CanChannel(lib, handle, name, use_fd)
                _checked(lib.ZCAN_ClearFilter(handle), f'{label} 清除通道滤波')
            for port in self._channels.values():
                _checked(lib.ZCAN_StartCAN(port.handle), f'CAN{port.name[-1]} 启动通道')
            self._read_order.extend(self._channels.values())
        except Exception as exc:
            try:
                self.shutdown()
            except Exception as cleanup:
                raise RuntimeError(f'{exc}；清理失败：{cleanup}') from exc
            raise

    @_serialized
    def send(self, frame, timeout=None):
        if not self._channels:
            raise RuntimeError('创芯 CANFD 通道未打开')
        self._channels[transmit_channel(frame.channel, self._channels)].send(frame)

    @_serialized
    def recv(self, timeout=None):
        if not self._read_order:
            raise RuntimeError('创芯 CANFD 通道未打开')
        # Rotate after each attempt so continuous traffic on CAN0 cannot starve CAN1.
        for _ in range(len(self._read_order)):
            port = self._read_order[0]
            self._read_order.rotate(-1)
            frame = port.recv()
            if frame is not None:
                return frame
        return None

    @_serialized
    def status(self):
        result = self._lib.ZCAN_IsDeviceOnLine(self._device)
        if result != 2:  # STATUS_ONLINE=2, STATUS_OFFLINE=3, not STATUS_OK=1
            raise RuntimeError(f'创芯 CANFD 设备已离线或状态异常（返回 {result}），请检查 USB 连接')

    @_serialized
    def shutdown(self):
        errors = []
        channels, self._channels = self._channels, {}
        self._read_order.clear()
        device, self._device = self._device, None
        for channel in reversed(tuple(channels.values())):
            try:
                _checked(self._lib.ZCAN_ResetCAN(channel.handle), f'复位 CAN{channel.name[-1]}')
            except Exception as exc:
                errors.append(str(exc))
        if device:
            try:
                _checked(self._lib.ZCAN_CloseDevice(device), '关闭 CANFD 设备')
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('；'.join(errors))
