"""Independent SSH command console; network I/O never runs on the GUI thread."""
import base64
import codecs
import hashlib
import os
import queue
import socket
import threading

from ssh_terminal import SSHTerminal

from PyQt5.QtCore import QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                            QLineEdit, QSpinBox, QPushButton, QLabel,
                            QFileDialog, QMessageBox)


class SSHWorker(QThread):
    connected = pyqtSignal()
    error = pyqtSignal(str)
    host_key = pyqtSignal(str, str)

    def __init__(self, options, known_hosts, parent=None):
        super().__init__(parent)
        self.options = options
        self.known_hosts = known_hosts
        self.incoming = queue.Queue(maxsize=128)
        self.outgoing = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.trust_answer = threading.Event()
        self.trusted = False
        self.client = None
        self.terminal_size = (80, 24)

    def stop(self):
        already_stopping = self.stopping.is_set()
        self.stopping.set()
        self.trust_answer.set()
        # Closing the transport also interrupts a pending shell/PTY request.
        # Paramiko may briefly join its thread, so keep this off the GUI thread.
        if self.client is not None and not already_stopping:
            threading.Thread(target=self.client.close, daemon=True).start()

    def run(self):
        try:
            import paramiko
            worker = self

            class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, client, hostname, key):
                    fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                    worker.host_key.emit(hostname, f'{key.get_name()} SHA256:{fingerprint}')
                    while not worker.trust_answer.wait(.1):
                        if worker.stopping.is_set():
                            raise RuntimeError('连接已取消')
                    if worker.stopping.is_set() or not worker.trusted:
                        raise RuntimeError('未信任此 SSH 主机')
                    client.get_host_keys().add(hostname, key.get_name(), key)
                    client.save_host_keys(worker.known_hosts)

            self.client = paramiko.SSHClient()
            self.client.load_system_host_keys()
            if os.path.isfile(self.known_hosts):
                self.client.load_host_keys(self.known_hosts)
            self.client.set_missing_host_key_policy(ConfirmHostKey())
            self.client.connect(**self.options, timeout=8, banner_timeout=8, auth_timeout=8)
            self.options.clear()  # release credentials after authentication
            if self.stopping.is_set():
                return
            transport = self.client.get_transport()
            transport.set_keepalive(15)
            channel = transport.open_session(timeout=8)
            channel.settimeout(.1)
            size = self.terminal_size
            channel.get_pty(term='xterm-256color', width=size[0], height=size[1])
            channel.invoke_shell()
            self.connected.emit()
            pending = b''
            while not self.stopping.is_set():
                if size != self.terminal_size:
                    size = self.terminal_size
                    channel.resize_pty(width=size[0], height=size[1])
                if not pending:
                    try:
                        pending = self.outgoing.get_nowait()
                    except queue.Empty:
                        pass
                if pending and channel.send_ready():
                    try:
                        sent = channel.send(pending)
                        if not sent:
                            raise RuntimeError('SSH 连接已断开')
                        pending = pending[sent:]
                    except socket.timeout:
                        pass
                if channel.recv_ready():
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    while not self.stopping.is_set():
                        try:
                            self.incoming.put(chunk, timeout=.1)
                            break
                        except queue.Full:
                            pass
                elif channel.closed or channel.eof_received or not transport.is_active():
                    break
                else:
                    self.stopping.wait(.01)
        except ImportError:
            self.error.emit('缺少 SSH 依赖，请执行：python3 -m pip install -r requirements.txt')
        except Exception as exc:
            if not self.stopping.is_set():
                self.error.emit(str(exc))
        finally:
            self.options.clear()
            if self.client:
                self.client.close()


