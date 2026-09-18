#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨平台单文件打包脚本（Linux / Windows 通用）
用法:
    python3 build.py            # 自动打包当前平台
    python3 build.py --clean    # 清理 build/dist/*.spec

依赖:
    pip install pyinstaller -r requirements.txt
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "main.py"
APP_NAME = "DebugTool"


def read_meta():
    """从 main.py 读取 __version__ / __author__."""
    text = ENTRY.read_text(encoding="utf-8")
    m_ver = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    m_auth = re.search(r'__author__\s*=\s*["\']([^"\']+)["\']', text)
    return (m_ver.group(1) if m_ver else "V1.0.2",
            m_auth.group(1) if m_auth else "RUIO")


def parse_version_tuple(v: str):
    nums = re.findall(r"\d+", v)
    nums = [int(x) for x in nums][:4]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)


def write_win_version_file(version: str, author: str, dst: Path):
    """生成 PyInstaller 使用的 Windows version 资源文件。"""
    vt = parse_version_tuple(version)
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
            StringStruct(u'OriginalFilename', u'{APP_NAME}.exe'),
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


def build():
    ensure_pyinstaller()
    # Fail explicitly instead of silently packaging an application without SSH.
    import paramiko  # noqa: F401
    import pyte  # noqa: F401
    ensure_icon()
    version, author = read_meta()
    is_win = sys.platform.startswith("win")

    # 产物名包含版本号，如 DebugTool-V1.0.2.exe / DebugTool-V1.0.2-linux
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
    ]

    # 图标
    icon = HERE / ("app.ico" if is_win else "app.png")
    if icon.exists():
        cmd += ["--icon", str(icon)]

    # Windows 版本资源
    if is_win:
        ver_file = HERE / "version_info.txt"
        write_win_version_file(version, author, ver_file)
        cmd += ["--version-file", str(ver_file)]

    cmd.append(str(ENTRY))

    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(HERE))

    print(f"\n✔ 打包完成  版本: {version}  作者: {author}")
    print(f"   产物目录: {HERE / 'dist'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="仅清理构建产物")
    args = ap.parse_args()

    if args.clean:
        clean()
        return
    build()


if __name__ == "__main__":
    main()
