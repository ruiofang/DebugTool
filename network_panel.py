"""Bounded, asynchronous TCP/UDP debugger (Qt 5.12+, Python 3.8+)."""
import codecs

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtNetwork import QAbstractSocket, QHostAddress, QNetworkProxy, QTcpServer, QTcpSocket, QUdpSocket
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                            QComboBox, QLineEdit, QSpinBox, QPushButton)


class NetworkPanel(QWidget):
    received = pyqtSignal(bytes, str, str)
    sent = pyqtSignal(bytes, str)
    status_changed = pyqtSignal(str)
    stop_sending = pyqtSignal()
    MAX_PENDING = 256 * 1024
    MAX_CLIENTS = 16

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.server = None
        self.socket = None
        self.peers = {}
        self.decoders = {}
        self.tx = self.rx = 0
        self.active = False
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        self.mode = QComboBox(self)
        self.mode.hide()
        self.mode.addItems(['TCP 客户端', 'TCP 服务端', 'UDP'])
        self.bind_host = QLineEdit('0.0.0.0')
        self.bind_port = QSpinBox(); self.bind_port.setRange(0, 65535)
        self.bind_port.setToolTip('0 表示由系统分配空闲端口；客户端忽略本地配置。')
        self.remote_host = QLineEdit('127.0.0.1')
        self.remote_host.setToolTip('TCP 支持 IP / 域名；UDP 使用目标 IP（支持 IPv4 广播）。')
        self.remote_port = QSpinBox(); self.remote_port.setRange(1, 65535); self.remote_port.setValue(9000)
        self.toggle = QPushButton('打开')
        self.toggle.clicked.connect(self.toggle_connection)
        self.local_fields = [QLabel('本地 IP'), self.bind_host, QLabel('本地端口'), self.bind_port]
        self.remote_fields = [QLabel('目标 IP / 域名'), self.remote_host, QLabel('目标端口'), self.remote_port]
        for row, fields in enumerate((self.local_fields[:2], self.local_fields[2:],
                                      self.remote_fields[:2], self.remote_fields[2:])):
            grid.addWidget(fields[0], row, 0)
            grid.addWidget(fields[1], row, 1)
        grid.addWidget(self.toggle, 4, 0, 1, 2)
        layout.addLayout(grid)
        row = QVBoxLayout()
        self.clients = QComboBox(); self.clients.addItem('全部客户端', None)
        self.drop_button = QPushButton('断开选中客户端')
        self.drop_button.clicked.connect(self.drop_client)
        row.addWidget(self.clients, 1); row.addWidget(self.drop_button)
        layout.addLayout(row)
        self.status = QLabel('未打开'); self.status.setWordWrap(True); layout.addWidget(self.status)
        layout.addStretch()
        self.poll = QTimer(self); self.poll.setInterval(20); self.poll.timeout.connect(self.drain)
        self.connect_timeout = QTimer(self); self.connect_timeout.setSingleShot(True)
        self.connect_timeout.timeout.connect(self.connection_timeout)
        self.mode.currentIndexChanged.connect(self.update_controls)
        self.load_settings(); self.update_controls()

    def update_controls(self, *args):
        mode = self.mode.currentIndex()
        self.mode.setEnabled(not self.active)
        for widget in self.local_fields:
            widget.setVisible(mode != 0)
        for widget in self.remote_fields:
            widget.setVisible(mode != 1)
        for widget in (self.bind_host, self.bind_port):
            widget.setEnabled(not self.active and mode != 0)
        for widget in (self.remote_host, self.remote_port):
            widget.setEnabled(mode != 1 and (not self.active or mode == 2))
        self.clients.setVisible(mode == 1); self.drop_button.setVisible(mode == 1)
        self.toggle.setText('关闭' if self.active else ('连接' if mode == 0 else '开始监听'))

    def note(self, text):
        self.status.setText(text)
        self.status_changed.emit(text)

    def reset_counts(self):
        self.tx = self.rx = 0

    def toggle_connection(self):
        if self.active:
            self.stop(); return
        mode = self.mode.currentIndex()
        if mode != 0:
            address = QHostAddress(self.bind_host.text().strip())
            if address.isNull():
                self.note('请输入有效的本地 IP，例如 0.0.0.0 或 ::'); return
        elif not self.remote_host.text().strip():
            self.note('请输入 TCP 目标 IP 或域名'); return
        self.active = True
        if mode == 1:
            self.server = QTcpServer(self)
            self.server.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
            self.server.setMaxPendingConnections(self.MAX_CLIENTS)
            self.server.newConnection.connect(self.accept_clients)
            if not self.server.listen(address, self.bind_port.value()):
                error = self.server.errorString(); self.stop(); self.note('监听失败：' + error); return
            self.note(f'TCP 监听 {self.server.serverAddress().toString()}:{self.server.serverPort()}')
        else:
            self.socket = QTcpSocket(self) if mode == 0 else QUdpSocket(self)
            sock = self.socket
            sock.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
            sock.setReadBufferSize(self.MAX_PENDING)
            if mode == 0:
                sock.connected.connect(self.connected)
                sock.disconnected.connect(self.client_disconnected)
                sock.error.connect(self.client_error)
                self.note('正在连接…')
                self.connect_timeout.start(10000)
                sock.connectToHost(self.remote_host.text().strip(), self.remote_port.value())
            elif not sock.bind(address, self.bind_port.value(), QAbstractSocket.DontShareAddress):
                error = sock.errorString(); self.stop(); self.note('UDP 绑定失败：' + error); return
            else:
                self.note(f'UDP 本地 {sock.localAddress().toString()}:{sock.localPort()}')
        self.poll.start(); self.update_controls()

    def connected(self):
        self.connect_timeout.stop()
        self.note('TCP 已连接 ' + self.endpoint(self.socket)); self.update_controls()

    @staticmethod
    def endpoint(sock):
        return f'[{sock.peerAddress().toString()}]:{sock.peerPort()}'

    def client_disconnected(self):
        if self.socket is not None:
            self.drain_tcp(self.socket, self.MAX_PENDING)
        self.stop(); self.note('TCP 连接已断开')

    def client_error(self, error):
        if self.socket is None or error == QAbstractSocket.RemoteHostClosedError:
            return
        message = self.socket.errorString()
        self.stop(); self.note('TCP 错误：' + message)

    def connection_timeout(self):
        self.stop(); self.note('TCP 连接超时（10 秒）')

    def accept_clients(self):
        while self.server is not None and self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if len(self.peers) >= self.MAX_CLIENTS:
                sock.abort(); sock.deleteLater(); continue
            sock.setReadBufferSize(self.MAX_PENDING)
            self.peers[sock] = self.endpoint(sock)
            self.clients.addItem(self.peers[sock], sock)
            sock.disconnected.connect(lambda s=sock: self.remove_client(s))
            self.note('客户端接入 ' + self.peers[sock])

    def remove_client(self, sock):
        if sock not in self.peers:
            return
        if self.clients.currentData() is sock:
            self.stop_sending.emit()
        self.drain_tcp(sock, self.MAX_PENDING)
        name = self.peers.pop(sock)
        self.decoders.pop(sock, None)
        for i in range(1, self.clients.count()):
            if self.clients.itemData(i) is sock:
                self.clients.removeItem(i); break
        sock.deleteLater(); self.note('客户端断开 ' + name)
        if not self.peers:
            self.stop_sending.emit()

    def drop_client(self):
        sock = self.clients.currentData()
        if sock in self.peers:
            sock.abort()
        else:
            self.note('请先选择一个客户端')

    def receive(self, data, peer, key=None):
        self.rx += len(data)
        if key is None:
            text = data.decode('utf-8', errors='replace')
        else:
            decoder = self.decoders.setdefault(key, codecs.getincrementaldecoder('utf-8')(errors='replace'))
            text = decoder.decode(data)
        self.received.emit(data, peer, text)

    def drain_tcp(self, sock, limit=4096):
        if sock.bytesAvailable():
            self.receive(bytes(sock.read(limit)), self.peers.get(sock, self.endpoint(sock)), sock)

    def drain(self):
        if self.server is not None:
            for sock in list(self.peers):
                self.drain_tcp(sock)
        elif isinstance(self.socket, QUdpSocket):
            # Keep datagram boundaries and bound work per GUI timer tick.
            budget = 65536
            for _ in range(32):
                if not self.socket.hasPendingDatagrams() or budget <= 0:
                    break
                data, host, port = self.socket.readDatagram(65535)
                budget -= max(1, len(data)); self.receive(bytes(data), f'[{host.toString()}]:{port}')
        elif self.socket is not None:
            self.drain_tcp(self.socket)

    def send_bytes(self, data):
        try:
            if not self.active:
                raise ValueError('请先连接或开始监听')
            if not data:
                raise ValueError("发送内容为空")
            if len(data) > 65507:
                raise ValueError("单次发送最多 65507 字节，请分段发送")
            if isinstance(self.socket, QUdpSocket):
                address = QHostAddress(self.remote_host.text().strip())
                if address.isNull():
                    raise ValueError('UDP 目标必须是有效 IP 地址')
                written = self.socket.writeDatagram(data, address, self.remote_port.value())
                if written != len(data):
                    raise ValueError('UDP 发送失败：' + self.socket.errorString())
                self.tx += written
                self.sent.emit(data, f'[{address.toString()}]:{self.remote_port.value()}')
            else:
                if self.server is not None:
                    selected = self.clients.currentData()
                    targets = [selected] if selected in self.peers else list(self.peers)
                else:
                    targets = [self.socket] if self.socket is not None else []
                if not targets:
                    raise ValueError('没有已连接的客户端')
                for sock in targets:
                    if sock.state() != QAbstractSocket.ConnectedState:
                        raise ValueError('TCP 尚未连接')
                    if sock.bytesToWrite() + len(data) > self.MAX_PENDING:
                        raise ValueError('TCP 发送缓冲区已满，循环发送已停止')
                for sock in targets:
                    written = sock.write(data)
                    if written != len(data):
                        raise ValueError('TCP 写入失败：' + sock.errorString())
                    self.tx += written
                    self.sent.emit(data, self.endpoint(sock))
            return True
        except (ValueError, UnicodeError) as exc:
            self.stop_sending.emit(); self.note(str(exc)); return False

    def stop(self):
        self.stop_sending.emit(); self.connect_timeout.stop(); self.poll.stop()
        if self.server is not None:
            self.server.close(); self.server.deleteLater(); self.server = None
        sockets = list(self.peers)
        if self.socket is not None:
            sockets.append(self.socket)
        self.socket = None; self.peers.clear(); self.decoders.clear()
        self.clients.clear(); self.clients.addItem('全部客户端', None)
        for sock in sockets:
            sock.blockSignals(True); sock.abort(); sock.deleteLater()
        self.active = False; self.update_controls(); self.note('已关闭')

    def load_settings(self):
        self.mode.setCurrentIndex(max(0, min(2, self.settings.value('network/mode', 0, type=int))))
        for key, widget in [('bind_host', self.bind_host), ('remote_host', self.remote_host)]:
            widget.setText(self.settings.value('network/' + key, widget.text()))
        for key, widget in [('bind_port', self.bind_port), ('remote_port', self.remote_port)]:
            widget.setValue(self.settings.value('network/' + key, widget.value(), type=int))

    def save_settings(self):
        self.settings.setValue('network/mode', self.mode.currentIndex())
        for key, widget in [('bind_host', self.bind_host), ('remote_host', self.remote_host)]:
            self.settings.setValue('network/' + key, widget.text())
        for key, widget in [('bind_port', self.bind_port), ('remote_port', self.remote_port)]:
            self.settings.setValue('network/' + key, widget.value())
