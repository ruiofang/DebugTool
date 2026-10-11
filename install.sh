#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# DebugTool 安装/卸载脚本 (Linux)
# 安装路径: /opt/qt5com
# 桌面入口: /usr/share/applications/qt5com.desktop
# 启动命令: DebugTool (兼容 qt5com)
#
# 用法:
#   sudo ./install.sh             # 安装，为 sudo 调用者配置文件及串口权限
#   sudo ./install.sh --user ruio # 为指定普通用户安装
#   sudo ./install.sh --permissions-only # 仅修复现有安装的配置及串口权限
#   sudo ./install.sh --uninstall # 卸载主程序，保留 CAN 助手和授权策略
#
set -e

APP_NAME="DebugTool"
APP_ID="qt5com"
INSTALL_DIR="/opt/qt5com"
BIN_LINK="/usr/local/bin/DebugTool"
LEGACY_BIN_LINK="/usr/local/bin/qt5com"
DESKTOP_FILE="/usr/share/applications/qt5com.desktop"
ICON_DEST="/usr/share/pixmaps/qt5com.png"
THEME_ICON_DEST="/usr/share/icons/hicolor/256x256/apps/qt5com.png"
ICON_THEME_DIR="/usr/share/icons/hicolor"
CAN_INSTALL_DIR="/usr/local/libexec/debugtool"
CAN_LAUNCHER="$CAN_INSTALL_DIR/can-config"
CAN_HELPER="$CAN_INSTALL_DIR/can_config.py"
CAN_POLICY_DIR="/usr/share/polkit-1/actions"
CAN_POLICY="$CAN_POLICY_DIR/org.ruio.debugtool.configure-can.policy"
CAN_PYTHON="/usr/bin/python3"
CAN_PKEXEC=""
CAN_IP=""
INSTALL_USER=""
INSTALL_UID=""
INSTALL_GID=""
INSTALL_HOME=""
SERIAL_GROUP=""
SERIAL_RULES_DIR="/etc/udev/rules.d"
SERIAL_RULES="$SERIAL_RULES_DIR/70-debugtool-serial.rules"
SERIAL_DEVICE_DIR="/dev"

HERE="$(cd "$(dirname "$0")" && pwd)"

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "✘ 请使用 sudo 运行: sudo $0 $*"
        exit 1
    fi
}

