# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for the distributable MavGCS.

Build with:  pyinstaller MavGCS.spec        (see build_release.py)

Deliberately a ONE-FOLDER build, not --onefile. QtWebEngine ships a
separate helper executable (QtWebEngineProcess.exe) plus its own
resources/locales; --onefile has to unpack all of that to a temp
directory on every launch, which is both slow and a common source of
"the map pane is blank" failures. One folder starts fast and is what
gets zipped for release anyway.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# imagecodecs loads its per-codec extension modules dynamically, so static
# analysis misses them. Without these the terrain radar fails at runtime
# with "requires the 'imagecodecs' package" - the Copernicus DEM tiles are
# Deflate-compressed with a floating-point predictor.
hiddenimports = collect_submodules("imagecodecs")

# pymavlink picks its dialect with a runtime __import__ (mavutil.set_dialect),
# so nothing here is reachable by static analysis either. Miss them and
# pymavlink falls back to GENERATING the dialect from its XML definitions at
# startup, which failed in the packaged app with:
#   [Errno 2] No such file or directory:
#   '_internal\\message_definitions\\v1.0\\ardupilotmega.xml'
# and no connection - serial, TCP or UDP - could be opened. Both the
# pre-generated dialect modules and the XML they'd be generated from are
# bundled, so the fast path works and the fallback is intact.
hiddenimports += collect_submodules("pymavlink.dialects")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("mavgcs_icon.png", "."),
        ("mavgcs_logo_watermark.png", "."),
    ] + collect_data_files("pymavlink"),   # message_definitions/*.xml
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trimming what this app never imports. matplotlib/tkinter in
    # particular get dragged in by scientific packages and add a lot of
    # weight to the download for no benefit.
    excludes=[
        "matplotlib", "tkinter", "scipy", "pandas", "PIL",
        "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
        "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia", "PySide6.QtSensors", "PySide6.QtTest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MavGCS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window behind it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="mavgcs_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MavGCS",
)
