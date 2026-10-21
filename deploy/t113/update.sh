#!/bin/sh
# Update sources only. The existing ARM runtime and user data remain in place.
set -eu
umask 077
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=${1:-/mnt/UDISK/qt5com}
case "$TARGET_DIR" in
    /*) ;;
    *) echo '更新目标必须为绝对路径。' >&2; exit 1 ;;
esac
[ "$TARGET_DIR" != / ] || { echo '不能将根目录用作更新目标。' >&2; exit 1; }
for directory in "$TARGET_DIR" "$TARGET_DIR/app" "$TARGET_DIR/bin"; do
    [ ! -L "$directory" ] || { echo "更新目录不能是符号链接：$directory" >&2; exit 1; }
done

# Verify the entire bundle before creating backups or changing installed files.
(cd "$SOURCE_DIR" && sha256sum -c SHA256SUMS)
mkdir -p "$TARGET_DIR/app" "$TARGET_DIR/bin"
BACKUP_DIR="$TARGET_DIR/backups/source-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP_DIR/app" "$BACKUP_DIR/bin"

copy_file() {
    relative=$1
    mode=$2
    if [ -e "$TARGET_DIR/$relative" ] || [ -L "$TARGET_DIR/$relative" ]; then
        cp -a -- "$TARGET_DIR/$relative" "$BACKUP_DIR/$relative"
    fi
    # Rename a complete file into place instead of writing through old links.
    staging=$(mktemp "$TARGET_DIR/$(dirname -- "$relative")/.update.XXXXXX")
    cp -- "$SOURCE_DIR/$relative" "$staging"
    chmod "$mode" "$staging"
    mv -f -- "$staging" "$TARGET_DIR/$relative"
}

for file in "$SOURCE_DIR"/app/*; do
    [ "$(basename -- "$file")" != main.py ] || continue
    copy_file "app/$(basename -- "$file")" 644
done
copy_file app/main.py 644
copy_file bin/python 755
copy_file bin/qt5com 755
copy_file fonts.conf 644
copy_file qt5com.service 644

echo "已更新源码：$TARGET_DIR/app"
echo "原程序备份：$BACKUP_DIR"
echo 'runtime、serial_tool.ini、日志和其他板端文件已保留；未停启服务或修改系统配置。'
echo '请按 README 在板端 Python 3.8 环境验证，再于维护窗口启动应用。'
