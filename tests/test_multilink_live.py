"""Several links to one aircraft, for real: sockets, threads, a plane.

A fake flight controller with several telemetry ports - each one its
own MAVLink channel, numbering its own frames, as an autopilot's serial
ports do - talks to the real MavlinkLink over real UDP and TCP sockets.
Then the links are made to misbehave the ways they do in the air, and
what the ground station does about it is checked: where its commands
actually arrive, when it moves, when it does not, and that it cleans up
after itself.

The rules' own timings are shortened so this takes seconds rather than
a minute. The rules are the same; test_multilink.py checks them at
their real values.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, Qt
from pymavlink import mavutil

import multilink
from mavlink_link import MavlinkLink
from fake_plane import FakePlane, TcpChannel, UdpChannel, free_port

try:
    sys.stdout.reconfigure(errors="backslashreplace")
except Exception:
    pass

multilink.LinkHealth.DEAD_AFTER_S = 1.0
multilink.LinkHealth.LOSS_WINDOW_S = 2.0
multilink.LinkChooser.HOLD_S = 1.0
multilink.LinkChooser.SETTLE_S = 1.0
multilink.LinkChooser.DWELL_S = 2.0
MavlinkLink.LINK_REOPEN_S = 0.5

M = mavutil.mavlink
fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


app = QCoreApplication.instance() or QCoreApplication([])
plane = FakePlane()
pa, pb, pc, pt = free_port(), free_port(), free_port(), \
    free_port(socket.SOCK_STREAM)
A, B = UdpChannel(pa), UdpChannel(pb)
plane.channels += [A, B]
plane.start()

events, stats, status = [], [], []
link = MavlinkLink("udpin:0.0.0.0:%d" % pa)
direct = Qt.ConnectionType.DirectConnection
link.command_feedback.connect(lambda t: events.append((time.time(), t)), direct)
link.link_stats_update.connect(lambda d: stats.append(d), direct)
link.connection_status.connect(lambda c, m: status.append((c, m)), direct)


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def said(fragment, since=0.0):
    return [t for (when, t) in events if when >= since and fragment in t]


def last_states():
    for d in reversed(stats):
        if d.get("links"):
            return [l["state"] for l in d["links"]]
    return None


print("")
print("1. one link, exactly as before")
link.start()
note("connects on the first link", wait_for(
    lambda: any(c for c, _ in status), 8.0), status[-1:] if status else "")
time.sleep(1.5)
note("one link: nothing about several links is running",
     link._multi is False and not any(d.get("links") for d in stats))

print("")
print("2. adding a second link")
t_add = time.time()
link.add_link("udpin:0.0.0.0:%d" % pb)
note("the second link comes up as the same aircraft",
     wait_for(lambda: said("UDP %d is up - same aircraft" % pb, t_add), 6.0),
     said("UDP %d" % pb, t_add))
note("the line shows the first in use and the second ready",
     wait_for(lambda: last_states() == ["active", "standby"], 3.0),
     last_states())
note("the new link was given its own telemetry rates",
     wait_for(lambda: ("COMMAND_LONG", M.MAV_CMD_SET_MESSAGE_INTERVAL) in B.got,
              2.0))
A.got.clear(), B.got.clear()
time.sleep(1.6)
note("the ground station's heartbeat goes out on both links",
     ("HEARTBEAT", None) in A.got and ("HEARTBEAT", None) in B.got)

print("")
print("3. commands go out on the link in use, and only there")
A.got.clear(), B.got.clear()
link.set_mode("LOITER")
time.sleep(1.2)
mode_cmd = ("COMMAND_LONG", M.MAV_CMD_DO_SET_MODE)
note("the mode change arrives on the first link", mode_cmd in A.got)
note("and never on the second", mode_cmd not in B.got)

print("")
print("4. the link in use goes quiet")
t_quiet = time.time()
A.silent = True
moved = wait_for(lambda: said("now using UDP %d" % pb, t_quiet), 4.0)
took = (said("now using UDP %d" % pb, t_quiet) and
        [w for w, t in events if w >= t_quiet and "now using" in t][0] - t_quiet)
note("moves to the second link by itself", moved,
     "%.1f s after the first went quiet" % took if moved else "")
note("and says why", bool(said("UDP %d went quiet" % pa, t_quiet)),
     said("now using", t_quiet))
note("in one message, not a second one saying lost",
     not said("UDP %d lost" % pa, t_quiet), said("UDP %d" % pa, t_quiet))
A.got.clear(), B.got.clear()
link.set_mode("RTL")
time.sleep(1.2)
note("commands now arrive on the second link", mode_cmd in B.got)
note("and no longer on the first", mode_cmd not in A.got)

print("")
print("5. the first link comes back")
t_back = time.time()
A.silent = False
note("its return is noticed",
     wait_for(lambda: said("UDP %d is back" % pa, t_back), 4.0))
time.sleep(4.0)
note("both fine again: it stays where it is",
     not said("now using", t_back), said("now using", t_back))

print("")
print("6. the link in use starts losing a third of everything")
t_loss = time.time()
B.drop_every = 3
note("moves back to the first link",
     wait_for(lambda: said("now using UDP %d" % pa, t_loss), 10.0),
     said("now using", t_loss))
note("because of the loss", bool(said("losing", t_loss)),
     said("losing", t_loss))
B.drop_every = 0

print("")
print("7. a link carrying a different aircraft")
C = UdpChannel(pc, sysid=2)
plane.channels.append(C)
t_c = time.time()
link.add_link("udpin:0.0.0.0:%d" % pc)
note("is recognised as another aircraft",
     wait_for(lambda: said("carries a different aircraft (system 2)", t_c),
              5.0), said("UDP %d" % pc, t_c))
time.sleep(1.5)
note("and is never used", not said("now using UDP %d" % pc, t_c))

print("")
print("8. a TCP link whose far end restarts")
T = TcpChannel(pt)
plane.channels.append(T)
t_t = time.time()
link.add_link("tcp:127.0.0.1:%d" % pt)
note("comes up", wait_for(lambda: said("TCP %d is up" % pt, t_t), 6.0),
     said("TCP %d" % pt, t_t))
t_hang = time.time()
T.hang_up()
note("the hang-up is noticed and named, not taken for silence",
     wait_for(lambda: said("TCP %d failed - closed by the other end" % pt,
                           t_hang), 4.0), said("TCP %d" % pt, t_hang))
note("and it is opened again by itself",
     wait_for(lambda: said("TCP %d is back" % pt, t_hang), 8.0),
     said("TCP %d" % pt, t_hang))
note("one message for the failure, not a second one saying lost",
     not said("TCP %d lost" % pt, t_hang), said("TCP %d" % pt, t_hang))

print("")
print("9. links that cannot be had")
dead = free_port(socket.SOCK_STREAM)
t_d = time.time()
link.add_link("tcp:127.0.0.1:%d" % dead)
note("a link that will not open says so once, and keeps trying",
     wait_for(lambda: said("could not open TCP %d" % dead, t_d), 30.0)
     and len(said("could not open TCP %d" % dead, t_d)) == 1,
     said("TCP %d" % dead, t_d))
note("and says why in plain words",
     bool(said("TCP %d - nothing is listening there" % dead, t_d)),
     said("TCP %d" % dead, t_d))
t_dup = time.time()
link.add_link("udpin:0.0.0.0:%d" % pa)
note("the same link twice is refused",
     wait_for(lambda: said("UDP %d is already connected" % pa, t_dup), 3.0))

print("")
print("9b. removing links")
t_r = time.time()
link.remove_link("udpin:0.0.0.0:%d" % pc)
note("a link carrying another aircraft can be removed",
     wait_for(lambda: said("UDP %d removed" % pc, t_r), 3.0))
link.remove_link("tcp:127.0.0.1:%d" % dead)
note("so can one that never opened",
     wait_for(lambda: said("TCP %d removed" % dead, t_r), 3.0))
t_r2 = time.time()
in_use = [t for t in said("now using") ][-1].split("now using ")[1].split(" -")[0]
note("(the link in use is %s)" % in_use, in_use == "UDP %d" % pa, in_use)
link.remove_link("udpin:0.0.0.0:%d" % pa)
note("removing the link in use moves to another first",
     wait_for(lambda: said("UDP %d removed" % pa, t_r2), 3.0)
     and bool(said("- UDP %d removed" % pa, t_r2)),
     said("removed", t_r2))
A.silent = True                       # nothing should be listening now
link.remove_link("tcp:127.0.0.1:%d" % pt)
wait_for(lambda: said("TCP %d removed" % pt, t_r2), 3.0)
t_r3 = time.time()
link.remove_link("udpin:0.0.0.0:%d" % pb)
note("the last link left is not removed - that is Disconnect",
     wait_for(lambda: said("UDP %d is the only link" % pb, t_r3), 3.0),
     said("UDP %d" % pb, t_r3))

print("")
print("10. disconnecting closes every link")
link.stop()
time.sleep(1.5)
reopened = []
for port in (pa, pb, pc):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
        reopened.append(port)
    except OSError:
        pass
    finally:
        s.close()
note("every UDP port it held is free again", len(reopened) == 3,
     "%d of 3 free" % len(reopened))
readers = [t for t in threading.enumerate() if t.name == "MavGCS link read"]
note("no reading thread outlives it", not readers, "%d left" % len(readers))

print("")
print("11. ONE link, on TCP, whose far end restarts")
# Not about several links - about the one most people have. On V2.3.3 a
# lone TCP link whose far end restarted (MavLTE in TCP mode, say) was
# read again at once, broken, in a tight loop for ever: a processor core
# pinned, pymavlink's complaint printed over a million times in ten
# seconds, and not one frame more, though the far end was listening
# again. It is now handed to the reopening machinery above.
pt2 = free_port(socket.SOCK_STREAM)
T2 = TcpChannel(pt2)
plane.channels.append(T2)
one_status, one_frames, one_said = [], [], []
one = MavlinkLink("tcp:127.0.0.1:%d" % pt2)
one.connection_status.connect(
    lambda c, m: one_status.append((time.time(), c, m)), direct)
one.attitude_update.connect(lambda *a: one_frames.append(time.time()), direct)
one.command_feedback.connect(lambda t: one_said.append((time.time(), t)),
                             direct)
one.start()
up = wait_for(lambda: any(c for _, c, _ in one_status), 15.0)
flowing = wait_for(lambda: len(one_frames) >= 10, 10.0)
note("(first, the one link is up and carrying frames)", up and flowing,
     "connected %s, %d frames" % (up, len(one_frames)))
time.sleep(1.0)
t_one = time.time()
cpu0 = time.process_time()
T2.hang_up()
back = wait_for(lambda: any(c and w >= t_one for w, c, _ in one_status), 10.0)
time.sleep(2.0)
cpu = time.process_time() - cpu0
# From the hang-up onwards, not strictly after it: on Python 3.12's
# Windows clock (about 15 ms a tick, CI's runner) MavGCS noticed the
# hang-up inside the same tick, and a strict ">" threw the
# disconnect away - a test failure with nothing wrong in MavGCS.
broke = [m for w, c, m in one_status if w >= t_one and not c]
# Everything heard after the hang-up, for a failure seen only elsewhere
# (CI's Windows runner, once) to explain itself.
heard = (["%+.2fs status %s %s" % (w - t_one, c, m)
          for w, c, m in one_status if w > t_one - 0.5]
         + ["%+.2fs said %s" % (w - t_one, t)
            for w, t in one_said if w > t_one - 0.5])
note("the break is shown, as a broken link always was",
     bool(broke) and broke[0].startswith("Link error: closed by the other end"),
     broke[:1] if broke else sorted(heard))
note("and it reconnects by itself", back)
note("frames flow again", any(t > t_one + 1.0 for t in one_frames),
     "%d frames after" % len([t for t in one_frames if t > t_one]))
note("without burning a processor core meanwhile", cpu < 3.0,
     "%.1f s of CPU in %.1f s" % (cpu, time.time() - t_one))
one.stop()

plane.stop()
print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
