"""Does the Terrain Follow button tell the truth about the aircraft?

Three things have to hold. Pressing it writes TERRAIN_FOLLOW. A write the
link swallows is sent again rather than lost, and the button says so
while that is happening. And the button follows the parameter wherever it
is changed - the parameter window, another ground station, a loaded
parameter file - because it shows what the aircraft has, not what was
last pressed here.

Drives the real methods against a stub link, so no Qt window and no
aircraft.
"""

import os
import sys
import threading

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mavlink_link import MavlinkLink

SET = MavlinkLink.set_terrain_follow
DRIVE = MavlinkLink._drive_terrain_follow
ON_VALUE = MavlinkLink._on_terrain_follow_value
SEND = MavlinkLink._send_terrain_follow
EMIT = MavlinkLink._emit_terrain_follow
SET_PARAM = MavlinkLink._set_param

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
        self.said.append(a if len(a) != 1 else a[0])


class Link:
    """Only what the terrain-follow path touches."""

    TF_RETRY_EVERY_S = MavlinkLink.TF_RETRY_EVERY_S
    TF_RETRY_FOR_S = MavlinkLink.TF_RETRY_FOR_S
    set_terrain_follow = SET
    _drive_terrain_follow = DRIVE
    _on_terrain_follow_value = ON_VALUE
    _send_terrain_follow = SEND
    _emit_terrain_follow = EMIT
    _set_param = SET_PARAM
    _request_terrain_follow = lambda self: None

    def __init__(self, delivery=1.0):
        self._send_lock = threading.Lock()
        self._terrain_follow = None
        self._tf_wanted = None
        self._tf_retry_next = 0.0
        self._tf_retry_until = 0.0
        self.command_feedback = Emitter()
        self.terrain_follow_update = Emitter()
        self.master = self
        self.delivery = delivery
        self.writes = []            # what actually reached the aircraft
        self.attempts = 0

    target_system = 1
    target_component = 1

    @property
    def mav(self):
        return self

    def param_set_send(self, sysid, compid, name, value, ptype):
        self.attempts += 1
        if self.attempts % max(1, int(round(1 / max(self.delivery, 0.01)))) == 0:
            self.writes.append((name.decode().rstrip("\x00"), float(value)))

    def button(self):
        """What the map would be showing: neutral, YELLOW or GREEN."""
        if not self.terrain_follow_update.said:
            return "neutral"
        on, pending = self.terrain_follow_update.said[-1]
        return "YELLOW" if pending else ("GREEN" if on else "neutral")


print("")
print("a clean link")
link = Link()
link._on_terrain_follow_value(0.0)          # read on connect: it is off
note("starts neutral", link.button() == "neutral", link.button())
link.set_terrain_follow(True)
note("the write goes out", link.writes == [("TERRAIN_FOLLOW", 1.0)],
     repr(link.writes))
note("and it says so while waiting", link.button() == "YELLOW",
     link.button())
link._on_terrain_follow_value(1.0)          # the aircraft echoes
note("green once the aircraft agrees", link.button() == "GREEN",
     link.button())
note("nothing left outstanding", link._tf_wanted is None)

print("")
print("turning it off again")
link.set_terrain_follow(False)
note("shows pending", link.button() == "YELLOW")
link._on_terrain_follow_value(0.0)
note("back to neutral", link.button() == "neutral")
note("the aircraft got both writes",
     link.writes == [("TERRAIN_FOLLOW", 1.0), ("TERRAIN_FOLLOW", 0.0)],
     repr(link.writes))

print("")
print("a link that swallows the first two writes")
link = Link(delivery=1.0 / 3)
link._on_terrain_follow_value(0.0)
link.set_terrain_follow(True)
now = 0.0
link._tf_retry_next = now + link.TF_RETRY_EVERY_S
link._tf_retry_until = now + link.TF_RETRY_FOR_S
while link._tf_wanted is not None and now < 6.0:
    now += 1.5
    link._drive_terrain_follow(now)
note("it was sent again rather than lost", link.attempts >= 3,
     "%d attempts" % link.attempts)
note("one got through", ("TERRAIN_FOLLOW", 1.0) in link.writes,
     repr(link.writes))
note("and it stayed yellow throughout", link.button() == "YELLOW",
     link.button())
link._on_terrain_follow_value(1.0)
note("green when the aircraft finally confirms",
     link.button() == "GREEN")

print("")
print("an aircraft that never answers")
link = Link(delivery=0.0)
link._on_terrain_follow_value(0.0)
link.set_terrain_follow(True)
now = 0.0
link._tf_retry_next = now + link.TF_RETRY_EVERY_S
link._tf_retry_until = now + link.TF_RETRY_FOR_S
ticks = 0
while link._tf_wanted is not None and ticks < 60:
    now += 0.5
    ticks += 1
    link._drive_terrain_follow(now)
note("gives up inside the window", link._tf_wanted is None
     and now <= link.TF_RETRY_FOR_S + 1, "gave up at %.1f s" % now)
note("stops being yellow", link.button() != "YELLOW", link.button())
note("and stays showing what the aircraft last said",
     link.button() == "neutral", link.button())
note("telling the pilot to press again",
     any("press it again" in m.lower() for m in link.command_feedback.said),
     repr(link.command_feedback.said[-1]))
note("without flooding the link", link.attempts <= 12,
     "%d writes in %.0f s" % (link.attempts, link.TF_RETRY_FOR_S))

print("")
print("changed somewhere else entirely")
link = Link()
link._on_terrain_follow_value(0.0)
note("neutral to begin with", link.button() == "neutral")
# The parameter window, another ground station, or a loaded parameter
# file - all of them arrive the same way, as a PARAM_VALUE.
link._on_terrain_follow_value(1.0)
note("the button turns green on its own", link.button() == "GREEN",
     link.button())
note("with nothing written from here", link.writes == [], repr(link.writes))
note("and it says who moved it",
     any("changed to on" in m.lower() for m in link.command_feedback.said),
     repr(link.command_feedback.said[-1]))
link._on_terrain_follow_value(0.0)
note("and back again", link.button() == "neutral")

print("")
print("the aircraft disagreeing with what was asked")
link = Link()
link._on_terrain_follow_value(0.0)
link.set_terrain_follow(True)
link._on_terrain_follow_value(0.0)   # it answers, but still off
note("stays outstanding rather than pretending",
     link._tf_wanted is True, repr(link._tf_wanted))
note("and stays yellow", link.button() == "YELLOW", link.button())

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
