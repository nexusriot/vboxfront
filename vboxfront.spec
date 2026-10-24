# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for vboxfront.
# Build with: pyinstaller vboxfront.spec

import pathlib
import platform
import re
from PyInstaller.utils.hooks import collect_all

# APP_VERSION is the single source of truth; read it rather than restating it
# without importing the application and its Qt UI at build configuration time.
APP_VERSION = re.search(
    r'^APP_VERSION = "([^"]+)"', pathlib.Path("vboxfront_app/version.py").read_text(), re.M
).group(1)

datas, binaries, hiddenimports = collect_all("PyQt6")
datas += [("vboxfront_app/icon.svg", "vboxfront_app")]

# scripts/render_icon.py rasterises icon.svg; PyInstaller turns the PNG into
# .ico/.icns itself when Pillow is installed. Without it, builds go iconless.
ICON_PNG = pathlib.Path("build/icon.png")
ICON = str(ICON_PNG) if ICON_PNG.exists() and platform.system() in ("Windows", "Darwin") else None

a = Analysis(
    ["vboxfront.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt6.QtWebEngine",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.QtQuick",
        "PyQt6.QtQml",
        "PyQt6.QtMultimedia",
        "PyQt6.QtBluetooth",
        "PyQt6.QtNfc",
        "PyQt6.QtLocation",
        "PyQt6.QtSensors",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="vboxfront",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,         # no terminal window; set True to see stderr
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

# macOS .app bundle
if platform.system() == "Darwin":
    app = BUNDLE(
        exe,
        name="vboxfront.app",
        icon=ICON,
        bundle_identifier="com.example.vboxfront",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": APP_VERSION,
        },
    )