resolve_install_user() {
    local requested="${INSTALL_USER:-${SUDO_USER:-}}" record name password uid gid gecos user_home login_shell
    if [[ -z "$requested" || "$requested" == root || "$requested" == -* ]]; then
        echo "✘ 无法确定普通用户；请通过 sudo 运行，或指定 --user 用户名。" >&2
        return 1
    fi
    record=$(getent passwd "$requested") || {
        echo "✘ 用户不存在: $requested" >&2
        return 1
    }
    IFS=: read -r name password uid gid gecos user_home login_shell <<< "$record"
    if [[ "$name" != "$requested" || ! "$uid" =~ ^[0-9]+$ || "$uid" == 0 ||
          ! "$gid" =~ ^[0-9]+$ || "$user_home" != /* ]]; then
        echo "✘ 必须指定有有效主目录的普通用户: $requested" >&2
        return 1
    fi
    INSTALL_USER="$name"
    INSTALL_UID="$uid"
    INSTALL_GID="$gid"
    INSTALL_HOME="$user_home"
}

check_app_directory() {
    # /opt is administrator-controlled; the application directory itself may
    # belong to the intended desktop user. Never follow a replaced directory.
    check_secure_directory_tree "$(dirname -- "$INSTALL_DIR")" || return 1
    if [[ -L "$INSTALL_DIR" || ( -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ) ]]; then
        echo "✘ 安装目录不能是符号链接或非目录: $INSTALL_DIR" >&2
        return 1
    fi
    if [[ -L "$INSTALL_DIR/serial_tool.ini" ||
          ( -e "$INSTALL_DIR/serial_tool.ini" && ! -f "$INSTALL_DIR/serial_tool.ini" ) ]]; then
        echo "✘ 配置文件不能是符号链接或非普通文件: $INSTALL_DIR/serial_tool.ini" >&2
        return 1
    fi
}

install_app_file() (
    # Stage in the root-owned parent, then rename over the destination. A
    # user-writable application directory must not turn root's install/chmod
    # into writes through an existing symlink or hard link.
    set -e
    local source="$1" target="$2" mode="$3" staged_dir
    check_app_directory || exit 1
    staged_dir=$(mktemp -d "$(dirname -- "$INSTALL_DIR")/.qt5com-install.XXXXXX") || exit 1
    trap 'rm -rf -- "$staged_dir"' EXIT
    install -o root -g root -m "$mode" -- "$source" "$staged_dir/file"
    mv -fT -- "$staged_dir/file" "$target"
)

configure_app_permissions() (
    set -e
    local source="" fallback="$INSTALL_HOME/.config/qt5com/serial_tool.ini" staged_dir
    check_app_directory || exit 1
    if [[ ! -x "$CAN_PYTHON" ]] || ! "$CAN_PYTHON" -I -S -c \
            'import os, shutil, stat, sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)'; then
        echo "✘ 安全迁移配置需要系统 Python 3.7+: $CAN_PYTHON" >&2
        exit 1
    fi
    install -d -o "$INSTALL_UID" -g "$INSTALL_GID" -m 0755 -- "$INSTALL_DIR"
    if [[ -f "$INSTALL_DIR/serial_tool.ini" ]]; then
        source="$INSTALL_DIR/serial_tool.ini"
    elif [[ -f "$fallback" && ! -L "$fallback" &&
            ! -L "$INSTALL_HOME/.config" && ! -L "$INSTALL_HOME/.config/qt5com" ]]; then
        source="$fallback"
    fi
    staged_dir=$(mktemp -d "$(dirname -- "$INSTALL_DIR")/.qt5com-config.XXXXXX") || exit 1
    trap 'rm -rf -- "$staged_dir"' EXIT
    if [[ -n "$source" ]]; then
        # Open every component without following links: a user can rename their
        # .config directory during a root install. Copy the bytes, never mutate
        # the original inode or follow a link into another user's settings.
        "$CAN_PYTHON" -I -S - "$source" "$staged_dir/serial_tool.ini" <<'PY'
import os
import shutil
import stat
import sys

directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
try:
    components = sys.argv[1].split("/")
    for part in components[1:-1]:
        if not part or part in (".", ".."):
            raise ValueError("invalid configuration path")
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    source = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(source, "rb") as original:
        metadata = os.fstat(original.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("configuration must be a regular file without hard links")
        with open(sys.argv[2], "xb") as staged:
            shutil.copyfileobj(original, staged)
finally:
    os.close(directory)
PY
    else
        : > "$staged_dir/serial_tool.ini"
    fi
    chown "$INSTALL_UID:$INSTALL_GID" "$staged_dir/serial_tool.ini"
    chmod 0600 "$staged_dir/serial_tool.ini"
    mv -fT -- "$staged_dir/serial_tool.ini" "$INSTALL_DIR/serial_tool.ini"
    [[ "$source" != "$fallback" ]] || echo "✔ 已保留原用户配置到 $INSTALL_DIR/serial_tool.ini（原文件保留）。"
    echo "✔ 配置目录: $INSTALL_DIR（$INSTALL_USER 所有，0755；配置文件仅该用户可读写）。"
)

check_serial_dependencies() {
    command -v usermod >/dev/null 2>&1 || {
        echo "✘ 配置串口权限需要 usermod。" >&2
        return 1
    }
    if getent group dialout >/dev/null; then
        SERIAL_GROUP=dialout
    elif getent group uucp >/dev/null; then
        SERIAL_GROUP=uucp
    else
        echo "✘ 未找到串口组 dialout 或 uucp，请先安装系统串口/udev 支持。" >&2
        return 1
    fi
    command -v udevadm >/dev/null 2>&1 || {
        echo "✘ 配置串口权限需要 udevadm，请先安装系统 udev。" >&2
        return 1
    }
    check_secure_file_target "$SERIAL_RULES" || return 1
}

install_serial_permissions() (
    set -e
    local staged_dir device basename permissions_pending=0
    local -a serial_devices=()
    ensure_secure_directory "$SERIAL_RULES_DIR"
    staged_dir=$(mktemp -d "$SERIAL_RULES_DIR/.debugtool-serial.XXXXXX") || exit 1
    trap 'rm -rf -- "$staged_dir"' EXIT
    # Run before systemd's 73-seat-late.rules so uaccess reaches active seats.
    # Limit this rule to actual serial names; never grant access to tty/console/pts.
    {
        echo '# DebugTool: serial group plus active-local-session access.'
        for basename in ttyUSB ttyACM ttyS ttyAMA ttyTHS ttymxc; do
            printf 'SUBSYSTEM=="tty", KERNEL=="%s[0-9]*", GROUP="%s", MODE="0660", TAG+="uaccess"\n' \
                "$basename" "$SERIAL_GROUP"
        done
    } > "$staged_dir/rules"
    install_can_file "$staged_dir/rules" "$SERIAL_RULES" 0644
    usermod -a -G "$SERIAL_GROUP" -- "$INSTALL_USER"
    udevadm control --reload-rules
    for device in "$SERIAL_DEVICE_DIR"/ttyUSB* "$SERIAL_DEVICE_DIR"/ttyACM* \
                  "$SERIAL_DEVICE_DIR"/ttyS* "$SERIAL_DEVICE_DIR"/ttyAMA* \
                  "$SERIAL_DEVICE_DIR"/ttyTHS* "$SERIAL_DEVICE_DIR"/ttymxc*; do
        basename=${device##*/}
        [[ "$basename" =~ ^(ttyUSB|ttyACM|ttyS|ttyAMA|ttyTHS|ttymxc)[0-9]+$ ]] || continue
        [[ -c "$device" && ! -L "$device" ]] || continue
        serial_devices+=("$device")
        # Reapply only matching serial rules; no serial open, I/O or process kill.
        udevadm trigger --action=change --subsystem-match=tty --sysname-match="$basename" || permissions_pending=1
    done
    # uaccess can rewrite ACLs asynchronously; set the selected user's ACL only
    # after all change events have completed.
    udevadm settle --timeout=5 || permissions_pending=1
    for device in "${serial_devices[@]}"; do
        [[ -c "$device" && ! -L "$device" ]] || continue
        # A precise ACL also serves an already-running session whose group list
        # predates usermod. ACL absence/failure is recoverable by logging in again.
        if command -v setfacl >/dev/null 2>&1; then
            setfacl -m "u:$INSTALL_UID:rw" -- "$device" || permissions_pending=1
        else
            permissions_pending=1
        fi
    done
    echo "✔ 串口权限: $INSTALL_USER 已加入 $SERIAL_GROUP；已安装持续生效的 udev 规则。"
    if (( permissions_pending )); then
        echo "   当前会话权限若未生效，请注销并重新登录，再重新插拔串口设备。"
    fi
    echo "   其他程序独占串口产生的 Errno 11 需要先关闭占用程序；权限修复不会抢占设备。"
)

repair_permissions() {
    need_root
    resolve_install_user || return 1
    if [[ ! -d "$INSTALL_DIR" ]]; then
        echo "✘ 尚未安装 $INSTALL_DIR，请先执行完整安装。" >&2
        return 1
    fi
    check_app_directory || return 1
    check_serial_dependencies || return 1
    configure_app_permissions
    install_serial_permissions
    echo "✔ 已修复安装权限；请以普通用户 $INSTALL_USER 重新打开 DebugTool。"
}

check_secure_directory_tree() {
    # The password-free entry must never load code from a user-writable path.
    local current="$1" owner mode
    if [[ "$current" != /* ]]; then
        echo "✘ CAN 安装路径必须是绝对路径: $current" >&2
        return 1
    fi
    while :; do
        if [[ -L "$current" || ( -e "$current" && ! -d "$current" ) ]]; then
            echo "✘ CAN 安装路径不能是符号链接或非目录: $current" >&2
            return 1
        fi
        if [[ -d "$current" ]]; then
            read -r owner mode < <(stat -c '%u %a' -- "$current") || return 1
            if [[ "$owner" != 0 ]] || (( (8#$mode & 0022) != 0 )); then
                echo "✘ CAN 安装目录须由 root 拥有，且普通用户不可写: $current" >&2
                return 1
            fi
        fi
        [[ "$current" == / ]] && break
        current=$(dirname -- "$current")
    done
}

check_secure_file_target() {
    local target="$1" owner mode
    check_secure_directory_tree "$(dirname -- "$target")" || return 1
    if [[ -L "$target" || ( -e "$target" && ! -f "$target" ) ]]; then
        echo "✘ CAN 安装目标不能是符号链接或非普通文件: $target" >&2
        return 1
    fi
    if [[ -f "$target" ]]; then
        read -r owner mode < <(stat -c '%u %a' -- "$target") || return 1
        if [[ "$owner" != 0 ]] || (( (8#$mode & 0022) != 0 )); then
            echo "✘ CAN 安装目标须由 root 拥有，且普通用户不可写: $target" >&2
            return 1
        fi
    fi
}

ensure_secure_directory() {
    check_secure_directory_tree "$1" || return 1
    install -d -o root -g root -m 0755 -- "$1"
}

check_can_dependencies() {
    local candidate
    if [[ ! -f "$HERE/can_config.py" || ! -r "$HERE/can_config.py" ]]; then
        echo "✘ 缺少 CAN 助手源码，请将 can_config.py 与 install.sh 放在同一目录。" >&2
        return 1
    fi
    if [[ ! -x "$CAN_PYTHON" ]] || ! "$CAN_PYTHON" -I -S -c \
            'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)'; then
        echo "✘ CAN 免密助手需要系统 Python 3.7 或更高版本: $CAN_PYTHON" >&2
        return 1
    fi
    if [[ -z "$CAN_PKEXEC" ]]; then
        for candidate in /usr/bin/pkexec /bin/pkexec; do
            [[ -f "$candidate" && -x "$candidate" ]] && CAN_PKEXEC="$candidate" && break
        done
    fi
    if [[ ! -f "$CAN_PKEXEC" || ! -x "$CAN_PKEXEC" || ! -d "$CAN_POLICY_DIR" ]]; then
        echo "✘ CAN 免密授权需要系统 polkit / pkexec，请安装后重试。" >&2
        return 1
    fi
    if [[ -z "$CAN_IP" ]]; then
        for candidate in /usr/sbin/ip /usr/bin/ip /sbin/ip /bin/ip; do
            [[ -f "$candidate" && -x "$candidate" ]] && CAN_IP="$candidate" && break
        done
    fi
    if [[ ! -f "$CAN_IP" || ! -x "$CAN_IP" ]]; then
        echo "✘ CAN 接口配置需要系统 ip 工具，请安装 iproute2 后重试。" >&2
        return 1
    fi
    check_secure_file_target "$CAN_LAUNCHER" || return 1
    check_secure_file_target "$CAN_HELPER" || return 1
    check_secure_file_target "$CAN_POLICY" || return 1
    # Parse only: no helper execution and no interface changes during install.
    "$CAN_PYTHON" -I -S -c \
        'import pathlib, sys; p = pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), "exec")' \
        "$HERE/can_config.py" || return 1
}

install_can_file() (
    # Stage beside the destination so replacement is atomic, including upgrades.
    set -e
    local source="$1" target="$2" mode="$3" staged
    check_secure_file_target "$target" || exit 1
    staged=$(mktemp "$(dirname -- "$target")/.debugtool-can.XXXXXX") || exit 1
    trap 'rm -f -- "$staged"' EXIT
    install -T -o root -g root -m "$mode" -- "$source" "$staged" || exit 1
    mv -fT -- "$staged" "$target" || exit 1
)

install_can_permissions() (
    set -e
    local staged_dir
    ensure_secure_directory "$CAN_INSTALL_DIR" || exit 1
    check_secure_directory_tree "$CAN_POLICY_DIR" || exit 1
    staged_dir=$(mktemp -d "$CAN_INSTALL_DIR/.install.XXXXXX") || exit 1
    trap 'rm -rf -- "$staged_dir"' EXIT
    cat > "$staged_dir/can-config" <<EOF
#!/bin/sh
# Dedicated CAN configuration entry; no GUI, arbitrary commands or user modules.
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C "$CAN_PYTHON" -I -S "$CAN_HELPER" "\$@"
EOF
    cat > "$staged_dir/can.policy" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD polkit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/software/polkit/policyconfig-1.dtd">
<policyconfig>
  <vendor>RUIO</vendor>
  <action id="org.ruio.debugtool.configure-can">
    <description>Configure a CAN interface for DebugTool</description>
    <description xml:lang="zh_CN">配置 DebugTool CAN 接口</description>
    <message>Administrator authorization is required to configure a CAN interface.</message>
    <message xml:lang="zh_CN">配置 CAN 接口需要管理员授权。</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">$CAN_LAUNCHER</annotate>
  </action>
</policyconfig>
EOF
    install_can_file "$HERE/can_config.py" "$CAN_HELPER" 0644 || exit 1
    install_can_file "$staged_dir/can-config" "$CAN_LAUNCHER" 0755 || exit 1
    # Publish authorization last, after both independently runnable files exist.
    install_can_file "$staged_dir/can.policy" "$CAN_POLICY" 0644 || exit 1
    echo "✔ 已配置 CAN 免密授权（本机当前活动桌面用户）。"
)

uninstall() {
    need_root "$@"
    echo ">>> 卸载 $APP_NAME ..."
    rm -rf "$INSTALL_DIR"
    rm -f  "$BIN_LINK" "$LEGACY_BIN_LINK" "$DESKTOP_FILE" "$ICON_DEST" "$THEME_ICON_DEST"

    # 清理各用户的配置文件 (~/.config/qt5com)
    echo ">>> 清理用户配置文件 ..."
    # /root
    rm -rf "/root/.config/qt5com" 2>/dev/null || true
    # /home/*
    for _home in /home/*; do
        [[ -d "$_home" ]] || continue
        rm -rf "$_home/.config/qt5com" 2>/dev/null || true
    done
    # 调用者 (sudo 前的用户), 兜底一次
    if [[ -n "${SUDO_USER:-}" ]]; then
        _uhome=$(getent passwd "$SUDO_USER" | cut -d: -f6)
        [[ -n "$_uhome" ]] && rm -rf "$_uhome/.config/qt5com" 2>/dev/null || true
    fi

    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$ICON_THEME_DIR" >/dev/null 2>&1 || true
    echo "✔ 卸载完成。"
    echo "   CAN 助手和授权策略已保留，重新安装可继续使用。"
    echo "   助手: $CAN_INSTALL_DIR"
    echo "   策略: $CAN_POLICY"
}

select_executable() {
    # VERSION is data, never a shell script. An explicit file always wins.
    EXE=""
    if [[ -n "${1:-}" ]]; then
        if [[ ! -f "$1" ]]; then
            echo "✘ 指定的可执行文件不存在: $1" >&2
            return 1
        fi
        EXE="$1"
    elif [[ -f "$HERE/VERSION" ]]; then
        local version component normalized=""
        local -a components
        version=$(<"$HERE/VERSION")
        # Match app_meta.normalize_version: allow surrounding whitespace and
        # an optional V/v prefix, then canonicalize numeric components.
        version=${version#"${version%%[![:space:]]*}"}
        version=${version%"${version##*[![:space:]]}"}
        if [[ ! "$version" =~ ^[Vv]?[0-9]{1,5}(\.[0-9]{1,5}){2,3}$ ]]; then
            echo "✘ VERSION 格式错误，应为 V1.2.3 或 V1.2.3.4。" >&2
            return 1
        fi
        IFS='.' read -r -a components <<< "${version#[Vv]}"
        for component in "${components[@]}"; do
            if (( 10#$component > 65535 )); then
                echo "✘ VERSION 每段数值必须在 0～65535 之间。" >&2
                return 1
            fi
            normalized+="${normalized:+.}$((10#$component))"
        done
        version="V$normalized"
        EXE="$HERE/dist/DebugTool-${version}-linux"
        if [[ ! -f "$EXE" || ! -x "$EXE" ]]; then
            echo "✘ 未找到 VERSION 对应的可执行文件: $EXE" >&2
            echo "    请先运行 python3 build.py，或显式指定要安装的文件。" >&2
            return 1
        fi
    else
        # Standalone distributions may not include VERSION. Sort numerically
        # so V1.0.10 is newer than V1.0.9, and exclude backups/archives.
        local candidate
        local -a candidates=()
        for candidate in "$HERE"/dist/DebugTool-*-linux; do
            if [[ -f "$candidate" && -x "$candidate" ]]; then
                candidates+=("$candidate")
            fi
        done
        if (( ${#candidates[@]} )); then
            while IFS= read -r -d '' candidate; do
                EXE="$candidate"
            done < <(printf '%s\0' "${candidates[@]}" | sort -z -V)
        fi
    fi
    if [[ -z "$EXE" ]]; then
        echo "✘ 未找到可执行文件。请先 'python3 build.py' 或手动指定:"
        echo "    sudo $0 /path/to/DebugTool"
        return 1
    fi
}

install_app() {
    need_root "$@"

    # 1. 定位可执行文件
    select_executable "${1:-}" || exit 1
    echo ">>> 使用可执行文件: $EXE"
    # Fail before changing the installed application if CAN authorization cannot
    # be installed. Normal launches never change system permissions themselves.
    check_can_dependencies || exit 1
    resolve_install_user || exit 1
    check_app_directory || exit 1
    check_serial_dependencies || exit 1

    # 2. 图标
    ICON_SRC=""
    for cand in "$HERE/app.png" "$HERE/icon.png"; do
        [[ -f "$cand" ]] && ICON_SRC="$cand" && break
    done

    # 3. 安装
    echo ">>> 安装到 $INSTALL_DIR ..."
    configure_app_permissions
    install_app_file "$EXE" "$INSTALL_DIR/DebugTool" 0755
    if [[ -n "$ICON_SRC" ]]; then
        install_app_file "$ICON_SRC" "$INSTALL_DIR/app.png" 0644
        install -m 0644 "$ICON_SRC" "$ICON_DEST"
        # XFCE and other Freedesktop desktops resolve Icon= names through the
        # active icon theme. /usr/share/pixmaps alone is not reliable there.
        mkdir -p "$(dirname "$THEME_ICON_DEST")"
        install -m 0644 "$ICON_SRC" "$THEME_ICON_DEST"
    fi

    # 4. /usr/local/bin 软链接
    ln -sf "$INSTALL_DIR/DebugTool" "$BIN_LINK"
    ln -sf "$INSTALL_DIR/DebugTool" "$LEGACY_BIN_LINK"

    # 5. 桌面入口
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
GenericName=DebugTool
Comment=串口、SSH、TCP 和 UDP 调试工具 (PyQt5)
Exec=$INSTALL_DIR/DebugTool
Icon=$APP_ID
Terminal=false
Categories=Development;Electronics;
Keywords=serial;uart;com;rs232;ssh;tcp;udp;debug;
StartupNotify=true
StartupWMClass=DebugTool
DBusActivatable=false
EOF
    chmod 0644 "$DESKTOP_FILE"

    # 6. 更新桌面数据库
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$ICON_THEME_DIR" >/dev/null 2>&1 || true

    # 7. Dedicated CAN-only authorization, independent of the GUI installation.
    install_can_permissions

    # 8. 安装时配置串口组、设备热插拔权限和当前设备 ACL。
    install_serial_permissions
    echo
    echo "✔ 安装完成!"
    echo "   启动:   DebugTool  (或在应用菜单中点击 \"$APP_NAME\")"
    echo "   卸载:   sudo $0 --uninstall"
    echo "   CAN:    本机活动桌面用户可免密配置比特率及 UP/DOWN；卸载保留此授权。"
    echo "   配置:   $INSTALL_DIR/serial_tool.ini（请以普通用户 $INSTALL_USER 启动）。"
    echo
}

main() {
    local mode=install executable=""
    while (( $# )); do
        case "$1" in
            -h|--help)
                echo "用法: sudo $0 [--user 用户名] [可执行文件]"
                echo "      sudo $0 [--user 用户名] --permissions-only"
                echo "      sudo $0 --uninstall"
                echo "默认用户为 SUDO_USER；root 直接运行时须指定 --user。"
                return 0 ;;
            --user)
                if (( $# < 2 )) || [[ -z "$2" || "$2" == -* ]]; then
                    echo "✘ --user 需要普通用户名。" >&2
                    return 1
                fi
                INSTALL_USER="$2"
                shift 2 ;;
            --permissions-only)
                [[ "$mode" == install ]] || { echo "✘ 操作参数冲突。" >&2; return 1; }
                mode=permissions
                shift ;;
            --uninstall|-u)
                [[ "$mode" == install ]] || { echo "✘ 操作参数冲突。" >&2; return 1; }
                mode=uninstall
                shift ;;
            -*) echo "✘ 未知参数: $1" >&2; return 1 ;;
            *)
                [[ -z "$executable" ]] || { echo "✘ 只能指定一个可执行文件。" >&2; return 1; }
                executable="$1"
                shift ;;
        esac
    done
    if [[ "$mode" != install && -n "$executable" ]]; then
        echo "✘ 此操作不能同时指定可执行文件。" >&2
        return 1
    fi
    case "$mode" in
        install) install_app "$executable" ;;
        permissions) repair_permissions ;;
        uninstall) uninstall ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
