"""Silent while armed, and the PID mask write believed only when echoed."""

import os
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
# No display on a build machine, and none needed: nothing here shows a
# window. Qt still has to be told, or importing it fails outright.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random

import threading

import time

from mavlink_link import MavlinkLink

# "Sent long ago, and never answered."
#
# Expressed against the clock rather than written as 0.0, because the
# retry at mavlink_link.py:1540 asks whether
# time.monotonic() - _pid_mask_sent_at >= PARAM_RETRY_EVERY_S, and 0.0
# only means "long ago" where time.monotonic() already returns a large
# number. It does on a build machine, which measures it from boot, and
# does not under the Xcode-bundled Python 3.9 on macOS, which starts near
# zero at process start: there the gap was about 0.4s against a 3.0s
# threshold, so the retry never fired and the two checks below asserted
# nothing while reporting failure. This is negative when the process is
# young, which is the point - it means "before this process existed", and
# the gap only widens as the run goes on.
LONG_AGO = time.monotonic() - MavlinkLink.PARAM_RETRY_EVERY_S - 1

RETRY = MavlinkLink._retry_missing_params
ON_SERVO = MavlinkLink._on_servo_param
ON_MASK = MavlinkLink._on_pid_mask
APPLY = MavlinkLink._apply_pid_mask
SET_MASK = MavlinkLink._set_pid_mask
MAY = MavlinkLink._may_configure
SETUP = MavlinkLink._request_elevator_setup
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
    SERVO_SCAN_CHANNELS = MavlinkLink.SERVO_SCAN_CHANNELS
    SERVO_FUNCTION_ELEVATOR = MavlinkLink.SERVO_FUNCTION_ELEVATOR
    SERVO_FUNCTION_MIXED = MavlinkLink.SERVO_FUNCTION_MIXED
    PID_MASK_PITCH = MavlinkLink.PID_MASK_PITCH
    PARAM_RETRY_EVERY_S = MavlinkLink.PARAM_RETRY_EVERY_S
    PARAM_RETRY_MAX = MavlinkLink.PARAM_RETRY_MAX
    ELEVATOR_CH = 3

    def __init__(self, delivery=1.0, armed=False, mask=0):
        # Seeded per Link, not once for the module, so that a scenario is
        # reproducible without depending on how many draws the scenarios
        # above it happened to consume - which is where
        # test_param_retry.py puts it, for the same reason, with the same
        # seed.
        #
        # Nothing here draws today: every Link below is delivery=1.0, and
        # random.random() returns in [0.0, 1.0), so the two
        # "random.random() <= self.delivery" tests on the wire are
        # unconditionally true. That makes this suite deterministic by
        # accident of its inputs rather than by construction, and the
        # first scenario to pass a real delivery would make it flaky with
        # no way to recover the draw that failed.
        random.seed(7)
        self._send_lock = threading.Lock()
        self._want_elevator = True
        self._was_armed = armed
        self._elevator_ch = None
        self._elevator_min = self._elevator_max = self._elevator_trim = None
        self._elevator_reversed = False
        self._elevator_half_us = 400.0
        self._servo_scan_seen = set()
        self._pid_mask_original = None
        self._pid_mask_current = None
        self._pid_mask_wanted = None
        # The real __init__ puts 0.0 here to mean "never sent", and is
        # safe in doing so because the check at mavlink_link.py:1540 is
        # gated on _pid_mask_wanted being non-None, which only happens at
        # 2136 - one line before the timestamp is stamped for real. A
        # scenario here can set _pid_mask_wanted by hand and reach the
        # check without that having happened, so the stub carries the
        # same intent in a form that does not depend on the clock.
        self._pid_mask_sent_at = LONG_AGO
        self._trim_throttle = None
        self._param_progress = None
        self._param_retries = 0
        self.elevator_status = Emitter()
        self.master = self
        self.delivery = delivery
        self.vehicle_mask = mask
        self.sent = []
        self.pending = []

    target_system = 1
    target_component = 1

    @property
    def mav(self):
        return self

    _may_configure = MAY
    _apply_pid_mask = APPLY
    _set_pid_mask = SET_MASK

    def _request_pid_mask(self):
        if not self._may_configure():
            return
        self._read("GCS_PID_MASK")

    def _request_trim_throttle(self):
        if not self._may_configure():
            return
        self._read("TRIM_THROTTLE")

    def _request_elevator_setup(self, ch):
        SETUP(self, ch)

    def _read(self, name):
        self.param_request_read_send(1, 1, name.encode(), -1)

    # -- the wire --
    def param_request_read_send(self, sysid, compid, name, index):
        self.sent.append(("read", name.decode()))
        if random.random() <= self.delivery:
            self.pending.append(("read", name.decode()))

    def param_set_send(self, sysid, compid, name, value, ptype):
        self.sent.append(("write", name.decode(), int(value)))
        if random.random() <= self.delivery:
            self.vehicle_mask = int(value)
            self.pending.append(("read", "GCS_PID_MASK"))

    def deliver(self):
        queued, self.pending = self.pending, []
        for kind, name in queued:
            if name == "GCS_PID_MASK":
                ON_MASK(self, self.vehicle_mask)
            elif name == "TRIM_THROTTLE":
                self._trim_throttle = 45.0
            elif name.startswith("SERVO"):
                ch = int(name[5:name.index("_")])
                suffix = name.split("_", 1)[1]
                ON_SERVO(self, name, {"FUNCTION": (19 if ch == self.ELEVATOR_CH
                                                   else 0),
                                      "MIN": 1000, "MAX": 2000, "TRIM": 1500,
                                      "REVERSED": 0}[suffix])


