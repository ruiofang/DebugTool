"""Small, stdlib-only SocketCAN configuration helper.

The GUI invokes this in a separate process, optionally through pkexec. It never
opens a CAN socket or sends a frame. All commands use fixed executables and argv.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


AUTH_REQUIRED = 77
COMMAND_TIMEOUT = 5
IP_PATHS = ('/usr/sbin/ip', '/usr/bin/ip', '/sbin/ip', '/bin/ip')


class ConfigError(Exception):
    pass


class AuthorizationRequired(ConfigError):
    pass


def validate_interface(interface):
    if (not isinstance(interface, str) or not interface
            or len(interface.encode('utf-8')) > 15 or interface in ('.', '..')
            or any(char in interface for char in '/\x00:')
            or any(char.isspace() for char in interface)):
        raise ConfigError('请输入有效的 CAN 接口名，例如 can0 或 vcan0')


def validate_request(interface, bitrate, fd, data_bitrate):
    validate_interface(interface)
    for value, label in ((bitrate, '比特率'), (data_bitrate, '数据比特率')):
        if type(value) is not int or not 1 <= value <= 100_000_000:
            raise ConfigError(f'{label}必须是 1..100000000 之间的整数')
    if type(fd) is not bool:
        raise ConfigError('CAN FD 开关无效')


def system_environment():
    """Remove PyInstaller's bundled libraries before running system programs."""
    env = os.environ.copy()
    for key in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'):
        original = env.pop(key + '_ORIG', None)
        if original:
            env[key] = original
        else:
            env.pop(key, None)
    for key in list(env):
        if key.startswith('_PYI_') or key in ('PYINSTALLER_RESET_ENVIRONMENT',
                                             'PYTHONHOME', 'PYTHONPATH'):
            env.pop(key, None)
    return env


def find_ip():
    for path in IP_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise ConfigError('未找到系统 ip 工具，请安装 iproute2 后重试')


def _run_ip(ip, args, *, mutation=False):
    try:
        result = subprocess.run([ip, *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=COMMAND_TIMEOUT, env=system_environment(),
                                check=False)
    except subprocess.TimeoutExpired as exc:
        raise ConfigError('系统 CAN 操作超时，请检查接口状态后重试') from exc
    except OSError as exc:
        raise ConfigError(f'无法启动系统 ip 工具：{exc}') from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2048:]
        # iproute2 diagnostics are made predictable by the helper's locale.
        if mutation and ('Operation not permitted' in detail or 'Permission denied' in detail):
            raise AuthorizationRequired('更改 CAN 接口需要系统管理员授权')
        raise ConfigError(f'系统 CAN 操作失败：{detail or "ip 返回错误"}')
    return result.stdout


def read_link(ip, interface):
    try:
        link_type = int((Path('/sys/class/net') / interface / 'type').read_text().strip())
    except (OSError, ValueError) as exc:
        raise ConfigError(f'CAN 接口 {interface} 不存在或无法读取，请连接设备并刷新') from exc
    if link_type != 280:
        raise ConfigError(f'{interface} 不是 CAN 接口')
    output = _run_ip(ip, ['-j', '-details', 'link', 'show', 'dev', interface])
    try:
        links = json.loads(output)
        if len(links) != 1 or links[0]['ifname'] != interface:
            raise ValueError('interface mismatch')
        link = links[0]
        if not isinstance(link.get('linkinfo'), dict):
            raise ValueError('missing link info')
        if (not isinstance(link.get('flags'), list)
                or any(not isinstance(flag, str) for flag in link['flags'])):
            raise ValueError('missing or invalid link flags')
        return link
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ConfigError('无法解析 CAN 接口状态，请检查系统 iproute2 版本') from exc


