"""Local POSIX shell attached to a real PTY, with bounded Qt event-loop I/O."""
import errno
import os
import signal
import subprocess

from PyQt5.QtCore import QSocketNotifier, QTimer, pyqtSignal
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from ssh_terminal import SSHTerminal


class LocalTerminalPanel(QWidget):
    stopped = pyqtSignal()
    MAX_PENDING = 65536

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.fd = None
        self.reader = self.writer = None
        self.pending = bytearray()
        self.closing = False
        self.output = SSHTerminal()
        self.output.data_ready.connect(self.send_bytes)
        self.output.size_changed.connect(self.resize_terminal)
        self.status = QLabel('本机 shell，使用当前用户权限')
        self.button = QPushButton('启动本地终端')
        self.button.clicked.connect(self.toggle)
        clear = QPushButton('清空显示')
        clear.clicked.connect(self.output.clear)
        row = QHBoxLayout()
        row.addWidget(self.status, 1); row.addWidget(self.button); row.addWidget(clear)
        layout = QVBoxLayout(self)
        layout.addLayout(row); layout.addWidget(self.output, 1)
        hint = QLabel('本机命令直接执行；Ctrl+C 中断 · Tab 补全 · ↑↓ 历史 · Ctrl+Shift+C/V 复制/粘贴。')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(50)
        self.poll_timer.timeout.connect(self.poll)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.kill)
        self.groups = set()
        if os.name != 'posix':
            self.button.setEnabled(False)
            self.status.setText('本地终端目前支持 Linux / macOS；Windows 暂不支持')

    def toggle(self):
        if self.process is not None:
            self.stop()
        else:
            self.start()

    def start(self, shell=None, cwd=None):
        if self.process is not None or os.name != 'posix':
            return
        import pty
        import fcntl
        import termios
        import struct
        shell = shell or os.environ.get('SHELL') or '/bin/sh'
        if not os.path.isfile(shell) or not os.access(shell, os.X_OK):
            shell = '/bin/sh'
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', self.output.screen.lines,
                                                          self.output.screen.columns, 0, 0))
            # Make the slave the controlling terminal for job control and vi.
            def child_setup():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            env = dict(os.environ, TERM='xterm-256color')
            for name in ('COLUMNS', 'LINES', 'PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH',
                         'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
                env.pop(name, None)
            self.process = subprocess.Popen([shell, '-i'], stdin=slave, stdout=slave,
                                            stderr=slave, cwd=cwd or os.path.expanduser('~'),
                                            env=env, preexec_fn=child_setup, close_fds=True)
            os.set_blocking(master, False)
        except Exception as exc:
            os.close(master)
            self.output.append_notice('[启动失败] ' + str(exc))
            return
        finally:
            os.close(slave)
        self.fd = master
        self.closing = False
        self.pending.clear()
        self.output.reset_stream()
        self.output.set_connected(True)
        self.reader = QSocketNotifier(master, QSocketNotifier.Read, self)
        self.reader.activated.connect(self.read_output)
        self.writer = QSocketNotifier(master, QSocketNotifier.Write, self)
        self.writer.activated.connect(self.write_pending)
        self.writer.setEnabled(False)
        self.status.setText('本机：' + shell)
        self.button.setText('关闭本地终端')
        self.button.setEnabled(True)
        self.poll_timer.start()
        self.output.setFocus()

    def read_output(self, *_):
        chunks = []
        for _ in range(4):
            try:
                data = os.read(self.fd, 4096)
                if not data:
                    self.reader.setEnabled(False)
                    break
                chunks.append(data)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self.reader.setEnabled(False)
                break
        if chunks:
            self.output.feed(b''.join(chunks))
        return len(chunks) == 4

    def send_bytes(self, data):
        if self.fd is None or self.closing:
            return
        if len(self.pending) + len(data) > self.MAX_PENDING:
            self.output.append_notice('[发送缓冲已满，请稍后重试]')
            return
        self.pending.extend(data)
        self.write_pending()

    def write_pending(self, *_):
        if self.fd is None:
            return
        try:
            if self.pending:
                count = os.write(self.fd, self.pending)
                del self.pending[:count]
        except BlockingIOError:
            pass
        except OSError:
            self.stop()
            return
        self.writer.setEnabled(bool(self.pending))

    def resize_terminal(self, columns, lines):
        if self.fd is not None:
            import fcntl
            import termios
            import struct
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', lines, columns, 0, 0))
            except OSError:
                pass

    def signal_groups(self, sig):
        for group in self.groups:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass

    def stop(self):
        if self.process is None or self.closing:
            return
        self.closing = True
        self.output.set_connected(False)
        self.button.setEnabled(False)
        self.status.setText('正在关闭本地终端…')
        self.groups = {self.process.pid}
        try:
            foreground = os.tcgetpgrp(self.fd)
            if foreground > 0:
                self.groups.add(foreground)
        except OSError:
            pass
        self.signal_groups(signal.SIGHUP)
        self.kill_timer.start(1000)

    def kill(self):
        self.signal_groups(signal.SIGKILL)

    def poll(self):
        if self.process is None or self.process.poll() is None:
            return
        if self.read_output():
            return  # Drain a large final output across event-loop turns.
        code = self.process.returncode
        if self.closing:
            self.kill()  # Also release any foreground job that ignored SIGHUP.
        self.kill_timer.stop()
        self.poll_timer.stop()
        for notifier in (self.reader, self.writer):
            notifier.setEnabled(False); notifier.deleteLater()
        os.close(self.fd)
        self.fd = self.process = self.reader = self.writer = None
        self.pending.clear(); self.groups.clear()
        self.closing = False
        self.output.set_connected(False)
        self.output.append_notice(f'[本地终端已退出，状态 {code}]')
        self.status.setText('已退出，可重新启动')
        self.button.setText('启动本地终端'); self.button.setEnabled(True)
        self.stopped.emit()
