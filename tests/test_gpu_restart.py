"""Does the graphics-card settling do the right thing, and only once?

Drives the real MainWindow._settle_graphics_card against a stub, so no
window is built and a mistake cannot actually restart anything. Settings
are an in-memory dict, so the user's settings.json is never touched.

Section 9 checks the step before all of that - that the app gets the
card's name from the map at all. V2.3.2 lost it, and sections 1-8 all
passed, because every one of them hands the settling the name directly.
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
        connected=False, platform="win32"):
    """Returns (stub, settings-after).

    The platform is faked along with everything else. Settling is
    Windows-only by design - main.py returns immediately anywhere else -
    but the decision it makes there is ordinary logic, worth checking
    whatever machine runs the suite. CI runs this on macOS too, where
    without the fake every scenario below would pass by doing nothing.
    Scenario 8 is the one that checks the guard itself.
    """
    store = dict(settings)
    app_main.load_settings = lambda: dict(store)
    app_main.save_setting = lambda k, v: store.__setitem__(k, v)
    app_main.gpu_preference = FakeGpu(discrete_present, on_discrete,
                                      note_written)
    app_main.QTimer = FakeTimer
    FakeTimer.fired = []
    stub = Stub(connected=connected)
    was = sys.platform
    try:
        sys.platform = platform
        stub._settle_graphics_card(RTX if on_discrete else IRIS)
    finally:
        sys.platform = was
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
print("8. not Windows: the mechanism stays out of the way entirely")
# A Mac has no registry preference to write and picks its own GPU, so
# main.py returns before any of the above can happen. Every scenario
# before this one reaches the logic only because run() fakes the
# platform; this is the one that checks what the real guard does.
for plat in ("darwin", "linux"):
    s, store = run({}, discrete_present=True, on_discrete=False,
                   note_written=True, platform=plat)
    note("%s: never restarts" % plat, s.restarted == 0)
    note("%s: writes no setting" % plat, store == {}, repr(store))
    note("%s: says nothing" % plat, s.said == [], repr(s.said))

print("")
print("9. the card's name actually reaches the settling")
# Every scenario above starts from the name. The app gets it by asking
# the map, every 3 seconds until the page answers, and V2.3.2 broke that
# step: the map's method raised on every call, _report_graphics_adapter
# swallowed it, and settling never ran - so the first launch never
# restarted onto the discrete card. This runs the real chain, from the
# timer's call through the map's real method to the settling. Only the
# page itself is faked, and settling is recorded rather than run.
import contextlib
import io

from map_view import MapView


class FakePage:
    def __init__(self, answer):
        self.answer = answer

    def runJavaScript(self, script, callback=None):
        if isinstance(self.answer, Exception):
            raise self.answer
        if callback is not None:
            callback(self.answer)


class FakeRepeatTimer:
    def __init__(self):
        self.active = True

    def stop(self):
        self.active = False


class Chain:
    """Only what asking for the card touches."""

    _report_graphics_adapter = app_main.MainWindow._report_graphics_adapter
    _on_graphics_adapter = app_main.MainWindow._on_graphics_adapter

    def __init__(self, answer):
        self._adapter_tries = 0
        self._graphics_adapter = ""
        self._adapter_timer = FakeRepeatTimer()
        view = MapView.__new__(MapView)     # no browser engine, no page load
        view._page_ready = False
        view._pending_js = []
        page = FakePage(answer)
        view.page = lambda: page
        self.map_view = view
        self.said = []
        self.settled = []

    def on_command_feedback(self, message):
        self.said.append(message)

    def _settle_graphics_card(self, name):
        self.settled.append(name)


def tick(chain):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        chain._report_graphics_adapter()
    return out.getvalue()


for label, card in (("the RTX", RTX), ("the Iris", IRIS)):
    c = Chain(card)
    tick(c)
    note("on %s: the name reaches the settling" % label,
         c.settled == [card], repr(c.settled)[:60])
    note("on %s: the card is named in the messages" % label,
         any(m.startswith("Graphics: rendering on ") for m in c.said),
         repr(c.said)[:60])
    note("on %s: and asking stops once answered" % label,
         c._adapter_timer.active is False)

c = Chain("")                   # the page is not up yet
tick(c)
note("no answer yet: keeps asking rather than giving up",
     c._adapter_timer.active is True and c.settled == [])

c = Chain(RuntimeError("the map could not be asked"))
logged = tick(c)
note("a map that cannot be asked stops the asking - never a flight",
     c._adapter_timer.active is False and c.settled == [])
note("and the failure is said in the log, not swallowed",
     "GPUADAPTER failed" in logged, logged.strip()[:60])

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
