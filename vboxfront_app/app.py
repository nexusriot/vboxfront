"""VBoxFront app."""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication
import sys

from .version import APP_VERSION
from .window import MainWindow


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"vboxfront {APP_VERSION}")
        return
    app = QApplication(sys.argv)
    app.setOrganizationName("vboxfront")
    app.setApplicationName("vboxfront")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
