"""Does the graphics-card settling do the right thing, and only once?

Drives the real MainWindow._settle_graphics_card against a stub, so no
window is built and a mistake cannot actually restart anything. Settings
are an in-memory dict, so the user's settings.json is never touched.
"""

import os
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
# No display on a build machine, and none needed: nothing here shows a
# window. Qt still has to be told, or importing it fails outright.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as app_main

IRIS = ("ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x0000A7A0) "
        "Direct3D11 vs_5_0 ps_5_0, D3D11)")
RTX = ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU (0x000028A0) "
       "Direct3D11 vs_5_0 ps_5_0, D3D11)")

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


class FakeGpu:
    """Stands in for gpu_preference, so any machine can be simulated."""

    def __init__(self, discrete_present, on_discrete, note_written):
        self._discrete = discrete_present
        self._on_discrete = on_discrete
        self._note = note_written

    def integrated_only(self):
        return not self._discrete

    def rendering_on_discrete(self, name):
        return self._on_discrete

    def is_high_performance(self):
        return self._note


class FakeTimer:
    fired = []

    @staticmethod
    def singleShot(ms, fn):
        FakeTimer.fired.append((ms, fn))


class Stub:
    """Only what _settle_graphics_card touches."""

    _settle_graphics_card = app_main.MainWindow._settle_graphics_card

    def __init__(self, connected=False):
        self.said = []
        self.link = object() if connected else None
        self.restarted = 0

    def on_command_feedback(self, message):
        self.said.append(message)

    def _restart_for_graphics_card(self):
        self.restarted += 1


def run(settings, discrete_present, on_discrete, note_written,
        connected=False):
    """Returns (stub, settings-after)."""
    store = dict(settings)
    app_main.load_settings = lambda: dict(store)
    app_main.save_setting = lambda k, v: store.__setitem__(k, v)
    app_main.gpu_preference = FakeGpu(discrete_present, on_discrete,
                                      note_written)
    app_main.QTimer = FakeTimer
    FakeTimer.fired = []
    stub = Stub(connected=connected)
    stub._settle_graphics_card(RTX if on_discrete else IRIS)
    for _ms, fn in FakeTimer.fired:       # the restart is deferred; run it
        fn()
    return stub, store


R = app_main.GPU_RESTART_SETTING
N = app_main.GPU_RESTART_NOTICE_SETTING
I = app_main.GPU_ON_INTEGRATED_SETTING
C = app_main.GPU_CHOICE_SETTING

print("")
print("1. normal dual-GPU launch, already on the RTX")
s, store = run({}, discrete_present=True, on_discrete=True, note_written=True)
note("does not restart", s.restarted == 0)
note("says nothing extra", s.said == [], repr(s.said))

print("")
print("2. first launch: discrete card present, running on the Iris")
s, store = run({}, discrete_present=True, on_discrete=False, note_written=True)
note("restarts exactly once", s.restarted == 1)
note("arms the one-shot guard", store.get(R) is True)
note("leaves a notice for the next launch", store.get(N) is True)
note("does NOT give up and force CPU rasterising yet",
     store.get(I) is not True)
note("tells the user what it is doing",
     any("restarting" in m.lower() for m in s.said), repr(s.said))

print("")
print("3. the launch after that, now on the RTX (the happy path)")
s, store = run(store, discrete_present=True, on_discrete=True,
               note_written=True)
note("does not restart again", s.restarted == 0)
note("explains the flicker, once", any("restarted once" in m.lower()
                                       for m in s.said), repr(s.said))
note("clears the notice so it is never said twice", store.get(N) is False)
note("disarms the guard, ready if hardware changes later",
     store.get(R) is False)

print("")
print("4. stubborn machine: restarted once, STILL on the Iris")
s, store = run({R: True}, discrete_present=True, on_discrete=False,
               note_written=True)
note("does NOT restart again - no loop", s.restarted == 0)
note("gives up and arranges CPU rasterising", store.get(I) is True)
note("and says so", any("integrated" in m.lower() for m in s.said),
     repr(s.said))

print("")
print("5. the launch after giving up: CPU rasterising is on")
before = dict(store)
s, store = run(store, discrete_present=True, on_discrete=False,
               note_written=True)
note("still does not restart", s.restarted == 0)
note("does not repeat the message", s.said == [], repr(s.said))
note("the flag that turns on CPU rasterising survives",
     store.get(I) is True)

print("")
print("6. guards that must stop a restart")
s, _ = run({}, discrete_present=False, on_discrete=False, note_written=True)
note("integrated-only machine: nothing to move to", s.restarted == 0)
s, _ = run({}, discrete_present=True, on_discrete=False, note_written=False)
note("no note written yet: a restart could not help", s.restarted == 0)
s, _ = run({C: False}, discrete_present=True, on_discrete=False,
           note_written=True)
note("user switched the preference off: respected", s.restarted == 0)
s, _ = run({}, discrete_present=True, on_discrete=False, note_written=True,
           connected=True)
note("connected to a vehicle: never restarts", s.restarted == 0)

print("")
print("7. a stale 'runs on integrated' is cleared once on the discrete card")
s, store = run({I: True, R: True}, discrete_present=True, on_discrete=True,
               note_written=True)
note("cleared, so CPU rasterising stops on a card that does not need it",
     store.get(I) is False)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
