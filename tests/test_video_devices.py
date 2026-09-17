"""Does the video window find cameras, and name the right platform's?

Drives the real list_devices, _camera_backend and _why_not_open with the
platform faked, so both the DirectShow and the AVFoundation decision are
checked on whatever machine runs this. No camera is opened and no
subprocess is spawned: system_profiler is stubbed, which is also the only
way to test a two-camera Mac on a one-camera Mac.

The Windows strings are asserted character for character. They are what a
user reads when a capture device refuses, and the macOS port had no
business changing them.
"""

import json
import os
import subprocess
import sys
import types

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
# No display on a build machine, and none needed: nothing here shows a
# window. Qt still has to be told, or importing it fails outright.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2

import video_view as vv

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


class FakeCv2:
    """Only the four names the code under test actually reads."""

    CAP_DSHOW = cv2.CAP_DSHOW
    CAP_AVFOUNDATION = cv2.CAP_AVFOUNDATION
    CAP_FFMPEG = cv2.CAP_FFMPEG

    def __init__(self, ffmpeg=True):
        self._ffmpeg = ffmpeg
        outer = self

        class Registry:
            @staticmethod
            def hasBackend(api):
                if outer._ffmpeg is None:
                    raise cv2.error("backend query not implemented")
                return bool(outer._ffmpeg)

        self.videoio_registry = Registry


class FakePlatform:
    """Runs a block as though it were on another operating system."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._was = sys.platform
        sys.platform = self.name

    def __exit__(self, *exc):
        sys.platform = self._was


def profiler(payload, fail=None):
    """Stand in for the system_profiler subprocess.

    Returns a stand-in module to rebind video_view's own `subprocess`
    name. Assigning to vv.subprocess.run would instead reach through to
    the real subprocess module and replace run() for everything in the
    process, which is a large thing to do to test a small one.
    """
    class Completed:
        stdout = payload if isinstance(payload, bytes) else json.dumps(
            payload).encode("utf-8")

    def run(argv, **kw):
        assert argv[0] == "system_profiler", argv
        if fail is not None:
            raise fail
        return Completed()

    return types.SimpleNamespace(run=run, PIPE=subprocess.PIPE,
                                 DEVNULL=subprocess.DEVNULL)


print("")
print("1. the capture backend is the platform's own")
# OpenCV defines every backend constant on every platform, so a wrong
# one here is not a NameError - it is a device that never opens. That is
# why this is asserted rather than assumed.
#
# The two below fake the platform, which is what lets a Mac check the
# Windows decision and the other way round. This first one is the only
# check that runs unfaked, against whatever machine this really is.
EXPECTED = {"darwin": cv2.CAP_AVFOUNDATION}.get(sys.platform, cv2.CAP_DSHOW)
note("this machine (%s) asks for the right backend" % sys.platform,
     vv._camera_backend(cv2) == EXPECTED)
with FakePlatform("darwin"):
    note("darwin -> AVFOUNDATION",
         vv._camera_backend(FakeCv2()) == cv2.CAP_AVFOUNDATION)
with FakePlatform("win32"):
    note("win32 -> DSHOW, not Media Foundation",
         vv._camera_backend(FakeCv2()) == cv2.CAP_DSHOW)
note("the two are not the same constant",
     cv2.CAP_DSHOW != cv2.CAP_AVFOUNDATION)

print("")
print("2. macOS device names come out of system_profiler, in order")
TWO = {"SPCameraDataType": [{"_name": "FaceTime HD Camera (Built-in)",
                             "spcamera_unique-id": "0x8020000005ac8514"},
                            {"_name": "USB Capture HDMI"}]}
vv.subprocess = profiler(TWO)
note("both cameras, order preserved",
     vv._macos_devices() == ["FaceTime HD Camera (Built-in)",
                             "USB Capture HDMI"],
     repr(vv._macos_devices()))

print("")
print("3. a Mac with nothing plugged in, and a system_profiler that surprises us")
for label, payload in (
        ("no cameras", {"SPCameraDataType": []}),
        ("key absent", {}),
        ("key is null", {"SPCameraDataType": None}),
):
    vv.subprocess = profiler(payload)
    note("%s -> empty list" % label, vv._macos_devices() == [])

# One odd entry must not cost the user every other camera on the machine.
vv.subprocess = profiler({"SPCameraDataType": [
    {"_name": "Good One"}, {"no_name_key": 1}, "not even a dict",
    {"_name": ""}, {"_name": "Second Good One"}]})
note("malformed entries skipped, the rest kept",
     vv._macos_devices() == ["Good One", "Second Good One"],
     repr(vv._macos_devices()))

print("")
print("4. list_devices picks the right enumerator and never raises")
with FakePlatform("darwin"):
    vv.subprocess = profiler(TWO)
    note("darwin uses system_profiler", vv.list_devices() ==
         ["FaceTime HD Camera (Built-in)", "USB Capture HDMI"])

    vv.subprocess = profiler(TWO, fail=OSError("system_profiler missing"))
    note("a broken system_profiler degrades to no devices",
         vv.list_devices() == [])

    vv.subprocess = profiler(b"{not json at all")
    note("unparsable output degrades to no devices", vv.list_devices() == [])

called = []
with FakePlatform("win32"):
    vv._windows_devices = lambda: called.append(1) or ["OBS Virtual Camera"]
    note("win32 uses pygrabber, not system_profiler",
         vv.list_devices() == ["OBS Virtual Camera"] and called == [1])
    vv._windows_devices = lambda: (_ for _ in ()).throw(
        ImportError("No module named 'pygrabber'"))
    note("a missing pygrabber degrades to no devices",
         vv.list_devices() == [])

with FakePlatform("linux"):
    note("a platform with no enumerator at all degrades to no devices",
         vv.list_devices() == [])

print("")
print("5. what the user is told when a source will not open")
WAS_DEVICE = ("That device would not open. It is usually already in use "
              "by another program.")
WAS_ADDRESS = "That address did not answer."
with FakePlatform("win32"):
    device = vv._Reader(0)._why_not_open(FakeCv2())
    address = vv._Reader("rtsp://camera/stream")._why_not_open(FakeCv2())
note("Windows device text unchanged, to the character",
     device == WAS_DEVICE, repr(device))
note("Windows address text unchanged, to the character",
     address == WAS_ADDRESS, repr(address))

with FakePlatform("darwin"):
    mac_device = vv._Reader(0)._why_not_open(FakeCv2())
    mac_address = vv._Reader("rtsp://camera/stream")._why_not_open(FakeCv2())
    no_ffmpeg = vv._Reader("rtsp://camera/stream")._why_not_open(
        FakeCv2(ffmpeg=False))
# The first open of a device is the one that triggers the permission
# prompt and fails, so on a Mac "already in use" is the wrong answer at
# exactly the wrong moment.
note("macOS device text mentions permission", "permission" in mac_device,
     repr(mac_device))
note("macOS keeps the plain address text when FFmpeg is there",
     mac_address == WAS_ADDRESS)
note("an OpenCV with no FFmpeg says so, instead of blaming the address",
     "FFmpeg" in no_ffmpeg and no_ffmpeg != WAS_ADDRESS, repr(no_ffmpeg))

print("")
print("6. an OpenCV too old to be asked about its backends")
note("an unanswerable probe assumes FFmpeg, rather than inventing a cause",
     vv._has_ffmpeg(FakeCv2(ffmpeg=None)) is True)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
