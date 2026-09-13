"""Do the balance-check parameters arrive over a link that loses most of them?

Runs the real _retry_missing_params against a stub 'vehicle' that answers
only a fraction of parameter reads, which is what the measured ELRS link
does. The method is called unbound on a plain object, so no QThread is
constructed and no Qt is needed.
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

import random

import threading


from mavlink_link import MavlinkLink

RETRY = MavlinkLink._retry_missing_params
ON_SERVO = MavlinkLink._on_servo_param
SETUP = MavlinkLink._request_elevator_setup
fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


class Emitter:
    def emit(self, *a):
        pass


class Link:
    """Only what the retry path touches."""

    SERVO_SCAN_CHANNELS = MavlinkLink.SERVO_SCAN_CHANNELS
    SERVO_FUNCTION_ELEVATOR = MavlinkLink.SERVO_FUNCTION_ELEVATOR
    SERVO_FUNCTION_MIXED = MavlinkLink.SERVO_FUNCTION_MIXED
    PID_MASK_PITCH = MavlinkLink.PID_MASK_PITCH
    PARAM_RETRY_EVERY_S = MavlinkLink.PARAM_RETRY_EVERY_S
    PARAM_RETRY_MAX = MavlinkLink.PARAM_RETRY_MAX
    _may_configure = MavlinkLink._may_configure
    ELEVATOR_CH = 3

    def __init__(self, delivery):
        self._send_lock = threading.Lock()
        self._want_elevator = True
        self._was_armed = False       # on the ground, as it must be
        self._elevator_ch = None
        self._elevator_min = self._elevator_max = self._elevator_trim = None
        self._elevator_reversed = False
        self._elevator_half_us = 400.0
        self._servo_scan_seen = set()
        self._pid_mask_original = None
        self._pid_mask_current = None
        self._pid_mask_wanted = None
        self._pid_mask_sent_at = 0.0
        self._trim_throttle = None
        self.elevator_status = Emitter()
        self.master = self
        self.delivery = delivery
        self.asked = 0
        self.pending = []
        self._param_progress = None
        self._param_retries = 0

    # the vehicle side
    target_system = 1
    target_component = 1

    @property
    def mav(self):
        return self

    def _request_pid_mask(self):
        self._read("GCS_PID_MASK")

    def _request_trim_throttle(self):
        self._read("TRIM_THROTTLE")

    def _request_elevator_setup(self, ch):
        SETUP(self, ch)

    def _read(self, name):
        self.param_request_read_send(1, 1, name.encode(), -1)

    def param_request_read_send(self, sysid, compid, name, index):
        """Queue a reply, or lose it. Replies are delivered afterwards.

        A real vehicle answers on the receive path, well after the send
        returns and with no lock held. Answering inline here would take
        _send_lock a second time from inside itself and deadlock - which
        it did, and which is a property of this stub, not of the code
        under test.
        """
        self.asked += 1
        if random.random() > self.delivery:
            return                      # the packet is lost
        self.pending.append(name.decode())

    def deliver(self):
        """Hand over the replies that survived, as the link would."""
        queued, self.pending = self.pending, []
        for name in queued:
            if name == "GCS_PID_MASK":
                self._pid_mask_current = 0
            elif name == "TRIM_THROTTLE":
                self._trim_throttle = 45.0
            elif name.startswith("SERVO"):
                ch = int(name[5:name.index("_")])
                suffix = name.split("_", 1)[1]
                value = {"FUNCTION": (19 if ch == self.ELEVATOR_CH else 0),
                         "MIN": 1000, "MAX": 2000, "TRIM": 1500,
                         "REVERSED": 0}[suffix]
                ON_SERVO(self, name, value)


def run(delivery, passes):
    random.seed(7)
    link = Link(delivery)
    used = 0
    while link._param_retries < passes:
        used += 1
        outstanding = RETRY(link)
        link._param_retries += 1
        link.deliver()
        if not outstanding:
            break
    link.passes_used = used
    return link


print("")
print("a clean link, as SITL is")
one = run(delivery=1.0, passes=3)   # scan, then the setup its answer triggers
note("one pass finds everything",
     one._elevator_ch == Link.ELEVATOR_CH and one._pid_mask_current == 0
     and one._trim_throttle == 45.0,
     "channel %s" % one._elevator_ch)

print("")
print("the measured ELRS link, about 20% getting through")
first = run(delivery=0.2, passes=1)
note("ONE pass leaves it unusable - this was the bug",
     first._elevator_ch is None or first._pid_mask_current is None,
     "elevator=%s pid_mask=%s trim=%s" % (first._elevator_ch,
                                          first._pid_mask_current,
                                          first._trim_throttle))

many = run(delivery=0.2, passes=MavlinkLink.PARAM_RETRY_MAX)
note("retrying finds the elevator", many._elevator_ch == Link.ELEVATOR_CH,
     "channel %s after %d passes" % (many._elevator_ch, many.passes_used))
note("and its travel and trim, so an offset means something",
     many._elevator_min == 1000 and many._elevator_max == 2000
     and many._elevator_trim == 1500)
note("and the pitch PID mask, so the integrator can run",
     many._pid_mask_current == 0)
note("and the trim throttle, which gates every sample",
     many._trim_throttle == 45.0)
note("then stops asking", RETRY(many) is False)
note("without flooding the link",
     many.asked < 400, "%d reads across %d passes" % (many.asked,
                                                      many.passes_used))

print("")
print("a link losing 95%")
bad = run(delivery=0.05, passes=MavlinkLink.PARAM_RETRY_MAX)
note("gets there eventually, taking longer",
     bad._elevator_ch == Link.ELEVATOR_CH and bad._pid_mask_current == 0,
     "%d passes, about %d seconds"
     % (bad.passes_used, bad.passes_used * MavlinkLink.PARAM_RETRY_EVERY_S))

print("")
print("a vehicle that never answers at all")
dead = run(delivery=0.0, passes=MavlinkLink.PARAM_RETRY_MAX)
note("gives up rather than asking for ever",
     dead.passes_used == MavlinkLink.PARAM_RETRY_MAX,
     "%d passes then stopped" % dead.passes_used)
note("and it learned nothing, so it will say so",
     dead._elevator_ch is None)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