class SSHPanel(QWidget):
    disconnected = pyqtSignal()
    MAX_CHARS = 512 * 1024

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        layout = QVBoxLayout(self)
        form = QFormLayout()
        row = QHBoxLayout()
        self.host = QLineEdit(str(settings.value('ssh/host', '')))
        self.host.setPlaceholderText('主机 IP 或域名')
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(settings.value('ssh/port', 22, type=int))
        row.addWidget(self.host)
        row.addWidget(QLabel('端口'))
        row.addWidget(self.port)
        form.addRow('SSH 主机', row)
        row = QHBoxLayout()
        self.user = QLineEdit(str(settings.value('ssh/user', '')))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText('密码 / 私钥口令（不保存）')
        row.addWidget(self.user)
        row.addWidget(self.password)
        form.addRow('用户名', row)
        row = QHBoxLayout()
        self.key = QLineEdit(str(settings.value('ssh/key', '')))
        self.key.setPlaceholderText('可选；留空使用密码或默认密钥 / SSH Agent')
        self.browse = QPushButton('选择私钥…')
        self.browse.clicked.connect(self.choose_key)
        row.addWidget(self.key)
        row.addWidget(self.browse)
        form.addRow('私钥文件', row)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.connect_button = QPushButton('连接 SSH')
        self.connect_button.clicked.connect(self.toggle_connection)
        self.status = QLabel('未连接')
        clear = QPushButton('清空输出')
        clear.clicked.connect(lambda: self.output.clear())
        row.addWidget(self.connect_button)
        row.addWidget(self.status, 1)
        row.addWidget(clear)
        layout.addLayout(row)
        self.output = SSHTerminal()
        self.output.data_ready.connect(self.send_bytes)
        self.output.size_changed.connect(self.resize_terminal)
        layout.addWidget(self.output, 1)
        hint = QLabel('点击终端直接输入 · Enter 执行 · Tab 补全 · ↑↓ 历史 · Ctrl+C 中断 · Ctrl+Shift+C/V 复制/粘贴')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.set_connected(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain_output)
        self.timer.start(20)

    def save_settings(self):
        for name in ('host', 'user', 'key'):
            self.settings.setValue('ssh/' + name, getattr(self, name).text().strip())
        self.settings.setValue('ssh/port', self.port.value())

    def choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 SSH 私钥')
        if path:
            self.key.setText(path)

    def set_connected(self, connected):
        self.output.set_connected(connected)

    def resize_terminal(self, columns, lines):
        if self.worker:
            self.worker.terminal_size = (columns, lines)

    def toggle_connection(self):
        if self.worker is not None:
            self.worker.stop()
            self.set_connected(False)
            self.connect_button.setEnabled(False)
            self.status.setText('正在断开…')
            return
        host, user = self.host.text().strip(), self.user.text().strip()
        if not host or not user:
            QMessageBox.warning(self, 'SSH', '请填写主机和用户名。')
            return
        key = self.key.text().strip()
        if key and not os.path.isfile(os.path.expanduser(key)):
            QMessageBox.warning(self, 'SSH', '私钥文件不存在。')
            return
        self.save_settings()
        secret = self.password.text() or None
        options = dict(hostname=host, port=self.port.value(), username=user,
                       password=None if key else secret,
                       key_filename=os.path.expanduser(key) if key else None,
                       passphrase=secret if key else None,
                       allow_agent=not bool(key or secret), look_for_keys=not bool(key or secret))
        self.password.clear()
        self.decoder.reset()
        self.output.reset_stream()
        self.worker = SSHWorker(options, os.path.join(os.path.dirname(self.settings.fileName()), 'ssh_known_hosts'), self)
        self.resize_terminal(self.output.screen.columns, self.output.screen.lines)
        self.worker.connected.connect(self.on_connected)
        self.worker.host_key.connect(self.confirm_host_key)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        for widget in (self.host, self.port, self.user, self.password, self.key, self.browse):
            widget.setEnabled(False)
        self.status.setText('正在连接…')
        self.connect_button.setText('断开 SSH')
        self.worker.start()

    def confirm_host_key(self, host, fingerprint):
        worker = self.worker
        if worker is None or worker.stopping.is_set():
            return
        answer = QMessageBox.question(self, '首次连接 SSH 主机',
                                      f'主机：{host}\n指纹：{fingerprint}\n\n请与服务器管理员核对。是否信任并保存？',
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        worker.trusted = answer == QMessageBox.Yes
        worker.trust_answer.set()

    def on_connected(self):
        if self.worker and not self.worker.stopping.is_set():
            self.status.setText(f'已连接 {self.user.text()}@{self.host.text()}')
            self.set_connected(True)
            self.output.setFocus()

    def on_error(self, message):
        self.output.append_notice('[SSH 错误] ' + message)

    def on_finished(self):
        self.drain_output(all_pending=True)
        self.append_output(self.decoder.decode(b'', final=True))
        self.output.append_notice('[SSH 已断开]')
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self.set_connected(False)
        for widget in (self.host, self.port, self.user, self.password, self.key, self.browse, self.connect_button):
            widget.setEnabled(True)
        self.connect_button.setText('连接 SSH')
        self.status.setText('未连接')
        self.disconnected.emit()

    def send_bytes(self, data):
        if not self.worker or not self.output.connected:
            return False
        if len(data) > 65536:
            QMessageBox.warning(self, 'SSH', '单次输入不能超过 64 KiB。')
            return False
        try:
            self.worker.outgoing.put_nowait(data)
            return True
        except queue.Full:
            QMessageBox.warning(self, 'SSH', '发送队列已满，请稍后重试。')
            return False

    def drain_output(self, all_pending=False):
        if self.worker is None:
            return
        chunks = []
        for _ in range(128 if all_pending else 16):
            try:
                chunks.append(self.worker.incoming.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self.append_output(self.decoder.decode(b''.join(chunks)))

    def append_output(self, text):
        self.output.append_output(text)
