#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# DebugTool 安装/卸载脚本 (Linux)
# 安装路径: /opt/qt5com
# 桌面入口: /usr/share/applications/qt5com.desktop
# 启动命令: DebugTool (兼容 qt5com)
#
# 用法:
#   sudo ./install.sh            # 安装
#   sudo ./install.sh --uninstall # 卸载
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

HERE="$(cd "$(dirname "$0")" && pwd)"

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "✘ 请使用 sudo 运行: sudo $0 $*"
        exit 1
    fi
}

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

    # 7. 确保 dialout 组提示
    echo
    echo "✔ 安装完成!"
    echo "   启动:   DebugTool  (或在应用菜单中点击 \"$APP_NAME\")"
    echo "   卸载:   sudo $0 --uninstall"
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
