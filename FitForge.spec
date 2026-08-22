# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for FIT Forge.

Build with:   pyinstaller FitForge.spec --noconfirm
Produces:     dist/FitForge.exe   (single file, no console)
"""

import os

# Resolve the icon relative to THIS spec file, not the current working directory.
_SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
_ICON = os.path.join(_SPEC_DIR, "icon.ico")
if not os.path.exists(_ICON):
    print(f"[spec] WARNING: no icon.ico found at {_ICON} - using default icon")
    _ICON = None
else:
    print(f"[spec] using icon: {_ICON}")

from PyInstaller.utils.hooks import collect_all, collect_data_files

# garmin_fit_sdk ships profile/CSV data that must travel with the binary.
sdk_datas, sdk_binaries, sdk_hidden = collect_all("garmin_fit_sdk")

datas = [
    ("fit_forge.html", "."),   # the UI, served from memory at runtime
]
datas += sdk_datas
datas += collect_data_files("webview")

hiddenimports = [
    "fit_server",
    "fit_writer",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
]
hiddenimports += sdk_hidden

a = Analysis(
    ["fit_app.py"],
    pathex=[],
    binaries=sdk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.testing", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FitForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)
