#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DebugTool (PyQt5)
Version: defined in VERSION
Author : RUIO
License: MIT
Features:
  1. 串口刷新
  2. 波特率选择 / 自定义
  3. HEX 发送、校验位 (None/Even/Odd/Mark/Space)
  4. 毫秒级日志，保存日志为 日期.log
  5. 自定义自动回复
  6. 简洁美观界面
  7. 配置文件 (程序同目录 ini) 保存上次设置与历史发送
"""

from __future__ import annotations

import os
import sys
import json
import time
import datetime
import codecs
import struct
import re
import threading
from collections import deque
from pathlib import Path


# The authenticated CAN helper needs no Qt, settings, or application window.
# Keep this before source-venv relaunch and GUI imports for frozen builds too.
if __name__ == "__main__" and sys.argv[1:2] == ["--configure-can"]:
    from can_config import main as configure_can_main
    raise SystemExit(configure_can_main(sys.argv[2:]))


# Direct source launches use the project's installed environment, including
# launches from an editor that still selects the base Python interpreter.
if __name__ == "__main__" and not getattr(sys, "frozen", False):
    _project_venv = Path(__file__).resolve().parent / ".venv"
    _project_python = _project_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if _project_python.is_file() and Path(sys.prefix).resolve() != _project_venv.resolve():
        os.execv(str(_project_python),
                 [str(_project_python), str(Path(__file__).resolve()), *sys.argv[1:]])


from app_meta import APP_NAME, AUTHOR, read_version

__version__ = read_version()
__author__ = AUTHOR


def app_dir() -> str:
    """返回可执行文件 / 脚本所在目录（兼容 PyInstaller 冻结打包）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(sys.executable))
    return os.path.dirname(os.path.realpath(__file__))


def app_icon_path() -> str:
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), "app.png")


def config_path() -> str:
    """返回配置文件路径。

    优先使用程序同目录下的 serial_tool.ini（便携模式）。
    安装器会将 /opt/qt5com 交给指定普通用户，优先在该目录保存。
    若目录或已有配置文件不可写，则回退到用户配置目录：
        Linux/macOS:  ~/.config/qt5com/serial_tool.ini
        Windows:      %APPDATA%/qt5com/serial_tool.ini
    """
    portable = os.path.join(app_dir(), "serial_tool.ini")
    # QSettings 原子保存还需要在父目录创建锁文件和临时文件。
    try:
        if os.access(app_dir(), os.W_OK | os.X_OK):
            if os.path.isfile(portable) and os.access(portable, os.W_OK):
                return portable
            if not os.path.lexists(portable):
                return portable
    except Exception:
        pass

    # 回退到用户目录
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    user_dir = os.path.join(base, "qt5com")
    try:
        os.makedirs(user_dir, exist_ok=True)
    except Exception:
        user_dir = os.path.expanduser("~")
    user_ini = os.path.join(user_dir, "serial_tool.ini")

    # 首次回退时，若程序同目录存在只读的默认配置，则拷贝一份作为初始值
    try:
        if not os.path.isfile(user_ini) and os.path.isfile(portable):
            import shutil
            shutil.copyfile(portable, user_ini)
    except Exception:
        pass
    return user_ini

# ---- 修正中文路径下 Qt 插件路径 ----
if __name__ == "__main__":
    from qt_input_method import select_qt_runtime_for_input_method
    select_qt_runtime_for_input_method()
try:
    import PyQt5
    from PyQt5.QtCore import QLibraryInfo
    _qt_plugin = QLibraryInfo.location(QLibraryInfo.PluginsPath)
    if os.path.isdir(_qt_plugin):
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH",
                              os.path.join(_qt_plugin, "platforms"))
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugin)
except Exception:
    pass

from PyQt5.QtCore import (Qt, QTimer, QThread, pyqtSignal, QSettings,
                          QRegExp, QDateTime, QItemSelectionModel)
from PyQt5.QtGui import (QTextCursor, QFont, QIcon, QRegExpValidator,
                         QTextCharFormat, QTextFormat, QColor, QPalette)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QTextEdit, QPlainTextEdit, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QSplitter, QFileDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QStatusBar, QSpinBox, QTabWidget, QAction,
    QStyleFactory, QToolButton, QSizePolicy, QAbstractItemView,
    QFormLayout, QInputDialog, QStackedWidget,
)

import serial
import serial.tools.list_ports
from ssh_panel import SSHPanel
from local_terminal import LocalTerminalPanel
from network_panel import NetworkPanel
from serial_terminal import SerialTerminalPanel
from serial_access import describe_serial_open_error
from async_log import AsyncLogWriter
from can_panel import CanPanel
from quick_command_table import QuickCommandTable


# ------------------------------------------------------------------ #
#  工具函数
# ------------------------------------------------------------------ #
def _linux_uart_is_real(device: str) -> bool:
    """Check whether the kernel reports actual UART hardware for ttyS."""
    name = os.path.basename(device)
    if not re.fullmatch(r"ttyS\d+", name):
        return True
    index = name[4:]
    # Allwinner's vendor kernel reports its UARTs under "uart", while
    # desktop 8250 drivers use "serial".
    for report in ("serial", "uart"):
        try:
            text = Path(f"/proc/tty/driver/{report}").read_text(
                encoding="ascii", errors="ignore")
        except OSError:
            continue
        match = re.search(rf"(?m)^{re.escape(index)}:\s+uart:(\S+)", text)
        if match and match.group(1).lower() != "unknown":
            return True
    return False


def enumerate_serial_ports():
    ports = {p.device: p for p in serial.tools.list_ports.comports()}
    if sys.platform.startswith("linux"):
        # pyserial excludes the platform subsystem, including real SUNXI
        # UARTs. Only add nodes confirmed by the kernel hardware report.
        from serial.tools.list_ports_common import ListPortInfo
        for node in Path("/dev").glob("ttyS*"):
            device = str(node)
            if device not in ports and _linux_uart_is_real(device):
                port = ListPortInfo(device)
                port.description = "板载 UART"
                ports[device] = port
    return sorted(ports.values(), key=lambda p: p.device)


def _probe_linux_tty(device: str) -> tuple[bool, str]:
    """Verify that a tty can accept its current termios settings.

    This catches legacy ttyS nodes which the kernel advertises as a UART but
    which fail with EIO when pyserial tries to configure them. The existing
    settings are written back unchanged; no application data is sent.
    """
    try:
        import termios
        flags = os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK
        fd = os.open(device, flags)
        try:
            attrs = termios.tcgetattr(fd)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        finally:
            os.close(fd)
    except OSError as exc:
        return False, f"端口配置探测失败：{exc.strerror or exc}"
    return True, "可用"


def serial_port_usability(port) -> tuple[bool, str]:
    """Classify an enumerated port without opening it or toggling DTR/RTS."""
    device = str(getattr(port, "device", "") or "")
    if not device:
        return False, "设备路径为空"
    if sys.platform.startswith("linux"):
        if not os.path.exists(device):
            return False, "设备节点不存在"
        if not os.access(device, os.R_OK | os.W_OK):
            return False, "当前用户无读写权限"
        name = os.path.basename(device)
        if name in ("tty", "ttyprintk") or name.startswith(("pts", "ptmx")):
            return False, "系统虚拟终端"
        if re.fullmatch(r"ttyS\d+", name):
            try:
                consoles = Path("/sys/class/tty/console/active").read_text().split()
            except OSError:
                consoles = []
            if name in consoles:
                return False, "系统控制台正在使用"
            if not _linux_uart_is_real(device):
                return False, "内核未检测到 UART 硬件"
            usable, reason = _probe_linux_tty(device)
            if not usable:
                return False, reason
    elif sys.platform == "darwin":
        # macOS exposes tty.* and cu.* pairs; cu.* is intended for initiating
        # outgoing serial connections and avoids duplicate devices.
        if device.startswith("/dev/tty."):
            return False, "与 cu.* 重复的呼入端口"
        if not os.path.exists(device) or not os.access(device, os.R_OK | os.W_OK):
            return False, "设备不存在或无读写权限"
    return True, "可用"


def open_serial_connection(**kwargs) -> serial.Serial:
    """Open one port exclusively while allowing independent app instances."""
    if os.name == "posix":
        kwargs["exclusive"] = True
    return serial.Serial(**kwargs)


def hex_str_to_bytes(text: str) -> bytes:
    """将 '01 A2 FF' 或 '01A2FF' 形式的字符串转为 bytes。"""
    clean = "".join(ch for ch in text if ch in "0123456789abcdefABCDEF")
    if len(clean) % 2:
        clean = "0" + clean
    return bytes.fromhex(clean) if clean else b""


def bytes_to_hex_str(data: bytes) -> str:
    return data.hex(" ").upper()


def now_ms() -> str:
    t = datetime.datetime.now()
    return t.strftime("%H:%M:%S.") + f"{t.microsecond // 1000:03d}"


# -------------------- 校验算法 -------------------- #
def calc_sum(data: bytes) -> bytes:
    return bytes([sum(data) & 0xFF])


def calc_xor(data: bytes) -> bytes:
    r = 0
    for b in data:
        r ^= b
    return bytes([r & 0xFF])


def calc_crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    # Modbus: 低字节在前
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def calc_crc16_ccitt(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    # 高字节在前
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])


CHECKSUM_TYPES = ["无", "SUM (1B)", "XOR (1B)", "CRC16-Modbus", "CRC16-CCITT"]

MODBUS_FUNCTIONS = [
    ("01 读线圈", 0x01),
    ("02 读离散输入", 0x02),
    ("03 读保持寄存器", 0x03),
    ("04 读输入寄存器", 0x04),
    ("05 写单个线圈", 0x05),
    ("06 写单个寄存器", 0x06),
    ("15 写多个线圈", 0x0F),
    ("16 写多个寄存器", 0x10),
]

MODBUS_FUNCTION_TEXT = {code: text for text, code in MODBUS_FUNCTIONS}
MODBUS_EXCEPTION_TEXT = {
    0x01: "非法功能码",
    0x02: "非法数据地址",
    0x03: "非法数据值",
    0x04: "从站设备故障",
    0x05: "确认",
    0x06: "从站忙",
    0x08: "存储奇偶校验错误",
    0x0A: "网关路径不可用",
    0x0B: "网关目标设备无响应",
}


def apply_checksum(data: bytes, ctype: str, start: int, end: int) -> bytes:
    """追加校验字节。start/end 为 1-based 闭区间，end<=0 表示到末尾。"""
    if ctype == "无" or not data:
        return data
    n = len(data)
    s = max(0, start - 1)
    e = n if end <= 0 else min(n, end)
    if s >= e:
        return data
    seg = data[s:e]
    if ctype.startswith("SUM"):
        chk = calc_sum(seg)
    elif ctype.startswith("XOR"):
        chk = calc_xor(seg)
    elif ctype == "CRC16-Modbus":
        chk = calc_crc16_modbus(seg)
    elif ctype == "CRC16-CCITT":
        chk = calc_crc16_ccitt(seg)
    else:
        chk = b""
    return data + chk


def parse_number_list(text: str) -> list[int]:
    values = []
    for item in text.replace("\n", ",").replace(";", ",").split(","):
        token = item.strip()
        if not token:
            continue
        values.append(int(token, 0))
    return values


