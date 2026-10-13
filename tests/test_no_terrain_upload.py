"""MavGCS sends terrain only where and when the pilot asks: never in flight.

ArduPilot with its terrain system on asks every connected ground station
for the terrain tiles it lacks - around where it is, and around the
mission's waypoints - in the air as on the ground. A station that serves
terrain answers with TERRAIN_DATA from its own SRTM: MAVProxy does by
default (its terrain module is in the default list), and Mission Planner
does too. The pilot does not want terrain sent to an aircraft in flight,
and starts missions in the air (2026-10-12).

MavGCS answers none of it, with one exception the pilot asked for the same
day: Terrain Upload, an area drawn on the map and loaded onto the card -
over the flight controller's own USB cable only, only while the aircraft
is disarmed, and only when the pilot starts it. This makes that a rule a
future change cannot break by accident:

1. Terrain is sent from one module, terrain_upload.py, and no other source
   file of the app may send or encode it, or handle the aircraft's asking.
2. A real link that is not the flight controller's USB, asked for terrain
   as ArduPilot asks, sends nothing terrain-related back - even with an
   upload forced through underneath the button's own refusal.
3. On a link that does count as the USB, an upload does send - which shows
   section 2 would have seen it - and the moment the aircraft reports
   itself armed, not one more terrain message goes out.
4. ArduPilot's simulator counts as well (the pilot asked to rehearse with
   SITL, 2026-10-12): it talks over the network but cannot fly, and it
   alone answers for its SIM_ settings. An aircraft that does not - the
   plane of section 2, on the same kind of link - stays refused.
5. The simulator's answer counts for the simulator only. Reaching a real
   aircraft later in the same session - by Add Link, by the link opening
   again onto another device, or by another device sending to the same
   port - must not carry the answer over to it (found 2026-10-13: all
   three did). A simulator started again is still recognised.

Each check first shows it can catch the thing it guards against.
"""

import ast
import io
import math
import os
import socket
import sys
import threading
import time
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication, Qt
from pymavlink import mavutil

import terrain_upload
from fake_plane import FakePlane, TcpChannel, UdpChannel, free_port
from fake_terrain import FakeTerrainPlane
from mavlink_link import MavlinkLink

M = mavutil.mavlink
fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


# Names that send or build terrain, or ask the aircraft about it, and the
# message types whose handling would be the start of serving it. Matched as
# code - names, and string literals that ARE the name - never in comments,
# and never inside a docstring that merely mentions one.
FORBIDDEN_NAMES = {"terrain_data_send", "terrain_data_encode",
                   "terrain_check_send", "terrain_check_encode",
                   "MAVLink_terrain_data_message",
                   "MAVLINK_MSG_ID_TERRAIN_DATA",
                   "MAVLINK_MSG_ID_TERRAIN_REQUEST"}
FORBIDDEN_STRINGS = FORBIDDEN_NAMES | {"TERRAIN_DATA", "TERRAIN_REQUEST"}
# The one module allowed them. Everything it sends goes through the link's
# own check as well as its own; sections 2 and 3 test both together.
ALLOWED = {"terrain_upload.py"}


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
print("1. terrain is sent from terrain_upload.py and nowhere else")
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
    if name in ALLOWED:
        continue
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        hits = offences(f.read())
    if hits:
        found[name] = hits
note("none in the app's other %d source files" % (len(app_files) - 1),
     not found, found or ", ".join(f for f in app_files if f not in ALLOWED))
with open(os.path.join(ROOT, "terrain_upload.py"), encoding="utf-8") as f:
    upload_src = f.read()
note("(and the one that may, does - so the line above is not vacuous)",
     any(w == "terrain_data_send" for _l, w in offences(upload_src)))


# ---------------------------------------------------------------------------
# A fake plane whose terrain is ArduPilot's, mirrored (tests/fake_terrain.py),
# on a real UDP link: it asks for what it lacks and answers what it is asked.

