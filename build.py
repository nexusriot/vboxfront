#!/usr/bin/env python3
"""Build vboxfront binary with PyInstaller."""

import os
import platform
import subprocess
import sys


def main():
    system = platform.system()
    name = "vboxfront"
    icon = None

    if system == "Windows":
        sep = ";"
        icon_path = "icon.ico"
        if os.path.exists(icon_path):
            icon = icon_path
    elif system == "Darwin":
        sep = ":"
        icon_path = "icon.icns"
        if os.path.exists(icon_path):
            icon = icon_path
    else:
        sep = ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
    ]

    if icon:
        cmd += ["--icon", icon]

    # Collect PyQt6 plugins needed for the platform
    cmd += [
        "--collect-all", "PyQt6",
    ]

    cmd.append("vboxfront.py")

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
