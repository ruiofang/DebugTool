#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨平台单文件打包脚本（Linux / Windows 通用）
用法:
    python3 build.py            # 输入版本号后打包，回车沿用当前版本
    python3 build.py --version 1.0.3  # 设置版本并打包
    python3 build.py --set-version 1.0.3  # 只设置版本，不打包
    python3 build.py --yes      # 不询问，使用当前版本打包
    python3 build.py --clean    # 清理 build/dist/*.spec

依赖:
    pip install pyinstaller -r requirements.txt
"""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app_meta import APP_NAME, AUTHOR, normalize_version, read_version, version_tuple, write_version

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "main.py"
CAN_MODULES = ('can', 'gs_usb', 'usb', 'libusb_package', 'uptime')


def read_meta():
    """Read the same VERSION source used by the application."""
    return read_version(HERE / "VERSION"), AUTHOR


def parse_version_tuple(v: str):
    return version_tuple(v)


def write_win_version_file(version: str, author: str, dst: Path, filename=None):
    """生成 PyInstaller 使用的 Windows version 资源文件。"""
    vt = parse_version_tuple(version)
    filename = filename or f"{APP_NAME}-{version}.exe"
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [
            StringStruct(u'CompanyName',      u'{author}'),
            StringStruct(u'FileDescription',  u'DebugTool'),
            StringStruct(u'FileVersion',      u'{version}'),
            StringStruct(u'InternalName',     u'{APP_NAME}'),
            StringStruct(u'LegalCopyright',   u'Copyright (c) {author}'),
            StringStruct(u'OriginalFilename', u'{filename}'),
            StringStruct(u'ProductName',      u'DebugTool'),
            StringStruct(u'ProductVersion',   u'{version}')
          ])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    dst.write_text(content, encoding="utf-8")


def clean():
    for d in ("build", "dist", "__pycache__"):
        p = HERE / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"removed {p}")
    for pat in ("*.spec", "version_info.txt"):
        for f in HERE.glob(pat):
            f.unlink()
            print(f"removed {f}")


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(">>> 安装 PyInstaller ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "pyinstaller", "pyqt5", "pyserial"])


def ensure_icon():
    """若 app.png/app.ico 缺失，则调用 gen_icon.py 生成。"""
    need = not ((HERE / "app.png").exists() and (HERE / "app.ico").exists())
    gen = HERE / "gen_icon.py"
    if need and gen.exists():
        print(">>> 生成图标 ...")
        env = os.environ.copy()
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        subprocess.check_call([sys.executable, str(gen)], cwd=str(HERE), env=env)


def can_packaging_options(is_win):
    """Collect dynamic CAN backends and libusb's bundled DLL without opening USB."""
    available = {name: importlib.util.find_spec(name) is not None for name in CAN_MODULES}
    missing = [name for name, present in available.items() if not present]
    if is_win and missing:
        raise RuntimeError('缺少 Windows CAN 打包依赖：' + ', '.join(missing) +
                           '；请使用当前 Python 执行 python -m pip install -r requirements.txt')
    options = []
    if available['can']:
        options += ['--hidden-import', 'can.interfaces.pcan']
        if available['gs_usb'] and available['usb']:
            options += ['--hidden-import', 'can.interfaces.gs_usb',
                        '--hidden-import', 'usb.backend.libusb1']
        if available['uptime']:
            options += ['--hidden-import', 'uptime']
        if available['libusb_package']:
            # libusb_package locates its DLL as a package resource at runtime.
            options += ['--collect-all', 'libusb_package']
    return options


def build():
    version, author = read_meta()
    # VERSION is authoritative on every platform. Refresh this generated file
    # before packaging so a stale Windows resource never looks like a second
    # version source when developing or building on Linux.
    ver_file = HERE / "version_info.txt"
    write_win_version_file(version, author, ver_file)
    ensure_pyinstaller()
    # Fail explicitly instead of silently packaging an application without SSH.
    import paramiko  # noqa: F401
    import pyte  # noqa: F401
    ensure_icon()
    is_win = sys.platform.startswith("win")
    can_options = can_packaging_options(is_win)

    # Output name, bundled version and Windows resources use the same value.
    plat_suffix = "" if is_win else f"-{sys.platform}"
    name = f"{APP_NAME}-{version}{plat_suffix}"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole" if is_win else "--windowed",
        "--name", name,
        "--clean",
        "--noconfirm",
        "--hidden-import", "serial",
        "--hidden-import", "serial.tools.list_ports",
        "--add-data", str(HERE / "app.png") + os.pathsep + ".",
        "--add-data", str(HERE / "VERSION") + os.pathsep + ".",
    ]
    cmd += can_options

    # 图标
    icon = HERE / ("app.ico" if is_win else "app.png")
    if icon.exists():
        cmd += ["--icon", str(icon)]

    # All platforms refresh the resource above; only Windows embeds it.
    if is_win:
        cmd += ["--version-file", str(ver_file)]

    cmd.append(str(ENTRY))

    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(HERE))

    print(f"\n✔ 打包完成  版本: {version}  作者: {author}")
    print(f"   产物目录: {HERE / 'dist'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description='一次设置版本号，自动同步界面、文件名和版本资源')
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--clean", action="store_true", help="仅清理构建产物")
    action.add_argument("--version", metavar="VERSION", help="保存版本号并开始打包，例如 1.0.3")
    action.add_argument("--set-version", metavar="VERSION", help="仅保存版本号，不打包")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过版本提示，使用当前版本打包")
    args = ap.parse_args(argv)

    if args.clean:
        clean()
        return 0

    selected = args.version if args.version is not None else args.set_version
    try:
        if selected is not None:
            version = write_version(selected, HERE / 'VERSION')
            print(f'✔ 版本号已设置为 {version}（保存在 VERSION）')
        else:
            version = read_version(HERE / 'VERSION')
            if not args.yes and sys.stdin.isatty():
                while True:
                    entered = input(f'请输入打包版本号 [当前 {version}，回车沿用]：').strip()
                    if not entered:
                        break
                    try:
                        selected = normalize_version(entered)
                    except ValueError as exc:
                        print(f'✘ {exc}')
                        continue
                    version = write_version(selected, HERE / 'VERSION')
                    break
            print(f'>>> 本次打包版本：{version}')
    except (EOFError, KeyboardInterrupt):
        print('\n已取消，未开始打包。')
        return 1
    except (OSError, ValueError) as exc:
        ap.error(str(exc))

    if args.set_version is not None:
        return 0
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
