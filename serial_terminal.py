"""Interactive VT console over the application's existing serial port."""
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                            QComboBox, QApplication, QToolButton, QMenu)
from ssh_terminal import SSHTerminal


class SerialTerminalPanel(QWidget):
    def __init__(self, send, toggle_port, parent=None, settings=None):
        super().__init__(parent)
        self.send = send
        self.settings = settings
        self._session_description = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status = QLabel()
        self.open_button = QPushButton('打开串口')
        self.open_button.clicked.connect(toggle_port)
        clear = QPushButton('清空终端')
        self.output = SSHTerminal()
        self.output.responses_enabled = False
        self.output.data_ready.connect(self.send_input)
        clear.clicked.connect(self.output.clear)
        row.addWidget(self.status, 1)
        row.addWidget(self.open_button)
        row.addWidget(clear)
        layout.addLayout(row)
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.newline = QComboBox()
        for label, data in [('CR', b'\r'), ('LF', b'\n'), ('CRLF', b'\r\n')]:
            self.newline.addItem(label, data)
        self.newline.currentIndexChanged.connect(self.update_keys)
        self.backspace = QComboBox()
        self.backspace.addItem('自动 / vi 兼容', None)
        self.backspace.addItem('BS (0x08)', b'\x08')
        self.backspace.addItem('DEL (0x7F)', b'\x7f')
        self.backspace.setToolTip('自动：默认发送 Ctrl+H，兼容 vi，并跟随远端 DECBKM 设置。登录界面若要求 DEL，可手动选择 0x7F。Delete 键仍发送独立的删除序列。')
        if settings is not None:
            saved = settings.value('serial_terminal/backspace', 'auto')
            self.backspace.setCurrentIndex({'auto': 0, 'bs': 1, 'del': 2}.get(saved, 0))
        self.backspace.currentIndexChanged.connect(self.update_keys)
        self.dimensions = QLabel()
        self.output.size_changed.connect(self.update_dimensions)
        self.sync_size_button = QToolButton()
        self.sync_size_button.setText('同步尺寸')
        self.sync_size_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.sync_size_button.setToolTip('在 Linux shell 提示符下点击，发送当前行列数的 stty 命令。窗口缩放后可再次同步；非 Linux 设备请勿使用。')
        self.sync_size_button.clicked.connect(self.sync_size)
        size_menu = QMenu(self.sync_size_button)
        size_menu.addAction('复制尺寸命令', self.copy_size_command)
        self.sync_size_button.setMenu(size_menu)
        self.interrupt_button = QPushButton('Ctrl+C')
        self.interrupt_button.clicked.connect(self.interrupt)
        self.refresh_prompt_button = QPushButton('刷新提示符')
        self.refresh_prompt_button.setToolTip('发送 Ctrl+L，让 Linux shell 重绘当前命令行；不会发送 Enter。非终端设备请勿使用。')
        self.refresh_prompt_button.clicked.connect(self.refresh_prompt)
        row.addWidget(QLabel('Enter'))
        row.addWidget(self.newline)
        row.addWidget(QLabel('退格'))
        row.addWidget(self.backspace)
        row.addStretch()
        row.addWidget(self.dimensions)
        row.addWidget(self.sync_size_button)
        row.addWidget(self.refresh_prompt_button)
        row.addWidget(self.interrupt_button)
        layout.addLayout(row)
        hint = QLabel('点击终端直接输入；Enter 登录/执行 · Tab 补全 · ↑↓ 历史 · Ctrl+C 中断 · Ctrl+Shift+C/V 复制/粘贴。\n'
                      'Linux 输出过窄：在 shell 提示符下点击“同步尺寸”，再执行命令；ps auxww 可显示完整命令行。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.update_dimensions(self.output.screen.columns, self.output.screen.lines)
        self.update_keys()
        self.set_connected(False)

    def update_keys(self):
        self.output.return_bytes = self.newline.currentData()
        value = self.backspace.currentData()
        self.output.backspace_bytes = value if value is not None else b'\x08'
        self.output.backspace_override = value is not None
        if self.settings is not None:
            self.settings.setValue('serial_terminal/backspace', ('auto', 'bs', 'del')[self.backspace.currentIndex()])
        if self.output.connected:
            # Return focus after the combo popup has closed, not while it owns focus.
            QTimer.singleShot(0, self.output.setFocus)

    def interrupt(self):
        self.output.reset_input()
        self.send_input(b'\x03')

    def update_dimensions(self, columns, lines):
        self.dimensions.setText(f'{columns} 列 × {lines} 行')

    def size_command(self):
        screen = self.output.screen
        return f'stty cols {screen.columns} rows {screen.lines}; export TERM=xterm-256color COLUMNS={screen.columns} LINES={screen.lines}'

    def sync_size(self):
        # Serial has no SSH-style resize channel. Only the explicit button
        # action may send shell commands; opening/resizing must send nothing.
        self.send_input(self.size_command().encode('ascii') + self.newline.currentData())
        self.output.setFocus()

    def copy_size_command(self):
        QApplication.clipboard().setText(self.size_command())
        self.output.setFocus()

    def refresh_prompt(self):
        self.send_input(b'\x0c')
        self.output.setFocus()

    def set_connected(self, connected, description=''):
        self.status.setText(('已连接 ' + description) if connected else '未连接：请在左侧设置串口并打开。无需 IP。')
        self.open_button.setText('关闭串口' if connected else '打开串口')
        self.output.set_connected(connected)
        self.interrupt_button.setEnabled(connected)
        self.sync_size_button.setEnabled(connected)
        self.refresh_prompt_button.setEnabled(connected)
        if connected:
            # Reopening a serial port does not make a shell resend its prompt.
            # Keep the last screen for the same device, but never mix devices.
            if description != self._session_description:
                self.output.reset_stream()
            else:
                self.output.resume_stream()
            self._session_description = description
            self.output.setFocus()
        else:
            # Keep the final screen available for diagnosis after disconnect.
            self.output.decoder.reset()

    def send_input(self, data):
        if not self.output.connected:
            return False
        if len(data) > 4096:
            self.output.append_notice('[本次输入超过 4096 字节，请分段粘贴]')
            return False
        # Direct input includes passwords, so never persist its contents locally.
        return self.send(data, True)
