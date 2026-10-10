"""Shared application metadata; VERSION is the only editable version source."""

import os
from pathlib import Path
import re
import stat
import sys
import tempfile

APP_NAME = 'DebugTool'
AUTHOR = 'RUIO'
VERSION_PATH = Path(__file__).resolve().with_name('VERSION')


def normalize_version(value):
    text = value.strip()
    if not re.fullmatch(r'[vV]?[0-9]{1,5}(?:\.[0-9]{1,5}){2,3}', text):
        raise ValueError('版本号请输入三段或四段数字，例如 1.0.3 或 V1.0.3.1')
    parts = [int(part) for part in text.lstrip('vV').split('.')]
    if any(part > 65535 for part in parts):
        raise ValueError('版本号每一段必须在 0–65535 之间，以兼容 Windows 版本资源')
    return 'V' + '.'.join(str(part) for part in parts)


def read_version(path=None):
    if path is None:
        path = (Path(sys._MEIPASS) / 'VERSION'
                if getattr(sys, 'frozen', False) else VERSION_PATH)
    return normalize_version(Path(path).read_text(encoding='utf-8'))


def write_version(value, path=None):
    """Validate first, then atomically persist the version for subsequent builds."""
    version = normalize_version(value)
    target = Path(path) if path is not None else VERSION_PATH
    content = version + '\n'
    if target.exists() and target.read_text(encoding='utf-8') == content:
        return version
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=str(target.parent), prefix='.VERSION-',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(target))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return version


def version_tuple(value):
    parts = tuple(int(part) for part in normalize_version(value)[1:].split('.'))
    return parts + (0,) * (4 - len(parts))
