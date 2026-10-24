"""VBoxFront app."""

from __future__ import annotations

import os
import sys

from .cli import main as cli_main, wants_cli


#: Beside this module both from source and in a PyInstaller bundle (the spec
#: ships it as data under the same package folder).
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg")


def main():
    if wants_cli(sys.argv):
        sys.exit(cli_main())
    # Imported only for the window, so a subcommand starts no GUI machinery.
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication
    from .window import MainWindow

    app = QApplication(sys.argv)
    app.setOrganizationName("vboxfront")
    app.setApplicationName("vboxfront")
    # Matches the installed .desktop file, so docks group the window with it.
    app.setDesktopFileName("vboxfront")
    app.setWindowIcon(QIcon(ICON_PATH))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
