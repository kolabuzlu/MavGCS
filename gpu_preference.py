"""
Ask Windows to run MavGCS on the discrete graphics card.

A laptop with two GPUs runs most programs on the integrated one to save
battery, and Windows decides that per executable. On this project's own
hardware that mattered: every crash captured by the watcher happened
while the map was rendering on the Intel Iris, and the WebEngine
compositor faults we chased for days never once appeared in a run on the
GeForce.

Chromium's own switches do not help - --force-high-performance-gpu,
--gpu-vendor-id and --gpu-device-id were all tried and the adapter stayed
Intel. The choice is made by Windows for the process before Chromium gets
a say, and the only thing that changes it is the per-application
preference Windows keeps here, which is exactly what the Graphics page in
Settings writes.

Two things worth knowing:

  * It applies from the NEXT start. The adapter for a running process is
    already chosen, so setting this cannot move the current one.
  * It is keyed on the executable. Frozen, that is MavGCS.exe and the
    setting affects nothing else. From source it is python.exe, and that
    is shared with every other Python program on the machine - so from
    source this asks rather than assumes.

HKEY_CURRENT_USER only: no administrator rights, and it is the same value
the Settings page edits, so a user can see and undo it there.
"""

import sys

# 2 is "High performance" in the Windows Graphics settings; 1 is "Power
# saving" and 0 is "Let Windows decide".
_HIGH_PERFORMANCE = "GpuPreference=2;"
_KEY = r"SOFTWARE\Microsoft\DirectX\UserGpuPreferences"


def _target_executable() -> str:
    return sys.executable or ""


def current_preference(exe: str = None):
    """What Windows currently has for this executable, or None."""
    if sys.platform != "win32":
        return None
    exe = exe or _target_executable()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
            value, _ = winreg.QueryValueEx(key, exe)
            return value
    except (OSError, ImportError):
        return None


def is_high_performance(exe: str = None) -> bool:
    return current_preference(exe) == _HIGH_PERFORMANCE


def apply(exe: str = None) -> bool:
    """Set the preference. True if it was written, False if it could not be.

    Already correct counts as success and writes nothing: this runs at
    every start and there is no reason to touch the registry each time.
    """
    if sys.platform != "win32":
        return False
    exe = exe or _target_executable()
    if not exe:
        return False
    if is_high_performance(exe):
        return True
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
            winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, _HIGH_PERFORMANCE)
        return True
    except OSError:
        # A locked-down machine, or a policy that forbids it. Nothing here
        # is important enough to stop the program starting.
        return False


def clear(exe: str = None) -> bool:
    """Put it back to whatever Windows would choose on its own."""
    if sys.platform != "win32":
        return False
    exe = exe or _target_executable()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, exe)
        return True
    except OSError:
        return False


def describe() -> str:
    """One line about where this program will render, for the message log."""
    if sys.platform != "win32":
        return ""
    exe = _target_executable()
    if is_high_performance(exe):
        return "Graphics: set to prefer the high-performance GPU."
    return ("Graphics: Windows is choosing the GPU for this program. "
            "See Telemetry Rates for the setting.")
