#!/usr/bin/env python3
"""Render vboxfront_app/icon.svg to the PNG PyInstaller turns into .ico/.icns.

PyInstaller converts a PNG icon itself when Pillow is installed, so one SVG
source is enough for every platform; this only has to rasterise it. Uses Qt's
own SVG renderer, which the app already depends on.

Usage: scripts/render_icon.py [output.png] [size]
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "icon.png")
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 512
    _app = QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(os.path.join(ROOT, "vboxfront_app", "icon.svg"))
    if not renderer.isValid():
        print("icon.svg could not be parsed", file=sys.stderr)
        return 1
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if not image.save(out):
        print(f"could not write {out}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
