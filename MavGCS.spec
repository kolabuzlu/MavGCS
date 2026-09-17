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

On macOS the same one-folder build is then wrapped in a .app by BUNDLE
below. That is not cosmetic: a bare Unix executable cannot own an icon,
cannot be double-clicked from Finder without opening a terminal, and -
the part that actually decides it - has no Info.plist, so it has nowhere
to declare why it wants the camera. See NSCameraUsageDescription.
"""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

MACOS = sys.platform == "darwin"

# Read the version from the one place that defines it, rather than
# keeping a second copy here to drift out of date.
_main = Path(SPECPATH, "main.py").read_text(encoding="utf-8")
_match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', _main, re.M)
# CFBundleShortVersionString wants digits and dots; APP_VERSION is
# written "V2.1.7" for people, and the leading V makes Finder show
# nothing at all rather than a wrong number.
APP_VERSION = (_match.group(1) if _match else "0").lstrip("Vv")

# Windows wants ICO, macOS wants ICNS, and neither will read the other's.
# Both are generated from the same 256px mavgcs_icon.png.
ICON = "mavgcs_icon.icns" if MACOS else "mavgcs_icon.ico"

# GPL v3 asks that the licence travel with the binary. On macOS it has to
# be placed by the build rather than copied in afterwards: BUNDLE ad-hoc
# signs the .app as its last act, and adding any file to a signed bundle -
# Contents/Resources included - breaks the seal. An invalid signature is
# merely untidy on Intel and fatal on Apple Silicon, where the kernel will
# not execute an arm64 binary whose signature does not check out.
#
# Windows is not listed here and keeps the copy build_release.py puts at
# the top of the folder. A data file would land in _internal/ there, which
# is neither where the licence was nor anywhere a person would look for
# it.
LICENCE = [("LICENSE", ".")] if MACOS else []

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

# The video window. Named here for two reasons: video_view imports these
# inside a function, so that a machine with a broken capture stack still
# gets a ground station; and PySide6's hook only collects the platform's
# multimedia backend plugins when it can see the module. Both modules sat
# in excludes below until the map grew a Video button - a build without
# them starts fine and then fails only when that button is pressed.
#
# pygrabber is Windows-only and is not installed on a Mac at all (see the
# marker in requirements.txt), so naming it here would turn a macOS build
# into a wall of "hidden import not found" warnings for a module that has
# no business being there. macOS enumerates cameras through
# system_profiler, which needs nothing bundled.
hiddenimports += ["cv2"]
if not MACOS:
    hiddenimports += ["pygrabber", "pygrabber.dshow_graph"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("mavgcs_icon.png", "."),
        ("mavgcs_logo_watermark.png", "."),
        # Leaflet, served locally by the tile proxy. Without it the map page
        # needs a CDN, so with no internet nothing renders at all.
        ("vendor/leaflet", "vendor/leaflet"),
        # CesiumJS, likewise served locally, for the 3D FPV view. It
        # streams its terrain and imagery from Cesium Ion, but the
        # library, workers and shaders themselves ship with the app.
        ("vendor/cesium", "vendor/cesium"),
    ] + LICENCE + collect_data_files("pymavlink"),  # message_definitions
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
    icon=ICON,
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

if MACOS:
    # Everything above produced dist/MavGCS/; this wraps it as
    # dist/MavGCS.app, which is the only form macOS treats as an
    # application.
    app = BUNDLE(
        coll,
        name="MavGCS.app",
        icon=ICON,
        # Reverse-DNS on a domain the author owns. It is not decoration:
        # macOS files the camera permission against this identifier, so
        # changing it later makes every user answer the prompt again.
        bundle_identifier="com.derinhakankarakurt.mavgcs",
        version=APP_VERSION,
        info_plist={
            # The one entry without which the port does not work at all.
            # Touching a capture device with no usage description is not
            # an error macOS reports - it terminates the process, so the
            # window simply vanishes the moment Start is pressed, with
            # nothing in any log to say why. The text is what the
            # permission dialog shows, so it is written for the person
            # reading it rather than for the developer.
            "NSCameraUsageDescription":
                "MavGCS shows the video feed from a camera or capture "
                "card connected to this Mac, so you can watch it beside "
                "the map.",
            # Telemetry is UDP or TCP to a radio, an autopilot or an
            # RTSP camera, and from macOS 15 reaching any of those on the
            # local network needs consent. Without this string the
            # system still asks, but in its own words, which give the
            # user no reason to weigh.
            "NSLocalNetworkUsageDescription":
                "MavGCS connects to autopilots, telemetry radios and "
                "network cameras on your local network.",
            # Qt draws at the display's real resolution; without this
            # the whole interface is scaled up from 1x and looks soft.
            "NSHighResolutionCapable": True,
            # The floor the PySide6 wheels are built against, stated so
            # an older Mac refuses the app with a clear message instead
            # of launching it into a dyld error.
            "LSMinimumSystemVersion": "12.0",
        },
    )
