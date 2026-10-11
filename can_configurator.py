"""Asynchronous GUI bridge to the minimal SocketCAN configuration helper."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

from PyQt5.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from can_config import (AUTH_REQUIRED, ConfigError, system_environment,
                        validate_interface, validate_request)


_LIVE_JOBS = set()
PKEXEC_PATHS = ('/usr/bin/pkexec', '/bin/pkexec')
INSTALLED_HELPER = Path('/usr/local/libexec/debugtool/can-config')
INSTALLED_SOURCE = INSTALLED_HELPER.with_name('can_config.py')
INSTALLED_POLICY = Path('/usr/share/polkit-1/actions/org.ruio.debugtool.configure-can.policy')


def _request_arguments(interface, bitrate=None, fd=False, data_bitrate=2_000_000, *, down=False):
    args = ['--interface', interface]
    if down:
        args.append('--down')
    else:
        args += ['--bitrate', str(bitrate), '--data-bitrate', str(data_bitrate)]
        if fd:
            args.append('--fd')
    return args


def helper_command(interface, bitrate=None, fd=False, data_bitrate=2_000_000, *, down=False):
    args = _request_arguments(interface, bitrate, fd, data_bitrate, down=down)
    executable = str(Path(sys.executable).absolute())
    if getattr(sys, 'frozen', False):
        return executable, ['--configure-can', *args]
    # -I ignores PYTHONPATH, user site packages and the working directory.
    return executable, ['-I', str(Path(__file__).with_name('can_config.py').resolve()), *args]


def _trusted_install_path(path, *, executable=False):
    """Reject writable, redirected or non-root-owned privilege entry points."""
    path = Path(path)
    if not path.is_absolute():
        return False
    try:
        # lstat deliberately rejects symlinks, including ancestor directories.
        # Checking ancestors prevents replacing an otherwise root-owned file.
        for entry in (*reversed(path.parents), path):
            info = entry.lstat()
            expected_type = stat.S_ISREG if entry == path else stat.S_ISDIR
            if (not expected_type(info.st_mode) or info.st_uid != 0
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                return False
        return not executable or os.access(path, os.X_OK)
    except OSError:
        return False


def find_installed_helper():
    """Use only the dedicated helper installed together with its polkit policy."""
    if (_trusted_install_path(INSTALLED_HELPER, executable=True)
            and _trusted_install_path(INSTALLED_SOURCE)
            and _trusted_install_path(INSTALLED_POLICY)):
        return str(INSTALLED_HELPER)
    return None


def find_pkexec():
    for path in PKEXEC_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


class _ProcessJob(QObject):
    """Keep a cancelled process alive until reaped, independently of the panel."""

    completed = pyqtSignal(int, str)

    def __init__(self, executable, args, elevated, interface):
        super().__init__()
        self.elevated = elevated
        self.interface = interface
        self.operation = '配置'
        self.output = bytearray()
        self.reported = False
        self.timed_out = False
        self.cancelled = False
        self.process = QProcess(self)
        self.process.setWorkingDirectory('/')
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        environment = system_environment()
        # A frozen helper must unpack independently, not reuse its GUI parent's
        # temporary library directory (especially after pkexec changes the UID).
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        environment['LC_ALL'] = 'C'
        qenv = QProcessEnvironment()
        for key, value in environment.items():
            qenv.insert(key, value)
        self.process.setProcessEnvironment(qenv)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._timeout)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.process.kill)
        self.executable, self.args = executable, args

    def start(self):
        _LIVE_JOBS.add(self)
        self.deadline.start(120_000 if self.elevated else 30_000)
        self.process.start(self.executable, self.args)

    def _read_output(self):
        self.output.extend(bytes(self.process.readAllStandardOutput()))
        del self.output[:-16384]

    def _report(self, code, message):
        if not self.reported:
            self.reported = True
            self.completed.emit(code, message)

    def _error(self, error):
        if error == QProcess.FailedToStart:
            self._report(-1, f'无法启动 CAN {self.operation}程序：{self.process.errorString()}')
            self._cleanup()

    def _finished(self, code, status):
        self._read_output()
        if not self.reported:
            message = self.output.decode('utf-8', errors='replace').strip()
            if status == QProcess.CrashExit:
                code = -1
                message = message or f'CAN {self.operation}程序意外退出'
            self._report(code, message)
        self._cleanup()

    def _cleanup(self):
        self.deadline.stop()
        self.kill_timer.stop()
        _LIVE_JOBS.discard(self)
        self.deleteLater()

    def _timeout(self):
        self.timed_out = True
        self._report(-2, f'CAN {self.operation}或系统认证超时，请重试')
        self.cancel()

    def cancel(self):
        self.cancelled = True
        self.deadline.stop()
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            self.kill_timer.start(1000)


class CanConfigurator(QObject):
    finished = pyqtSignal(bool, str)
    progress = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._job = None
        self._request = None
        self._down = False

    @property
    def running(self):
        return self._request is not None

    def start(self, interface, bitrate, fd=False, data_bitrate=2_000_000):
        self._begin(interface, bitrate, fd, data_bitrate, down=False)

    def stop_interface(self, interface):
        self._begin(interface, down=True)

    @property
    def _operation(self):
        return '停用' if self._down else '配置'

    def _begin(self, interface, bitrate=None, fd=False, data_bitrate=2_000_000, *, down):
        self.cancel()
        self._down = down
        try:
            if down:
                validate_interface(interface)
            else:
                validate_request(interface, bitrate, fd, data_bitrate)
        except ConfigError as exc:
            self.finished.emit(False, str(exc))
            return
        if not sys.platform.startswith('linux'):
            self.finished.emit(False, f'SocketCAN 接口{self._operation}仅支持 Linux')
            return
        # Terminating an authenticated root helper may not be permitted. Keep
        # another request from racing its remaining bounded configuration steps.
        if any(job.interface == interface and job.process.state() != QProcess.NotRunning
               for job in _LIVE_JOBS):
            self.finished.emit(False, '上一次接口操作仍在结束，请稍后重试')
            return
        self._request = (interface, bitrate, fd, data_bitrate)
        action = '停用' if down else '检查并配置'
        self.progress.emit(f'正在{action} {interface}…')
        self._launch(False)

    def _launch(self, elevated):
        executable, args = helper_command(*self._request, down=self._down)
        if elevated:
            pkexec = find_pkexec()
            if not pkexec:
                self._request = None
                self.finished.emit(False, f'自动{self._operation}需要系统 pkexec；请安装 polkit 后重试')
                return
            installed = find_installed_helper()
            if not installed:
                self._request = None
                self.finished.emit(
                    False, 'CAN 授权助手未安装或权限不安全；请运行 sudo ./install.sh '
                    '重新安装独立的 CAN 助手后重试')
                return
            # The GUI directory is writable by its ordinary user. Never elevate
            # the executable or source helper from there, even with a password.
            executable = installed
            # The installed entry point accepts only CAN arguments. Never
            # pass interpreter/frozen-GUI flags to its polkit authorization.
            args = _request_arguments(*self._request, down=self._down)
            args = ['--disable-internal-agent', executable, *args]
            executable = pkexec
        self._job = _ProcessJob(executable, args, elevated, self._request[0])
        self._job.operation = self._operation
        self._job.completed.connect(self._completed)
        self._job.start()

    def _completed(self, code, message):
        job = self.sender()
        if job is not self._job or self._request is None:
            return
        self._job = None
        if code == AUTH_REQUIRED and not job.elevated:
            self.progress.emit(f'正在申请 CAN 接口{self._operation}授权…')
            self._launch(True)
            return
        self._request = None
        if job.elevated and code == 126:
            message = ('停用 CAN 接口的系统认证已取消，请重试关闭' if self._down else
                       '已取消系统认证，CAN 接口未打开')
        elif job.elevated and code == 127:
            message = '系统授权失败：请确认桌面认证服务可用且当前账户有管理员权限'
        elif code == AUTH_REQUIRED:
            message = f'系统授权后仍无法{self._operation} CAN 接口，请检查系统权限限制'
        success = 'CAN 接口已停用（DOWN）' if self._down else 'CAN 接口已启用'
        self.finished.emit(code == 0, message or (success if code == 0 else f'CAN {self._operation}失败'))

    def cancel(self):
        self._request = None
        job, self._job = self._job, None
        if job is not None:
            job.cancel()