def coils_to_bytes(values: list[int]) -> bytes:
    packed = bytearray((len(values) + 7) // 8)
    for idx, value in enumerate(values):
        if value:
            packed[idx // 8] |= 1 << (idx % 8)
    return bytes(packed)


def bytes_to_coils(data: bytes, quantity: int) -> list[int]:
    values = []
    for idx in range(quantity):
        values.append(1 if data[idx // 8] & (1 << (idx % 8)) else 0)
    return values


def build_modbus_rtu_request(slave_id: int, function_code: int, address: int,
                             quantity: int, values: list[int] | None = None) -> bytes:
    if not (0 <= slave_id <= 255):
        raise ValueError("从站地址必须在 0~255 之间。")
    if not (0 <= address <= 0xFFFF):
        raise ValueError("寄存器/线圈地址必须在 0~65535 之间。")

    if function_code in (0x01, 0x02):
        if not (1 <= quantity <= 2000):
            raise ValueError("读线圈/离散输入数量必须在 1~2000 之间。")
        pdu = struct.pack(">BHH", function_code, address, quantity)
    elif function_code in (0x03, 0x04):
        if not (1 <= quantity <= 125):
            raise ValueError("读寄存器数量必须在 1~125 之间。")
        pdu = struct.pack(">BHH", function_code, address, quantity)
    elif function_code == 0x05:
        if not values or len(values) != 1 or values[0] not in (0, 1):
            raise ValueError("写单个线圈需要提供 0 或 1。")
        coil = 0xFF00 if values[0] else 0x0000
        pdu = struct.pack(">BHH", function_code, address, coil)
    elif function_code == 0x06:
        if not values or len(values) != 1:
            raise ValueError("写单个寄存器需要提供 0~65535 数值。")
        if not (0 <= values[0] <= 0xFFFF):
            raise ValueError("寄存器值必须在 0~65535 之间。")
        pdu = struct.pack(">BHH", function_code, address, values[0])
    elif function_code == 0x0F:
        if not values:
            raise ValueError("写多个线圈需要提供 0/1 列表。")
        if not all(v in (0, 1) for v in values):
            raise ValueError("线圈值只能是 0 或 1。")
        quantity = len(values)
        if not (1 <= quantity <= 1968):
            raise ValueError("写多个线圈数量必须在 1~1968 之间。")
        payload = coils_to_bytes(values)
        pdu = struct.pack(">BHHB", function_code, address, quantity, len(payload)) + payload
    elif function_code == 0x10:
        if not values:
            raise ValueError("写多个寄存器需要提供数值列表。")
        if not all(0 <= v <= 0xFFFF for v in values):
            raise ValueError("寄存器值必须在 0~65535 之间。")
        quantity = len(values)
        if not (1 <= quantity <= 123):
            raise ValueError("写多个寄存器数量必须在 1~123 之间。")
        payload = b"".join(struct.pack(">H", value) for value in values)
        pdu = struct.pack(">BHHB", function_code, address, quantity, len(payload)) + payload
    else:
        raise ValueError("当前仅支持 01/02/03/04/05/06/15/16 功能码。")

    span = 1 if function_code in (5, 6) else quantity
    if address + span > 65536:
        raise ValueError("请求地址范围超出 65535。")
    frame = bytes([slave_id]) + pdu
    return frame + calc_crc16_modbus(frame)


def parse_modbus_rtu_response(request: dict, response: bytes) -> dict:
    if len(response) < 5:
        raise ValueError("响应长度不足。")
    body = response[:-2]
    if calc_crc16_modbus(body) != response[-2:]:
        raise ValueError("CRC 校验失败。")

    slave_id = response[0]
    function_code = response[1]
    if slave_id != request["slave_id"]:
        raise ValueError(f"从站地址不匹配：期望 {request['slave_id']}，收到 {slave_id}。")

    if function_code & 0x80:
        if function_code != request["function_code"] | 0x80 or len(response) != 5:
            raise ValueError("异常响应功能码或长度不匹配。")
        exc_code = response[2] if len(response) >= 5 else None
        exc_text = MODBUS_EXCEPTION_TEXT.get(exc_code, "未知异常")
        raise ValueError(f"从站异常 0x{exc_code:02X}: {exc_text}")
    if function_code != request["function_code"]:
        raise ValueError(f"功能码不匹配：期望 0x{request['function_code']:02X}，收到 0x{function_code:02X}。")

    result = {
        "slave_id": slave_id,
        "function_code": function_code,
        "function_text": MODBUS_FUNCTION_TEXT.get(function_code, f"0x{function_code:02X}"),
    }

    if function_code in (0x01, 0x02):
        byte_count = response[2]
        payload = response[3:-2]
        if byte_count != len(payload) or byte_count != (request["quantity"] + 7) // 8:
            raise ValueError("字节计数与响应长度不一致。")
        result["values"] = bytes_to_coils(payload, request["quantity"])
    elif function_code in (0x03, 0x04):
        byte_count = response[2]
        payload = response[3:-2]
        if byte_count != len(payload) or byte_count != request["quantity"] * 2:
            raise ValueError("寄存器响应字节数无效。")
        result["values"] = [
            struct.unpack(">H", payload[idx:idx + 2])[0]
            for idx in range(0, len(payload), 2)
        ]
    elif function_code in (0x05, 0x06):
        if len(response) != 8:
            raise ValueError("写响应长度无效。")
        address, value = struct.unpack(">HH", response[2:6])
        expected = (0xFF00 if request["values"][0] else 0) if function_code == 5 else request["values"][0]
        if address != request["address"] or value != expected:
            raise ValueError("写响应地址或数值不匹配。")
        result["address"] = address
        result["value"] = value
    elif function_code in (0x0F, 0x10):
        if len(response) != 8:
            raise ValueError("写响应长度无效。")
        address, quantity = struct.unpack(">HH", response[2:6])
        if address != request["address"] or quantity != len(request["values"]):
            raise ValueError("写响应地址或数量不匹配。")
        result["address"] = address
        result["quantity"] = quantity

    return result


def _read_mbp_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read an MFC Unicode CString, preserving empty/duplicate register names."""
    if data[offset:offset + 3] != b"\xff\xfe\xff":
        raise ValueError("不支持的 MBP 字符串格式。")
    offset += 3
    size = data[offset]
    offset += 1
    if size == 0xFF:
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if size == 0xFFFF:
            size = struct.unpack_from("<I", data, offset)[0]
            offset += 4
    end = offset + size * 2
    if end > len(data):
        raise ValueError("MBP 字符串被截断。")
    return data[offset:end].decode("utf-16le").strip(), end


def parse_binary_mbp_profile(data: bytes) -> dict | None:
    """Import the Modbus Poll 0x2454 layout verified with mbp/夹爪.mbp.

    Header: version, header tag, function, wire address, quantity.
    Names follow 52-byte cell styles; a 0x55555555 separator precedes
    the saved uint16 register array. Later sections contain 100 scale
    records (50 bytes each), 128 bytes of flags, 2000 CStrings, then
    the scan interval and slave ID. Reject other layouts, never guess.
    """
    try:
        magic, tag, function, address, quantity = struct.unpack_from("<5I", data)
        if (magic, tag) != (0x2454, 0xA8):
            return None
        limit = 2000 if function in (1, 2) else 125 if function in (3, 4) else 0
        if not 1 <= quantity <= limit or address + quantity > 65536:
            return None
        offset = 520
        names = []
        for _ in range(quantity):
            name, offset = _read_mbp_string(data, offset + 52)
            names.append(name)
        if data[offset:offset + 4] != b"UUUU":
            return None
        offset += 4
        values = struct.unpack_from(f"<{quantity}H", data, offset)
        offset += quantity * 2 + 100 * 50 + 128
        for _ in range(2000):
            _, offset = _read_mbp_string(data, offset)
        # This version has a 240-byte settings trailer (including one CString).
        if len(data) - offset != 240:
            return None
        scan_rate, slave_id = struct.unpack_from("<2I", data, offset)
        if not 1 <= slave_id <= 247 or not 1 <= scan_rate <= 3600000:
            return None
    except (ValueError, IndexError, struct.error, UnicodeError):
        return None
    return {
        "format": "binary-mbp", "magic": magic,
        "slave_id": slave_id, "function_code": function,
        "function_text": MODBUS_FUNCTION_TEXT[function],
        "start_address": address, "quantity": quantity,
        "scan_rate": scan_rate, "signed": True,
        "entries": [
            {"index": i, "address": address + i, "value": value, "comment": names[i]}
            for i, value in enumerate(values)
        ],
        "has_explicit_values": True, "notes_only": False,
    }


# ------------------------------------------------------------------ #
#  串口读取线程
# ------------------------------------------------------------------ #
class SerialReader(QThread):
    error = pyqtSignal(str)
    FLUSH_INTERVAL = 0.020
    MAX_CHUNK_BYTES = 4096
    MAX_PENDING_BYTES = 1024 * 1024

    def __init__(self, ser: serial.Serial, parent=None):
        super().__init__(parent)
        self.ser = ser
        self._running = True
        self.log_writer = None
        self.hex_mode = False
        self._lock = threading.Lock()
        self._pending = deque()
        self._pending_bytes = 0
        self._received_bytes = 0
        self._dropped_bytes = 0

    def _publish(self, data):
        # Log before applying the UI limit. No Qt event is posted per packet:
        # the GUI pulls a bounded batch on its own timer.
        writer = self.log_writer
        if writer is not None:
            writer.write_received(data, self.hex_mode)
        with self._lock:
            self._received_bytes += len(data)
            self._pending.append(data)
            self._pending_bytes += len(data)
            while self._pending_bytes > self.MAX_PENDING_BYTES:
                first = self._pending.popleft()
                removed = min(len(first), self._pending_bytes - self.MAX_PENDING_BYTES)
                if removed < len(first):
                    self._pending.appendleft(first[removed:])
                self._pending_bytes -= removed
                self._dropped_bytes += removed

    def take_received(self, max_bytes):
        chunks = []
        with self._lock:
            while self._pending and max_bytes > 0:
                chunk = self._pending.popleft()
                if len(chunk) > max_bytes:
                    self._pending.appendleft(chunk[max_bytes:])
                    chunk = chunk[:max_bytes]
                chunks.append(chunk)
                self._pending_bytes -= len(chunk)
                max_bytes -= len(chunk)
            received, dropped = self._received_bytes, self._dropped_bytes
            self._received_bytes = self._dropped_bytes = 0
        return chunks, received, dropped

    def run(self):
        buf = bytearray()
        last_flush = time.monotonic()
        while self._running:
            try:
                n = self.ser.in_waiting
                if n:
                    chunk = self.ser.read(min(n, self.MAX_CHUNK_BYTES - len(buf)))
                    buf.extend(chunk)
                # 连续输入也必须交付，不能依赖串口出现空闲间隙。
                now = time.monotonic()
                if buf and (not n or len(buf) >= self.MAX_CHUNK_BYTES or
                            now - last_flush >= self.FLUSH_INTERVAL):
                    self._publish(bytes(buf))
                    buf.clear()
                    last_flush = now
                if not n:
                    self.msleep(1)
            except Exception as e:
                self.error.emit(str(e))
                break
        if buf:
            self._publish(bytes(buf))

    def stop(self):
        self._running = False
        self.wait(1000)


# ------------------------------------------------------------------ #
#  主窗口
# ------------------------------------------------------------------ #
class DebugTool(QMainWindow):
    CAN_MODE = 4
    (QUICK_COL_LOOP, QUICK_COL_NAME, QUICK_COL_CONTENT,
     QUICK_COL_HEX, QUICK_COL_NEWLINE, QUICK_COL_SEND) = range(6)
    COMMON_BAUDS = ["1200", "2400", "4800", "9600", "19200", "38400",
                    "57600", "115200", "230400", "460800", "921600"]
    DATA_BITS = ["8", "7", "6", "5"]
    PARITY_MAP = {
        "None (N)":  serial.PARITY_NONE,
        "Even (E)":  serial.PARITY_EVEN,
        "Odd  (O)":  serial.PARITY_ODD,
        "Mark (M)":  serial.PARITY_MARK,
        "Space (S)": serial.PARITY_SPACE,
    }
    STOP_MAP = {
        "1":   serial.STOPBITS_ONE,
        "1.5": serial.STOPBITS_ONE_POINT_FIVE,
        "2":   serial.STOPBITS_TWO,
    }
    MAX_HISTORY = 30
    MAX_DISPLAY_BLOCKS = 2000
    MAX_DISPLAY_CHARS = 512 * 1024
    MAX_DISPLAY_LINE_CHARS = 1024
    SERIAL_UI_BATCH_BYTES = 32 * 1024
    LOG_COLOR_PROPERTY = QTextFormat.UserProperty + 1
    DARK_LOG_COLORS = {
        "#202020": "#e6e6e6",  # 系统提示
        "#0d47a1": "#8ab4f8",  # 发送
        "#1b5e20": "#81c995",  # 接收
        "#c0392b": "#ff8a80",  # 错误
        "#6a1b9a": "#ce93d8",  # 自动回复
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DebugTool")
        icon_path = app_icon_path()
        self.setWindowIcon(
            QIcon(icon_path) if os.path.isfile(icon_path)
            else QIcon.fromTheme("qt5com"))
        self.resize(1200, 900)

        self.ser: serial.Serial | None = None
        self.reader: SerialReader | None = None
        self.log_file = None
        self._reported_log_error = None
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.rx_display_dropped = 0
        self.pending_modbus_request = None
        self.modbus_rx_buffer = bytearray()
        self.modbus_entries = []
        self.modbus_table_context = None
        self.modbus_timeout = QTimer(self)
        self.modbus_timeout.setSingleShot(True)
        self.modbus_timeout.timeout.connect(self._on_modbus_timeout)
        self.modbus_poll_timer = QTimer(self)
        self.modbus_poll_timer.timeout.connect(self._poll_modbus)

        # 配置文件：优先程序同目录（便携），不可写时自动回退到用户目录
        self._config_path = config_path()
        self.settings = QSettings(self._config_path, QSettings.IniFormat)

        self.quick_loop_timer = QTimer(self)
        self.quick_loop_timer.setSingleShot(True)
        self.quick_loop_timer.setTimerType(Qt.PreciseTimer)
        self.quick_loop_timer.timeout.connect(self._send_next_quick)
        self._quick_loop_running = False
        self._quick_loop_commands = []
        self._quick_loop_index = 0
        self._quick_loop_round = 0
        self._quick_loop_sent = 0
        self._quick_loop_round_limit = 0

        self._build_ui()
        self._apply_style()
        self._load_settings()

        self.serial_receive_timer = QTimer(self)
        self.serial_receive_timer.setInterval(20)
        self.serial_receive_timer.timeout.connect(self._drain_serial_data)
        self.chk_rx_hex.toggled.connect(self._set_serial_hex_mode)
        self.log_error_timer = QTimer(self)
        self.log_error_timer.setInterval(250)
        self.log_error_timer.timeout.connect(self._check_log_error)

        # 启动时刷新一次，之后仅手动刷新
        self.refresh_ports()

        # 自动发送定时器
        self.auto_send_timer = QTimer(self)
        self.auto_send_timer.setTimerType(Qt.PreciseTimer)
        self.auto_send_timer.timeout.connect(self.on_send_clicked)
        self.spn_interval.valueChanged.connect(self.auto_send_timer.setInterval)
        self.transport_mode.setCurrentIndex(max(0, min(self.CAN_MODE, self.settings.value('transport_mode', 0, type=int))))
        self._on_transport_changed(self.transport_mode.currentIndex())

    # -------------------------------------------------------------- #
    #  UI
    # -------------------------------------------------------------- #
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self.can_panel = CanPanel(self.settings)

        # ============ 左侧：串口配置 ============
        cfg_box = QGroupBox("串口配置")
        g = QGridLayout(cfg_box)
        g.setVerticalSpacing(6)
        g.setHorizontalSpacing(6)

        self.cmb_port = QComboBox()
        self.cmb_port.setMinimumWidth(150)
        self.btn_refresh = QToolButton()
        self.btn_refresh.setObjectName("refreshBtn")
        self.btn_refresh.setText("⟳")
        self.btn_refresh.setToolTip("刷新串口")
        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.chk_show_all_ports = QCheckBox("显示全部（含不可用 ttyS）")
        self.chk_show_all_ports.setToolTip(
            "显示系统枚举的全部端口，包括无权限、虚拟端口及未检测到硬件的 ttyS 端口")
        self.chk_show_all_ports.toggled.connect(self.refresh_ports)

        self.cmb_baud = QComboBox()
        self.cmb_baud.setEditable(True)  # 支持自定义
        self.cmb_baud.addItems(self.COMMON_BAUDS)
        self.cmb_baud.setValidator(QRegExpValidator(QRegExp(r"\d{1,7}")))

        self.cmb_data = QComboBox(); self.cmb_data.addItems(self.DATA_BITS)
        self.cmb_parity = QComboBox(); self.cmb_parity.addItems(list(self.PARITY_MAP.keys()))
        self.cmb_stop = QComboBox(); self.cmb_stop.addItems(list(self.STOP_MAP.keys()))

        self.btn_open = QPushButton("打开串口")
        self.btn_open.setCheckable(True)
        self.btn_open.setObjectName("openBtn")
        self.btn_open.clicked.connect(self.toggle_port)

        row = 0
        g.addWidget(QLabel("端口"), row, 0)
        g.addWidget(self.cmb_port, row, 1)
        g.addWidget(self.btn_refresh, row, 2); row += 1
        g.addWidget(self.chk_show_all_ports, row, 1, 1, 2); row += 1
        g.addWidget(QLabel("波特率"), row, 0); g.addWidget(self.cmb_baud, row, 1, 1, 2); row += 1
        g.addWidget(QLabel("数据位"), row, 0); g.addWidget(self.cmb_data, row, 1, 1, 2); row += 1
        g.addWidget(QLabel("校验位"), row, 0); g.addWidget(self.cmb_parity, row, 1, 1, 2); row += 1
        g.addWidget(QLabel("停止位"), row, 0); g.addWidget(self.cmb_stop, row, 1, 1, 2); row += 1
        g.addWidget(self.btn_open, row, 0, 1, 3); row += 1

        # ============ 显示选项 ============
        disp_box = QGroupBox("显示 / 日志")
        dl = QGridLayout(disp_box)
        self.chk_rx_hex = QCheckBox("接收 HEX 显示")
        self.chk_show_time = QCheckBox("显示时间戳 (ms)")
        self.chk_show_time.setChecked(True)
        self.chk_show_time.setToolTip("同时显示时间和 >> / << 方向前缀；关闭后保留数据和帧信息。")
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        self.chk_log_save = QCheckBox("保存日志到文件")
        self.btn_log_dir = QPushButton("日志目录…")
        self.btn_log_dir.clicked.connect(self.choose_log_dir)
        self.btn_clear = QPushButton("清空接收")
        self.btn_clear.clicked.connect(lambda: self.recv_edit.clear())

        self.cmb_theme = QComboBox()
        self.cmb_theme.addItems(["浅色", "深色"])
        self.cmb_theme.currentTextChanged.connect(self._on_theme_changed)

        dl.addWidget(self.chk_rx_hex,     0, 0)
        dl.addWidget(self.chk_show_time,  0, 1)
        dl.addWidget(self.chk_autoscroll, 1, 0)
        dl.addWidget(self.chk_log_save,   1, 1)
        dl.addWidget(self.btn_log_dir,    2, 0)
        dl.addWidget(self.btn_clear,      2, 1)
        dl.addWidget(QLabel("皮肤"),      3, 0)
        dl.addWidget(self.cmb_theme,      3, 1)

        # ============ 发送区 ============
        send_box = QGroupBox("发送")
        sv = QVBoxLayout(send_box)
        sv.addWidget(self.can_panel.send_options)
        self.can_panel.send_options.hide()

        # ---- 第 1 行：多行输入框（上移、加高） ----
        self.send_edit = QPlainTextEdit()
        self.send_edit.setPlaceholderText(
            "在此输入待发送内容（支持多行）…\nHEX 模式下请输入十六进制，如：AA 55 01 02")
        self._default_send_placeholder = self.send_edit.placeholderText()
        self.send_edit.setMinimumHeight(140)
        sv.addWidget(self.send_edit, 1)

        # ---- 第 2 行：历史 + 循环发送 + 发送按钮 ----
        hist_row = QHBoxLayout()
        self.cmb_history = QComboBox()
        self.cmb_history.setEditable(False)
        # Restored long commands must not become the right pane's minimum
        # width and leave no room to drag the horizontal splitter.
        self.cmb_history.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_history.setMinimumContentsLength(12)
        self.cmb_history.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.cmb_history.activated.connect(self.on_history_selected)
        self.btn_history_delete = QToolButton()
        self.btn_history_delete.setText("删除所选")
        self.btn_history_delete.setToolTip("删除当前选中的发送历史，保留发送区内容")
        self.btn_history_delete.clicked.connect(self.on_history_delete)
        self.btn_history_clear = QToolButton()
        self.btn_history_clear.setText("清空历史")
        self.btn_history_clear.setToolTip("清空全部发送历史并立即保存，保留发送区内容")
        self.btn_history_clear.clicked.connect(self.on_history_clear)

        self.chk_auto_send = QCheckBox("循环发送")
        self.spn_interval = QSpinBox()
        self.spn_interval.setRange(1, 600000)
        self.spn_interval.setValue(1000)
        self.spn_interval.setSuffix(" ms")
        self.spn_interval.setToolTip("循环发送间隔，最小 1 ms；循环运行时也可修改。")
        self.spn_interval.setFixedWidth(130)
        self.chk_auto_send.toggled.connect(self.toggle_auto_send)

        self.btn_send = QPushButton("发  送")
        self.btn_send.setObjectName("sendBtn")
        self.btn_send.clicked.connect(self.on_send_clicked)

        hist_row.addWidget(QLabel("历史:"))
        hist_row.addWidget(self.cmb_history, 1)
        hist_row.addWidget(self.btn_history_delete)
        hist_row.addWidget(self.btn_history_clear)
        hist_row.addSpacing(8)
        hist_row.addWidget(self.chk_auto_send)
        hist_row.addWidget(self.spn_interval)
        hist_row.addSpacing(8)
        hist_row.addWidget(self.btn_send)
        sv.addLayout(hist_row)

        # ---- 第 3 行：发送选项 ----
        opt_row = QHBoxLayout()
        self.chk_tx_hex = QCheckBox("HEX 发送")
        self.chk_tx_newline = QCheckBox("附加 \\r\\n")
        opt_row.addWidget(self.chk_tx_hex)
        opt_row.addWidget(self.chk_tx_newline)
        opt_row.addStretch()
        sv.addLayout(opt_row)

        # ---- 第 4 行：校验设置 ----
        chk_row = QHBoxLayout()
        self.chk_checksum = QCheckBox("附加校验")
        self.cmb_checksum = QComboBox()
        self.cmb_checksum.addItems(CHECKSUM_TYPES)
        self.cmb_checksum.setCurrentText("SUM (1B)")
        self.spn_chk_start = QSpinBox()
        self.spn_chk_start.setRange(1, 9999)
        self.spn_chk_start.setValue(1)
        self.spn_chk_start.setPrefix("起:")
        self.spn_chk_end = QSpinBox()
        self.spn_chk_end.setRange(0, 9999)
        self.spn_chk_end.setValue(0)
        self.spn_chk_end.setPrefix("止:")
        self.spn_chk_end.setToolTip("0 表示到数据末尾")
        chk_row.addWidget(self.chk_checksum)
        chk_row.addWidget(self.cmb_checksum)
        chk_row.addWidget(self.spn_chk_start)
        chk_row.addWidget(self.spn_chk_end)
        chk_row.addWidget(QLabel("(字节, 1-based)"))
        chk_row.addStretch()
        sv.addLayout(chk_row)

        # ============ 自动回复 ============
        auto_box = QGroupBox("自动回复（匹配到接收内容后自动发送）")
        av = QVBoxLayout(auto_box)
        top_row = QHBoxLayout()
        self.chk_auto_reply = QCheckBox("启用自动回复")
        self.chk_reply_hex_match = QCheckBox("按 HEX 匹配")
        self.chk_reply_hex_send = QCheckBox("按 HEX 发送")
        top_row.addWidget(self.chk_auto_reply)
        top_row.addWidget(self.chk_reply_hex_match)
        top_row.addWidget(self.chk_reply_hex_send)
        top_row.addStretch()
        av.addLayout(top_row)

        self.reply_table = QTableWidget(0, 3)
        self.reply_table.setHorizontalHeaderLabels(["启用", "触发内容 (收到)", "回复内容 (发送)"])
        hh = self.reply_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.reply_table.verticalHeader().setDefaultSectionSize(26)
        self.reply_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        av.addWidget(self.reply_table)

        btn_row = QHBoxLayout()
        self.btn_add_rule = QPushButton("添加")
        self.btn_del_rule = QPushButton("删除所选")
        self.btn_add_rule.clicked.connect(lambda: self._add_reply_row("", "", True))
        self.btn_del_rule.clicked.connect(self._del_rule_rows)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_add_rule)
        btn_row.addWidget(self.btn_del_rule)
        av.addLayout(btn_row)

        # ============ 接收区 ============
        self.recv_edit = QPlainTextEdit()
        self.recv_edit.setReadOnly(True)
        self.recv_edit.setFont(QFont("Consolas", 10))
        self.recv_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.recv_edit.setUndoRedoEnabled(False)
        self.recv_edit.setMaximumBlockCount(self.MAX_DISPLAY_BLOCKS)
        self.recv_edit.setToolTip("显示最近 2000 行，最多 512K 字符；长行分段显示。完整数据请启用日志保存。")

        # ============ 总体布局 ============
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        connection_box = QGroupBox("连接配置")
        connection_layout = QVBoxLayout(connection_box)
        self.transport_mode = QComboBox()
        self.transport_mode.addItems(["串口", "TCP 客户端", "TCP 服务端", "UDP", "CAN"])
        connection_layout.addWidget(self.transport_mode)
        self.connection_stack = QStackedWidget()
        self.connection_stack.addWidget(cfg_box)
        self.network_panel = NetworkPanel(self.settings)
        self.connection_stack.addWidget(self.network_panel)
        self.connection_stack.addWidget(self.can_panel)
        connection_layout.addWidget(self.connection_stack)
        lv.addWidget(connection_box)
        lv.addWidget(disp_box)
        lv.addStretch()

        right_top = QWidget()
        rtv = QVBoxLayout(right_top)
        rtv.setContentsMargins(0, 0, 0, 0)
        rtv.addWidget(QLabel("接收区："))
        rtv.addWidget(self.recv_edit)

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(right_top)

        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        tab = QTabWidget()
        self.io_tabs = tab
        tab.addTab(send_box, "发送")
        tab.addTab(auto_box, "自动回复")
        tab.addTab(self._build_quick_tab(), "快捷按钮")
        tab.addTab(self._build_modbus_tab(), "Modbus")
        self.serial_terminal = SerialTerminalPanel(self._send_terminal_data, self.btn_open.click, settings=self.settings)
        tab.addTab(self.serial_terminal, "串口终端")
        self.ssh_panel = SSHPanel(self.settings)
        tab.addTab(self.ssh_panel, "SSH（网络）")
        self.local_terminal = LocalTerminalPanel()
        tab.addTab(self.local_terminal, "本地终端")
        bv.addWidget(tab)
        right_split.addWidget(bottom)
        right_split.setStretchFactor(0, 3)
        right_split.setStretchFactor(1, 2)
        normal_split_sizes = []

        def resize_for_modbus(index):
            self.serial_terminal.output.responses_enabled = tab.widget(index) is self.serial_terminal
            if tab.tabText(index) in ("Modbus", "串口终端", "SSH（网络）", "本地终端"):
                if not normal_split_sizes:
                    normal_split_sizes[:] = right_split.sizes()
                right_split.setSizes([100, max(500, right_split.height() - 100)])
            elif normal_split_sizes:
                right_split.setSizes(normal_split_sizes)
                normal_split_sizes.clear()
            if tab.widget(index) is self.serial_terminal:
                self._stop_quick_loop()
                self.chk_auto_send.setChecked(False)
                self.chk_auto_reply.setChecked(False)
                self.chk_modbus_poll.setChecked(False)
                self.serial_terminal.output.setFocus()

        tab.currentChanged.connect(resize_for_modbus)

        # Small embedded displays often use portrait orientation. Keep the
        # desktop layout unchanged unless the device launcher opts in.
        portrait = os.environ.get("QT5COM_PORTRAIT") == "1"
        main_split = QSplitter(Qt.Vertical if portrait else Qt.Horizontal)
        main_split.addWidget(left)
        main_split.addWidget(right_split)
        main_split.setStretchFactor(0, 0)
        main_split.setStretchFactor(1, 1)
        main_split.setSizes([450, 800] if portrait else [270, 780])

        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(main_split)

        # ============ 状态栏 ============
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.lbl_state = QLabel("未连接")
        self.lbl_counter = QLabel("TX: 0  RX: 0")
        self.lbl_rx_overflow = QLabel()
        self.lbl_rx_overflow.setToolTip("界面接收缓冲超限时保留最新数据，RX 仍统计全部已接收字节。请查看文件日志；日志错误会另行提示。")
        self.btn_reset_cnt = QPushButton("清零")
        self.btn_reset_cnt.setFlat(True)
        self.btn_reset_cnt.clicked.connect(self._reset_counter)
        self.lbl_version = QLabel(f"{__version__}  by {__author__}")
        self.lbl_version.setStyleSheet("color: #888;")
        self.status.addWidget(self.lbl_state, 1)
        self.status.addPermanentWidget(self.lbl_counter)
        self.status.addPermanentWidget(self.lbl_rx_overflow)
        self.status.addPermanentWidget(self.btn_reset_cnt)
        self.status.addPermanentWidget(self.lbl_version)
        self.transport_mode.currentIndexChanged.connect(self._on_transport_changed)
        self.network_panel.received.connect(self._on_network_received)
        self.network_panel.sent.connect(self._on_network_sent)
        self.network_panel.status_changed.connect(self._on_network_status)
        self.network_panel.stop_sending.connect(lambda: self.chk_auto_send.setChecked(False))
        self.network_panel.stop_sending.connect(self._stop_quick_loop)
        self.can_panel.received.connect(self._on_can_received)
        self.can_panel.sent.connect(self._on_can_sent)
        self.can_panel.status_changed.connect(self._on_can_status)
        self.can_panel.stop_sending.connect(lambda: self.chk_auto_send.setChecked(False))
        self.chk_log_save.toggled.connect(self._sync_shared_log)


    # -------------------------------------------------------------- #
    #  样式 / 主题
    # -------------------------------------------------------------- #
    LIGHT_QSS = """
        QWidget { color: #202020; }
        QMainWindow, QDialog { background: #f5f6f8; }
        QMenu { background: #ffffff; color: #202020; border: 1px solid #b5b5b5; padding: 4px; }
        QMenu::item { padding: 6px 24px; background: transparent; }
        QMenu::item:selected { background: #2a6fb2; color: #ffffff; }
        QMenu::item:disabled { color: #747474; background: transparent; }
        QMenu::separator { height: 1px; background: #d0d4d8; margin: 4px 8px; }
        QGroupBox {
            border: 1px solid #c8c8c8; border-radius: 6px;
            margin-top: 10px; padding-top: 6px;
            font-weight: bold;
            background: #fafbfc;
        }
        QGroupBox::title {
            subcontrol-origin: margin; left: 10px; padding: 0 4px;
            color: #2a6fb2;
        }
        QPushButton {
            padding: 5px 14px; border-radius: 4px;
            border: 1px solid #b5b5b5; background: #f7f7f7; color: #202020;
        }
        QPushButton:hover { background: #e8f1fb; border-color: #2a6fb2; }
        QPushButton:pressed { background: #d4e6f7; }
        QPushButton:disabled { color: #888; background: #eee; }
        QPushButton#openBtn:checked {
            background: #2a6fb2; color: white; border-color: #2a6fb2;
        }
        QPushButton#sendBtn {
            background: #2a6fb2; color: white; border-color: #2a6fb2;
            font-weight: bold; min-width: 80px;
        }
        QPushButton#sendBtn:hover { background: #3b83c9; }
        QToolButton {
            padding: 4px 8px; border-radius: 4px;
            border: 1px solid #b5b5b5; background: #f7f7f7; color: #202020;
            min-width: 24px;
        }
        QToolButton:hover { background: #e8f1fb; border-color: #2a6fb2; color: #2a6fb2; }
        QToolButton#refreshBtn { font-size: 16px; font-weight: bold; color: #2a6fb2; }
        QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {
            border: 1px solid #c8c8c8; border-radius: 3px;
            padding: 2px 4px; background: white; color: #202020;
            selection-background-color: #2a6fb2; selection-color: white;
        }
        QTextEdit { background: #fafafa; }
        QFileDialog QAbstractItemView {
            background: #ffffff; alternate-background-color: #f2f5f9;
            color: #202020; border: 1px solid #c8c8c8;
            selection-background-color: #2a6fb2; selection-color: #ffffff;
        }
        QFileDialog QAbstractItemView::item:hover {
            background: #e8f1fb; color: #202020;
        }
        QFileDialog QAbstractItemView::item:selected {
            background: #2a6fb2; color: #ffffff;
        }
        /* ---- 下拉列表：不透明背景，避免覆盖文字 ---- */
        QComboBox QAbstractItemView {
            background: #ffffff;
            color: #202020;
            border: 1px solid #b5b5b5;
            selection-background-color: #2a6fb2;
            selection-color: white;
            outline: 0;
        }
        QTableWidget { gridline-color: #dcdcdc; background: white; color: #202020; }
        QHeaderView::section {
            background: #eef2f7; padding: 4px; color: #202020;
            border: none; border-right: 1px solid #dcdcdc;
        }
        QTabWidget::pane { border: 1px solid #c8c8c8; border-radius: 4px; top: -1px; }
        QTabBar::tab {
            padding: 6px 14px; background: #e9ecef; color: #202020;
            border: 1px solid #c8c8c8; border-bottom: none;
            border-top-left-radius: 4px; border-top-right-radius: 4px;
        }
        QTabBar::tab:selected { background: #fafbfc; color: #2a6fb2; }
        QStatusBar { background: #eef2f7; }
        QCheckBox { color: #202020; }
    """

    DARK_QSS = """
        QWidget { color: #e6e6e6; }
        QMainWindow, QDialog { background: #232629; }
        QMenu { background: #2b2f33; color: #e6e6e6; border: 1px solid #59616a; padding: 4px; }
        QMenu::item { padding: 6px 24px; background: transparent; }
        QMenu::item:selected { background: #245ba8; color: #ffffff; }
        QMenu::item:disabled { color: #9aa3ad; background: transparent; }
        QMenu::separator { height: 1px; background: #59616a; margin: 4px 8px; }
        QGroupBox {
            border: 1px solid #3c4045; border-radius: 6px;
            margin-top: 10px; padding-top: 6px;
            font-weight: bold;
            background: #2b2f33;
        }
        QGroupBox::title {
            subcontrol-origin: margin; left: 10px; padding: 0 4px;
            color: #5aa9e6;
        }
        QPushButton {
            padding: 5px 14px; border-radius: 4px;
            border: 1px solid #4a4f55; background: #343a40; color: #e6e6e6;
        }
        QPushButton:hover { background: #3f4850; border-color: #5aa9e6; }
        QPushButton:pressed { background: #2a3036; }
        QPushButton:disabled { color: #777; background: #2a2d30; }
        QPushButton#openBtn:checked {
            background: #1f6feb; color: white; border-color: #1f6feb;
        }
        QPushButton#sendBtn {
            background: #1f6feb; color: white; border-color: #1f6feb;
            font-weight: bold; min-width: 80px;
        }
        QPushButton#sendBtn:hover { background: #3b83d9; }
        QToolButton {
            padding: 4px 8px; border-radius: 4px;
            border: 1px solid #4a4f55; background: #343a40; color: #e6e6e6;
            min-width: 24px;
        }
        QToolButton:hover { background: #3f4850; border-color: #5aa9e6; color: #5aa9e6; }
        QToolButton#refreshBtn { font-size: 16px; font-weight: bold; color: #5aa9e6; }
        QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {
            border: 1px solid #4a4f55; border-radius: 3px;
            padding: 2px 4px; background: #1e2124; color: #e6e6e6;
            selection-background-color: #1f6feb; selection-color: white;
        }
        QTextEdit { background: #1a1c1f; }
        /* 文件列表、详细视图及侧栏都需要匹配深色文字的背景。 */
        QFileDialog QAbstractItemView {
            background: #1e2124; alternate-background-color: #2b2f33;
            color: #e6e6e6; border: 1px solid #4a4f55;
            selection-background-color: #1f6feb; selection-color: #ffffff;
        }
        QFileDialog QAbstractItemView::item:hover {
            background: #343f4b; color: #ffffff;
        }
        QFileDialog QAbstractItemView::item:selected {
            background: #1f6feb; color: #ffffff;
        }
        QComboBox QAbstractItemView {
            background: #2b2f33;
            color: #e6e6e6;
            border: 1px solid #4a4f55;
            selection-background-color: #1f6feb;
            selection-color: white;
            outline: 0;
        }
        QTableWidget { gridline-color: #3c4045; background: #1e2124; color: #e6e6e6; }
        QHeaderView { background: #1e2124; }
        QTableCornerButton::section {
            background: #2b2f33; border: none; border-right: 1px solid #3c4045;
        }
        QHeaderView::section {
            background: #2b2f33; padding: 4px; color: #e6e6e6;
            border: none; border-right: 1px solid #3c4045;
        }
        QTabWidget::pane { border: 1px solid #3c4045; border-radius: 4px; top: -1px; }
        QTabBar::tab {
            padding: 6px 14px; background: #2b2f33; color: #cfd2d5;
            border: 1px solid #3c4045; border-bottom: none;
            border-top-left-radius: 4px; border-top-right-radius: 4px;
        }
        QTabBar::tab:selected { background: #1e2124; color: #5aa9e6; }
        QStatusBar { background: #2b2f33; }
        QCheckBox { color: #e6e6e6; }
        QToolTip { color: #e6e6e6; background: #2b2f33; border: 1px solid #4a4f55; }
    """

    def _apply_style(self):
        QApplication.setStyle(QStyleFactory.create("Fusion"))
        self._apply_theme(self.settings.value("theme", "浅色"))

    def _apply_theme(self, name: str):
        dark = name == "深色"
        changed = getattr(self, "_dark_theme", None) != dark
        self._dark_theme = dark
        if dark:
            self.setStyleSheet(self.DARK_QSS)
        else:
            self.setStyleSheet(self.LIGHT_QSS)
        if changed:
            self._recolor_log()

    def _log_color(self, color: str) -> QColor:
        return QColor(self.DARK_LOG_COLORS.get(color, color) if self._dark_theme else color)

    def _recolor_log(self):
        # 显式字符颜色会覆盖 QSS；切换主题时也更新已有日志。
        scrollbars = (self.recv_edit.verticalScrollBar(), self.recv_edit.horizontalScrollBar())
        positions = [bar.value() for bar in scrollbars]
        document = self.recv_edit.document()
        ranges = []
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                color = fragment.charFormat().property(self.LOG_COLOR_PROPERTY)
                if fragment.isValid() and color:
                    ranges.append((fragment.position(), fragment.length(), color))
                iterator += 1
            block = block.next()
        # 先收集范围，再修改格式，避免使文档迭代器失效。
        cursor = QTextCursor(document)
        for start, length, color in ranges:
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(self._log_color(color))
            cursor.mergeCharFormat(fmt)
        for bar, position in zip(scrollbars, positions):
            bar.setValue(position)

    def _on_theme_changed(self, name: str):
        self._apply_theme(name)
        self.settings.setValue("theme", name)

    # -------------------------------------------------------------- #
    #  快捷按钮标签页
    # -------------------------------------------------------------- #
    def _build_quick_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        loop_row = QHBoxLayout()
        self.chk_quick_loop = QCheckBox("定时循环发送")
        self.chk_quick_loop.setToolTip("按表格顺序发送勾选的指令；勾选后立即发送第一条。")
        self.spn_quick_interval = QSpinBox()
        self.spn_quick_interval.setRange(1, 600000)
        self.spn_quick_interval.setValue(1000)
        self.spn_quick_interval.setSuffix(" ms")
        self.spn_quick_interval.setToolTip("每条指令发送后的等待时间，下一轮也使用此间隔。")
        self.spn_quick_rounds = QSpinBox()
        self.spn_quick_rounds.setRange(0, 1000000)
        self.spn_quick_rounds.setValue(1)
        self.spn_quick_rounds.setSpecialValueText("无限")
        self.spn_quick_rounds.setToolTip("全部勾选的指令发送一遍为一轮；0 表示无限循环。")
        self.btn_quick_stop = QPushButton("停止")
        self.btn_quick_stop.setEnabled(False)
        self.chk_quick_loop.toggled.connect(self._toggle_quick_loop)
        self.btn_quick_stop.clicked.connect(lambda: self._stop_quick_loop())
        loop_row.addWidget(self.chk_quick_loop)
        loop_row.addWidget(QLabel("间隔:"))
        loop_row.addWidget(self.spn_quick_interval)
        loop_row.addWidget(QLabel("循环次数:"))
        loop_row.addWidget(self.spn_quick_rounds)
        loop_row.addWidget(self.btn_quick_stop)
        loop_row.addStretch()
        v.addLayout(loop_row)
        self.lbl_quick_loop_status = QLabel("勾选要循环的指令，每组发送一遍计为一轮。")
        self.lbl_quick_loop_status.setWordWrap(True)
        v.addWidget(self.lbl_quick_loop_status)

        self.quick_table = QuickCommandTable(0, 6)
        self.quick_table.move_rows_requested.connect(self._move_quick_rows)
        self.quick_table.setHorizontalHeaderLabels(
            ["循环", "名称", "内容", "HEX", "附加\\r\\n", "发送"])
        hh = self.quick_table.horizontalHeader()
        for column in range(6):
            hh.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.QUICK_COL_CONTENT, QHeaderView.Stretch)
        self.quick_table.verticalHeader().setDefaultSectionSize(28)
        self.quick_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.addWidget(self.quick_table)

        row = QHBoxLayout()
        self.btn_quick_add = QPushButton("添加按钮")
        self.btn_quick_delete = QPushButton("删除所选")
        self.btn_quick_add.clicked.connect(lambda: self._add_quick_row("新指令", "", False, False))
        self.btn_quick_delete.clicked.connect(self._del_quick_rows)
        row.addWidget(QLabel("拖动行号调整顺序（可多选）"))
        row.addStretch()
        row.addWidget(self.btn_quick_add)
        row.addWidget(self.btn_quick_delete)
        v.addLayout(row)
        return w

    def _build_modbus_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        form = QFormLayout()

        top_row = QHBoxLayout()
        self.spn_modbus_slave = QSpinBox()
        self.spn_modbus_slave.setRange(0, 255)
        self.spn_modbus_slave.setValue(1)
        self.cmb_modbus_func = QComboBox()
        for text, code in MODBUS_FUNCTIONS:
            self.cmb_modbus_func.addItem(text, code)
        self.cmb_modbus_func.currentIndexChanged.connect(self._on_modbus_func_changed)
        self.chk_modbus_base1 = QCheckBox("按设备地址(Base 1)")
        top_row.addWidget(QLabel("从站"))
        top_row.addWidget(self.spn_modbus_slave)
        top_row.addSpacing(8)
        top_row.addWidget(QLabel("功能"))
        top_row.addWidget(self.cmb_modbus_func, 1)
        top_row.addSpacing(8)
        top_row.addWidget(self.chk_modbus_base1)
        form.addRow(top_row)

        addr_row = QHBoxLayout()
        self.spn_modbus_addr = QSpinBox()
        self.spn_modbus_addr.setRange(0, 65535)
        self.spn_modbus_qty = QSpinBox()
        self.spn_modbus_qty.setRange(1, 2000)
        self.spn_modbus_qty.setValue(1)
        addr_row.addWidget(QLabel("地址"))
        addr_row.addWidget(self.spn_modbus_addr)
        addr_row.addSpacing(8)
        addr_row.addWidget(QLabel("数量"))
        addr_row.addWidget(self.spn_modbus_qty)
        self.spn_modbus_scan = QSpinBox()
        self.spn_modbus_scan.setRange(1, 3600000)
        self.spn_modbus_scan.setValue(1000)
        self.spn_modbus_scan.setSuffix(" ms")
        self.chk_modbus_poll = QCheckBox("自动读取")
        self.chk_modbus_poll.toggled.connect(self._toggle_modbus_poll)
        self.chk_modbus_signed = QCheckBox("有符号16位")
        self.chk_modbus_signed.setChecked(True)
        self.chk_modbus_signed.toggled.connect(self._render_modbus_table)
        addr_row.addWidget(QLabel("扫描周期"))
        addr_row.addWidget(self.spn_modbus_scan)
        addr_row.addWidget(self.chk_modbus_poll)
        addr_row.addWidget(self.chk_modbus_signed)
        form.addRow(addr_row)

        self.edit_modbus_values = QLineEdit()
        self.edit_modbus_values.setPlaceholderText("写操作输入数值，多个值用逗号分隔，例如：1, 2, 100 或 1,0,1,1")
        form.addRow("写入值", self.edit_modbus_values)

        v.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_modbus_read = QPushButton("读取一次")
        self.btn_modbus_write = QPushButton("写入一次")
        self.btn_modbus_to_send = QPushButton("填入发送区")
        self.btn_modbus_new = QPushButton("新建 MBP")
        self.btn_modbus_new.setToolTip("按当前从站、读功能、地址和数量建立空白表格")
        self.btn_modbus_new.clicked.connect(self.on_modbus_new_profile)
        self.chk_modbus_edit = QCheckBox("编辑配置")
        self.chk_modbus_edit.toggled.connect(self._toggle_modbus_edit)
        self.btn_modbus_open = QPushButton("打开 MBP…")
        self.btn_modbus_save = QPushButton("保存 MBP…")
        self.btn_modbus_read.clicked.connect(self.on_modbus_read_clicked)
        self.btn_modbus_write.clicked.connect(self.on_modbus_write_clicked)
        self.btn_modbus_to_send.clicked.connect(self.on_modbus_fill_send_clicked)
        self.btn_modbus_open.clicked.connect(self.on_modbus_open_profile)
        self.btn_modbus_save.clicked.connect(self.on_modbus_save_profile)
        btn_row.addWidget(self.btn_modbus_read)
        btn_row.addWidget(self.btn_modbus_write)
        btn_row.addWidget(self.btn_modbus_to_send)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_modbus_new)
        btn_row.addWidget(self.chk_modbus_edit)
        btn_row.addWidget(self.btn_modbus_open)
        btn_row.addWidget(self.btn_modbus_save)
        v.addLayout(btn_row)

        self.modbus_result = QPlainTextEdit()
        self.modbus_result.setReadOnly(True)
        self.modbus_result.setPlaceholderText(
            "Modbus RTU 解析结果显示在这里。\n"
            "说明：当前实现基于现有串口连接，支持 01/02/03/04/05/06/15/16。"
        )
        self.modbus_result.setMaximumHeight(72)
        v.addWidget(self.modbus_result)
        self.modbus_table = QTableWidget(0, 4)
        self.modbus_table.setHorizontalHeaderLabels(["Name", "值", "Name", "值"])
        self.modbus_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.modbus_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.modbus_table.verticalHeader().setDefaultSectionSize(25)
        self.modbus_table.cellDoubleClicked.connect(self._modbus_cell_to_write)
        self.modbus_table.itemChanged.connect(self._on_modbus_item_changed)
        self.modbus_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.modbus_table.setToolTip("勾选编辑配置后双击修改名称/值；退出编辑后双击可填入设备写入表单。")
        edit_row = QHBoxLayout()
        self.btn_modbus_clear_items = QPushButton("清空所选项")
        self.btn_modbus_clear_items.setToolTip("清除所选寄存器的名称和值，保留地址和数量；可用 Ctrl/Shift 多选")
        self.btn_modbus_clear_items.clicked.connect(self.on_modbus_clear_items)
        self.btn_modbus_trim = QPushButton("缩减末尾项…")
        self.btn_modbus_trim.setToolTip("指定保留数量，只移除末尾寄存器，并同步读取参数")
        self.btn_modbus_trim.clicked.connect(self.on_modbus_trim)
        self.btn_modbus_clear_items.setEnabled(False)
        self.btn_modbus_trim.setEnabled(False)
        edit_row.addWidget(self.btn_modbus_clear_items)
        edit_row.addWidget(self.btn_modbus_trim)
        edit_row.addStretch()
        v.addLayout(edit_row)
        v.addWidget(self.modbus_table, 1)

        self._on_modbus_func_changed()
        return w

    def _make_center_checkbox(self, checked: bool) -> QWidget:
        """返回一个居中显示的 QCheckBox（用于表格单元）。"""
        c = QWidget()
        cb = QCheckBox()
        cb.setChecked(checked)
        lay = QHBoxLayout(c)
        lay.addWidget(cb)
        lay.setAlignment(Qt.AlignCenter)
        lay.setContentsMargins(0, 0, 0, 0)
        c.checkbox = cb  # type: ignore[attr-defined]
        return c

    def _cell_checkbox(self, widget: QWidget) -> QCheckBox | None:
        return getattr(widget, "checkbox", None) if widget else None

    def _quick_entries(self):
        entries = []
        for row in range(self.quick_table.rowCount()):
            name = self.quick_table.item(row, self.QUICK_COL_NAME)
            content = self.quick_table.item(row, self.QUICK_COL_CONTENT)
            entry = {"name": name.text() if name else "",
                     "content": content.text() if content else ""}
            for key, column in (("hex", self.QUICK_COL_HEX),
                                ("newline", self.QUICK_COL_NEWLINE),
                                ("loop", self.QUICK_COL_LOOP)):
                checkbox = self._cell_checkbox(self.quick_table.cellWidget(row, column))
                entry[key] = bool(checkbox and checkbox.isChecked())
            entries.append(entry)
        return entries

    def _move_quick_rows(self, rows, destination):
        if self._quick_loop_running or not self.quick_table.isEnabled():
            return
        count = self.quick_table.rowCount()
        rows = sorted(set(rows))
        if (not rows or any(type(row) is not int or not 0 <= row < count for row in rows)
                or type(destination) is not int or not 0 <= destination <= count):
            return
        remaining = [row for row in range(count) if row not in rows]
        insert_at = destination - sum(row < destination for row in rows)
        order = remaining[:insert_at] + rows + remaining[insert_at:]
        if order == list(range(count)):
            return
        # 先提交正在编辑的名称/内容，再快照重建整行。
        self.quick_table.setFocus(Qt.MouseFocusReason)
        entries = self._quick_entries()
        self.quick_table.setUpdatesEnabled(False)
        try:
            self.quick_table.setRowCount(0)
            for row in order:
                entry = entries[row]
                self._add_quick_row(entry["name"], entry["content"], entry["hex"],
                                    entry["newline"], entry["loop"])
            selection = self.quick_table.selectionModel()
            for row in range(insert_at, insert_at + len(rows)):
                selection.select(self.quick_table.model().index(row, self.QUICK_COL_NAME),
                                 QItemSelectionModel.Select | QItemSelectionModel.Rows)
            self.quick_table.setCurrentCell(insert_at, self.QUICK_COL_NAME,
                                            QItemSelectionModel.NoUpdate)
        finally:
            self.quick_table.setUpdatesEnabled(True)
        self.settings.setValue("quick_buttons", json.dumps(self._quick_entries(), ensure_ascii=False))
        self.settings.sync()

    def _add_quick_row(self, name: str, content: str, is_hex: bool, newline: bool,
                       loop_enabled: bool = False):
        r = self.quick_table.rowCount()
        self.quick_table.insertRow(r)
        self.quick_table.setCellWidget(r, self.QUICK_COL_LOOP, self._make_center_checkbox(loop_enabled))
        self.quick_table.setItem(r, self.QUICK_COL_NAME, QTableWidgetItem(name))
        self.quick_table.setItem(r, self.QUICK_COL_CONTENT, QTableWidgetItem(content))
        self.quick_table.setCellWidget(r, self.QUICK_COL_HEX, self._make_center_checkbox(is_hex))
        self.quick_table.setCellWidget(r, self.QUICK_COL_NEWLINE, self._make_center_checkbox(newline))
        btn = QPushButton("发送")
        btn.clicked.connect(lambda _=False, b=btn: self._send_quick_row(b))
        self.quick_table.setCellWidget(r, self.QUICK_COL_SEND, btn)

    def _prepare_quick_command(self, row: int, strict_hex: bool = False):
        item = self.quick_table.item(row, self.QUICK_COL_CONTENT)
        text = item.text() if item else ""
        if not text:
            raise ValueError(f"第 {row + 1} 行发送内容为空")
        name_item = self.quick_table.item(row, self.QUICK_COL_NAME)
        name = name_item.text() if name_item else ""
        hex_cb = self._cell_checkbox(self.quick_table.cellWidget(row, self.QUICK_COL_HEX))
        nl_cb = self._cell_checkbox(self.quick_table.cellWidget(row, self.QUICK_COL_NEWLINE))
        is_hex = bool(hex_cb and hex_cb.isChecked())
        if is_hex and strict_hex:
            compact = re.sub(r"\s", "", text)
            if not compact or len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
                raise ValueError(f"第 {row + 1} 行 HEX 格式无效，请填写完整字节，例如 AA 55")
        data = hex_str_to_bytes(text) if is_hex else text.encode("utf-8")
        if nl_cb and nl_cb.isChecked() and not is_hex:
            data += b"\r\n"
        if not data:
            raise ValueError(f"第 {row + 1} 行发送内容为空")
        data = self._maybe_append_checksum(data)
        if strict_hex and self.transport_mode.currentIndex() in (1, 2, 3) and len(data) > 65507:
            raise ValueError(f"第 {row + 1} 行超过网络单次发送上限 65507 字节")
        return {"name": name, "text": text, "hex": is_hex, "data": data}

    def _send_quick_payload(self, command) -> bool:
        data = command["data"]
        try:
            if self.transport_mode.currentIndex() in (1, 2, 3):
                success = self.network_panel.send_bytes(data)
                if success:
                    self._push_history(command["text"])
                return success
            if self.transport_mode.currentIndex() != 0 or not (self.ser and self.ser.is_open):
                raise IOError("请先打开串口")
            written = self.ser.write(data)
            self.tx_bytes += written
            self._update_counter()
            if written != len(data):
                raise IOError(f"仅发送 {written}/{len(data)} 字节，请检查设备")
        except Exception as e:
            self.chk_auto_send.setChecked(False)
            self.append_log(f"[快捷发送失败] {e}", color="#c0392b")
            return False
        text, name = command["text"], command["name"]
        shown = bytes_to_hex_str(data) if command["hex"] else text
        self.append_log(f"{self._display_prefix('TX')}[{name}] {shown}", color="#0d47a1")
        self._write_log_raw(
            f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"TX[{name}] -> {shown}\n")
        return True

    def _send_quick_row(self, btn: QPushButton):
        if self.transport_mode.currentIndex() == self.CAN_MODE:
            self.status.showMessage("CAN 请在发送页设置帧 ID 和数据后发送。", 5000)
            return
        # 通过按钮定位所在行
        for r in range(self.quick_table.rowCount()):
            if self.quick_table.cellWidget(r, self.QUICK_COL_SEND) is btn:
                break
        else:
            return
        if self.transport_mode.currentIndex() == 0 and not (self.ser and self.ser.is_open):
            QMessageBox.information(self, "提示", "请先打开串口。")
            return
        try:
            command = self._prepare_quick_command(r)
        except Exception as e:
            self.append_log(f"[快捷发送失败] {e}", color="#c0392b")
            return
        self._stop_quick_loop()
        self._send_quick_payload(command)

    def _set_quick_loop_controls(self, running: bool):
        for widget in (self.quick_table, self.btn_quick_add, self.btn_quick_delete,
                       self.spn_quick_interval, self.spn_quick_rounds):
            widget.setEnabled(not running)
        self.btn_quick_stop.setEnabled(running)
        # Keep the selected network destination stable during a sequence.
        if running:
            for widget in (self.network_panel.remote_host, self.network_panel.remote_port,
                           self.network_panel.clients, self.network_panel.drop_button):
                widget.setEnabled(False)
        else:
            self.network_panel.update_controls()
            self.network_panel.clients.setEnabled(True)
            self.network_panel.drop_button.setEnabled(True)

    def _quick_loop_progress(self, message: str):
        target = str(self._quick_loop_round_limit) if self._quick_loop_round_limit else "无限"
        self.lbl_quick_loop_status.setText(
            f"{message}：已完成 {self._quick_loop_round}/{target} 轮，已发送 {self._quick_loop_sent} 条")

    def _toggle_quick_loop(self, enabled: bool):
        if not enabled:
            self._stop_quick_loop()
            return
        try:
            if self.transport_mode.currentIndex() == self.CAN_MODE:
                raise ValueError("CAN 请使用发送页的循环发送")
            if self.transport_mode.currentIndex() == 0 and not (self.ser and self.ser.is_open):
                raise ValueError("请先打开串口")
            if self.transport_mode.currentIndex() in (1, 2, 3) and not self.network_panel.active:
                raise ValueError("请先连接或开始监听")
            commands = []
            for row in range(self.quick_table.rowCount()):
                cb = self._cell_checkbox(self.quick_table.cellWidget(row, self.QUICK_COL_LOOP))
                if cb and cb.isChecked():
                    commands.append(self._prepare_quick_command(row, strict_hex=True))
            if not commands:
                raise ValueError("请先勾选要循环发送的指令")
        except Exception as exc:
            self._stop_quick_loop("未启动")
            self.lbl_quick_loop_status.setText(f"未启动：{exc}")
            return
        self.chk_auto_send.setChecked(False)
        self.chk_modbus_poll.setChecked(False)
        self._quick_loop_commands = commands
        self._quick_loop_index = self._quick_loop_round = self._quick_loop_sent = 0
        self._quick_loop_round_limit = self.spn_quick_rounds.value()
        self._quick_loop_interval = self.spn_quick_interval.value()
        self._quick_loop_running = True
        self._set_quick_loop_controls(True)
        self._send_next_quick()

    def _send_next_quick(self):
        if not self._quick_loop_running:
            return
        command = self._quick_loop_commands[self._quick_loop_index]
        success = self._send_quick_payload(command)
        if not success:
            self._stop_quick_loop("发送失败，循环已停止")
            return
        # A network stop signal can arrive synchronously during send_bytes.
        if not self._quick_loop_running:
            return
        self._quick_loop_sent += 1
        self._quick_loop_index += 1
        if self._quick_loop_index == len(self._quick_loop_commands):
            self._quick_loop_index = 0
            self._quick_loop_round += 1
        if self._quick_loop_round_limit and self._quick_loop_round >= self._quick_loop_round_limit:
            self._stop_quick_loop("已完成")
            return
        self._quick_loop_progress("循环中")
        self.quick_loop_timer.start(self._quick_loop_interval)

    def _stop_quick_loop(self, message=None):
        if not self._quick_loop_running and not self.chk_quick_loop.isChecked() and message is None:
            return
        self.quick_loop_timer.stop()
        self._quick_loop_running = False
        self._quick_loop_commands = []
        blocked = self.chk_quick_loop.blockSignals(True)
        self.chk_quick_loop.setChecked(False)
        self.chk_quick_loop.blockSignals(blocked)
        self._set_quick_loop_controls(False)
        self._quick_loop_progress(message or "已停止")

    def _del_quick_rows(self):
        if self._quick_loop_running:
            return
        rows = sorted({i.row() for i in self.quick_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.quick_table.removeRow(r)

    # -------------------------------------------------------------- #
    #  自动回复行辅助
    # -------------------------------------------------------------- #
    def _add_reply_row(self, trigger: str, reply: str, enabled: bool):
        r = self.reply_table.rowCount()
        self.reply_table.insertRow(r)
        self.reply_table.setCellWidget(r, 0, self._make_center_checkbox(enabled))
        self.reply_table.setItem(r, 1, QTableWidgetItem(trigger))
        self.reply_table.setItem(r, 2, QTableWidgetItem(reply))

    # -------------------------------------------------------------- #
    #  校验辅助
    # -------------------------------------------------------------- #
    def _maybe_append_checksum(self, data: bytes) -> bytes:
        if not self.chk_checksum.isChecked():
            return data
        return apply_checksum(
            data,
            self.cmb_checksum.currentText(),
            self.spn_chk_start.value(),
            self.spn_chk_end.value(),
        )

    # -------------------------------------------------------------- #
    #  端口
    # -------------------------------------------------------------- #
    def refresh_ports(self):
        current = self._current_port_device()
        ports = enumerate_serial_ports()
        entries = []
        hidden = 0
        for p in ports:
            usable, reason = serial_port_usability(p)
            if not usable and not self.chk_show_all_ports.isChecked():
                hidden += 1
                continue
            desc = p.description if p.description and p.description != "n/a" else ""
            suffix = f"  ({desc})" if desc else ""
            if not usable:
                suffix += f"  [不可用：{reason}]"
            entries.append((f"{p.device}{suffix}", p.device, usable, reason))
        items = [entry[0] for entry in entries]
        existing = [self.cmb_port.itemText(i) for i in range(self.cmb_port.count())]
        if items != existing:
            self.cmb_port.blockSignals(True)
            self.cmb_port.clear()
            for label, device, usable, reason in entries:
                self.cmb_port.addItem(label, device)
                index = self.cmb_port.count() - 1
                self.cmb_port.setItemData(index, usable, Qt.UserRole + 1)
                self.cmb_port.setItemData(index, reason, Qt.ToolTipRole)
            # 恢复之前选择
            for i, (_, device, _, _) in enumerate(entries):
                if device == current:
                    self.cmb_port.setCurrentIndex(i)
                    break
            self.cmb_port.blockSignals(False)
        usable_count = sum(1 for entry in entries if entry[2])
        if not entries:
            message = "未检测到可用串口"
        else:
            message = f"检测到 {usable_count} 个可用串口"
        if hidden:
            message += f"，已隐藏 {hidden} 个不可用端口"
        self.status.showMessage(message, 5000)

    def _current_port_device(self) -> str:
        device = self.cmb_port.currentData(Qt.UserRole)
        if device:
            return str(device)
        txt = self.cmb_port.currentText().strip()
        return txt.split()[0] if txt else ""

    def toggle_port(self, checked):
        if checked:
            self.open_port()
        else:
            self.close_port()

    def open_port(self):
        # A repeated open request must not replace a live handle with a new
        # connection that fails against our own exclusive lock.
        if self.ser is not None and self.ser.is_open:
            self.btn_open.setChecked(True)
            return
        dev = self._current_port_device()
        if not dev:
            QMessageBox.warning(self, "提示", "未选择串口。")
            self.btn_open.setChecked(False)
            return
        if self.cmb_port.currentData(Qt.UserRole + 1) is False:
            reason = self.cmb_port.currentData(Qt.ToolTipRole) or "不可用"
            QMessageBox.warning(self, "提示", f"所选串口当前不可用：{reason}")
            self.btn_open.setChecked(False)
            return
        try:
            baud = int(self.cmb_baud.currentText())
        except ValueError:
            QMessageBox.warning(self, "提示", "波特率无效。")
            self.btn_open.setChecked(False)
            return
        try:
            self.ser = open_serial_connection(
                port=dev,
                baudrate=baud,
                bytesize=int(self.cmb_data.currentText()),
                parity=self.PARITY_MAP[self.cmb_parity.currentText()],
                stopbits=self.STOP_MAP[self.cmb_stop.currentText()],
                timeout=0,
                write_timeout=1,
            )
        except Exception as e:
            self.btn_open.setChecked(False)
            # Keep the attempted device selected. A transient lock failure
            # must not hide it, select another port, or prevent a later retry.
            self._show_serial_open_error(dev, e)
            return

        self.reader = SerialReader(self.ser)
        reader = self.reader
        reader.hex_mode = self.chk_rx_hex.isChecked()
        reader.error.connect(lambda message: self.on_serial_error(message)
                             if self.reader is reader else None)
        self.rx_display_dropped = 0
        self.lbl_rx_overflow.clear()

        self.btn_open.setText("关闭串口")
        self.lbl_state.setText(
            f"已连接 {dev} @ {baud} {self.cmb_data.currentText()}"
            f"{self.cmb_parity.currentText()[0]}{self.cmb_stop.currentText()}"
        )
        self._set_config_enabled(False)
        self.serial_terminal.set_connected(True, f"{dev} @ {baud}")

        if self.chk_log_save.isChecked():
            self._open_log_file()
        # Finish terminal initialization before any first receive is delivered.
        self.reader.start()
        self.serial_receive_timer.start()

    def _show_serial_open_error(self, device: str, error: Exception):
        guidance, details = describe_serial_open_error(device, error)
        dialog = QMessageBox(QMessageBox.Critical, "打开失败", guidance,
                             QMessageBox.Ok, self)
        dialog.setTextFormat(Qt.PlainText)
        dialog.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dialog.setDetailedText(details)
        copy_button = dialog.addButton("复制诊断信息", QMessageBox.ActionRole)
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(guidance + "\n\n" + details))
        dialog.exec_()

    def close_port(self):
        self._stop_quick_loop()
        self.serial_receive_timer.stop()
        self.chk_modbus_poll.setChecked(False)
        self.modbus_timeout.stop()
        self.pending_modbus_request = None
        self.modbus_rx_buffer.clear()
        if self.reader:
            self.reader.stop()
            # Account for the final receive batch, but never render an entire
            # overloaded backlog while the user is trying to disconnect.
            self._drain_serial_data(final=True)
            self.reader = None
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.serial_terminal.set_connected(False)
        self.btn_open.setText("打开串口")
        self.btn_open.setChecked(False)
        self.lbl_state.setText("未连接")
        self._set_config_enabled(True)
        self._close_log_file()

    def _set_config_enabled(self, enabled: bool):
        for w in (self.cmb_port, self.cmb_baud, self.cmb_data,
                  self.cmb_parity, self.cmb_stop, self.btn_refresh,
                  self.chk_show_all_ports):
            w.setEnabled(enabled)

    def on_serial_error(self, msg):
        self.append_log(f"[错误] {msg}", color="#c0392b")
        self.close_port()

    # -------------------------------------------------------------- #
    #  日志
    # -------------------------------------------------------------- #
    def choose_log_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择日志目录",
            self.settings.value("log_dir", str(Path.home())))
        if d:
            self.settings.setValue("log_dir", d)
            self.status.showMessage(f"日志目录：{d}", 3000)

    def _log_dir(self) -> str:
        return self.settings.value("log_dir", os.path.join(app_dir(), "logs"))

    def _open_log_file(self):
        try:
            d = self._log_dir()
            os.makedirs(d, exist_ok=True)
            name = datetime.datetime.now().strftime("%Y-%m-%d") + ".log"
            self.log_file = AsyncLogWriter(os.path.join(d, name))
            self._reported_log_error = None
            self.log_error_timer.start()
            self._write_log_raw(f"\n===== Session start {datetime.datetime.now()} =====\n")
            if self.reader is not None:
                self.reader.log_writer = self.log_file
        except Exception as e:
            self.log_file = None
            QMessageBox.warning(self, "日志", f"无法打开日志文件：{e}")

    def _close_log_file(self):
        # This can also be called while settings are loaded during startup.
        if hasattr(self, "log_error_timer"):
            self.log_error_timer.stop()
        if self.reader is not None:
            self.reader.log_writer = None
        if self.log_file:
            try:
                self._write_log_raw(f"===== Session end {datetime.datetime.now()} =====\n")
                self.log_file.close()
                self._check_log_error()
            except Exception:
                pass
            self.log_file = None

    def _write_log_raw(self, text: str):
        if self.log_file:
            try:
                self.log_file.write(text)
            except Exception:
                pass

    def _check_log_error(self):
        if self.log_file is not None and self.log_file.error:
            message = f"日志保存失败：{self.log_file.error}"
            if message == self._reported_log_error:
                return
            self._reported_log_error = message
            self.append_log(message, color="#c0392b")
            self.lbl_state.setText(message)
            self.lbl_state.setToolTip(message)
            self.status.showMessage(message)

    def _set_serial_hex_mode(self, enabled):
        if self.reader is not None:
            self.reader.hex_mode = enabled

    # -------------------------------------------------------------- #
    #  接收
    # -------------------------------------------------------------- #
    def _on_transport_changed(self, index):
        self._stop_quick_loop()
        self.chk_auto_send.setChecked(False)
        if index != 0:
            self.chk_auto_reply.setChecked(False)
        self.chk_modbus_poll.setChecked(False)
        if self.network_panel.active:
            self.network_panel.stop()
        if self.can_panel.active or self.can_panel.connecting or self.can_panel.closing:
            self.can_panel.stop()
        if self.ser is not None:
            self.close_port()
        is_can = index == self.CAN_MODE
        self.connection_stack.setCurrentIndex(2 if is_can else 0 if index == 0 else 1)
        if index in (1, 2, 3):
            self.network_panel.mode.setCurrentIndex(index - 1)
            self.network_panel.update_controls()
        self.can_panel.send_options.setVisible(is_can)
        for widget in (self.chk_tx_hex, self.chk_tx_newline, self.chk_checksum,
                       self.cmb_checksum, self.spn_chk_start, self.spn_chk_end,
                       self.chk_rx_hex):
            widget.setEnabled(not is_can)
        self.send_edit.setPlaceholderText(
            "单帧 CAN 数据（HEX）：01 02 AA FF；留空可发送零字节数据帧。\n"
            "经典 CAN 最多 8 字节；CAN FD 最多 64 字节；远程帧请清空数据并设置 DLC。"
            if is_can else self._default_send_placeholder)
        self.btn_send.setText("发送 CAN 帧" if is_can else "发  送")
        self.chk_auto_reply.setEnabled(index == 0)
        self.chk_auto_reply.setToolTip('自动回复用于串口模式')
        for tab_index in (1, 3, 4):
            self.io_tabs.setTabEnabled(tab_index, index == 0)
        self.io_tabs.setTabEnabled(2, not is_can)
        if (index and self.io_tabs.currentIndex() in (1, 3, 4)) or (is_can and self.io_tabs.currentIndex() == 2):
            self.io_tabs.setCurrentIndex(0)
        self.lbl_rx_overflow.setVisible(index == 0)
        self.lbl_state.setText(self.transport_mode.currentText() + '：未连接')
        self._sync_shared_log()
        self._update_counter()

    def _sync_shared_log(self, *args):
        connected = (bool(self.ser and self.ser.is_open) or self.network_panel.active
                     or self.can_panel.active or
                     (self.can_panel.closing and getattr(self.can_panel.bus, 'async_tx', False)))
        if self.chk_log_save.isChecked() and connected:
            if self.log_file is None:
                self._open_log_file()
        else:
            self._close_log_file()

    def _on_network_status(self, message):
        if self.transport_mode.currentIndex() not in (1, 2, 3):
            return
        label = self.network_panel.mode.currentText()
        if self.network_panel.active:
            self._sync_shared_log()
        self.lbl_state.setText(f'{label}：{message}')
        self.append_log(f'[{label}] {message}')
        self._write_log_raw(f'{datetime.datetime.now().isoformat(timespec="milliseconds")} [{label}] {message}\n')
        if not self.network_panel.active:
            self._sync_shared_log()

    def _on_network_received(self, data, peer, text):
        self.rx_bytes += len(data)
        self._update_counter()
        shown = bytes_to_hex_str(data) if self.chk_rx_hex.isChecked() else text
        self._network_log('RX', peer, shown, len(data))

    def _on_network_sent(self, data, peer):
        self.tx_bytes += len(data)
        self._update_counter()
        shown = bytes_to_hex_str(data) if self.chk_tx_hex.isChecked() else data.decode('utf-8', errors='replace')
        self._network_log('TX', peer, shown, len(data))

    def _network_log(self, direction, peer, text, size):
        label = self.network_panel.mode.currentText()
        self.append_log(f'{self._display_prefix(direction)}[{label} {peer}] ({size} B) {text}',
                        color='#1b5e20' if direction == 'RX' else '#0d47a1')
        self._write_log_raw(f'{datetime.datetime.now().isoformat(timespec="milliseconds")} {direction} [{label} {peer}] {text}\n')

    def _on_can_status(self, message):
        is_can = self.transport_mode.currentIndex() == self.CAN_MODE
        # Background device discovery also runs when the serial page is shown.
        # Only carry CAN status across a mode switch to finish a closing session.
        if not is_can and not self.can_panel.closing and not getattr(self, '_can_status_pending', False):
            return
        self._can_status_pending = self.can_panel.closing
        if self.can_panel.active or self.can_panel.closing:
            self._sync_shared_log()
        if is_can:
            self.lbl_state.setText('CAN：' + message)
        if getattr(self, '_can_exit_pending', False):
            self.status.showMessage('退出前等待 CAN 驱动关闭：' + message)
        self.append_log('[CAN] ' + message)
        self._write_log_raw(f'{datetime.datetime.now().isoformat(timespec="milliseconds")} [CAN] {message}\n')
        if not self.can_panel.active:
            self._sync_shared_log()
        self._update_counter()

    @staticmethod
    def _can_frame_text(frame):
        kind = 'ERROR' if frame.error else 'RTR' if frame.remote else 'FD' if frame.fd else 'DATA'
        width = 8 if frame.extended or frame.error else 3
        flags = ['EXT' if frame.extended else 'STD', kind]
        if frame.bitrate_switch:
            flags.append('BRS')
        if frame.error_state_indicator:
            flags.append('ESI')
        length = frame.dlc if frame.remote else len(frame.data)
        return (f'ID=0x{frame.can_id:0{width}X} {" ".join(flags)} LEN={length} '
                f'DATA={bytes_to_hex_str(frame.data) if frame.data else "-"}')

    def _log_can_frames(self, frames, direction):
        shown, records = [], []
        channel = self.can_panel.interface.currentText().strip()
        backend = self.can_panel.backend.currentData()
        if backend != 'socketcan':
            channel = f'{backend}:{channel}'
        for frame in frames:
            timestamp = (datetime.datetime.fromtimestamp(frame.timestamp)
                         if frame.timestamp is not None else datetime.datetime.now())
            details = f'[CAN {channel}] {self._can_frame_text(frame)}'
            shown.append(f'{self._display_prefix(direction, timestamp)}{details}')
            records.append(f'{timestamp.isoformat(timespec="milliseconds")} {direction} {details}\n')
        if shown:
            self.append_log('\n'.join(shown), color='#1b5e20' if direction == 'RX' else '#0d47a1')
            self._write_log_raw(''.join(records))

    def _on_can_received(self, frames):
        # The worker may still have received frames queued when a mode switch
        # starts closing it; these belong in the log as well as confirmed TX.
        self.rx_bytes += sum(len(frame.data) for frame in frames)
        self._update_counter()
        self._log_can_frames(frames, 'RX')

    def _on_can_sent(self, frame):
        # A driver send already in progress can finish after a mode switch.
        # Preserve its confirmed TX record while asynchronous shutdown drains.
        self.tx_bytes += len(frame.data)
        self._update_counter()
        self._log_can_frames([frame], 'TX')

    def _drain_serial_data(self, final=False):
        reader = self.reader
        if reader is None:
            return
        chunks, received, dropped = reader.take_received(self.SERIAL_UI_BATCH_BYTES)
        if final:
            # Retain the newest preview on disconnect, accounting for discarded
            # UI history without affecting the already queued file log.
            remaining, extra_received, extra_dropped = reader.take_received(reader.MAX_PENDING_BYTES)
            received += extra_received
            dropped += extra_dropped
            if remaining:
                chunks = deque(chunks + remaining)
                excess = max(0, sum(map(len, chunks)) - self.SERIAL_UI_BATCH_BYTES)
                dropped += excess
                while excess:
                    first = chunks.popleft()
                    removed = min(excess, len(first))
                    if removed < len(first):
                        chunks.appendleft(first[removed:])
                    excess -= removed
                chunks = list(chunks)
        self.rx_bytes += received
        self._update_counter()
        self._check_log_error()
        if dropped:
            self.rx_display_dropped += dropped
            self.lbl_rx_overflow.setText(f"界面已跳过 {self.rx_display_dropped:,} B")
            self.serial_terminal.note_gap(dropped)
            # A protocol parser must not treat noncontiguous data as a frame.
            self.chk_auto_reply.setChecked(False)
            self.chk_modbus_poll.setChecked(False)
            if self.pending_modbus_request:
                self.modbus_result.setPlainText("串口界面处理超限，响应数据不连续；已停止自动读取，请重新读取。")
            self.pending_modbus_request = None
            self.modbus_timeout.stop()
            self.modbus_rx_buffer.clear()
        if not chunks:
            return
        data = b"".join(chunks)
        self.serial_terminal.feed(data)

        if self.chk_rx_hex.isChecked():
            shown = [bytes_to_hex_str(chunk) for chunk in chunks]
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                shown = [chunk.decode("latin-1") for chunk in chunks]
            else:
                # 保留读取段边界，同时避免批内 UTF-8 字符跨段时被拆坏。
                decoder = codecs.getincrementaldecoder("utf-8")()
                shown = [decoder.decode(chunk) for chunk in chunks]

        prefix = self._display_prefix('RX')
        self.append_log("\n".join(prefix + part for part in shown if part), color="#1b5e20")
        if not final:
            for chunk in chunks:
                if self.pending_modbus_request:
                    self._receive_modbus_data(chunk)
            if self.chk_auto_reply.isChecked():
                # 展示分段不应把同一刷新批次中的回复触发词拆开。
                self._check_auto_reply(data)

    def _send_terminal_data(self, data: bytes, sensitive: bool = False) -> bool:
        if not (self.ser and self.ser.is_open):
            self.serial_terminal.output.append_notice("[请先打开串口]")
            return False
        try:
            # Console input bypasses HEX mode and checksums used by device protocols.
            written = self.ser.write(data)
            self.tx_bytes += written
            self._update_counter()
            if written != len(data):
                raise IOError(f"仅发送 {written}/{len(data)} 字节，请检查设备")
        except Exception as exc:
            self.serial_terminal.output.append_notice(f"[发送失败] {exc}")
            return False
        shown = f"[终端输入 {len(data)} 字节，内容未记录]" if sensitive else repr(data.decode('utf-8', errors='replace'))
        self.append_log(f"{self._display_prefix('TX')}(终端) {shown}", color="#0d47a1")
        self._write_log_raw(f"{datetime.datetime.now().isoformat(timespec='milliseconds')} TX(terminal) -> {shown}\n")
        return True

    def _display_prefix(self, direction: str, timestamp=None) -> str:
        if not self.chk_show_time.isChecked():
            return ""
        stamp = timestamp.strftime("%H:%M:%S.%f")[:-3] if timestamp is not None else now_ms()
        marker = '<<' if direction == 'RX' else '>>'
        return f"[{stamp}] {marker} "

    def append_log(self, text: str, color: str = "#202020"):
        # 行数和字符数同时限制，避免无换行的大包使文档无限增长。
        text = (text + "\n")[-self.MAX_DISPLAY_CHARS:]
        # Qt lays out an entire text block on every append. Limit individual
        # lines too, especially the 3x expansion of a large HEX receive batch.
        width = self.MAX_DISPLAY_LINE_CHARS
        text = "\n".join("\n".join(line[i:i + width] for i in range(0, len(line), width))
                         for line in text.split("\n"))[-self.MAX_DISPLAY_CHARS:]
        scroll = self.recv_edit.verticalScrollBar()
        old_scroll = scroll.value()
        cursor = self.recv_edit.textCursor()
        excess = self.recv_edit.document().characterCount() - 1 + len(text) - self.MAX_DISPLAY_CHARS
        if excess > 0:
            # NextCharacter with a large repeat count walks every glyph in Qt
            # (especially costly for HEX). Select the range by index instead.
            cursor.setPosition(0)
            cursor.setPosition(excess, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setProperty(self.LOG_COLOR_PROPERTY, color)
        fmt.setForeground(self._log_color(color))
        cursor.insertText(text, fmt)
        if self.chk_autoscroll.isChecked():
            self.recv_edit.moveCursor(QTextCursor.End)
        else:
            scroll.setValue(old_scroll)

    # -------------------------------------------------------------- #
    #  发送
    # -------------------------------------------------------------- #
    def on_send_clicked(self):
        self._stop_quick_loop()
        if self.transport_mode.currentIndex() == self.CAN_MODE:
            text = self.send_edit.toPlainText()
            if self.can_panel.send_text(text):
                self._push_history(text)
            return
        if self.transport_mode.currentIndex() == 0 and not (self.ser and self.ser.is_open):
            if not self.chk_auto_send.isChecked():
                QMessageBox.information(self, "提示", "请先打开串口。")
            return
        text = self.send_edit.toPlainText()
        if not text:
            return
        try:
            if self.chk_tx_hex.isChecked():
                data = hex_str_to_bytes(text)
            else:
                data = text.encode("utf-8")
                if self.chk_tx_newline.isChecked():
                    data += b"\r\n"
            data = self._maybe_append_checksum(data)
            if self.transport_mode.currentIndex() != 0:
                if self.network_panel.send_bytes(data):
                    self._push_history(text)
                return
            self.ser.write(data)
        except Exception as e:
            self.chk_auto_send.setChecked(False)
            self.append_log(f"[发送失败] {e}", color="#c0392b")
            return

        self.tx_bytes += len(data)
        self._update_counter()
        shown = bytes_to_hex_str(data) if self.chk_tx_hex.isChecked() else text
        self.append_log(f"{self._display_prefix('TX')}{shown}", color="#0d47a1")
        self._write_log_raw(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} TX -> {shown}\n")

        self._push_history(text)

    def toggle_auto_send(self, on: bool):
        if on:
            self._stop_quick_loop()
            self.auto_send_timer.start(self.spn_interval.value())
        else:
            self.auto_send_timer.stop()

    # -------------------------------------------------------------- #
    #  历史
    # -------------------------------------------------------------- #
    def _push_history(self, text: str):
        text = text.strip()
        if not text:
            return
        if self.cmb_history.count() and self.cmb_history.itemText(0) == text:
            return
        items = [self.cmb_history.itemText(i) for i in range(self.cmb_history.count())]
        if text in items:
            items.remove(text)
        items.insert(0, text)
        items = items[:self.MAX_HISTORY]
        self.cmb_history.blockSignals(True)
        self.cmb_history.clear()
        self.cmb_history.addItems(items)
        self.cmb_history.blockSignals(False)

    def on_history_selected(self, idx: int):
        if idx < 0:
            return
        self.send_edit.setPlainText(self.cmb_history.itemText(idx))

    def _persist_history(self):
        items = [self.cmb_history.itemText(i) for i in range(self.cmb_history.count())]
        self.settings.setValue("history", json.dumps(items, ensure_ascii=False))
        self.settings.sync()

    def on_history_delete(self):
        idx = self.cmb_history.currentIndex()
        if idx < 0:
            return
        self.cmb_history.removeItem(idx)
        self._persist_history()
        self.status.showMessage("已删除所选发送历史", 3000)

    def on_history_clear(self):
        self.cmb_history.clear()
        self._persist_history()
        self.status.showMessage("已清空发送历史", 3000)

    # -------------------------------------------------------------- #
    #  自动回复
    # -------------------------------------------------------------- #
    def _del_rule_rows(self):
        rows = sorted({i.row() for i in self.reply_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.reply_table.removeRow(r)

    def _check_auto_reply(self, data: bytes):
        if not (self.ser and self.ser.is_open):
            return
        hex_match = self.chk_reply_hex_match.isChecked()
        hex_send = self.chk_reply_hex_send.isChecked()

        if hex_match:
            haystack = bytes_to_hex_str(data).replace(" ", "").upper()
        else:
            try:
                haystack = data.decode("utf-8", errors="replace")
            except Exception:
                haystack = ""

        for r in range(self.reply_table.rowCount()):
            en_cb = self._cell_checkbox(self.reply_table.cellWidget(r, 0))
            if not (en_cb and en_cb.isChecked()):
                continue
            trig_item = self.reply_table.item(r, 1)
            reply_item = self.reply_table.item(r, 2)
            if not trig_item or not reply_item:
                continue
            trig = trig_item.text()
            reply = reply_item.text()
            if not trig or not reply:
                continue

            if hex_match:
                needle = trig.replace(" ", "").upper()
                hit = needle and needle in haystack
            else:
                hit = trig in haystack

            if hit:
                try:
                    out = hex_str_to_bytes(reply) if hex_send else reply.encode("utf-8")
                    out = self._maybe_append_checksum(out)
                    self.ser.write(out)
                    self.tx_bytes += len(out)
                    self._update_counter()
                    shown = bytes_to_hex_str(out) if hex_send else reply
                    self.append_log(f"{self._display_prefix('TX')}(auto) {shown}", color="#6a1b9a")
                    self._write_log_raw(
                        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} TX(auto) -> {shown}\n")
                except Exception as e:
                    self.append_log(f"[自动回复失败] {e}", color="#c0392b")

    # -------------------------------------------------------------- #
    #  Modbus
    # -------------------------------------------------------------- #
    def _on_modbus_func_changed(self):
        function_code = self.cmb_modbus_func.currentData()
        is_read = function_code in (0x01, 0x02, 0x03, 0x04)
        if not is_read:
            self.chk_modbus_poll.setChecked(False)
        is_multi_write = function_code in (0x0F, 0x10)
        self.btn_modbus_read.setEnabled(is_read)
        self.btn_modbus_write.setEnabled(not is_read)
        self.spn_modbus_qty.setEnabled(is_read or is_multi_write)
        self.edit_modbus_values.setEnabled(not is_read)

        if function_code in (0x01, 0x02):
            self.spn_modbus_qty.setRange(1, 2000)
        elif function_code in (0x03, 0x04):
            self.spn_modbus_qty.setRange(1, 125)
        elif function_code == 0x0F:
            self.spn_modbus_qty.setRange(1, 1968)
        elif function_code == 0x10:
            self.spn_modbus_qty.setRange(1, 123)
        else:
            self.spn_modbus_qty.setRange(1, 1)
            self.spn_modbus_qty.setValue(1)

    def _modbus_address_wire(self) -> int:
        address = self.spn_modbus_addr.value()
        if self.chk_modbus_base1.isChecked():
            if address <= 0:
                raise ValueError("Base 1 地址模式下，地址必须大于 0。")
            address -= 1
        return address

    def _current_modbus_config(self) -> dict:
        function_code = int(self.cmb_modbus_func.currentData())
        address_wire = self._modbus_address_wire()
        values = parse_number_list(self.edit_modbus_values.text()) if self.edit_modbus_values.text().strip() else []
        quantity = self.spn_modbus_qty.value()
        if function_code in (0x0F, 0x10) and values:
            quantity = len(values)
        return {
            "slave_id": self.spn_modbus_slave.value(),
            "function_code": function_code,
            "address": address_wire,
            "quantity": quantity,
            "values": values,
            "display_address": self.spn_modbus_addr.value(),
            "base1": self.chk_modbus_base1.isChecked(),
            "scan_rate": self.spn_modbus_scan.value(),
            "signed": self.chk_modbus_signed.isChecked(),
            "entries": self.modbus_entries,
            "table_context": self.modbus_table_context,
        }

    def _format_modbus_result(self, result: dict) -> str:
        lines = [
            f"从站: {result['slave_id']}",
            f"功能: {result['function_text']} (0x{result['function_code']:02X})",
        ]
        if "values" in result:
            values = result["values"]
            lines.append(f"数据: {', '.join(str(v) for v in values)}")
        if "address" in result:
            lines.append(f"地址: {result['address']}")
        if "value" in result:
            lines.append(f"回显值: {result['value']}")
        if "quantity" in result:
            lines.append(f"数量: {result['quantity']}")
        return "\n".join(lines)

    def _format_binary_mbp_profile(self, profile: dict) -> str:
        return (f"已导入 Modbus Poll 配置：ID={profile['slave_id']}，"
                f"F={profile['function_code']:02d}，地址=0x{profile['start_address']:04X}，"
                f"数量={profile['quantity']}，SR={profile['scan_rate']} ms\n"
                "表格显示文件保存值；读取成功后更新为设备实时值。")

    def _render_modbus_table(self):
        blocked = self.modbus_table.blockSignals(True)
        entries = self.modbus_entries
        rows = min(16, len(entries))
        groups = max(1, (len(entries) + 15) // 16)
        self.modbus_table.setRowCount(rows)
        self.modbus_table.setColumnCount(groups * 2)
        headers = []
        for group in range(groups):
            idx = group * 16
            headers.extend(["Name", f"{entries[idx]['address']:04X}" if idx < len(entries) else "值"])
        self.modbus_table.setHorizontalHeaderLabels(headers)
        self.modbus_table.setVerticalHeaderLabels([f"{i:X}" for i in range(rows)])
        self.modbus_table.clearContents()
        for i, entry in enumerate(entries):
            row, col = i % 16, (i // 16) * 2
            value = entry.get("value")
            if value is not None and self.chk_modbus_signed.isChecked() and value >= 32768:
                value -= 65536
            for column, text in ((col, entry.get("comment", "")),
                                 (col + 1, "未知" if value is None else str(value))):
                item = QTableWidgetItem(text)
                item.setToolTip(f"地址: {entry['address']} (0x{entry['address']:04X})")
                self.modbus_table.setItem(row, column, item)
        self.modbus_table.blockSignals(blocked)

    def _modbus_cell_to_write(self, row, column):
        if self.chk_modbus_edit.isChecked():
            return
        idx = (column // 2) * 16 + row
        if idx >= len(self.modbus_entries) or not self.modbus_table_context:
            return
        slave, function = self.modbus_table_context
        if function not in (1, 3):
            return
        entry = self.modbus_entries[idx]
        self.chk_modbus_poll.setChecked(False)
        self.spn_modbus_slave.setValue(slave)
        self.cmb_modbus_func.setCurrentIndex(self.cmb_modbus_func.findData(5 if function == 1 else 6))
        self.chk_modbus_base1.setChecked(False)
        self.spn_modbus_addr.setValue(entry["address"])
        self.edit_modbus_values.setText(str(entry["value"]) if entry.get("value") is not None else "")

    def _toggle_modbus_poll(self, enabled):
        if enabled and self.chk_modbus_edit.isChecked():
            self.chk_modbus_poll.setChecked(False)
            self.modbus_result.setPlainText("请先退出编辑配置，再开始自动读取。")
            return
        if enabled:
            if not (self.ser and self.ser.is_open) or self.cmb_modbus_func.currentData() not in (1, 2, 3, 4):
                self.chk_modbus_poll.setChecked(False)
                self.modbus_result.setPlainText("自动读取需要先打开串口并选择读功能码。")
                return
            self._stop_quick_loop()
            self.modbus_poll_timer.start(self.spn_modbus_scan.value())
            self._poll_modbus()
        else:
            self.modbus_poll_timer.stop()

    def _poll_modbus(self):
        self.modbus_poll_timer.setInterval(self.spn_modbus_scan.value())
        if self.pending_modbus_request:
            return
        try:
            self._send_modbus_request(self._current_modbus_config())
        except Exception as e:
            self.chk_modbus_poll.setChecked(False)
            self.modbus_result.setPlainText(str(e))

    def _on_modbus_timeout(self):
        self.modbus_timeout.stop()
        self.pending_modbus_request = None
        self.modbus_rx_buffer.clear()
        self.modbus_result.setPlainText("Modbus 响应超时，请检查从站、串口参数与接线。")

    def _receive_modbus_data(self, data):
        self.modbus_rx_buffer.extend(data)
        request = self.pending_modbus_request
        buf = self.modbus_rx_buffer
        while len(buf) >= 2:
            if buf[0] != request["slave_id"] or buf[1] not in (request["function_code"], request["function_code"] | 0x80):
                del buf[0]
                continue
            if len(buf) < 3:
                return
            size = 5 if buf[1] & 0x80 else 5 + buf[2] if buf[1] in (1, 2, 3, 4) else 8
            if len(buf) < size:
                return
            frame = bytes(buf[:size])
            if calc_crc16_modbus(frame[:-2]) != frame[-2:]:
                del buf[0]
                continue
            self.modbus_timeout.stop()
            self.pending_modbus_request = None
            buf.clear()
            try:
                result = parse_modbus_rtu_response(request, frame)
                if "values" in result:
                    context = [request["slave_id"], request["function_code"]]
                    old = {e["address"]: e for e in self.modbus_entries} if self.modbus_table_context == context else {}
                    self.modbus_entries = [
                        {"address": request["address"] + i, "value": value,
                         "comment": old.get(request["address"] + i, {}).get("comment", "")}
                        for i, value in enumerate(result["values"])
                    ]
                    self.modbus_table_context = context
                    self._render_modbus_table()
                self.modbus_result.setPlainText(self._format_modbus_result(result))
            except ValueError as e:
                self.modbus_result.setPlainText(f"Modbus 响应解析失败：{e}")
            return

    def _send_modbus_request(self, config: dict, update_send_editor: bool = False):
        if self.chk_modbus_edit.isChecked() and not update_send_editor:
            raise ValueError("请先退出编辑配置，再与设备通信。")
        frame = build_modbus_rtu_request(
            config["slave_id"],
            config["function_code"],
            config["address"],
            config["quantity"],
            config["values"],
        )
        hex_frame = bytes_to_hex_str(frame)
        if update_send_editor:
            self.send_edit.setPlainText(hex_frame)
            self.chk_tx_hex.setChecked(True)
            self.modbus_result.setPlainText(f"待发送帧：\n{hex_frame}")
            return frame

        if not (self.ser and self.ser.is_open):
            self.modbus_result.setPlainText(f"待发送帧：\n{hex_frame}")
            return frame

        if self.pending_modbus_request:
            raise ValueError("正在等待上一次 Modbus 响应，请稍后重试。")
        self.modbus_rx_buffer.clear()
        written = self.ser.write(frame)
        if written != len(frame):
            raise ValueError("Modbus 请求未完整发送。")
        self.pending_modbus_request = {
            **config,
            "tx_hex": hex_frame,
        }
        self.modbus_timeout.start(1000)
        self.tx_bytes += len(frame)
        self._update_counter()
        self.append_log(f"{self._display_prefix('TX')}[Modbus] {hex_frame}", color="#0d47a1")
        self._write_log_raw(
            f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} TX[Modbus] -> {hex_frame}\n")
        self.modbus_result.setPlainText(f"请求已发送：\n{hex_frame}")
        return frame

    def on_modbus_read_clicked(self):
        try:
            config = self._current_modbus_config()
            if config["function_code"] not in (0x01, 0x02, 0x03, 0x04):
                raise ValueError("当前功能码不是读操作。")
            self._send_modbus_request(config)
        except Exception as e:
            QMessageBox.warning(self, "Modbus", str(e))

    def on_modbus_write_clicked(self):
        try:
            config = self._current_modbus_config()
            if config["function_code"] in (0x01, 0x02, 0x03, 0x04):
                raise ValueError("当前功能码不是写操作。")
            self._send_modbus_request(config)
        except Exception as e:
            QMessageBox.warning(self, "Modbus", str(e))

    def on_modbus_fill_send_clicked(self):
        try:
            config = self._current_modbus_config()
            self._send_modbus_request(config, update_send_editor=True)
        except Exception as e:
            QMessageBox.warning(self, "Modbus", str(e))

    def _set_modbus_config(self, config: dict):
        self.chk_modbus_edit.setChecked(False)
        self.chk_modbus_poll.setChecked(False)
        self.modbus_timeout.stop()
        self.pending_modbus_request = None
        self.modbus_rx_buffer.clear()
        self.spn_modbus_slave.setValue(int(config.get("slave_id", 1)))
        function_code = int(config.get("function_code", 0x03))
        idx = self.cmb_modbus_func.findData(function_code)
        if idx >= 0:
            self.cmb_modbus_func.setCurrentIndex(idx)
        self.chk_modbus_base1.setChecked(bool(config.get("base1", False)))
        self.spn_modbus_addr.setValue(int(config.get("display_address", config.get("address", 0))))
        self.spn_modbus_qty.setValue(max(1, int(config.get("quantity", 1))))
        self.edit_modbus_values.setText(", ".join(str(v) for v in config.get("values", [])))
        self.spn_modbus_scan.setValue(int(config.get("scan_rate", 1000)))
        self.chk_modbus_signed.setChecked(bool(config.get("signed", True)))
        self.modbus_entries = config.get("entries", [])
        self.modbus_table_context = config.get("table_context", [self.spn_modbus_slave.value(), function_code])
        self._render_modbus_table()
        self._on_modbus_func_changed()

    def _toggle_modbus_edit(self, enabled):
        if enabled:
            self.chk_modbus_poll.setChecked(False)
            if self.pending_modbus_request:
                self.chk_modbus_edit.setChecked(False)
                self.modbus_result.setPlainText("请等待当前请求完成后再编辑配置。")
                return
        self.modbus_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            if enabled else QAbstractItemView.NoEditTriggers)
        self.btn_modbus_clear_items.setEnabled(enabled)
        self.btn_modbus_trim.setEnabled(enabled)

    def on_modbus_clear_items(self):
        if not self.chk_modbus_edit.isChecked():
            return
        indices = {item.column() // 2 * 16 + item.row()
                   for item in self.modbus_table.selectedItems()}
        indices = {i for i in indices if i < len(self.modbus_entries)}
        if not indices:
            self.modbus_result.setPlainText("请先选中要清空的寄存器名称或值，可用 Ctrl/Shift 多选。")
            return
        for i in indices:
            self.modbus_entries[i].update(comment="", value=None)
        self._render_modbus_table()
        self.modbus_result.setPlainText(f"已清空 {len(indices)} 项，地址和数量保持不变；请保存 MBP。")

    def on_modbus_trim(self):
        if not self.chk_modbus_edit.isChecked():
            return
        total = len(self.modbus_entries)
        if total <= 1 or not self.modbus_table_context:
            self.modbus_result.setPlainText("至少需要保留 1 项，当前表格无法继续缩减。")
            return
        count, accepted = QInputDialog.getInt(
            self, "缩减末尾项", f"当前 {total} 项，保留前多少项？", total - 1, 1, total - 1)
        if not accepted:
            return
        entries = self.modbus_entries[:count]
        slave, function = self.modbus_table_context
        # Use the table's source context, even if the form was switched to a single write.
        self._set_modbus_config({
            "slave_id": slave, "function_code": function,
            "address": entries[0]["address"], "quantity": count,
            "entries": entries, "table_context": [slave, function],
            "scan_rate": self.spn_modbus_scan.value(),
            "signed": self.chk_modbus_signed.isChecked(),
        })
        self.chk_modbus_edit.setChecked(True)
        self.modbus_result.setPlainText(f"已保留前 {count} 项，移除末尾 {total - count} 项；读取参数已同步，请保存 MBP。")

    def _on_modbus_item_changed(self, item):
        idx = item.column() // 2 * 16 + item.row()
        if not self.chk_modbus_edit.isChecked() or idx >= len(self.modbus_entries):
            return
        entry = self.modbus_entries[idx]
        if item.column() % 2 == 0:
            entry["comment"] = item.text()
        else:
            text = item.text().strip()
            try:
                if not text or text == "未知":
                    value = None
                else:
                    value = int(text, 16) if text.lower().startswith("0x") else int(text)
                    function = self.modbus_table_context[1]
                    minimum = -32768 if self.chk_modbus_signed.isChecked() else 0
                    if function in (1, 2):
                        if value not in (0, 1):
                            raise ValueError("线圈值只能为 0 或 1。")
                    elif not minimum <= value <= 65535:
                        raise ValueError(f"寄存器值范围为 {minimum}~65535。")
                    value &= 0xFFFF
                entry["value"] = value
            except ValueError as e:
                self.modbus_result.setPlainText(f"数值无效，已恢复原值：{e}")
                self._render_modbus_table()
                return
        self.modbus_result.setPlainText("配置已修改，请点击保存 MBP；编辑不会写入设备。")

    def on_modbus_new_profile(self):
        try:
            if self.pending_modbus_request:
                raise ValueError("请等待当前请求完成后再新建配置。")
            function = self.cmb_modbus_func.currentData()
            if function not in (1, 2, 3, 4):
                raise ValueError("新建表格前请选择 01/02/03/04 读功能码。")
            address = self._modbus_address_wire()
            quantity = self.spn_modbus_qty.value()
            build_modbus_rtu_request(self.spn_modbus_slave.value(), function, address, quantity)
            self.chk_modbus_poll.setChecked(False)
            self.modbus_entries = [
                {"address": address + i, "comment": "", "value": 0}
                for i in range(quantity)
            ]
            self.modbus_table_context = [self.spn_modbus_slave.value(), function]
            self._render_modbus_table()
            self.chk_modbus_edit.setChecked(True)
            self.modbus_result.setPlainText("已按当前参数新建配置；双击名称或值进行编辑，然后保存 MBP。")
        except ValueError as e:
            QMessageBox.warning(self, "新建失败", str(e))

    def on_modbus_open_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 Modbus 配置",
            str(Path.home()),
            "Modbus Profile (*.mbp *.json);;All Files (*)",
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))
            return

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            profile = parse_binary_mbp_profile(data)
            if not profile:
                QMessageBox.warning(
                    self,
                    "打开失败",
                    "该 .mbp 不是本工具保存的 JSON 配置，或属于尚未支持/已损坏的 Modbus Poll 二进制版本。",
                )
                return
            self._set_modbus_config({
                "slave_id": profile["slave_id"],
                "scan_rate": profile["scan_rate"],
                "signed": profile["signed"],
                "entries": profile["entries"],
                "function_code": profile["function_code"],
                "address": profile["start_address"],
                "display_address": profile["start_address"],
                "quantity": profile["quantity"],
                "values": [],
                "base1": False,
            })
            self.modbus_result.setPlainText(self._format_binary_mbp_profile(profile))
            self.status.showMessage(f"已导入 {os.path.basename(path)}", 5000)
            return

        if not isinstance(payload, dict) or payload.get("format") != "qt5com-mbp":
            QMessageBox.warning(self, "打开失败", "文件格式不是 DebugTool 的 Modbus 配置。")
            return

        self._set_modbus_config(payload.get("modbus", {}))
        self.modbus_result.setPlainText(f"已打开配置：{path}")
        self.status.showMessage(f"已打开 {os.path.basename(path)}", 3000)

    def on_modbus_save_profile(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存 Modbus 配置",
            str(Path.home() / "modbus_profile.mbp"),
            "DebugTool Profile (*.mbp);;JSON (*.json)",
        )
        if not path:
            return
        try:
            if not Path(path).suffix:
                path += ".mbp"
            payload = {
                "format": "qt5com-mbp",
                "version": 1,
                "modbus": self._current_modbus_config(),
            }
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.status.showMessage(f"已保存 {os.path.basename(path)}", 3000)
        self.modbus_result.setPlainText(f"已保存配置：{path}")

    # -------------------------------------------------------------- #
    #  计数器
    # -------------------------------------------------------------- #
    def _update_counter(self):
        if self.transport_mode.currentIndex() == self.CAN_MODE:
            panel = self.can_panel
            self.lbl_counter.setText(f"CAN TX: {panel.tx_frames} 帧 / {panel.tx_bytes} B  "
                                     f"RX: {panel.rx_frames} 帧 / {panel.rx_bytes} B")
        else:
            self.lbl_counter.setText(f"TX: {self.tx_bytes}  RX: {self.rx_bytes}")

    def _reset_counter(self):
        self.network_panel.reset_counts()
        self.can_panel.reset_counts()
        self.tx_bytes = self.rx_bytes = 0
        self._update_counter()

    # -------------------------------------------------------------- #
    #  配置文件
    # -------------------------------------------------------------- #
    def _load_settings(self):
        s = self.settings
        baud = s.value("baud", "115200")
        idx = self.cmb_baud.findText(baud)
        if idx >= 0:
            self.cmb_baud.setCurrentIndex(idx)
        else:
            self.cmb_baud.setEditText(str(baud))
        self.cmb_data.setCurrentText(s.value("data", "8"))
        self.cmb_parity.setCurrentText(s.value("parity", "None (N)"))
        self.cmb_stop.setCurrentText(s.value("stop", "1"))

        self.chk_rx_hex.setChecked(s.value("rx_hex", False, type=bool))
        self.chk_tx_hex.setChecked(s.value("tx_hex", False, type=bool))
        self.chk_tx_newline.setChecked(s.value("tx_newline", False, type=bool))
        self.chk_show_time.setChecked(s.value("show_time", True, type=bool))
        self.chk_autoscroll.setChecked(s.value("autoscroll", True, type=bool))
        self.chk_log_save.setChecked(s.value("log_save", False, type=bool))
        self.chk_show_all_ports.setChecked(
            s.value("show_all_ports", False, type=bool))

        theme = s.value("theme", "浅色")
        self.cmb_theme.blockSignals(True)
        self.cmb_theme.setCurrentText(theme)
        self.cmb_theme.blockSignals(False)

        self.spn_interval.setValue(int(s.value("auto_interval", 1000)))
        self.spn_quick_interval.setValue(s.value("quick_loop_interval", 1000, type=int))
        self.spn_quick_rounds.setValue(s.value("quick_loop_rounds", 1, type=int))

        self.chk_auto_reply.setChecked(s.value("auto_reply", False, type=bool))
        self.chk_reply_hex_match.setChecked(s.value("reply_hex_match", False, type=bool))
        self.chk_reply_hex_send.setChecked(s.value("reply_hex_send", False, type=bool))

        # 校验
        self.chk_checksum.setChecked(s.value("chk_enable", False, type=bool))
        self.cmb_checksum.setCurrentText(s.value("chk_type", "SUM (1B)"))
        self.spn_chk_start.setValue(int(s.value("chk_start", 1)))
        self.spn_chk_end.setValue(int(s.value("chk_end", 0)))

        modbus_raw = s.value("modbus_profile", "")
        if modbus_raw:
            try:
                self._set_modbus_config(json.loads(modbus_raw))
            except Exception:
                pass

        # 历史
        hist = s.value("history", [])
        if isinstance(hist, str):
            try:
                hist = json.loads(hist)
            except Exception:
                hist = [hist]
        if hist:
            self.cmb_history.addItems(list(hist))

        # 自动回复规则
        rules_raw = s.value("reply_rules", "[]")
        try:
            rules = json.loads(rules_raw) if isinstance(rules_raw, str) else rules_raw
        except Exception:
            rules = []
        for rule in rules or []:
            self._add_reply_row(
                rule.get("trigger", ""),
                rule.get("reply", ""),
                bool(rule.get("enabled", True)),
            )

        # 快捷按钮
        quicks_raw = s.value("quick_buttons", "[]")
        try:
            quicks = json.loads(quicks_raw) if isinstance(quicks_raw, str) else quicks_raw
        except Exception:
            quicks = []
        for q in quicks or []:
            self._add_quick_row(
                q.get("name", ""),
                q.get("content", ""),
                bool(q.get("hex", False)),
                bool(q.get("newline", False)),
                bool(q.get("loop", False)),
            )

        last_send = s.value("last_send", "")
        if last_send:
            self.send_edit.setPlainText(last_send)

        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)

    def _save_settings(self):
        s = self.settings
        self.ssh_panel.save_settings()
        self.network_panel.save_settings()
        self.can_panel.save_settings()
        self.settings.setValue("transport_mode", self.transport_mode.currentIndex())
        s.setValue("baud", self.cmb_baud.currentText())
        s.setValue("data", self.cmb_data.currentText())
        s.setValue("parity", self.cmb_parity.currentText())
        s.setValue("stop", self.cmb_stop.currentText())

        s.setValue("rx_hex", self.chk_rx_hex.isChecked())
        s.setValue("tx_hex", self.chk_tx_hex.isChecked())
        s.setValue("tx_newline", self.chk_tx_newline.isChecked())
        s.setValue("show_time", self.chk_show_time.isChecked())
        s.setValue("autoscroll", self.chk_autoscroll.isChecked())
        s.setValue("log_save", self.chk_log_save.isChecked())
        s.setValue("show_all_ports", self.chk_show_all_ports.isChecked())

        s.setValue("auto_interval", self.spn_interval.value())
        s.setValue("quick_loop_interval", self.spn_quick_interval.value())
        s.setValue("quick_loop_rounds", self.spn_quick_rounds.value())
        s.setValue("auto_reply", self.chk_auto_reply.isChecked())
        s.setValue("reply_hex_match", self.chk_reply_hex_match.isChecked())
        s.setValue("reply_hex_send", self.chk_reply_hex_send.isChecked())

        s.setValue("chk_enable", self.chk_checksum.isChecked())
        s.setValue("chk_type", self.cmb_checksum.currentText())
        s.setValue("chk_start", self.spn_chk_start.value())
        s.setValue("chk_end", self.spn_chk_end.value())
        s.setValue("modbus_profile", json.dumps(self._current_modbus_config(), ensure_ascii=False))

        hist = [self.cmb_history.itemText(i) for i in range(self.cmb_history.count())]
        s.setValue("history", json.dumps(hist, ensure_ascii=False))

        rules = []
        for r in range(self.reply_table.rowCount()):
            en_cb = self._cell_checkbox(self.reply_table.cellWidget(r, 0))
            t = self.reply_table.item(r, 1)
            p = self.reply_table.item(r, 2)
            rules.append({
                "enabled": bool(en_cb and en_cb.isChecked()),
                "trigger": t.text() if t else "",
                "reply": p.text() if p else "",
            })
        s.setValue("reply_rules", json.dumps(rules, ensure_ascii=False))

        s.setValue("quick_buttons", json.dumps(self._quick_entries(), ensure_ascii=False))

        s.setValue("last_send", self.send_edit.toPlainText())
        s.setValue("geometry", self.saveGeometry())

    # -------------------------------------------------------------- #
    def _wait_for_can_exit(self):
        if self.can_panel.closing:
            QTimer.singleShot(50, self._wait_for_can_exit)
        else:
            self.close()

    def closeEvent(self, ev):
        self._stop_quick_loop()
        # Let the worker release the hardware and deliver its final confirmed
        # sends before Qt exits. Driver calls never run or join on this thread.
        if getattr(self.can_panel.bus, 'async_tx', False):
            if not getattr(self, '_can_exit_pending', False):
                self._can_exit_pending = True
                self.chk_auto_send.setChecked(False)
                self.transport_mode.setEnabled(False)
                self.connection_stack.setEnabled(False)
                self.btn_send.setEnabled(False)
                self.chk_auto_send.setEnabled(False)
                self.can_panel.stop()
                self.status.showMessage('正在关闭 CAN 驱动，完成后退出…')
                QTimer.singleShot(50, self._wait_for_can_exit)
            ev.ignore()
            return
        if self.local_terminal.process is not None:
            if not getattr(self, '_local_closing', False):
                self._local_closing = True
                self.local_terminal.stopped.connect(self.close)
            self.local_terminal.stop()
            ev.ignore()
            return
        if self.ssh_panel.worker is not None:
            self.ssh_panel.worker.stop()
            if not getattr(self, '_ssh_closing', False):
                self._ssh_closing = True
                self.ssh_panel.disconnected.connect(self.close)
            self.ssh_panel.status.setText("正在关闭 SSH，请稍候…")
            ev.ignore()
            return
        try:
            self._save_settings()
        finally:
            self.network_panel.stop()
            self.can_panel.stop()
            self.close_port()
        super().closeEvent(ev)


# ------------------------------------------------------------------ #
def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("RUIO")
    # GNOME Shell uses the desktop file ID to associate a running window with
    # its launcher and icon. The value must match qt5com.desktop without suffix.
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("qt5com")
    theme_icon = QIcon.fromTheme("qt5com")
    bundled_icon = app_icon_path()
    app.setWindowIcon(
        QIcon(bundled_icon) if os.path.isfile(bundled_icon) else theme_icon)
    w = DebugTool()
    if os.environ.get("QT5COM_FULLSCREEN") == "1":
        # linuxfb has no window manager to correct saved desktop geometry.
        w.setGeometry(app.primaryScreen().geometry())
        w.showFullScreen()
    else:
        w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