class _TerrainPort:
    """A plane's port that also carries its terrain system."""

    def _carry(self, terrain):
        self.terrain = terrain
        self.lock = threading.Lock()
        self.terrain_heard = []         # (type, when) of terrain from the GCS
        self.asked = 0                  # TERRAIN_REQUESTs it sent
        self._parse = M.MAVLink(None)
        # The plane's own thread and the terrain's both send on this port,
        # and encoding a frame hands it back through one shared buffer.
        self._out_lock = threading.Lock()

    def frame(self, msg):
        with self._out_lock:
            return super().frame(msg)

    def later(self, data, now):
        with self._out_lock:
            super().later(data, now)

    def heard(self, data):
        super().heard(data)
        try:
            msgs = self._parse.parse_buffer(data) or []
        except Exception:
            return
        t = self.terrain
        with self.lock:
            for m in msgs:
                kind = m.get_type()
                if kind.startswith("TERRAIN"):
                    self.terrain_heard.append((kind, time.monotonic()))
                if kind == "TERRAIN_CHECK":
                    t.terrain_check_send(m.lat, m.lon)
                elif kind == "TERRAIN_DATA":
                    t.terrain_data_send(m.lat, m.lon, m.grid_spacing,
                                        m.gridbit, list(m.data))
                elif kind == "PARAM_REQUEST_READ":
                    t.param_request_read_send(1, 1, m.param_id, -1)
                elif kind == "PARAM_SET":
                    t.param_set_send(1, 1, m.param_id, m.param_value, 9)
                elif kind == "COMMAND_LONG":
                    t.command_long_send(1, 1, m.command, 0, m.param1,
                                        m.param2)

    def pump(self, now):
        """The terrain system's own time, and what it has to say."""
        with self.lock:
            self.terrain.step(now)
            out = self.terrain.take()
        for kind, msg in out:
            if kind == "PARAM_VALUE":
                name, value = msg
                msg = M.MAVLink_param_value_message(
                    name.encode(), value, M.MAV_PARAM_TYPE_REAL32, 1, 0)
            if kind == "TERRAIN_REQUEST":
                self.asked += 1
            data = self.frame(msg)
            if data is not None:
                self.later(data, time.time())


class TerrainChannel(_TerrainPort, UdpChannel):
    """On UDP, sending to the ground station's port."""

    def __init__(self, gcs_port, terrain):
        UdpChannel.__init__(self, gcs_port)
        self._carry(terrain)


class TcpTerrainChannel(_TerrainPort, TcpChannel):
    """On TCP, as the server - as SITL's own port and MavLTE's are."""

    def __init__(self, port, terrain):
        TcpChannel.__init__(self, port)
        self._carry(terrain)


def pumping(ch, stop):
    t0 = time.monotonic()
    while not stop.is_set():
        ch.pump(time.monotonic() - t0)
        time.sleep(0.02)


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def ground(lats, lons):
    return 900.0 + 300.0 * np.sin(np.asarray(lats) * 40.0) * np.cos(
        np.asarray(lons) * 30.0)


class _Tile:
    def sample_many(self, lats, lons):
        return ground(lats, lons)


class _Map:
    """No internet in a test: the map is made up, and the same every run."""

    def tile_for(self, lat, lon):
        return _Tile(), "ok"


# The link builds its upload with the app's real map; here, with the one
# above. Nothing else about the upload or the link is replaced.
terrain_upload._default_provider = lambda on_status: _Map()

AREA = [(39.90, 32.80), (39.91, 32.80), (39.91, 32.81), (39.90, 32.81)]


def start_plane(terrain, port=None, channel=TerrainChannel):
    plane = FakePlane()
    if port is None:
        port = free_port(socket.SOCK_STREAM if channel is TcpTerrainChannel
                         else socket.SOCK_DGRAM)
    ch = channel(port, terrain)
    plane.channels.append(ch)
    plane.start()
    stop = threading.Event()
    threading.Thread(target=pumping, args=(ch, stop), daemon=True).start()
    return plane, ch, port, stop


def connect(port, connection=None, **kw):
    link = MavlinkLink(connection or "udpin:0.0.0.0:%d" % port, **kw)
    status, finished = [], []
    link.connection_status.connect(lambda c, m: status.append(c),
                                   Qt.ConnectionType.DirectConnection)
    link.terrain_upload_finished.connect(
        lambda ok, text: finished.append((ok, text)),
        Qt.ConnectionType.DirectConnection)
    link.start()
    return link, status, finished


app = QCoreApplication.instance() or QCoreApplication([])

