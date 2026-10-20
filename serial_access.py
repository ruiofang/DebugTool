"""Actionable serial-open diagnostics without changing device permissions."""

from __future__ import annotations

import errno
import os
import shlex
import sys


def describe_serial_open_error(device: str, error: Exception) -> tuple[str, str]:
    """Return user guidance and copyable details for the actual attempted port."""
    code = getattr(error, "errno", None)
    message = str(error)
    if code in (errno.EAGAIN, errno.EBUSY) or (
            code is None and "exclusively lock port" in message):
        guidance = (
            f"串口 {device} 正被其他程序或另一个 DebugTool 窗口占用。\n\n"
            "请先在占用程序中关闭此端口，再直接重试。"
            "调整文件权限不能解除独占锁。"
        )
    elif code in (errno.EACCES, errno.EPERM):
        guidance = (
            f"当前用户没有串口 {device} 的读写权限。\n\n"
            "请重新运行安装脚本以配置串口访问权限；若用户组刚刚更新，"
            "请注销并重新登录后重试。"
        )
    elif code in (errno.ENOENT, errno.ENODEV, errno.ENXIO, errno.EIO):
        guidance = (
            f"串口 {device} 已断开、设备节点不存在或设备无法响应。\n\n"
            "请检查设备连接，刷新串口列表并确认设备编号后重试。"
        )
    else:
        guidance = (
            f"无法打开串口 {device}。\n\n"
            "请检查串口参数和设备状态，展开详细信息查看原始错误。"
        )
    details = f"尝试打开：{device}\n原始错误：{message}"
    if sys.platform.startswith("linux"):
        quoted = shlex.quote(device)
        details += (
            "\n\n在终端执行以下只读命令可检查占用及权限："
            f"\nfuser -v -- {quoted}"
            f"\nlsof -- {quoted}"
            f"\nls -l -- {quoted}"
            "\nid"
            "\n\n如果普通用户看不到占用进程，可使用 sudo fuser -v -- "
            f"{quoted} 查看。请在占用程序中正常关闭端口。"
        )
        try:
            readable = os.access(device, os.R_OK)
            writable = os.access(device, os.W_OK)
            details += f"\n当前进程访问检查：可读={readable}，可写={writable}"
        except OSError:
            pass
    return guidance, details
