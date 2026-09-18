#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成程序 Logo：
    app.png  (256x256)  -- 通用 / Linux
    app.ico             -- Windows（包含多种尺寸）
用法:
    python3 gen_icon.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

from PyQt5.QtCore import Qt, QRectF, QPointF, QByteArray, QBuffer, QIODevice
from PyQt5.QtGui import (QImage, QPainter, QColor, QLinearGradient, QPen,
                         QBrush, QPainterPath)
from PyQt5.QtWidgets import QApplication


def draw_logo(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing |
                     QPainter.SmoothPixmapTransform)

    # Draw in a fixed design grid so every output size keeps the same proportions.
    p.scale(size / 256.0, size / 256.0)
    rect = QRectF(8, 8, 240, 240)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor("#1f6feb"))
    grad.setColorAt(1.0, QColor("#0d3b78"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor(0, 0, 0, 40), 2))
    p.drawRoundedRect(rect, 40, 40)

    # Terminal window: common to serial consoles and SSH, without tiny lettering.
    pen = QPen(QColor("#ffffff"), 10)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(QColor(8, 35, 78, 80))
    p.drawRoundedRect(QRectF(48, 48, 160, 108), 16, 16)
    p.setBrush(Qt.NoBrush)
    prompt = QPainterPath(QPointF(77, 79))
    prompt.lineTo(99, 100)
    prompt.lineTo(77, 121)
    p.drawPath(prompt)
    p.drawLine(QPointF(122, 121), QPointF(155, 121))

    # One terminal, multiple links: serial / TCP / UDP debugging.
    pen.setColor(QColor("#9ad0ff"))
    pen.setWidthF(8)
    p.setPen(pen)
    p.drawLine(QPointF(128, 161), QPointF(128, 204))
    p.drawLine(QPointF(68, 184), QPointF(188, 184))
    p.drawLine(QPointF(68, 184), QPointF(68, 204))
    p.drawLine(QPointF(188, 184), QPointF(188, 204))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#9ad0ff"))
    for x in (68, 128, 188):
        p.drawEllipse(QPointF(x, 207), 11, 11)

    p.end()
    return img


def save_ico(png_path: Path, ico_path: Path) -> bool:
    # PNG-backed ICO entries avoid an optional Pillow dependency, so the
    # Windows icon cannot silently remain on the previous design.
    source = QImage(str(png_path))
    if source.isNull():
        raise ValueError(f"无法读取图标: {png_path}")
    sizes = (16, 32, 48, 64, 128, 256)
    entries, payloads = [], []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        image = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise IOError("无法编码图标")
        buffer.close()
        payload = bytes(data)
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256,
                                   0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    ico_path.write_bytes(struct.pack('<HHH', 0, 1, len(sizes))
                         + b''.join(entries) + b''.join(payloads))
    return True


def main():
    app = QApplication(sys.argv)  # noqa: F841  Qt 绘图必需
    png = HERE / "app.png"
    ico = HERE / "app.ico"
    img = draw_logo(256)
    img.save(str(png), "PNG")
    print(f"✔ 生成 {png}")
    if save_ico(png, ico):
        print(f"✔ 生成 {ico}")


if __name__ == "__main__":
    main()