print("")
print("2. on any link but the flight controller's USB, nothing terrain")
plane, ch, port, stop = start_plane(FakeTerrainPlane(position=(
    int(39.905 * 1e7), int(32.805 * 1e7))))

# First, that the plane's record would show it: TERRAIN_DATA sent to it
# the way a station serving terrain would send it.
server = M.MAVLink(None, srcSystem=255, srcComponent=190)
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
probe.sendto(server.terrain_data_encode(
    -353632610, 1491652300, 100, 0, [0] * 16).pack(server),
    ch.sock.getsockname())
note("the plane's record would show terrain data sent to it",
     wait_for(lambda: ("TERRAIN_DATA", None) in ch.got, 3.0))
probe.close()
ch.got.clear()
ch.terrain_heard.clear()

link, status, finished = connect(port)
note("(the link is up)", wait_for(lambda: any(status), 15.0))
note("(and the plane, missing its terrain, is asking for it)",
     wait_for(lambda: ch.asked >= 2, 10.0), "%d requests" % ch.asked)
note("(the link asked whether it is the simulator, and was not told so)",
     ("PARAM_REQUEST_READ", None) in ch.got and not link._simulator)
blocker = link.terrain_upload_blocker()
note("Terrain Upload is refused over it, and the pilot told why",
     blocker is not None and "own USB cable" in blocker
     and "network link" in blocker, blocker)
# Underneath the button's refusal, as a bug might: the upload itself must
# still send nothing on this link.
link.start_terrain_upload(AREA)
note("an upload forced through anyway stops at once",
     wait_for(lambda: finished, 10.0)
     and not finished[0][0] and "not the flight controller's own USB" in finished[0][1],
     finished)
time.sleep(3.0)
note("asked for terrain all the while: nothing terrain-related sent back",
     not ch.terrain_heard, ch.terrain_heard[:5])
note("(and the link answered as a ground station all along)",
     ("HEARTBEAT", None) in ch.got)
link.stop()
stop.set()
plane.stop()


print("")
print("3. on the USB, it sends - and not one message once the plane is armed")
plane, ch, port, stop = start_plane(FakeTerrainPlane())
link, status, finished = connect(
    port, usb_check=lambda connection: (True, "the flight controller's USB"))
note("(the link is up)", wait_for(lambda: any(status), 15.0))
note("the upload is allowed: a USB link and a disarmed aircraft",
     link.terrain_upload_blocker() is None, link.terrain_upload_blocker())
link.start_terrain_upload(AREA)
note("it sends terrain - so section 2 would have seen any",
     wait_for(lambda: any(k == "TERRAIN_DATA" for k, _t in ch.terrain_heard),
              30.0), "%d terrain messages" % len(ch.terrain_heard))
armed_seen = []
link.status_update.connect(
    lambda d: armed_seen.append(time.monotonic())
    if d.get("armed") == "YES" and not armed_seen else None,
    Qt.ConnectionType.DirectConnection)
plane.armed = True
note("(the link hears the aircraft armed)",
     wait_for(lambda: armed_seen, 5.0))
note("the upload stops, saying why",
     wait_for(lambda: finished, 10.0) and not finished[0][0]
     and "armed" in finished[0][1], finished)
time.sleep(3.0)
# Anything already on its way when the heartbeat arrived lands within
# milliseconds on this machine; a quarter of a second covers it.
late = [(k, round(t - armed_seen[0], 3)) for k, t in ch.terrain_heard
        if armed_seen and t > armed_seen[0] + 0.25]
note("not one terrain message once the aircraft said it was armed",
     armed_seen and not late, late[:5])
link.stop()
stop.set()
plane.stop()

print("")
print("4. ArduPilot's simulator: over the network, and allowed")
plane, ch, port, stop = start_plane(FakeTerrainPlane(sim_speedup=1))
link, status, finished = connect(port)
note("(the link is up)", wait_for(lambda: any(status), 15.0))
note("it says it is the simulator", wait_for(lambda: link._simulator, 10.0))
note("so the upload is allowed, on a network link",
     link.terrain_upload_blocker() is None, link.terrain_upload_blocker())
link.start_terrain_upload(AREA)
note("and it loads", wait_for(lambda: finished, 120.0) and finished[0][0],
     finished)
