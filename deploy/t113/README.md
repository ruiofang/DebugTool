# 全志 T113 实机部署

2026-09-18 验证设备：Kickpi，ARMv7/armhf，Ubuntu 20.04，Linux 5.4.61，
128 MB 物理内存（Linux 可用约 100 MB），800×1280 framebuffer 和触摸屏。

## 运行

部署目录 `/mnt/UDISK/qt5com`，项目源码在 `app/`，独立依赖在 `runtime/`。
启动入口为 `app/main.py`，工具名称为 DebugTool；服务名保持 `qt5com`。
本目录的 `qt5com.service` 使用上述部署路径，与板端启动配置一致。
此目录的 Python 和 Qt 不替换系统库。桌面端 x86_64 打包产物不能在 ARM 上运行。
更新板端源码应使用下方的完整源码更新包，文件清单见 `app_files.txt`。
不要只复制 `main.py`：版本文件、通信模块以及新增的 `quick_command_table.py` 都是运行依赖。

## 本次源码同步（2026-10-21）

更新包包含当前 V1.0.4 的深色日志配色、普通/快捷循环最小 1 ms、隐藏时间及 `>>` / `<<`
前缀、串口读取段分行、快捷指令整行拖动排序及保存。CAN 仍按实际帧逐行显示。
1 ms 是软件请求间隔，实际间隔受 T113 负载、系统调度及接口速度影响；串口读取段
也不等同于设备协议报文，系统已合并的数据仍需按协议判断。

在开发电脑的项目根目录生成更新包：

```sh
.venv/bin/python deploy/t113/make_update.py
```

产物为 `dist/t113/DebugTool-V1.0.4-t113-update.tar.gz` 及对应 `.sha256` 文件。
包内 `SHA256SUMS` 覆盖全部载荷，包含完整 `app/` 源码、启动包装器、字体配置、
服务配置副本和 `update.sh`；不含 x86 可执行程序、`.venv`、用户配置或 ARM runtime。

将压缩包传到板端后，在应用已经退出的维护窗口执行：

```sh
cd /tmp
tar -xzf DebugTool-V1.0.4-t113-update.tar.gz
cd DebugTool-V1.0.4-t113-update
sh update.sh /mnt/UDISK/qt5com
QT_QPA_PLATFORM=offscreen /mnt/UDISK/qt5com/bin/python -c \
  'import main, quick_command_table, touch_poll; print(main.__version__)'
```

更新脚本先验证校验和，再将原文件备份至部署目录的 `backups/source-*`，逐个原子替换
本包文件。保留现有 `runtime/`、`app/serial_tool.ini`、日志及包外板端文件；不自动停启
服务，也不写 `/etc/systemd`、网络或设备权限配置。正常部署仍依赖原有完整 ARM runtime。
`qt5com.service` 副本继续使用原路径，只补充了 CAN 功能描述。

本机已检查完整打包清单、Python 3.8 语法、校验和、触摸入口、重复更新及配置保留。
本次没有连接或更新板端；新包在板端的完整导入、真实触摸拖动、1 ms 时序及实体通信
仍需现场验证，不能将下面的历史实机记录视为本次更新已验收。

## 板端运行及历史记录

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
触摸屏本身不提供软键盘。内核触摸 IRQ 异常后来由应用内轮询绕过，见下方触摸说明。

## 环境及布局

- 独立 Ubuntu armhf 软件包：Python 3.8.10、PyQt5 5.14.1 / Qt 5.12.8，
  Qt 动态库、字体、输入设备库和 cryptography/bcrypt/nacl。
- 纯 Python 包：pyserial 3.5、Paramiko 2.12.0、pyte 0.8.2、wcwidth 0.2.13。
- 项目通用开发说明以 Python 3.10+ 为基线；T113 使用已验证的 Python 3.8 兼容代码和
  此板现有独立运行时。`serial_access.py` 的注解采用延迟求值，避免 Python 3.8 在导入时
  求值 `tuple[str, str]` 而失败。2026-10-20 更新时的源码已在板端 Python 3.8.10 下
  编译并完整导入验证；本次新增改动的验证范围见上方“本次源码同步”。
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

历史板端记录完成：800×1280 窗口渲染及截图、中文字体、ANSI 终端、PTY 双向收发、
项目 SSHWorker 连接实际 SSH 服务并执行 shell 命令。
串口枚举补充读取全志 `/proc/tty/driver/uart`，不再被 pyserial 的 platform
过滤逻辑隐藏。ttyS1、ttyS4、ttyS5 可枚举；系统控制台 ttyS3 不探测、不开放连接。
2026-10-20 的 V1.0.4 已更新并能由 systemd 启动；快捷指令循环、当前项目的 CAN 界面及实体 CAN 设备
尚未在此板完成验收。物理 UART 接线收发和中文输入法也没有完成实机人工验收。
项目支持 Linux SocketCAN，但板端需有内核 CAN 驱动、已识别并配置的 CAN 接口；
不要据此推断本板 CAN 硬件或权限配置已就绪。
2026-09-18 的真实触摸验收未通过；2026-10-20 的后续部署记录显示，应用内 I²C 轮询
恢复了触摸，用户现场确认正常。内核事件路径本身仍未修复，详见
[触摸诊断及后续修复记录](TOUCH_DIAGNOSTICS.md)。

本目录收录该次已验证的 `touch_poll.py`。打包时仅在 T113 的 `app/main.py` 副本中
加入相同的显示后启动/退出清理入口，根目录桌面源码保持原样；锚点不唯一或入口发生
变化时生成脚本会拒绝打包，要求先检查合并位置。`qt5com` 包装器默认设置
`QT5COM_TOUCH_POLL=1`，轮询 `/dev/i2c-1` 的 `0x38`，每 16 ms 将首触点转换为 Qt
鼠标事件。使用正常 evdev 触摸的其他板型应设置 `QT5COM_TOUCH_POLL=0`，避免重复输入。
不会读取触摸硬件，直到实际启动启用了该选项的应用。

板端内存较小，apt 解析索引曾触发 OOM，因此软件包在本机解析/下载后仅解包到
独立 runtime 中。不要在设备上同时执行大型包管理或构建任务。应用端未启用交换分区、
未重启设备、未修改现有网络和 SSH 登录配置。

部署目录中的 `packages.tsv` 记录 Ubuntu 包版本，`probe.log` 为板端启动检测结果。
