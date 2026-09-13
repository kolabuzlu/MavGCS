"""Does a mode press survive a link that drops it, and stop when it should?

Drives the real set_mode / _drive_mode_request against a stub link whose
wire loses whatever fraction is asked for, and the real ModePanel styling
logic against a stub panel. No Qt window, no aircraft.
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

import threading


from mavlink_link import MavlinkLink, PLANE_MODES

SET_MODE = MavlinkLink.set_mode
SEND = MavlinkLink._send_mode
DRIVE = MavlinkLink._drive_mode_request
CLEAR = MavlinkLink._clear_mode_request

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


class Emitter:
    def __init__(self):
        self.said = []

    def emit(self, *a):
        self.said.append(a[0] if a else "")


class Link:
    """Only what the mode path touches."""

    MODE_RETRY_EVERY_S = MavlinkLink.MODE_RETRY_EVERY_S
    MODE_RETRY_FOR_S = MavlinkLink.MODE_RETRY_FOR_S
    set_mode = SET_MODE
    _send_mode = SEND
    _drive_mode_request = DRIVE
    _clear_mode_request = CLEAR

    def __init__(self, delivery=1.0):
        self._send_lock = threading.Lock()
        self._mode_wanted = None
        self._mode_retry_next = 0.0
        self._mode_retry_until = 0.0
        self.command_feedback = Emitter()
        self.mode_pending = Emitter()
        self.master = self
        self.delivery = delivery
        self.on_wire = []          # what the aircraft actually received
        self.attempts = 0

    target_system = 1
    target_component = 1

    @property
    def mav(self):
        return self

    def command_long_send(self, sysid, compid, command, conf, p1, p2, *rest):
        self.attempts += 1
        if self.delivery >= 1.0 or (self.attempts % int(1 / max(self.delivery, 0.01)) == 0):
            self.on_wire.append(int(p2))     # custom_mode that arrived


print("")
print("1. a clean link: one send, pending until the heartbeat confirms")
link = Link(delivery=1.0)
link.set_mode("RTL")
note("it went out once", link.attempts == 1)
note("the aircraft got RTL", link.on_wire == [PLANE_MODES["RTL"]])
note("the panel is told it is pending", link.mode_pending.said == ["RTL"])
note("and is still outstanding", link._mode_wanted == "RTL")
# the heartbeat comes back in RTL
link._clear_mode_request()
note("confirmed clears it", link._mode_wanted is None)
note("and the panel is told", link.mode_pending.said[-1] == "")

print("")
print("2. a link that loses the first two: MavGCS keeps sending")
link = Link(delivery=1.0 / 3)          # every third send arrives
link.set_mode("RTL")
now = 0.0
link._mode_retry_next = now + link.MODE_RETRY_EVERY_S
link._mode_retry_until = now + link.MODE_RETRY_FOR_S
while link._mode_wanted is not None and now < 5.0:
    now += 1.0
    link._drive_mode_request(now)
note("it was sent again rather than lost", link.attempts >= 3,
     "%d attempts" % link.attempts)
# The retry does not stop when one arrives - only a heartbeat in that
# mode stops it, and this case never sends one - so more than one
# reaching the aircraft is correct. Setting the same mode twice is
# harmless; that is why repeating it is safe in the first place.
note("at least one got through, and every one was RTL",
     len(link.on_wire) >= 1
     and set(link.on_wire) == {PLANE_MODES["RTL"]},
     "arrived: %s" % link.on_wire)
note("still pending until a heartbeat says so", link._mode_wanted == "RTL")

print("")
print("3. a dead link: it gives up, and says so, rather than for ever")
link = Link(delivery=0.0)
link.set_mode("RTL")
now = 0.0
link._mode_retry_next = now + link.MODE_RETRY_EVERY_S
link._mode_retry_until = now + link.MODE_RETRY_FOR_S
ticks = 0
while link._mode_wanted is not None and ticks < 40:
    now += 0.5
    ticks += 1
    link._drive_mode_request(now)
note("stops within the ten-second window", link._mode_wanted is None
     and now <= link.MODE_RETRY_FOR_S + 1, "gave up at %.1f s" % now)
note("tells the pilot to press again",
     any("press it again" in m.lower() for m in link.command_feedback.said),
     repr(link.command_feedback.said[-1]))
note("and the button stops being lit", link.mode_pending.said[-1] == "")
note("it did not flood the link", link.attempts <= 12,
     "%d sends in %.0f s" % (link.attempts, link.MODE_RETRY_FOR_S))

print("")
print("4. pressing a second mode replaces the first - never both at once")
link = Link(delivery=1.0)
link.set_mode("RTL")
link.set_mode("CRUISE")
note("only CRUISE is outstanding", link._mode_wanted == "CRUISE")
note("the panel shows CRUISE", link.mode_pending.said[-1] == "CRUISE")
link._mode_retry_next = 0.0
link._mode_retry_until = 100.0
link.on_wire.clear()
link._drive_mode_request(50.0)
note("and RTL is never sent again",
     link.on_wire == [PLANE_MODES["CRUISE"]], "resent: %s" % link.on_wire)

print("")
print("5. the pilot's own switch counts as confirmation")
link = Link(delivery=1.0)
link.set_mode("RTL")
# a heartbeat arrives reporting RTL, however it got there
link._clear_mode_request()
before = link.attempts
link._mode_retry_next, link._mode_retry_until = 0.0, 100.0
link._drive_mode_request(50.0)
note("nothing more is sent", link.attempts == before,
     "%d -> %d" % (before, link.attempts))

print("")
print("6. the button colours")
import main as app_main
Panel = app_main.ModePanel


class FakeBtn:
    def __init__(self):
        self.style = ""

    def setStyleSheet(self, s):
        self.style = s

    def setText(self, t):
        self.text = t

    def setToolTip(self, t):
        pass


class PanelStub:
    VTOL_MODES = Panel.VTOL_MODES
    ABORT_TEXT = Panel.ABORT_TEXT
    ABORT_STYLE = Panel.ABORT_STYLE
    ACTIVE_STYLE = Panel.ACTIVE_STYLE
    RTL_STYLE = Panel.RTL_STYLE
    NORMAL_STYLE = Panel.NORMAL_STYLE
    PENDING_STYLE = Panel.PENDING_STYLE
    set_active_mode = Panel.set_active_mode
    set_pending_mode = Panel.set_pending_mode

    def __init__(self):
        self._landing = False
        self._active = None
        self._pending = None
        self._vtol_active = "VTOLACTIVE"
        self._vtol_rest = "VTOLREST"
        self.vtol_btn = FakeBtn()
        self.buttons = {n: FakeBtn() for n in
                        ("MANUAL", "FBWA", "CRUISE", "RTL", "AUTOLAND",
                         "LOITER", "AUTO")}


p = PanelStub()
p.set_active_mode("CRUISE")
note("flying in CRUISE: it is green",
     p.buttons["CRUISE"].style == Panel.ACTIVE_STYLE)
note("RTL rests red", p.buttons["RTL"].style == Panel.RTL_STYLE)

p.set_pending_mode("RTL")
note("press RTL: it goes YELLOW, not red",
     p.buttons["RTL"].style == Panel.PENDING_STYLE)
note("CRUISE stays green while RTL is on its way",
     p.buttons["CRUISE"].style == Panel.ACTIVE_STYLE)

p.set_active_mode("RTL")
note("aircraft reports RTL: it turns green",
     p.buttons["RTL"].style == Panel.ACTIVE_STYLE)
note("and nothing is pending any more", p._pending is None)
note("CRUISE returns to normal",
     p.buttons["CRUISE"].style == Panel.NORMAL_STYLE)

p2 = PanelStub()
p2.set_active_mode("CRUISE")
p2.set_pending_mode("QLOITER")
note("a pending VTOL mode lights the menu button",
     p2.vtol_btn.style == Panel.PENDING_STYLE)
note("and the menu says which one", p2.vtol_btn.text == "QLOITER")

p3 = PanelStub()
p3.set_active_mode("CRUISE")
p3.set_pending_mode("RTL")
p3.set_pending_mode("")
note("giving up clears the yellow",
     p3.buttons["RTL"].style == Panel.RTL_STYLE)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
