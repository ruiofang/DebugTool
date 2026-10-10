"""Bounded Qt CAN transports and explicit CAN frame send controls."""

import queue
import shlex
import sys
import threading
import time
from pathlib import Path

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from socketcan_bus import (CanFrame, SocketCanBus, interface_details, list_can_interfaces,
                           parse_can_id, parse_hex_payload)
from can_configurator import CanConfigurator
from driver_can_bus import DriverCanBus, discover_channels


class CanPanel(QWidget):
    received = pyqtSignal(object)  # One bounded list[CanFrame] per timer tick.
    sent = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    stop_sending = pyqtSignal()

    MAX_RX_BATCH = 256
    MAX_RX_SECONDS = 0.006
    FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
    DRIVER_WAIT_SECONDS = 15
    DISCOVERY_WAIT_SECONDS = 10

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.bus = None
        self.active = False
        self.connecting = False
        self.closing = False
        self.configurator = None
        self._connect_request = 0
        self._pending_connection = None
        self._connected_interface = None
        self._pending_close = None
        self._failed_close_interface = None
        self._selected_backend = None
        self._channels = {}
        self._discovery_jobs = {}
        self._driver_wait_since = 0
        self._driver_wait_reported = False
        self._driver_close_reason = ''
        self._driver_receive_error_reported = False
        self.tx_frames = self.rx_frames = self.tx_bytes = self.rx_bytes = 0
        self._reported_drops = 0
        layout = QVBoxLayout(self)
        grid = QGridLayout()

        self.backend = QComboBox()
        self.backend.addItem('SocketCAN（Linux）', 'socketcan')
        self.backend.addItem('PCAN（PEAK）', 'pcan')
        self.backend.addItem('candleLight（gs_usb）', 'gs_usb')
        if sys.platform != 'linux':
            self.backend.model().item(0).setEnabled(False)
        self.backend.setToolTip('选择适配器驱动；Windows 使用 PCAN 或 candleLight。')
        grid.addWidget(QLabel('CAN 驱动'), 0, 0)
        grid.addWidget(self.backend, 0, 1)

        self.interface = QComboBox()
        self.interface.setEditable(True)
        self.interface.setToolTip('Linux SocketCAN 接口名称，例如 can0 或 vcan0。')
        self.refresh = QPushButton('刷新')
        self.refresh.clicked.connect(self.refresh_interfaces)
        interface_row = QHBoxLayout()
        interface_row.addWidget(self.interface, 1)
        interface_row.addWidget(self.refresh)
        self.interface_label = QLabel('CAN 接口')
        grid.addWidget(self.interface_label, 1, 0, 1, 2)
        grid.addLayout(interface_row, 2, 0, 1, 2)
        self.bitrate = QComboBox()
        self.bitrate.setEditable(True)
        self.bitrate.addItems(['10000', '20000', '50000', '100000', '125000',
                               '250000', '500000', '800000', '1000000'])
        self.bitrate.setCurrentText('500000')
        self.bitrate.setToolTip('单位 bit/s；必须与总线其他节点一致。点击打开时自动应用。')
        self.data_bitrate = QComboBox()
        self.data_bitrate.setEditable(True)
        self.data_bitrate.addItems(['1000000', '2000000', '4000000', '5000000', '8000000'])
        self.data_bitrate.setCurrentText('2000000')
        self.data_bitrate.setToolTip('CAN FD 数据比特率，单位 bit/s；需与总线配置一致。')
        self.data_bitrate_label = QLabel('FD 数据比特率')
        grid.addWidget(QLabel('仲裁比特率'), 3, 0)
        grid.addWidget(self.bitrate, 3, 1)
        grid.addWidget(self.data_bitrate_label, 4, 0)
        grid.addWidget(self.data_bitrate, 4, 1)
        self.enable_fd = QCheckBox('启用 CAN FD')
        self.enable_fd.setToolTip('同时接收经典 CAN 和 CAN FD；发送 FD 帧也需要接口支持 CAN FD。')
        self.receive_own = QCheckBox('接收本程序发送的帧')
        self.receive_own.setToolTip('启用 SocketCAN 本 socket 回环；默认关闭，避免把 TX 回环当作设备回复。')
        self.error_frames = QCheckBox('接收总线错误帧')
        self.error_frames.setChecked(True)
        grid.addWidget(self.enable_fd, 5, 0, 1, 2)
        grid.addWidget(self.receive_own, 6, 0, 1, 2)
        grid.addWidget(self.error_frames, 7, 0, 1, 2)
        self.rx_id = QLineEdit('0')
        self.rx_mask = QLineEdit('0')
        self.rx_format = QComboBox()
        self.rx_format.addItems(['任意格式', '标准帧', '扩展帧'])
        self.rx_id.setToolTip('接收 ID，十六进制；连接前设置。')
        self.rx_mask.setToolTip('接收掩码，十六进制；0 接收所有 ID，7FF 精确匹配标准 ID，1FFFFFFF 精确匹配扩展 ID。')
        for row, label, widget in (
            (8, 'RX ID (HEX)', self.rx_id),
            (9, 'RX 掩码', self.rx_mask),
            (10, 'RX 格式', self.rx_format),
        ):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        self.toggle = QPushButton('打开')
        self.toggle.clicked.connect(self.toggle_connection)
        self.config_button = QPushButton('高级…')
        self.config_button.clicked.connect(self.show_config_dialog)
        grid.addWidget(self.toggle, 11, 0)
        grid.addWidget(self.config_button, 11, 1)
        layout.addLayout(grid)
        self.interface_hint = QLabel('比特率单位 bit/s，需与总线一致。\n点击打开自动配置。')
        self.interface_hint.setWordWrap(True)
        self.interface_hint.setToolTip('需要权限时弹出系统认证窗口。点击“关闭”会停用共享 CAN 接口，'
                                       '其他使用此接口的程序也会停止收发。切换模式或退出只断开本程序连接。')
        layout.addWidget(self.interface_hint)
        self.status = QLabel('未打开')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.retry_stop = QPushButton()
        self.retry_stop.clicked.connect(self.close_interface)
        self.retry_stop.hide()
        layout.addWidget(self.retry_stop)
        self.counts = QLabel()
        self.counts.setToolTip('字节数仅统计数据载荷，RTR 为 0 字节。TX 计数表示内核接受发送，不表示已收到总线 ACK。')
        layout.addWidget(self.counts)
        self.overflow = QLabel()
        self.overflow.setWordWrap(True)
        self.overflow.setStyleSheet('color: #d97706;')
        self.overflow.hide()
        layout.addWidget(self.overflow)
        layout.addStretch()

        # Main embeds these alongside the shared send editor and send timer.
        self.send_options = QWidget(self)
        send_layout = QGridLayout(self.send_options)
        send_layout.setContentsMargins(0, 0, 0, 0)
        self.tx_id = QLineEdit('123')
        self.tx_id.setMaximumWidth(140)
        self.tx_id.setToolTip('十六进制 ID：标准帧 000–7FF，扩展帧 00000000–1FFFFFFF。')
        self.tx_extended = QCheckBox('扩展 ID')
        self.frame_type = QComboBox()
        self.frame_type.addItems(['经典数据帧', '远程帧 RTR', 'CAN FD 数据帧'])
        self.rtr_dlc = QSpinBox()
        self.rtr_dlc.setRange(0, 8)
        self.rtr_dlc.setToolTip('RTR 请求的数据长度；发送框必须为空，RTR 帧不携带数据。')
        self.rtr_dlc_label = QLabel('请求长度')
        self.brs = QCheckBox('BRS 加速')
        self.brs.setToolTip('CAN FD 数据阶段切换到系统配置的 dbitrate。')
        send_layout.addWidget(QLabel('CAN ID (HEX)'), 0, 0)
        send_layout.addWidget(self.tx_id, 0, 1)
        send_layout.addWidget(self.tx_extended, 0, 2)
        send_layout.addWidget(self.frame_type, 0, 3)
        send_layout.addWidget(self.rtr_dlc_label, 0, 4)
        send_layout.addWidget(self.rtr_dlc, 0, 5)
        send_layout.addWidget(self.brs, 0, 6)
        send_layout.setColumnStretch(7, 1)
        self.payload_hint = QLabel('发送框填写 HEX 数据；经典 CAN 最多 8 字节，可发送空数据帧。')
        self.payload_hint.setWordWrap(True)
        send_layout.addWidget(self.payload_hint, 1, 0, 1, 8)

        self.config_dialog = QDialog(self)
        self.config_dialog.setWindowTitle('SocketCAN 高级信息')
        self.config_dialog.resize(610, 330)
        config_layout = QVBoxLayout(self.config_dialog)
        self.config_hint = QLabel('在主界面选择比特率后直接点击“打开”，程序会自动配置并启用接口；'
                             '需要权限时弹出系统认证窗口。点击“关闭”会将共享接口置为 DOWN；'
                             '切换模式或退出只断开本程序连接。'
                             '以下命令仅供排查问题时参考，无需手动执行。')
        self.config_hint.setWordWrap(True)
        config_layout.addWidget(self.config_hint)
        self.config_commands = QPlainTextEdit()
        self.config_commands.setReadOnly(True)
        config_layout.addWidget(self.config_commands)
        config_buttons = QHBoxLayout()
        self.copy_config = QPushButton('复制命令')
        self.copy_config.clicked.connect(lambda: QApplication.clipboard().setText(
            self.config_commands.toPlainText()))
        close_config = QPushButton('关闭')
        close_config.clicked.connect(self.config_dialog.close)
        config_buttons.addWidget(self.copy_config)
        config_buttons.addStretch()
        config_buttons.addWidget(close_config)
        config_layout.addLayout(config_buttons)
        self.interface.currentTextChanged.connect(self.update_config_commands)
        self.enable_fd.toggled.connect(self.update_config_commands)
        self.bitrate.currentTextChanged.connect(self.update_config_commands)
        self.data_bitrate.currentTextChanged.connect(self.update_config_commands)

        self.poll = QTimer(self)
        self.poll.setInterval(20)
        self.poll.timeout.connect(self.drain)
        self.discovery_poll = QTimer(self)
        self.discovery_poll.setInterval(100)
        self.discovery_poll.timeout.connect(self._poll_discovery)
        self.enable_fd.toggled.connect(self.update_controls)
        self.frame_type.currentIndexChanged.connect(self.update_controls)
        self.load_settings()
        self.backend.currentIndexChanged.connect(self._backend_changed)
        self._apply_backend_ui()
        self.refresh_interfaces()
        self.update_controls()
        self.update_config_commands()
        self._update_counts()

    def show_config_dialog(self):
        self.update_config_commands()
        self.config_dialog.show()
        self.config_dialog.raise_()

    def backend_name(self):
        return self.backend.currentData() or 'socketcan'

    def _backend_changed(self, *args):
        if self.active or self.connecting or self.closing:
            # Disabled widgets can still be changed by settings/code. Keep the
            # transport identity stable until its worker has released the device.
            self.backend.blockSignals(True)
            self.backend.setCurrentIndex(self.backend.findData(self._selected_backend))
            self.backend.blockSignals(False)
            return
        if self._selected_backend:
            self._channels[self._selected_backend] = self.interface.currentText().strip()
        self._selected_backend = self.backend_name()
        default = {'socketcan': 'can0', 'pcan': 'PCAN_USBBUS1', 'gs_usb': '0'}
        self.interface.clear()
        self.interface.setCurrentText(self._channels.get(self._selected_backend,
                                                        default[self._selected_backend]))
        self._failed_close_interface = None
        self._apply_backend_ui()
        self.refresh_interfaces()
        self.update_controls()
        self.update_config_commands()

    def _apply_backend_ui(self):
        backend = self.backend_name()
        socketcan = backend == 'socketcan'
        self.interface_label.setText('CAN 接口' if socketcan else '设备通道')
        self.interface.setToolTip({
            'socketcan': 'Linux SocketCAN 接口名称，例如 can0 或 vcan0。',
            'pcan': 'PEAK PCAN 通道，例如 PCAN_USBBUS1；点击刷新搜索已连接设备。',
            'gs_usb': 'candleLight 设备索引，从 0 开始；插拔后请刷新并确认设备顺序。',
        }[backend])
        if socketcan:
            self.interface_hint.setText('比特率单位 bit/s，需与总线一致。\n点击打开自动配置。')
            self.interface_hint.setToolTip('需要权限时弹出系统认证窗口。点击“关闭”会停用共享 CAN 接口，'
                                           '其他使用此接口的程序也会停止收发。切换模式或退出只断开本程序连接。')
            self.receive_own.setToolTip('启用 SocketCAN 本 socket 回环；默认关闭，避免把 TX 回环当作设备回复。')
        else:
            self.interface_hint.setText('选择设备通道和比特率后直接打开。' +
                ('CAN FD 需要 PCAN FD 适配器。' if backend == 'pcan' else 'candleLight 当前支持经典 CAN。'))
            self.interface_hint.setToolTip('关闭会释放本程序的驱动通道。请在“高级”查看驱动要求。')
            self.receive_own.setToolTip('显示本程序发送成功的帧回显；回显不代表设备回复或总线 ACK。')
        self.counts.setToolTip('字节数仅统计数据载荷，RTR 为 0 字节。TX 计数表示'
                              + ('内核' if socketcan else '驱动') + '接受发送，不表示已收到总线 ACK。')
        if backend == 'gs_usb':
            self.enable_fd.setChecked(False)
            self.brs.setChecked(False)
        self.frame_type.model().item(2).setEnabled(backend != 'gs_usb')

    def _poll_discovery(self):
        for backend, job in list(self._discovery_jobs.items()):
            try:
                names, error = job['result'].get_nowait()
            except queue.Empty:
                if not job['warned'] and time.monotonic() - job['started'] >= self.DISCOVERY_WAIT_SECONDS:
                    job['warned'] = True
                    if backend == self.backend_name() and not (self.active or self.connecting or self.closing):
                        self.note('设备枚举仍在等待驱动响应；可手动填写通道，界面仍可操作。')
                continue
            del self._discovery_jobs[backend]
            if backend != self.backend_name() or self.active or self.connecting or self.closing:
                continue
            previous = self.interface.currentText().strip()
            if error:
                self.note('无法枚举 CAN 设备：' + error)
            else:
                self.interface.clear()
                self.interface.addItems(names)
                self.interface.setCurrentText(previous or (names[0] if names else
                    'PCAN_USBBUS1' if backend == 'pcan' else '0'))
                self.note('找到 ' + str(len(names)) + ' 个 CAN 通道' if names else
                          '未发现 CAN 设备；请检查驱动及 USB 连接，也可手动填写通道。')
            self.update_controls()
        if not self._discovery_jobs:
            self.discovery_poll.stop()

    def update_config_commands(self, *args):
        backend = self.backend_name()
        if backend != 'socketcan':
            self.config_dialog.setWindowTitle('CAN 驱动信息')
            self.config_hint.setText('选择通道和比特率后直接打开；设备枚举、连接和收发均在后台执行。'
                                     '关闭释放本程序的驱动通道。')
            self.copy_config.setText('复制信息')
            self.config_commands.setPlainText(
                'PCAN（PEAK）\n安装 PEAK 官方 PCAN 驱动及 PCAN-Basic；PCANBasic.dll 必须与程序位数一致。\n'
                '通道示例：PCAN_USBBUS1。CAN FD 需要支持 FD 的适配器；比特率应与总线一致。'
                if backend == 'pcan' else
                'candleLight（gs_usb）\nWindows 需要设备使用 WinUSB 驱动以及可用的 libusb 运行库。\n'
                '设备通道为从 0 开始的索引；插拔设备后请刷新并确认顺序。\n'
                '当前 gs_usb 后端支持经典 CAN（含扩展 ID、RTR），不支持 CAN FD / BRS。')
            return
        self.config_dialog.setWindowTitle('SocketCAN 高级信息')
        self.copy_config.setText('复制命令')
        self.config_hint.setText('主界面选择比特率后直接打开；需要权限时弹出系统认证窗口。'
                                 '关闭会将共享接口置为 DOWN；切换模式或退出只断开本程序连接。'
                                 '以下命令仅供排查问题，无需手动执行。')
        name = self.interface.currentText().strip() or 'can0'
        device = shlex.quote(name)
        fd = self.enable_fd.isChecked()
        self.data_bitrate.setEnabled(fd and not (self.active or self.connecting or self.closing))
        try:
            virtual = str((Path('/sys/class/net') / name).resolve()).startswith('/sys/devices/virtual/net/')
        except (OSError, RuntimeError, ValueError):
            virtual = False
        if virtual:
            self.config_commands.setPlainText(
                f'# 虚拟 CAN 接口不配置比特率\nsudo ip link set dev {device} up\n'
                f'ip -details -statistics link show dev {device}')
            return
        config = f'sudo ip link set dev {device} type can bitrate {shlex.quote(self.bitrate.currentText())}'
        if fd:
            config += f' dbitrate {shlex.quote(self.data_bitrate.currentText())} fd on'
        else:
            # Classic-only drivers can reject an unsupported FD ctrlmode mask,
            # even when its requested value is "off". Clear it only if sysfs
            # says this interface already operates with the CAN FD MTU.
            try:
                mtu = interface_details(name).get('mtu')
            except (OSError, ValueError):
                mtu = None
            if mtu is not None and mtu >= 72:
                config += ' fd off'
        self.config_commands.setPlainText(
            f'sudo ip link set dev {device} down\n{config}\n'
            f'sudo ip link set dev {device} up\n'
            f'ip -details -statistics link show dev {device}'
        )

    def refresh_interfaces(self):
        backend = self.backend_name()
        if backend != 'socketcan':
            if backend in self._discovery_jobs:
                return
            result = queue.SimpleQueue()
            # The worker only owns a plain queue, never Qt objects or the panel.
            def discover():
                try:
                    result.put((discover_channels(backend), ''))
                except Exception as exc:
                    result.put(([], str(exc)))
            worker = threading.Thread(target=discover, name='can-enumerate-' + backend, daemon=True)
            self._discovery_jobs[backend] = dict(result=result, started=time.monotonic(), warned=False)
            worker.start()
            self.discovery_poll.start()
            self.update_controls()
            self.note('正在枚举 CAN 设备…')
            return
        previous = self.interface.currentText().strip()
        try:
            names = list_can_interfaces()
        except OSError as exc:
            self.note('无法枚举 CAN 接口：' + str(exc))
            return
        self.interface.clear()
        self.interface.addItems(names)
        self.interface.setCurrentText(previous or (names[0] if names else 'can0'))

    def update_controls(self, *args):
        busy = self.active or self.connecting or self.closing
        backend = self.backend_name()
        for widget in (self.backend, self.interface, self.refresh, self.enable_fd, self.receive_own,
                       self.error_frames, self.rx_id, self.rx_mask, self.rx_format,
                       self.bitrate):
            widget.setEnabled(not busy)
        self.enable_fd.setEnabled(not busy and backend != 'gs_usb')
        self.refresh.setEnabled(not busy and backend not in self._discovery_jobs)
        self.data_bitrate_label.setVisible(self.enable_fd.isChecked())
        self.data_bitrate.setVisible(self.enable_fd.isChecked())
        self.data_bitrate.setEnabled(self.enable_fd.isChecked() and not busy)
        self.toggle.setText('正在关闭…' if self.closing else
                            '取消打开' if self.connecting else '关闭' if self.active else '打开')
        self.toggle.setEnabled(not self.closing)
        self.toggle.setToolTip(('停止收发并将此共享 CAN 接口置为 DOWN，可能需要系统认证。'
                               if backend == 'socketcan' else '停止收发并释放本程序的驱动通道。')
                               if self.active else '')
        self.config_button.setEnabled(not (self.connecting or self.closing))
        self.retry_stop.setText('重试停用 ' + (self._failed_close_interface or ''))
        self.retry_stop.setVisible(bool(self._failed_close_interface) and not busy)
        self.retry_stop.setEnabled(not busy)
        kind = self.frame_type.currentIndex()
        self.rtr_dlc_label.setVisible(kind == 1)
        self.rtr_dlc.setVisible(kind == 1)
        self.brs.setVisible(kind == 2)
        self.brs.setEnabled(backend != 'gs_usb' and kind == 2 and (not self.active or self.enable_fd.isChecked()))
        if kind == 1:
            hint = 'RTR 远程帧：发送框必须为空，请设置请求长度（0–8）。'
        elif kind == 2:
            hint = ('candleLight 不支持 CAN FD；请选择经典数据帧或 RTR。' if backend == 'gs_usb' else
                    'CAN FD：HEX 数据长度为 0–8、12、16、20、24、32、48 或 64 字节；需先启用 CAN FD。')
        else:
            hint = '发送框填写 HEX 数据；经典 CAN 最多 8 字节，可发送空数据帧。'
        self.payload_hint.setText(hint)

    def note(self, text):
        self.status.setText(text)
        self.status_changed.emit(text)

    def _update_counts(self):
        self.counts.setText(f'TX {self.tx_frames:,} 帧 / {self.tx_bytes:,} B\n'
                            f'RX {self.rx_frames:,} 帧 / {self.rx_bytes:,} B')

    def reset_counts(self):
        self.tx_frames = self.rx_frames = self.tx_bytes = self.rx_bytes = 0
        self._update_counts()

    def toggle_connection(self):
        if self.closing:
            return
        if self.active:
            self.close_interface()
            return
        if self.connecting:
            self.stop()
            return
        self._failed_close_interface = None
        self.update_controls()
        try:
            backend = self.backend_name()
            if backend == 'socketcan' and sys.platform != 'linux':
                raise ValueError('SocketCAN 仅支持 Linux；Windows 请选择 PCAN 或 candleLight 驱动')
            name = self.interface.currentText().strip()
            if not name:
                raise ValueError('请输入 CAN 接口或设备通道')
            if backend == 'gs_usb' and self.enable_fd.isChecked():
                raise ValueError('candleLight 当前不支持 CAN FD')
            rx_extended = (None, False, True)[self.rx_format.currentIndex()]
            filter_id = parse_can_id(self.rx_id.text(), extended=rx_extended is not False)
            filter_mask = parse_can_id(self.rx_mask.text(), extended=rx_extended is not False)
            rate_text = self.bitrate.currentText().strip()
            data_rate_text = self.data_bitrate.currentText().strip() if self.enable_fd.isChecked() else '2000000'
            for label, value in (('仲裁比特率', rate_text), ('FD 数据比特率', data_rate_text)):
                if (not value.isascii() or not value.isdecimal() or len(value) > 9
                        or not 1 <= int(value) <= 100000000):
                    raise ValueError(f'{label}必须是正整数，范围 1–100000000 bit/s')
            bitrate, data_bitrate = int(rate_text), int(data_rate_text)
            options = dict(fd=self.enable_fd.isChecked(), receive_own=self.receive_own.isChecked(),
                error_frames=self.error_frames.isChecked(), filter_id=filter_id,
                filter_mask=filter_mask, filter_extended=rx_extended,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self.stop_sending.emit()
            self.note('CAN 打开失败：' + str(exc))
            return

        self._connect_request += 1
        request = self._connect_request
        self._pending_connection = (name, options)
        self.connecting = True
        self.update_controls()
        if backend != 'socketcan':
            self.note(f'正在打开 {name}…')
            self._driver_wait_since = time.monotonic()
            self._driver_wait_reported = False
            self._driver_close_reason = ''
            self._driver_receive_error_reported = False
            try:
                self.bus = DriverCanBus(backend, name, bitrate,
                                        data_bitrate=data_bitrate, **options)
            except (OSError, ValueError, RuntimeError) as exc:
                self._pending_connection = None
                self.connecting = False
                self.update_controls()
                self.stop_sending.emit()
                self.note('CAN 打开失败：' + str(exc))
                return
            self.poll.start()
            return
        self.note(f'正在配置 {name}；需要权限时请在系统认证窗口中确认…')
        try:
            configurator = CanConfigurator(self)
            self.configurator = configurator
            configurator.progress.connect(
                lambda text: self._configuration_progress(request, configurator, text))
            configurator.finished.connect(
                lambda success, message: self._configuration_finished(
                    request, configurator, success, message))
            configurator.start(name, bitrate, options['fd'], data_bitrate)
        except (OSError, ValueError, RuntimeError) as exc:
            self.stop()
            self.note('CAN 打开失败：' + str(exc))

    def _configuration_progress(self, request, configurator, text):
        if (self.connecting and request == self._connect_request
                and configurator is self.configurator):
            self.note(text)

    def _configuration_finished(self, request, configurator, success, message):
        if (not self.connecting or request != self._connect_request
                or configurator is not self.configurator):
            return
        name, options = self._pending_connection
        self._pending_connection = None
        self.configurator = None
        self.connecting = False
        configurator.deleteLater()
        if not success:
            self.update_controls()
            self.stop_sending.emit()
            self.note('CAN 打开失败：' + message)
            return
        try:
            self.bus = SocketCanBus(name, **options)
        except (OSError, ValueError, RuntimeError) as exc:
            self.update_controls()
            self.stop_sending.emit()
            self.note('CAN 打开失败：' + str(exc))
            return
        self.active = True
        self._connected_interface = name
        self._reported_drops = 0
        self.overflow.clear()
        self.overflow.hide()
        self.poll.start()
        self.update_controls()
        self.save_settings()
        mode = '经典 CAN + CAN FD' if options['fd'] else '经典 CAN'
        self.note(f'已打开 {name}（{mode}）')

    def _record_sent(self, frame):
        self.tx_frames += 1
        self.tx_bytes += 0 if frame.remote else len(frame.data)
        self.sent.emit(frame)

    def _drain_confirmed(self, bus):
        frames = bus.drain_sent(self.MAX_RX_BATCH)
        for frame in frames:
            self._record_sent(frame)
        if frames:
            self._update_counts()
        return len(frames)

    def _stop_driver(self, reason=''):
        self.stop_sending.emit()
        self._connect_request += 1
        self._pending_connection = None
        self.connecting = False
        self.active = False
        if not self.closing:
            self.closing = True
            self._driver_wait_since = time.monotonic()
            self._driver_wait_reported = False
            self._driver_close_reason = reason
        elif reason and not self._driver_close_reason:
            self._driver_close_reason = reason
        self.bus.close()
        self.poll.start()
        self.update_controls()
        self.note((self._driver_close_reason + '；' if self._driver_close_reason else '')
                  + '正在等待驱动释放通道…')

    def _poll_driver(self, bus):
        # Only a worker that was already finished before both queue reads can
        # prove that no final RX/TX result can arrive between drain and release.
        closed_before_drain = bus.closed
        confirmed = self._drain_confirmed(bus)
        if self.bus is not bus:
            return False
        if self.connecting:
            if bus.error or bus.closed:
                self._stop_driver('CAN 打开失败：' + (bus.error or '驱动已关闭'))
            elif bus.state == 'open':
                name, options = self._pending_connection
                self._pending_connection = None
                self.connecting = False
                self.active = True
                self._connected_interface = name
                self._reported_drops = 0
                self.overflow.clear()
                self.overflow.hide()
                self.update_controls()
                self.save_settings()
                mode = '经典 CAN + CAN FD' if options['fd'] else '经典 CAN'
                self.note(f'已打开 {name}（{mode}）')
            elif time.monotonic() - self._driver_wait_since >= self.DRIVER_WAIT_SECONDS:
                self._stop_driver('CAN 打开超时，已取消本次打开')
        if self.closing:
            frames, receive_error, receive_empty = self._receive_batch(bus)
            self._deliver_frames(frames)
            if self.bus is not bus:
                return False
            self._report_drops(bus)
            if receive_error is not None and not self._driver_receive_error_reported:
                self._driver_receive_error_reported = True
                message = 'CAN 驱动收发失败：' + str(receive_error)
                if str(receive_error) not in self._driver_close_reason:
                    self._driver_close_reason += ('；' if self._driver_close_reason else '') + message
                    self.note(self._driver_close_reason + '；正在等待驱动释放通道…')
            if closed_before_drain and receive_empty and confirmed < self.MAX_RX_BATCH:
                self.bus = None
                self.closing = False
                self._connected_interface = None
                self.poll.stop()
                self.update_controls()
                reason = self._driver_close_reason
                if bus.close_error:
                    self.note((reason + '；' if reason else '') +
                              '驱动通道释放失败：' + bus.close_error)
                else:
                    self.note((reason + '；' if reason else '') + '驱动通道已关闭')
            elif (not self._driver_wait_reported and
                  time.monotonic() - self._driver_wait_since >= self.DRIVER_WAIT_SECONDS):
                self._driver_wait_reported = True
                self.note((self._driver_close_reason + '；' if self._driver_close_reason else '') +
                          '驱动释放通道超时，仍在后台等待；请检查设备，界面仍可操作。')
            return False
        return self.active

    def close_interface(self):
        """Explicit Close/Retry action: disconnect first, then stop the saved link."""
        if self.connecting or self.closing:
            return
        if self.bus is not None and getattr(self.bus, 'async_tx', False):
            self._stop_driver()
            return
        name = self._connected_interface if self.active else self._failed_close_interface
        if not name:
            return
        self.stop_sending.emit()
        self.poll.stop()
        bus, self.bus = self.bus, None
        self.active = False
        self._connected_interface = None
        if bus is not None:
            try:
                bus.close()
            except OSError:
                # Still request link DOWN so no traffic can continue on the link.
                pass
        self._connect_request += 1
        request = self._connect_request
        self.closing = True
        self._pending_close = name
        self._failed_close_interface = None
        self.update_controls()
        self.note(f'CAN 连接已断开，正在停用 {name}；需要权限时请在系统认证窗口中确认…')
        configurator = None
        try:
            configurator = CanConfigurator(self)
            self.configurator = configurator
            configurator.progress.connect(
                lambda text: self._close_progress(request, configurator, text))
            configurator.finished.connect(
                lambda success, message: self._close_finished(
                    request, configurator, success, message))
            configurator.stop_interface(name)
        except (OSError, ValueError, RuntimeError) as exc:
            self._close_finished(request, configurator, False, str(exc))

    def _close_progress(self, request, configurator, text):
        if (self.closing and request == self._connect_request
                and configurator is self.configurator):
            self.note('CAN 连接已断开；' + text)

    def _close_finished(self, request, configurator, success, message):
        if (not self.closing or request != self._connect_request
                or configurator is not self.configurator):
            return
        name = self._pending_close
        self._pending_close = None
        self.configurator = None
        self.closing = False
        if configurator is not None:
            configurator.deleteLater()
        self._failed_close_interface = None if success else name
        self.update_controls()
        self.note(f'已关闭，{name} 接口已 DOWN' if success else
                  f'CAN 连接已断开，但 {name} DOWN 状态未确认：{message}')

    def _deliver_frames(self, frames):
        if frames:
            self.rx_frames += len(frames)
            self.rx_bytes += sum(0 if frame.remote else len(frame.data) for frame in frames)
            self._update_counts()
            self.received.emit(frames)
            if not self.closing and self.backend_name() != 'pcan' and any(
                    frame.error and frame.can_id & 0x40 for frame in frames):
                self.stop_sending.emit()
                self.note('CAN 总线进入 BUS-OFF，循环发送已停止；请检查接线、终端电阻及比特率')

    def _receive_batch(self, bus):
        """Drain at most one bounded RX batch; distinguish empty from budget stop."""
        frames = []
        deadline = time.monotonic() + self.MAX_RX_SECONDS
        error = None
        empty = False
        try:
            for _ in range(self.MAX_RX_BATCH):
                if time.monotonic() >= deadline:
                    break
                frame = bus.receive()
                if frame is None:
                    empty = True
                    break
                frames.append(frame)
        except (OSError, ValueError, RuntimeError) as exc:
            error = exc
            # DriverCanBus reports its terminal error only after queued RX is
            # drained. SocketCAN errors still follow the existing close path.
            empty = bool(getattr(bus, 'async_tx', False))
        return frames, error, empty

    def _report_drops(self, bus):
        dropped = bus.rx_dropped
        if dropped > self._reported_drops:
            self._reported_drops = dropped
            layer = '驱动' if getattr(bus, 'async_tx', False) else '内核'
            message = f'CAN {layer}接收缓冲区已丢失 {dropped:,} 帧，接收记录不完整。'
            self.overflow.setText(message)
            self.overflow.show()
            self.note(message)

    def drain(self):
        bus = self.bus
        if bus is None:
            return
        if getattr(bus, 'async_tx', False) and not self._poll_driver(bus):
            return
        if not self.active:
            return
        frames, error, _ = self._receive_batch(bus)
        self._deliver_frames(frames)
        # A connected receiver may synchronously close the panel after delivery.
        if self.bus is not bus:
            return
        self._report_drops(bus)
        if error is not None:
            if getattr(bus, 'async_tx', False):
                self._driver_receive_error_reported = True
                self._stop_driver('CAN 驱动收发失败：' + str(error))
                return
            self.stop()
            self.note('CAN 接收失败，已关闭：' + str(error))

    def send_text(self, text):
        try:
            if not self.active or self.bus is None:
                raise ValueError('请先打开 CAN 接口')
            can_id = parse_can_id(self.tx_id.text(), extended=self.tx_extended.isChecked())
            kind = self.frame_type.currentIndex()
            if kind == 1 and text.strip():
                raise ValueError('RTR 远程帧不能携带数据，请清空发送框')
            data = parse_hex_payload(text)
            if kind == 0 and len(data) > 8:
                raise ValueError('经典 CAN 单帧最多 8 字节，请修改发送内容')
            if kind == 2:
                if self.backend_name() == 'gs_usb':
                    raise ValueError('candleLight 当前不支持 CAN FD / BRS，请选择经典数据帧或 RTR')
                if not self.enable_fd.isChecked():
                    raise ValueError('发送 CAN FD 帧需要关闭接口后勾选“启用 CAN FD”并重新打开')
                if len(data) not in self.FD_LENGTHS:
                    raise ValueError('CAN FD 长度必须为 0–8、12、16、20、24、32、48 或 64 字节；请明确填写完整数据')
            frame = CanFrame(
                can_id, data, extended=self.tx_extended.isChecked(), remote=kind == 1,
                fd=kind == 2, bitrate_switch=kind == 2 and self.brs.isChecked(),
                dlc=self.rtr_dlc.value() if kind == 1 else None,
            )
            self.bus.send(frame)
        except (ValueError, UnicodeError, BufferError) as exc:
            self.stop_sending.emit()
            self.note('CAN 发送失败：' + str(exc))
            return False
        except (OSError, RuntimeError) as exc:
            if self.bus is not None and getattr(self.bus, 'async_tx', False):
                self._stop_driver('CAN 发送失败：' + str(exc))
                return False
            self.stop()
            self.note('CAN 发送失败，已关闭：' + str(exc))
            return False
        if getattr(self.bus, 'async_tx', False):
            return True
        self._record_sent(frame)
        self._update_counts()
        return True

    def stop(self):
        """Local cleanup for mode changes, exit, cancellation and transport errors."""
        if self.bus is not None and getattr(self.bus, 'async_tx', False):
            self._stop_driver('已取消打开' if self.connecting else '')
            return
        self.stop_sending.emit()
        self.poll.stop()
        was_connecting = self.connecting
        closing_name = self._pending_close if self.closing else None
        self._connect_request += 1
        self.connecting = False
        self.closing = False
        self._pending_connection = None
        self._pending_close = None
        if closing_name:
            self._failed_close_interface = closing_name
        configurator, self.configurator = self.configurator, None
        if configurator is not None:
            configurator.cancel()
            configurator.deleteLater()
        bus, self.bus = self.bus, None
        self.active = False
        self._connected_interface = None
        close_error = None
        if bus is not None:
            try:
                bus.close()
            except OSError as exc:
                close_error = exc
        self.update_controls()
        if close_error is not None:
            self.note('CAN 断开连接失败：' + str(close_error))
        elif closing_name:
            self.note(f'CAN 连接已断开，但 {closing_name} DOWN 状态未确认；'
                      '已授权的接口停用操作可能仍在完成')
        elif was_connecting:
            self.note('已取消打开；已授权的接口配置可能仍在完成')
        elif self._failed_close_interface:
            self.note(f'CAN 连接已断开，但 {self._failed_close_interface} DOWN 状态未确认')
        else:
            self.note('已断开本程序连接（未停用 CAN 接口）')

    def load_settings(self):
        default_backend = 'socketcan' if sys.platform == 'linux' else 'pcan'
        backend = self.settings.value('can/backend', default_backend)
        if self.backend.findData(backend) < 0 or (backend == 'socketcan' and sys.platform != 'linux'):
            backend = default_backend
        self.backend.setCurrentIndex(self.backend.findData(backend))
        self._selected_backend = backend
        defaults = {'socketcan': self.settings.value('can/interface', ''),
                    'pcan': 'PCAN_USBBUS1', 'gs_usb': '0'}
        self._channels = {key: str(self.settings.value('can/channel_' + key, default))
                          for key, default in defaults.items()}
        self.interface.setCurrentText(self._channels[backend])
        for key, widget in (('rx_id', self.rx_id), ('rx_mask', self.rx_mask), ('tx_id', self.tx_id)):
            widget.setText(self.settings.value('can/' + key, widget.text()))
        for key, widget in (('enable_fd', self.enable_fd), ('receive_own', self.receive_own),
                            ('error_frames', self.error_frames), ('tx_extended', self.tx_extended),
                            ('brs', self.brs)):
            widget.setChecked(self.settings.value('can/' + key, widget.isChecked(), type=bool))
        for key, widget in (('rx_format', self.rx_format), ('frame_type', self.frame_type)):
            widget.setCurrentIndex(max(0, min(widget.count() - 1,
                                             self.settings.value('can/' + key, 0, type=int))))
        self.rtr_dlc.setValue(self.settings.value('can/rtr_dlc', 0, type=int))
        for key, widget in (('bitrate', self.bitrate), ('data_bitrate', self.data_bitrate)):
            value = self.settings.value('can/' + key, widget.currentText())
            widget.setCurrentText(str(value))

    def save_settings(self):
        backend = self.backend_name()
        self.settings.setValue('can/backend', backend)
        self._channels[backend] = self.interface.currentText()
        for key, channel in self._channels.items():
            self.settings.setValue('can/channel_' + key, channel)
        if backend == 'socketcan':
            self.settings.setValue('can/interface', self.interface.currentText())
        for key, widget in (('rx_id', self.rx_id), ('rx_mask', self.rx_mask), ('tx_id', self.tx_id)):
            self.settings.setValue('can/' + key, widget.text())
        for key, widget in (('enable_fd', self.enable_fd), ('receive_own', self.receive_own),
                            ('error_frames', self.error_frames), ('tx_extended', self.tx_extended),
                            ('brs', self.brs)):
            self.settings.setValue('can/' + key, widget.isChecked())
        for key, widget in (('rx_format', self.rx_format), ('frame_type', self.frame_type)):
            self.settings.setValue('can/' + key, widget.currentIndex())
        self.settings.setValue('can/rtr_dlc', self.rtr_dlc.value())
        for key, widget in (('bitrate', self.bitrate), ('data_bitrate', self.data_bitrate)):
            self.settings.setValue('can/' + key, widget.currentText())
