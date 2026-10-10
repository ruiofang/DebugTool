#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# DebugTool 安装/卸载脚本 (Linux)
# 安装路径: /opt/qt5com
# 桌面入口: /usr/share/applications/qt5com.desktop
# 启动命令: DebugTool (兼容 qt5com)
#
# 用法:
#   sudo ./install.sh             # 安装，默认配置本机活动桌面的 CAN 免密授权
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

HERE="$(cd "$(dirname "$0")" && pwd)"

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "✘ 请使用 sudo 运行: sudo $0 $*"
        exit 1
    fi
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

    # 2. 图标
    ICON_SRC=""
    for cand in "$HERE/app.png" "$HERE/icon.png"; do
        [[ -f "$cand" ]] && ICON_SRC="$cand" && break
    done

    # 3. 安装
    echo ">>> 安装到 $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"
    install -m 0755 "$EXE" "$INSTALL_DIR/DebugTool"
    if [[ -n "$ICON_SRC" ]]; then
        install -m 0644 "$ICON_SRC" "$INSTALL_DIR/app.png"
        install -m 0644 "$ICON_SRC" "$ICON_DEST"
        # XFCE and other Freedesktop desktops resolve Icon= names through the
        # active icon theme. /usr/share/pixmaps alone is not reliable there.
        mkdir -p "$(dirname "$THEME_ICON_DEST")"
        install -m 0644 "$ICON_SRC" "$THEME_ICON_DEST"
    fi
    # 保持系统安装目录只允许管理员写入，普通用户配置自动回退到用户目录。
    chmod 0755 "$INSTALL_DIR"

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

    # 8. 确保 dialout 组提示
    echo
    echo "✔ 安装完成!"
    echo "   启动:   DebugTool  (或在应用菜单中点击 \"$APP_NAME\")"
    echo "   卸载:   sudo $0 --uninstall"
    echo "   CAN:    本机活动桌面用户可免密配置比特率及 UP/DOWN；卸载保留此授权。"
    echo
    echo "Tips: 如果打开串口提示权限不足，请执行:"
    echo "    sudo usermod -aG dialout \$USER   # 然后重新登录"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
case "${1:-}" in
    -h|--help)
        sed -n '2,12p' "$0"
        ;;
    --uninstall|-u)
        uninstall
        ;;
    *)
        install_app "$@"
        ;;
esac
fi