def configuration_commands(link, interface, bitrate, fd, data_bitrate):
    """Return the minimal link changes. An already matching UP link is untouched."""
    info = link.get('linkinfo', {})
    kind = info.get('info_kind')
    up = 'UP' in link.get('flags', [])
    if kind in ('vcan', 'vxcan'):
        if fd and link.get('mtu', 0) < 72:
            raise ConfigError(f'{interface} 的 MTU 不支持 CAN FD，请先调整虚拟接口配置')
        return [] if up else [['link', 'set', 'dev', interface, 'up']]
    if kind != 'can':
        raise ConfigError(f'{interface} 不是支持配置的 SocketCAN 接口')
    data = info.get('info_data') or {}
    current_fd = link.get('mtu') == 72
    matches = ((data.get('bittiming') or {}).get('bitrate') == bitrate
               and current_fd == fd
               and (not fd or (data.get('data_bittiming') or {}).get('bitrate') == data_bitrate))
    if up and matches:
        return []
    commands = []
    if up:
        commands.append(['link', 'set', 'dev', interface, 'down'])
    config = ['link', 'set', 'dev', interface, 'type', 'can', 'bitrate', str(bitrate)]
    if fd:
        config += ['dbitrate', str(data_bitrate), 'fd', 'on']
    elif current_fd:
        # Classic-only drivers may reject even an explicit "fd off" request.
        config += ['fd', 'off']
    commands.extend([config, ['link', 'set', 'dev', interface, 'up']])
    return commands


def configure(interface, bitrate, fd=False, data_bitrate=2_000_000):
    if not sys.platform.startswith('linux'):
        raise ConfigError('SocketCAN 接口配置仅支持 Linux')
    validate_request(interface, bitrate, fd, data_bitrate)
    ip = find_ip()
    link = read_link(ip, interface)
    commands = configuration_commands(link, interface, bitrate, fd, data_bitrate)
    if not commands:
        return f'{interface} 已启用，配置与所选参数一致'
    # The ip executable may have permissions that this Python helper does not.
    # Let the real operation decide; only EPERM/EACCES requests authorization.
    changed = False
    try:
        for command in commands:
            _run_ip(ip, command, mutation=True)
            changed = True
    except ConfigError as exc:
        if changed:
            error_type = AuthorizationRequired if isinstance(exc, AuthorizationRequired) else ConfigError
            raise error_type(f'{exc}；接口配置未完成，请检查状态后重试') from exc
        raise
    return f'{interface} 已配置并启用'


def disable(interface):
    """Stop only the selected CAN interface and verify its administrative state."""
    if not sys.platform.startswith('linux'):
        raise ConfigError('SocketCAN 接口停用仅支持 Linux')
    validate_interface(interface)
    ip = find_ip()
    link = read_link(ip, interface)
    if link['linkinfo'].get('info_kind') not in ('can', 'vcan', 'vxcan'):
        raise ConfigError(f'{interface} 不是支持停用的 SocketCAN 接口')
    if 'UP' not in link['flags']:
        return f'{interface} 已处于 DOWN 状态'
    try:
        _run_ip(ip, ['link', 'set', 'dev', interface, 'down'], mutation=True)
    except AuthorizationRequired as exc:
        raise AuthorizationRequired('停用 CAN 接口需要系统管理员授权') from exc
    except ConfigError as exc:
        raise ConfigError(f'停用 {interface} 失败：{exc}') from exc
    try:
        stopped_link = read_link(ip, interface)
    except ConfigError as exc:
        raise ConfigError(f'已请求停用 {interface}，但无法确认 DOWN 状态：{exc}') from exc
    if 'UP' in stopped_link['flags']:
        raise ConfigError(f'停用 {interface} 后接口仍处于 UP 状态，请检查系统网络服务后重试')
    return f'{interface} 已停用（DOWN）'


def main(argv=None):
    parser = argparse.ArgumentParser(description='配置启用或停用 Linux SocketCAN 接口')
    parser.add_argument('--interface', required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--bitrate', type=int)
    operation.add_argument('--down', action='store_true')
    parser.add_argument('--fd', action='store_true')
    parser.add_argument('--data-bitrate', type=int)
    args = parser.parse_args(argv)
    if args.down and (args.fd or args.data_bitrate is not None):
        parser.error('--down 不能与 --fd 或 --data-bitrate 同时使用')
    # Force only the external utility's diagnostics, not GUI translations.
    os.environ['LC_ALL'] = 'C'
    try:
        message = (disable(args.interface) if args.down else
                   configure(args.interface, args.bitrate, args.fd,
                             args.data_bitrate if args.data_bitrate is not None else 2_000_000))
    except AuthorizationRequired as exc:
        print(str(exc), flush=True)
        return AUTH_REQUIRED
    except ConfigError as exc:
        print(str(exc), flush=True)
        return 1
    print(message, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
