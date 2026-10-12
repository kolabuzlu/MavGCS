"""MavGCS never sends terrain data to the aircraft.

ArduPilot with its terrain system on asks every connected ground station
for the terrain tiles it lacks - around where it is, and around the
mission's waypoints - in the air as on the ground. A station that serves
terrain answers with TERRAIN_DATA from its own SRTM: MAVProxy does by
default (its terrain module is in the default list), and Mission Planner
does as far as is known. The pilot does not want terrain sent to an
aircraft in flight, and starts missions in the air (2026-10-12).

MavGCS has never sent it - nothing was ever written to - so this makes
that a rule a future change cannot break by accident: no source file of
the app may send or encode TERRAIN_DATA or handle TERRAIN_REQUEST, and a
real link asked for terrain by the fake plane, as ArduPilot asks, must
send nothing terrain-related back. Each check first shows it can catch
the thing it guards against.
"""

import ast
import io
import os
import socket
import sys
import time
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, Qt
from pymavlink import mavutil

from fake_plane import FakePlane, UdpChannel, free_port
from mavlink_link import MavlinkLink

M = mavutil.mavlink
fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


# Names that send or build terrain data, and the two message types whose
# handling would be the start of serving it. Matched as code - names, and
# string literals that ARE the name - never in comments, and never inside
# a docstring that merely mentions one.
FORBIDDEN_NAMES = {"terrain_data_send", "terrain_data_encode",
                   "MAVLink_terrain_data_message",
                   "MAVLINK_MSG_ID_TERRAIN_DATA",
                   "MAVLINK_MSG_ID_TERRAIN_REQUEST"}
FORBIDDEN_STRINGS = FORBIDDEN_NAMES | {"TERRAIN_DATA", "TERRAIN_REQUEST"}


def offences(source):
    """Every (line, what) in this source that would serve terrain."""
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.NAME and tok.string in FORBIDDEN_NAMES:
            found.append((tok.start[0], tok.string))
        elif tok.type == tokenize.STRING:
            try:
                value = ast.literal_eval(tok.string)
            except Exception:
                continue
            if isinstance(value, str) and value in FORBIDDEN_STRINGS:
                found.append((tok.start[0], repr(value)))
    return found


print("")
print("1. no source file of the app can serve terrain")
note("the check finds one when there is one",
     offences("link.master.mav.terrain_data_send(1, 2, 3, 4, [])\n"
              "if mtype == 'TERRAIN_REQUEST':\n    pass\n")
     == [(1, "terrain_data_send"), (2, "'TERRAIN_REQUEST'")])
note("and not in a comment or a docstring that only mentions it",
     offences('"""Never answers TERRAIN_REQUEST."""\n'
              "# terrain_data_send is not called anywhere\n") == [])
# The app is the modules beside main.py - what the build packs. Tests and
# tools are not shipped, and a tool may play the aircraft.
app_files = sorted(f for f in os.listdir(ROOT) if f.endswith(".py"))
found = {}
for name in app_files:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        hits = offences(f.read())
    if hits:
        found[name] = hits
note("none in the app's %d source files" % len(app_files), not found,
     found or ", ".join(app_files))


print("")
print("2. asked for terrain as ArduPilot asks, a real link sends none")
app = QCoreApplication.instance() or QCoreApplication([])
plane = FakePlane()
port = free_port()
ch = UdpChannel(port)
plane.channels.append(ch)
plane.start()


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def terrain_heard():
    return [t for t, _c in ch.got if t.startswith("TERRAIN")]


# First, that the plane's record would show it: TERRAIN_DATA sent to it
# the way a station serving terrain would send it.
server = M.MAVLink(None, srcSystem=255, srcComponent=190)
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
probe.sendto(server.terrain_data_encode(
    -353632610, 1491652300, 100, 0, [0] * 16).pack(server),
    ch.sock.getsockname())
note("the plane's record would show terrain data sent to it",
     wait_for(lambda: "TERRAIN_DATA" in terrain_heard(), 3.0),
     terrain_heard())
probe.close()
ch.got.clear()

link = MavlinkLink("udpin:0.0.0.0:%d" % port)
status = []
link.connection_status.connect(lambda c, m: status.append(c),
                               Qt.ConnectionType.DirectConnection)
link.start()
note("(the link is up)", wait_for(lambda: any(status), 15.0))
# What ArduPilot sends with TERRAIN_ENABLE on and tiles missing: a 4x8
# grid of 4x4 blocks around a point, every block asked for.
asker = M.MAVLink(None, srcSystem=1, srcComponent=1)
for i in range(6):
    msg = asker.terrain_request_encode(-353632610, 1491652300, 100,
                                       (1 << 56) - 1)
    ch.sock.sendto(msg.pack(asker), ch.dest)
    time.sleep(0.5)
time.sleep(2.0)
note("six terrain requests over three seconds: nothing terrain sent back",
     not terrain_heard(), terrain_heard())
note("(and the link answered as a ground station all along)",
     ("HEARTBEAT", None) in ch.got)
link.stop()
plane.stop()

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