link.stop()
stop.set()
plane.stop()

# A SIM_SPEEDUP that is not a number is no answer at all.
plane, ch, port, stop = start_plane(FakeTerrainPlane(sim_speedup=float("nan")))
link, status, finished = connect(port)
note("(the link is up)", wait_for(lambda: any(status), 15.0))
time.sleep(3.0)
note("an answer that is not a number does not make it the simulator",
     not link._simulator and link.terrain_upload_blocker() is not None,
     link.terrain_upload_blocker())
link.stop()
stop.set()
plane.stop()


print("")
print("5. the simulator's answer counts for the simulator only")


def rehearse(connection=None, port=None, channel=TerrainChannel):
    """A session that starts on the simulator, as a rehearsal does."""
    sim = start_plane(FakeTerrainPlane(sim_speedup=1), port=port,
                      channel=channel)
    link, status, finished = connect(sim[2], connection=connection)
    note("(the link is up, and the simulator recognised)",
         wait_for(lambda: any(status), 15.0)
         and wait_for(lambda: link.terrain_upload_blocker() is None, 10.0),
         link.terrain_upload_blocker())
    return sim, link, status, finished


def closed(plane):
    plane[3].set()
    plane[0].stop()


def refused(how, link, ch, finished):
    """The aircraft reached now is not the simulator: refused at the
    button, refused underneath it, and sent nothing terrain-related."""
    blocker = link.terrain_upload_blocker()
    note("%s: Terrain Upload is refused, saying why" % how,
         blocker is not None and "network link" in blocker, blocker)
    link.start_terrain_upload(AREA)
    note("%s: an upload forced through anyway stops at once" % how,
         wait_for(lambda: finished, 10.0) and not finished[-1][0]
         and "not the flight controller's own USB" in finished[-1][1],
         finished[-1:])
    time.sleep(2.0)
    note("%s: nothing terrain-related reached the aircraft" % how,
         not ch.terrain_heard, ch.terrain_heard[:5])


# Add Link: the pilot's likeliest road. The simulator stops, the button
# still reads Add Link, and the real aircraft's link is added to the
# session - the same aircraft, as far as the link can tell.
sim, link, status, finished = rehearse()
closed(sim)
real = start_plane(FakeTerrainPlane())
link.add_link("udpin:0.0.0.0:%d" % real[2])
note("(the session moves onto the added link)",
     wait_for(lambda: link._active_connection().endswith(":%d" % real[2]),
              30.0), link._active_connection())
refused("added by Add Link", link, real[1], finished)
link.stop()
closed(real)

# The link opening again onto another device: the simulator's TCP port
# closes, and a real aircraft's bridge (MavLTE) serves the same port.
port = free_port(socket.SOCK_STREAM)
sim, link, status, finished = rehearse(
    connection="tcp:127.0.0.1:%d" % port, port=port,
    channel=TcpTerrainChannel)
broke_at = len(status)
closed(sim)
real = start_plane(FakeTerrainPlane(), port=port, channel=TcpTerrainChannel)
note("(the link breaks, and opens again onto the other device)",
     wait_for(lambda: False in status[broke_at:]
              and status[-1] is True, 30.0), status[broke_at:])
time.sleep(2.0)
refused("the same TCP port, opened again", link, real[1], finished)
link.stop()
closed(real)

# Another device sending to the same listening UDP port.
sim, link, status, finished = rehearse()
closed(sim)
real = start_plane(FakeTerrainPlane(), port=sim[2])
note("(the link hears the other device)",
     wait_for(lambda: ("HEARTBEAT", None) in real[1].got, 10.0))
time.sleep(2.0)
refused("another device on the same UDP port", link, real[1], finished)
link.stop()
closed(real)

# And the simulator itself, started again on the same port: asked again,
# it says so again, and the rehearsal goes on without reconnecting.
sim, link, status, finished = rehearse()
closed(sim)
again = start_plane(FakeTerrainPlane(sim_speedup=1), port=sim[2])
note("the simulator started again is recognised again",
     wait_for(lambda: link.terrain_upload_blocker() is None, 20.0),
     link.terrain_upload_blocker())
link.stop()
closed(again)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