def passes(link, n):
    for _ in range(n):
        if not RETRY(link):
            break
        link.deliver()


print("")
print("armed: the aircraft is left alone")
armed = Link(armed=True)
passes(armed, 8)
note("nothing was sent at all", armed.sent == [], "%d messages" % len(armed.sent))
note("and it is still reported as outstanding", RETRY(armed) is True)

print("")
print("armed, and someone presses OK in Settings")
armed2 = Link(armed=True)
armed2._request_pid_mask()
armed2._request_trim_throttle()
SETUP(armed2, 3)
note("still nothing sent", armed2.sent == [],
     "%d messages" % len(armed2.sent))

print("")
print("disarmed: it does its work")
ground = Link(armed=False)
passes(ground, 6)
note("elevator found", ground._elevator_ch == Link.ELEVATOR_CH)
note("pitch bit written", ("write", "GCS_PID_MASK", 2) in ground.sent)
note("aircraft now holds it", ground.vehicle_mask == 2)
note("write confirmed, nothing left pending",
     ground._pid_mask_wanted is None)
writes = [m for m in ground.sent if m[0] == "write"]
note("written exactly once - no echo loop", len(writes) == 1,
     "%d writes" % len(writes))

print("")
print("then it arms mid-session")
ground._was_armed = True
before = len(ground.sent)
passes(ground, 5)
note("goes quiet", len(ground.sent) == before,
     "%d new messages" % (len(ground.sent) - before))

print("")
print("a write that is lost")
lossy = Link(armed=False, delivery=1.0)
passes(lossy, 6)                       # everything learned, mask written
lossy._pid_mask_current = 0            # pretend the write never landed
lossy.vehicle_mask = 0
lossy._pid_mask_wanted = 2
lossy._pid_mask_sent_at = LONG_AGO     # sent long ago, unanswered
before = len([m for m in lossy.sent if m[0] == "write"])
RETRY(lossy)
after = len([m for m in lossy.sent if m[0] == "write"])
note("it is sent again rather than assumed", after > before,
     "%d -> %d writes" % (before, after))
lossy.deliver()
note("and confirmed once it lands", lossy._pid_mask_wanted is None
     and lossy.vehicle_mask == 2)

print("")
print("a mask that already has the pitch bit")
already = Link(armed=False, mask=2)
passes(already, 6)
note("is not written to at all",
     [m for m in already.sent if m[0] == "write"] == [])
note("and the original is remembered as 2, so restore is honest",
     already._pid_mask_original == 2)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
