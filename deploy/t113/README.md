# 全志 T113 实机部署

2026-09-18 验证设备：Kickpi，ARMv7/armhf，Ubuntu 20.04，Linux 5.4.61，
128 MB 物理内存（Linux 可用约 100 MB），800×1280 framebuffer 和触摸屏。

## 运行

部署目录 `/mnt/UDISK/qt5com`，项目源码在 `app/`，独立依赖在 `runtime/`。
启动入口为 `app/main.py`，工具名称为 DebugTool；服务名保持 `qt5com`。
本目录的 `qt5com.service` 使用上述部署路径，与板端启动配置一致。
此目录的 Python 和 Qt 不替换系统库。桌面端 x86_64 打包产物不能在 ARM 上运行。
更新板端源码时，请将项目根目录的 `VERSION` 和 `app_meta.py` 一起同步到 `app/`，
它们是源码运行时读取应用版本所需的文件。

```sh
systemctl start qt5com   # 在设备屏幕打开
systemctl stop qt5com    # 关闭
systemctl status qt5com --no-pager
journalctl -u qt5com -n 30 --no-pager
systemctl enable qt5com # 启用开机自启
systemctl disable qt5com # 取消开机自启，不停止当前程序
```

也可以在服务停止后前台运行 `/mnt/UDISK/qt5com/bin/qt5com`。
2026-10-20 通过串口控制台更新至 V1.0.4（见根目录 `VERSION`）。更新前的 2026-09-18
实机记录为 V1.0.2。更新后使用板端 Python 3.8.10 完成源码编译和应用导入检查，
`qt5com.service` 已重新启动并确认运行；快捷指令循环、CAN 和物理串口通信尚未逐项实机验收。
服务等待部署目录挂载后启动，异常退出后间隔 5 秒重试，60 秒内最多启动 3 次。
没有安装完整桌面或中文输入法；中文显示已验证，键盘输入需要 USB 键盘，
触摸屏本身不提供软键盘，真实触摸仍未通过验收。

## 环境及布局

- 独立 Ubuntu armhf 软件包：Python 3.8.10、PyQt5 5.14.1 / Qt 5.12.8，
  Qt 动态库、字体、输入设备库和 cryptography/bcrypt/nacl。
- 纯 Python 包：pyserial 3.5、Paramiko 2.12.0、pyte 0.8.2、wcwidth 0.2.13。
- 项目通用开发说明以 Python 3.10+ 为基线；T113 使用已验证的 Python 3.8 兼容代码和
  此板现有独立运行时。`serial_access.py` 的注解采用延迟求值，避免 Python 3.8 在导入时
  求值 `tuple[str, str]` 而失败。源码已在板端 Python 3.8.10 下编译并完整导入验证。
- 通用 `requirements.txt` 面向桌面新版本环境，不要直接在板端运行 pip 安装整个列表，
  也不要把 x86 的 `.venv` 复制过来。
- 本板使用经过实测的系统 Qt 版本；升级 Python 时，PyQt5 绑定及其运行库也必须匹配，
  不应与现有 Python 3.8 运行时混用。
- `python` 包装器设置独立运行库、插件和字体路径。
- `qt5com` 包装器启用 `linuxfb:fb=/dev/fb0:mmsize=212x339`。
  mmsize 用于将此板错误报告的约 312 DPI 校准到约 96 DPI；换屏应重新计算。
- `QT5COM_PORTRAIT=1` 启用上下布局；`QT5COM_FULLSCREEN=1` 使用屏幕尺寸，
  避免从桌面保存的大窗口尺寸导致 framebuffer 超出显示边界和额外内存开销。

## 已验证与限制

板端完成：800×1280 窗口渲染及截图、中文字体、ANSI 终端、PTY 双向收发、
项目 SSHWorker 连接实际 SSH 服务并执行 shell 命令。
串口枚举补充读取全志 `/proc/tty/driver/uart`，不再被 pyserial 的 platform
过滤逻辑隐藏。ttyS1、ttyS4、ttyS5 可枚举；系统控制台 ttyS3 不探测、不开放连接。
V1.0.4 已更新并能由 systemd 启动；快捷指令循环、当前项目的 CAN 界面及实体 CAN 设备
尚未在此板完成验收。物理 UART 接线收发和中文输入法也没有完成实机人工验收。
项目支持 Linux SocketCAN，但板端需有内核 CAN 驱动、已识别并配置的 CAN 接口；
不要据此推断本板 CAN 硬件或权限配置已就绪。
后续真实触摸验收未通过；当前底层触摸事件未正常上报，详见
[触摸诊断记录](TOUCH_DIAGNOSTICS.md)。不要把界面渲染成功当作触摸可用。

板端内存较小，apt 解析索引曾触发 OOM，因此软件包在本机解析/下载后仅解包到
独立 runtime 中。不要在设备上同时执行大型包管理或构建任务。应用端未启用交换分区、
未重启设备、未修改现有网络和 SSH 登录配置。

部署目录中的 `packages.tsv` 记录 Ubuntu 包版本，`probe.log` 为板端启动检测结果。
