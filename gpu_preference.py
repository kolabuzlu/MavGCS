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

Three things worth knowing:

  * It applies from the NEXT start. The adapter for a running process is
    already chosen, so setting this cannot move the current one.
  * It is keyed on the executable. Frozen, that is MavGCS.exe and the
    setting affects nothing else. From source it is python.exe, which is
    shared with every other Python program on the machine, so the app
    says so plainly in the message log when it writes it.
  * The caller writes it once and then leaves it alone - see
    MainWindow._prefer_high_performance_gpu. A user who moves MavGCS back
    to the integrated card, for battery or a bad discrete driver, keeps
    that choice instead of having it overwritten on the next launch.

HKEY_CURRENT_USER only: no administrator rights, and it is the same value
the Settings page edits, so a user can see and undo it there.
"""

import sys

# 2 is "High performance" in the Windows Graphics settings; 1 is "Power
# saving" and 0 is "Let Windows decide".
_HIGH_PERFORMANCE = "GpuPreference=2;"
_KEY = r"SOFTWARE\Microsoft\DirectX\UserGpuPreferences"


def target_executable() -> str:
    """The executable Windows keys the preference on."""
    return sys.executable or ""


def current_preference(exe: str = None):
    """What Windows currently has for this executable, or None."""
    if sys.platform != "win32":
        return None
    exe = exe or target_executable()
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
    exe = exe or target_executable()
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
    exe = exe or target_executable()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, exe)
        return True
    except OSError:
        return False


# ---- machines with nothing to switch to -----------------------------------
#
# The preference above only helps a machine that has a discrete card. One
# with only integrated graphics still hits the compositor fault: ANGLE's
# Direct3D 11 input layout cache overflows after twenty minutes to an hour
# of flying, and the access violation follows within a second. Reproduced
# on demand on 2026-09-13 - the real app on the Intel Iris, fed a full
# ArduPlane stream in a loiter, dies at 20 minutes - and the one thing that
# stopped it without costing anything was moving rasterisation off the GPU:
# --disable-gpu-rasterization survived two 70-minute runs with zero
# overflows, at an identical 60 fps, with compositing (and so pixel
# snapping, and so the tile seams) left exactly where they were. That is a
# different flag from --disable-gpu, which moves compositing too and was
# rightly rejected for the jitter it caused.
#
# The flag has to be in the environment before Qt WebEngine starts, which
# is why the decision is made here, from DXGI, before any Qt import. It is
# deliberately conservative: an adapter counts as discrete only if it is
# NVIDIA or AMD with real dedicated memory, and any failure to enumerate
# answers "assume discrete", so a broken check can never move a healthy
# machine onto CPU rasterisation.

_VENDOR_NVIDIA, _VENDOR_AMD, _VENDOR_INTEL, _VENDOR_MICROSOFT = (
    0x10DE, 0x1002, 0x8086, 0x1414)
_MIN_DISCRETE_VRAM = 512 * 1024 * 1024
_DXGI_ADAPTER_FLAG_SOFTWARE = 2


def adapters():
    """Every DXGI adapter as (description, vendor_id, dedicated_vram_bytes,
    is_software), or [] if enumeration fails for any reason."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import (POINTER, byref, c_void_p, c_uint, c_size_t,
                            c_long, c_ulong, Structure, WINFUNCTYPE, HRESULT)

        class GUID(Structure):
            _fields_ = [("d1", c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        class LUID(Structure):
            _fields_ = [("LowPart", c_ulong), ("HighPart", c_long)]

        class DESC1(Structure):
            _fields_ = [("Description", ctypes.c_wchar * 128),
                        ("VendorId", c_uint), ("DeviceId", c_uint),
                        ("SubSysId", c_uint), ("Revision", c_uint),
                        ("DedicatedVideoMemory", c_size_t),
                        ("DedicatedSystemMemory", c_size_t),
                        ("SharedSystemMemory", c_size_t),
                        ("AdapterLuid", LUID), ("Flags", c_uint)]

        # IID_IDXGIFactory1
        iid = GUID(0x770aae78, 0xf26f, 0x4dba,
                   (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c,
                                        0x83, 0xd1, 0xb3, 0x87))
        factory = c_void_p()
        if ctypes.windll.dxgi.CreateDXGIFactory1(byref(iid),
                                                 byref(factory)) != 0:
            return []

        def method(obj, index, proto):
            vtable = ctypes.cast(obj.value, POINTER(c_void_p))[0]
            return proto(ctypes.cast(vtable, POINTER(c_void_p))[index])

        enum_adapters1 = WINFUNCTYPE(HRESULT, c_void_p, c_uint,
                                     POINTER(c_void_p))
        get_desc1 = WINFUNCTYPE(HRESULT, c_void_p, POINTER(DESC1))
        found = []
        for index in range(16):
            adapter = c_void_p()
            try:
                method(factory, 12, enum_adapters1)(factory, index,
                                                    byref(adapter))
            except OSError:
                break                       # DXGI_ERROR_NOT_FOUND: the end
            desc = DESC1()
            method(adapter, 10, get_desc1)(adapter, byref(desc))
            found.append((desc.Description, desc.VendorId,
                          int(desc.DedicatedVideoMemory),
                          bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE)))
        return found
    except Exception:
        return []


def discrete_adapters():
    """Just the adapters that count as discrete.

    An integrated chip is not discrete however fast it is, and an AMD APU
    reports its vendor as AMD while having no dedicated memory at all -
    which is why the memory size, not the vendor alone, decides.
    """
    return [(description, vendor, vram, software)
            for description, vendor, vram, software in adapters()
            if not software
            and vendor in (_VENDOR_NVIDIA, _VENDOR_AMD)
            and vram >= _MIN_DISCRETE_VRAM]


def integrated_only() -> bool:
    """True only when enumeration succeeded and no adapter is discrete."""
    if not adapters():
        return False                        # unknown: assume discrete
    return not discrete_adapters()


def rendering_on_discrete(adapter_name) -> bool:
    """Is that adapter - as the map reports it - one of the discrete cards?

    The map answers with a WebGL string like

        ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x0000A7A0) ...)

    which carries the adapter's description verbatim, so the reported name
    can be matched against what DXGI enumerated rather than guessed at
    from the vendor. That matters on a machine whose integrated and
    discrete chips share a vendor, where the vendor alone says nothing.

    An empty or unrecognised name answers True: not knowing is never a
    reason to act.
    """
    name = adapter_name or ""
    if not name:
        return True
    for description, _vendor, _vram, _software in discrete_adapters():
        if description and description in name:
            return True
    return False
