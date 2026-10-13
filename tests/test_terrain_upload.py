"""Terrain Upload: the right ground, in the right place, to the right link.

What it must do, from the pilot (2026-10-12): draw an area, press Done,
and its terrain - with a margin - goes onto the aircraft's card. Over the
flight controller's own USB cable and nothing else, and never to an armed
aircraft.

What is checked here, against tests/fake_terrain.py - ArduPilot's terrain
code mirrored in Python, with time stepped rather than waited for:

1. Every point of a square sits where ArduPilot's own arithmetic puts it,
   and each square is named by the corner the aircraft names it by.
2. Sent and read back through the aircraft's own interpolation, a known
   ground comes back right - so nothing is turned, mirrored or shifted.
3. The squares found for an area cover all of it and its margin.
4. Only a flight controller's own USB counts as one.
5. The upload, start to end, with what can go wrong along the way: squares
   already on the card, Disable Download, no card, a small memory, a lost
   write, ground the map lacks, the aircraft armed or the cable no longer
   the link in use halfway through.
"""

import math
import os
import random
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("MAVLINK20", "1")

import numpy as np

import terrain_upload as T
from fake_terrain import FakeTerrainPlane
from terrain_provider import _TileData

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


# Near Ankara. Somewhere real, with a degree line close enough to use.
LAT0, LON0 = 39.92, 32.85


def planar(lats, lons):
    """A tilted plane: interpolation between grid points is exact on it,
    so what the aircraft reads back can be held to a metre."""
    return 900.0 + 2500.0 * (np.asarray(lats) - LAT0) + 1700.0 * (
        np.asarray(lons) - LON0)


def hills(lats, lons):
    lats, lons = np.asarray(lats), np.asarray(lons)
    return 1100.0 + 350.0 * np.sin(lats * 37.0) * np.cos(lons * 29.0)


class Tile:
    def __init__(self, fn):
        self.fn = fn

    def sample_many(self, lats, lons):
        return self.fn(lats, lons)


class Map:
    """Stands in for TerrainProvider: tile_for() as it answers."""

    def __init__(self, fn=planar, absent=(), failed=()):
        self.fn, self.absent, self.failed = fn, set(absent), set(failed)

    def tile_for(self, lat, lon):
        key = (math.floor(lat), math.floor(lon))
        if key in self.absent:
            return None, "absent"
        if key in self.failed:
            return None, "failed"
        return Tile(self.fn), "ok"


def box(lat, lon, half_km):
    d_lat = half_km / 111.32
    d_lon = half_km / (111.32 * math.cos(math.radians(lat)))
    return [(lat - d_lat, lon - d_lon), (lat - d_lat, lon + d_lon),
            (lat + d_lat, lon + d_lon), (lat + d_lat, lon - d_lon)]


# --------------------------------------------------------------------------
print("")
print("1. every point where ArduPilot puts it")
random.seed(7)
corner_bad = check_bad = point_bad = compared = 0
worst = 0.0
for _ in range(400):
    lat, lon = random.uniform(-60, 60), random.uniform(-179, 179)
    spacing = random.choice([30, 50, 100, 200])
    sq = T.square_at(int(lat * 1e7), int(lon * 1e7), spacing)[0]
    c = T.square_corner(sq, spacing)
    corner_bad += T.square_of_corner(c[0], c[1], spacing) != sq
    p = T.check_point(sq, spacing)
    check_bad += T.square_at(p[0], p[1], spacing)[0] != sq
    lats, lons = T.square_points(sq, spacing)
    for x in range(0, T.BLOCK_SIZE_X, 3):
        for y in range(0, T.BLOCK_SIZE_Y, 3):
            s2, ix, iy, fx, fy = T.square_at(int(round(lats[x][y] * 1e7)),
                                              int(round(lons[x][y] * 1e7)),
                                              spacing)
            if (s2.lat_deg, s2.lon_deg) != (sq.lat_deg, sq.lon_deg):
                continue        # past the degree's edge: the next degree's
            d = max(abs(s2.gx * 24 + ix + fx - (sq.gx * 24 + x)),
                    abs(s2.gy * 28 + iy + fy - (sq.gy * 28 + y)))
            worst = max(worst, d)
            compared += 1
            point_bad += d > 0.001
note("a square's corner names it as the aircraft names it", corner_bad == 0,
     "%d wrong" % corner_bad)
note("the point a square is asked about is in that square, even on a "
     "degree's edge", check_bad == 0, "%d wrong" % check_bad)
note("every point lands on its own grid step", point_bad == 0,
     "%d of %d off; worst %.5f of a step" % (point_bad, compared, worst))
# The last square of a row can be all but gone at its degree's east line:
# a strip metres wide, often tapering to nothing inside the square. A
# quarter step in was already the next degree, so the square was asked
# about there - and never loaded, but counted as already on the card
# (found 2026-10-13). Every row's last square, 60 S to 60 N.


def east_line(square, north_steps, spacing):
    """Steps east of its corner where a square's degree ends, north_steps
    north of that corner, by the aircraft's arithmetic (get_distance_NE
    scales by the latitude halfway to the point)."""
    lat, _lon = T._place(square, north_steps, 0, spacing)
    return (1e7 * T.LATLON_TO_M * T.longitude_scale(
        int((square.lat_deg * 1e7 + float(lat)) / 2.0))) / spacing


rows_seen = strip_bad = 0
for spacing in (30, 50, 100, 200):
    top = 1e7 * T.LATLON_TO_M / spacing
    for lat_deg in range(-60, 60):
        for gx in range(int(top // T.BLOCK_SPACING_X) + 1):
            n0 = gx * T.BLOCK_SPACING_X
            if n0 >= top:
                continue
            first = T.Square(lat_deg, 29, gx, 0)
            widest = max(east_line(first, n0 + c, spacing)
                         for c in (0, min(T.BLOCK_SPACING_X, top - n0)))
            sq = T.Square(lat_deg, 29, gx, int(widest // T.BLOCK_SPACING_Y))
            p = T.check_point(sq, spacing)
            rows_seen += 1
            strip_bad += p is None or T.square_at(p[0], p[1], spacing)[0] != sq
note("the last square of every row, however thin, is asked about inside it",
     strip_bad == 0, "%d wrong of %d" % (strip_bad, rows_seen))
# One worked by hand, so the mirror is not only consistent with itself:
# a square's corner is a whole number of square-widths from its degree's
# corner, and the first square is that corner.
sq0 = T.Square(39, 32, 0, 0)
note("the first square of a degree starts at the degree's corner",
     T.square_corner(sq0, 100) == (390000000, 320000000))
lat1, lon1 = T.square_corner(T.Square(39, 32, 1, 1), 100)
north, east = T.distance_ne(390000000, 320000000, lat1, lon1)
note("the next one is 2.4 km north and 2.8 km east of it",
     abs(north - 2400) < 0.05 and abs(east - 2800) < 0.05,
     "%.3f m, %.3f m" % (north, east))


# --------------------------------------------------------------------------
print("")
print("2. sent, stored and read back by the aircraft, the ground comes back")
plane = FakeTerrainPlane(spacing=100)
sq = T.square_at(int(LAT0 * 1e7), int(LON0 * 1e7), 100)[0]
heights, state = T.square_heights(Map(planar), sq, 100)
lat_c, lon_c = T.square_corner(sq, 100)
plane.height_amsl(*T.check_point(sq, 100))    # in its memory, as a check puts it
plane.update(); plane.update()                # the card read, empty
for bit in range(T.GRIDBITS):
    plane.terrain_data_send(lat_c, lon_c, 100, bit, T.gridbit_data(heights, bit))
errors = []
ref_lat, ref_lon = sq.lat_deg * 10_000_000, sq.lon_deg * 10_000_000
for _ in range(300):
    # Anywhere the aircraft files under this square: 0..24 steps north,
    # 0..28 east of its corner.
    n = (sq.gx * 24 + random.uniform(0, 23.99)) * 100
    e = (sq.gy * 28 + random.uniform(0, 27.99)) * 100
    lat, lon = T.offset(ref_lat, ref_lon, n, e)
    got = plane.height_amsl(lat, lon)
    want = float(planar(lat / 1e7, lon / 1e7))
    errors.append(None if got is None else got - want)
held = [e for e in errors if e is not None]
note("the map had this ground", state == "ok", state)
note("every place in the square has a height on the aircraft",
     len(held) == len(errors), "%d of %d" % (len(held), len(errors)))
note("within the metre the heights are rounded up to, never below",
     held and min(held) > -0.01 and max(held) < 1.01,
     "%.3f .. %.3f m" % (min(held), max(held)) if held else "")
# The same ground turned a quarter: if north and east were swapped, or a
# part landed in another's place, this is what the aircraft would read. On
# ground that rises one way and not the other, the point each square is
# checked at tells the two apart.
steep, _ = T.square_heights(Map(lambda la, lo: 900.0 + 20000.0 * (
    np.asarray(la) - LAT0) + 0.0 * np.asarray(lo)), sq, 100)
turned = steep.copy()
turned[:24, :24] = steep[:24, :24].T
note("(and a square sent turned would read back far off - the check can "
     "tell)", abs(T.interpolate(turned, 12, 14, 0.25, 0.75)
                  - T.interpolate(steep, 12, 14, 0.25, 0.75)) > 10)
note("the place each square is checked at reads back exactly as computed",
     abs(plane.height_amsl(*T.check_point(sq, 100))
         - T.expected_report(heights, sq, 100)) < 1e-3)


# --------------------------------------------------------------------------
print("")
print("3. the squares cover the area and its margin")


def coverage(poly, margin, samples, near=None):
    """(places in reach left out, squares loaded far beyond, how many)."""
    squares = set(T.squares_for_area(poly, 100, margin))
    to_xy, to_latlon = T._flat(poly)
    pts = [to_xy(a, b) for a, b in poly]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    missed = 0
    for _ in range(samples):
        if near is not None and random.random() < 0.7:
            # Mostly right beside a degree line, where a square can be a
            # strip a step or two wide.
            lat, lon = near(random)
            x, y = to_xy(lat, lon)
        else:
            x = random.uniform(min(xs) - margin, max(xs) + margin)
            y = random.uniform(min(ys) - margin, max(ys) + margin)
        inside = T._inside(pts, x, y)
        if not inside and T._edge_distance(pts, x, y) > margin - 10:
            continue
        lat, lon = to_latlon(x, y)
        if T.square_at(int(lat * 1e7), int(lon * 1e7), 100)[0] not in squares:
            missed += 1
    far = 0
    for sq in squares:
        lat, lon = T.check_point(sq, 100)
        x, y = to_xy(lat / 1e7, lon / 1e7)
        if not T._inside(pts, x, y) and T._edge_distance(pts, x, y) > (
                margin + 4300):
            far += 1
    return missed, far, len(squares)


poly = [(39.90, 32.80), (39.95, 32.83), (39.93, 32.90), (39.88, 32.87)]
missed, far, count = coverage(poly, 3000, 4000)
note("no place inside the area or its 3 km margin is left out", missed == 0,
     "%d missed" % missed)
note("and nothing far beyond it is loaded", far == 0,
     "%d of %d squares far off" % (far, count))
# Across the 40th parallel and the 33rd meridian: the last squares of the
# degrees to the south and west are strips there, the rest of each in the
# next degree.
corner = [(39.97, 32.96), (40.03, 32.97), (40.04, 33.05), (39.96, 33.04)]
missed, far, count = coverage(corner, 3000, 6000, near=lambda rnd: (
    rnd.choice([40.0, rnd.uniform(39.95, 40.05)]) + rnd.uniform(-0.0004, 0.0004),
    rnd.choice([33.0, rnd.uniform(32.95, 33.05)]) + rnd.uniform(-0.0004, 0.0004)))
note("where an area crosses a degree line, the strips there are not missed",
     missed == 0, "%d of the places checked missed, %d squares" % (missed, count))
note("ordered so each map tile is read once",
     list(T.squares_for_area(poly, 100, 3000))
     == sorted(T.squares_for_area(poly, 100, 3000)))


# --------------------------------------------------------------------------
print("")
print("4. only the flight controller's own USB")
port = lambda dev, vid, pid, man="": SimpleNamespace(
    device=dev, vid=vid, pid=pid, manufacturer=man)
ports = [port("COM7", 0x1209, 0x5741, "Microsoft"),          # SpeedyBee F405
         port("COM36", 0x0403, 0x6015, "FTDI"),               # an RFD
         port("COM12", 0x10C4, 0xEA60, "Silicon Labs"),       # a SiK radio
         port("COM5", 0x2DAE, 0x1016, "CubePilot"),
         port("COM9", 0x1234, 0x5678, "Somebody"),
         port("COM3", None, None, ""),
         port("/dev/cu.usbmodem1101", 0x1209, 0x5740, "ArduPilot")]
comports = lambda: ports
cases = [("COM7:115200", True), ("com7:57600", True), ("COM36:57600", False),
         ("COM12:57600", False), ("COM5:115200", True), ("COM9:115200", False),
         ("COM3:115200", False), ("COM99:115200", False),
         ("tcp:127.0.0.1:5760", False), ("udpin:0.0.0.0:14550", False),
         ("/dev/tty.usbmodem1101:115200", True)]
wrong = [(c, T.flight_controller_usb(c, comports)) for c, want in cases
         if T.flight_controller_usb(c, comports)[0] != want]
note("ArduPilot's own USB, by its id, on Windows and on a Mac", not wrong,
     wrong)
note("a radio says what it is", "FTDI" in T.flight_controller_usb(
    "COM36:57600", comports)[1])
note("a network link is never the USB",
     T.flight_controller_usb("tcp:127.0.0.1:5760", comports)
     == (False, "a network link"))


# --------------------------------------------------------------------------
print("")
print("5. the upload, start to end")


class Rig:
    """The link, as the upload sees it, joined to a FakeTerrainPlane."""

    def __init__(self, plane, polygon, usb=True, armed=False, world=None,
                 link_refuses=True, **kw):
        self.plane = plane
        self.t = 0.0
        self.usb = usb
        self.is_armed = armed
        self.link_refuses = link_refuses
        self.progress_seen = []
        self.notes = []
        self.done = None
        self.consumed = []
        self.session = T.TerrainUpload(
            polygon, self, provider_factory=lambda status: world or Map(),
            clock=lambda: self.t, threaded=False, **kw)

    def where(self):
        return ((True, "the flight controller's USB") if self.usb
                else (False, "COM36 (an FTDI radio)"))

    def armed(self):
        return self.is_armed

    def send_terrain(self, fn):
        if self.link_refuses and (not self.usb or self.is_armed):
            return False
        fn(self.plane, 1, 1)
        return True

    def send_setting(self, fn):
        if self.link_refuses and not self.usb:
            return False
        fn(self.plane, 1, 1)
        return True

    def progress(self, done, total, text):
        self.progress_seen.append((done, total, text))

    def note(self, text):
        self.notes.append(text)

    def finished(self, ok, text):
        self.done = (ok, text)

    def run(self, until=900.0, at=None):
        step = 0
        while self.t < until and self.done is None:
            step += 1
            self.t = step * 0.02
            self.plane.step(self.t)
            for kind, msg in self.plane.take():
                if kind == "PARAM_VALUE":
                    self.session.on_param(*msg)
                else:
                    self.consumed.append(self.session.on_message(msg, kind))
            if at:
                at(self)
            self.session.tick(self.t)
        return self.done

    def sent(self, kind, after=float("-inf")):
        return [w for k, w, t in self.plane.got if k == kind and t > after]


AREA = box(LAT0, LON0, 1.0)

r = Rig(FakeTerrainPlane(), AREA)
ok_text = r.run()
area_sq = r.session._usable
card = [r.plane.on_card(sq) for sq in area_sq]
note("done, on a plane with an empty card and no position fix",
     ok_text is not None and ok_text[0], ok_text)
note("every square of the area is on its card, whole",
     all(c is not None and c[0] == (1 << 56) - 1 for c in card),
     "%d squares" % len(area_sq))
world_heights = {sq: T.square_heights(Map(), sq, 100)[0] for sq in area_sq}
note("holding exactly the heights sent",
     all(np.array_equal(r.plane.on_card(sq)[1], world_heights[sq])
         for sq in area_sq))
note("its request rate was raised for the upload and put back",
     not r.plane.interval_set and any(
         w[0] == 511 and w[2] > 0 for w in r.sent("COMMAND_LONG")))
note("the answers to its own questions were not passed on as the "
     "aircraft's terrain", any(r.consumed) and r.consumed.count(False) >= 0)
note("the pilot was told how many squares and how long",
     any("squares of 2.4 x 2.8 km" in n for n in r.notes), r.notes)

# With a position fix, outside the area: the aircraft asks for its own
# square first, every time, until it has it.
away = (int((LAT0 + 0.3) * 1e7), int((LON0 + 0.3) * 1e7))
r = Rig(FakeTerrainPlane(position=away), AREA)
done = r.run()
own = T.square_at(away[0], away[1], 100)[0]
note("with a fix: done, though the aircraft asks for its own square first",
     done is not None and done[0], done)
note("its own square was answered, and said so",
     r.plane.on_card(own) is not None and "asked for itself" in done[1])
note("at the aircraft's pace - about 2 s a square",
     r.t < len(r.session._usable) * 2.0 + 60, "%.0f s for %d squares"
     % (r.t, len(r.session._usable)))

# Already on the card: left alone.
plane = FakeTerrainPlane()
keep = list(T.squares_for_area(AREA, 100, 3000))[:3]
for sq in keep:
    plane.disk[sq] = ((1 << 56) - 1, T.square_heights(Map(), sq, 100)[0])
r = Rig(plane, AREA)
done = r.run()
note("squares already on the card are counted, not sent again",
     done[0] and "3 already on the card" in done[1]
     and not any(w in keep for w in r.sent("TERRAIN_DATA")), done)

plane = FakeTerrainPlane()
for sq in keep:
    plane.disk[sq] = ((1 << 56) - 1,
                      T.square_heights(Map(), sq, 100)[0] + 20)
r = Rig(plane, AREA)
done = r.run()
note("and when they differ from the map, the pilot is told by how much",
     done[0] and "up to 20 m" in done[1] and "TERRAIN folder" in done[1],
     done)

# Disable Download: off for the upload, then back.
r = Rig(FakeTerrainPlane(options=1), AREA)
done = r.run()
note("Disable Download is switched off for the upload and back on after",
     done[0] and "is back on" in done[1]
     and int(r.plane.params["TERRAIN_OPTIONS"]) == 1, done)
note("the pilot was told before it was switched",
     any("Disable Download" in n for n in r.notes))

r = Rig(FakeTerrainPlane(options=2), AREA)
done = r.run()
note("Disable Disk: refused, nothing terrain sent",
     not done[0] and "Disable Disk" in done[1]
     and not r.sent("TERRAIN_CHECK") and not r.sent("TERRAIN_DATA"), done)

r = Rig(FakeTerrainPlane(enable=0), AREA)
done = r.run()
note("terrain switched off on the aircraft: refused, nothing sent",
     not done[0] and "TERRAIN_ENABLE" in done[1]
     and not r.sent("TERRAIN_CHECK"), done)

# The aircraft armed halfway. The link would refuse anyway - here it is
# told not to, so this is the upload's own guard being tested.
def arm_at(t):
    def at(rig):
        if rig.t >= t:
            rig.is_armed = True
    return at


r = Rig(FakeTerrainPlane(position=away, options=1), AREA,
        link_refuses=False)
done = r.run(at=arm_at(8.0))
after = r.sent("TERRAIN_DATA", 8.0) + r.sent("TERRAIN_CHECK", 8.0)
note("armed halfway: stopped, and not one terrain message after",
     done and not done[0] and "armed" in done[1] and not after,
     "%d after; %s" % (len(after), done))
note("but what it had changed was put back",
     int(r.plane.params["TERRAIN_OPTIONS"]) == 1 and "back on" in done[1])


def unplug_at(t):
    def at(rig):
        if rig.t >= t:
            rig.usb = False
    return at


r = Rig(FakeTerrainPlane(position=away, options=1), AREA,
        link_refuses=False)
done = r.run(at=unplug_at(8.0))
after = (r.sent("TERRAIN_DATA", 8.0) + r.sent("TERRAIN_CHECK", 8.0)
         + r.sent("PARAM_SET", 8.0))
note("the cable no longer the link in use: stopped, and nothing more sent "
     "on any link", done and not done[0] and not after,
     "%d after; %s" % (len(after), done))
note("and the pilot is told what could not be put back",
     "could NOT be put back" in done[1], done)

r = Rig(FakeTerrainPlane(), AREA, usb=False, link_refuses=False)
done = r.run()
note("never the USB at all: nothing whatever sent",
     done and not done[0] and not r.plane.got, (r.plane.got[:3], done))

r = Rig(FakeTerrainPlane(), AREA, armed=True, link_refuses=False)
done = r.run()
note("armed from the start: nothing whatever sent",
     done and not done[0] and not r.plane.got, (r.plane.got[:3], done))

# The pilot's Stop.
def stop_at(t):
    def at(rig):
        if rig.t >= t and not rig.session.ended:
            rig.session.stop("Stop was pressed")
    return at


r = Rig(FakeTerrainPlane(options=1), AREA)
done = r.run(at=stop_at(1.5))
note("Stop: ends at once, puts things back, says how far it got",
     done and not done[0] and "Stop was pressed" in done[1]
     and "press Terrain Upload again" in done[1]
     and int(r.plane.params["TERRAIN_OPTIONS"]) == 1, done)

# The link closing mid-upload (Disconnect, the window shut): no time to
# hear answers, so what puts the aircraft back is sent and said unconfirmed.
def close_at(t):
    def at(rig):
        if rig.t >= t and not rig.session.ended:
            rig.session.close("the link was closed")
    return at


r = Rig(FakeTerrainPlane(options=1), AREA)
done = r.run(at=close_at(1.5))
note("the link closing: ends at once, sends what puts the aircraft back, "
     "and says it is unconfirmed",
     done and not done[0] and "unconfirmed" in done[1]
     and int(r.plane.params["TERRAIN_OPTIONS"]) == 1, done)

# No card: the aircraft never finishes reading it, so it never asks - as a
# real ArduPlane 4.7.1 with no card did (2026-10-13): every square held
# waiting for the card, nothing asked for. Said within seconds, not after
# every square has timed out (that took over ten minutes on "Loading").
r = Rig(FakeTerrainPlane(card=False), AREA)
done = r.run(until=3000.0)
note("no card: said within seconds, asking whether a card is in",
     done and not done[0] and "is there a card in it" in done[1]
     and r.t < 30.0, "%.0f s: %s" % (r.t, done))
note("and with a position fix too",
     Rig(FakeTerrainPlane(card=False, position=(
         int(LAT0 * 1e7), int(LON0 * 1e7))), AREA).run(until=3000.0)[1]
     .count("is there a card in it") == 1)

# A small memory, and a write the card lost once. Loaded early, that square
# is pushed out of the aircraft's memory long before the check, so the
# check finds it gone - and loads it once more rather than call the card
# broken.
plane = FakeTerrainPlane(cache_size=6)
big = box(LAT0, LON0, 6.0)
first = T.squares_for_area(big, 100, 3000)[0]
plane.drop_writes = {first}
r = Rig(plane, big)
done = r.run(until=3000.0)
note("a small memory and a write lost once: found, loaded again, all kept",
     done and done[0] and r.session._reload_round == 1
     and all(r.plane.on_card(sq) for sq in r.session._usable),
     (r.session._reload_round, done))

plane = FakeTerrainPlane(cache_size=6)
plane.always_drop = {first}
r = Rig(plane, big)
done = r.run(until=3000.0)
note("a card that never keeps a square: said plainly, not passed",
     done and not done[0] and "1 NOT kept" in done[1], done)

# Ground the map does not have: a degree of it missing.
edge_area = box(LAT0, 33.0, 2.0)            # across the 33rd meridian
r = Rig(FakeTerrainPlane(), edge_area, world=Map(absent={(39, 33)}))
done = r.run()
nomap = [sq for sq, res in r.session._result.items() if res == "nomap"]
note("ground the map lacks is left out and said so, never made up",
     done and done[0] and nomap and "no map data there" in done[1]
     and not any(sq in nomap for sq in r.sent("TERRAIN_DATA")), done)

r = Rig(FakeTerrainPlane(), AREA, world=Map(failed={(39, 32)}))
done = r.run()
note("the map not downloadable: refused, nothing sent",
     done and not done[0] and "could not be downloaded" in done[1]
     and not r.sent("TERRAIN_DATA"), done)

# The aircraft's own correction from arming: one offset on every height.
r = Rig(FakeTerrainPlane(offset=7.0), AREA)
done = r.run()
note("a height correction from arming is allowed for, and said",
     done and done[0] and "+7.0 m" in done[1], done)

# Between batches, the card's turn: a batch loaded, then nothing new until
# the aircraft has written it. A new square to ask about is a read, reads go
# before writes, and the next new square can push a finished one out of the
# aircraft's memory unwritten. The pause only ever began after a timeout,
# never after a batch answered normally (found 2026-10-13): over an area of
# about ninety squares the card then lost some for good.
events = []                         # ("batch" or "pause", reload round)
_tick_loading = T.TerrainUpload._tick_loading


def counted(self, now):
    was_empty, was_settling = not self._in_flight, self._settling_since
    _tick_loading(self, now)
    if was_settling is None and self._settling_since is not None:
        events.append(("pause", self._reload_round))
    if was_empty and self._in_flight:
        events.append(("batch", self._reload_round))


T.TerrainUpload._tick_loading = counted
try:
    r = Rig(FakeTerrainPlane(), box(LAT0, LON0, 8.0))
    done = r.run(until=3000.0)
finally:
    T.TerrainUpload._tick_loading = _tick_loading
whole = [sq for sq in r.session._usable if r.plane.on_card(sq) is not None
         and r.plane.on_card(sq)[0] == (1 << 56) - 1]
unpaused = sum(1 for a, b in zip(events, events[1:])
               if a == b and a[0] == "batch")
note("the card gets its turn between one batch and the next",
     sum(e[0] == "batch" for e in events) > 2 and unpaused == 0,
     "%d batches, %d pauses, %d started with no pause before them"
     % (sum(e[0] == "batch" for e in events),
        sum(e[0] == "pause" for e in events), unpaused))
note("so a large area is kept whole, every square of it",
     done and done[0] and len(whole) == len(r.session._usable),
     "%d of %d; %s" % (len(whole), len(r.session._usable), done))
# And checked as it is. Checked one square after another, new ones went
# ahead of squares still waiting to be read from the card, three of them
# timed out as "lost", were found on the card when loaded again - and the
# summary said "3 already on the card" for a card that was empty (found
# 2026-10-13).
note("onto an empty card, nothing is said to be on it already, and nothing "
     "had to be loaded twice",
     done and "already on the card" not in done[1]
     and r.session._reload_round == 0,
     "reload rounds %d; %s" % (r.session._reload_round, done))


class DemMap:
    """The map's own tiles (terrain_provider._TileData): one degree each,
    chosen by the floor, PixelIsPoint from the north-west corner - only
    coarser than the real ones, at 30 arcseconds. Map above never refuses
    a point; these refuse what a real tile refuses."""

    N = 120

    def __init__(self, fn=planar):
        self.fn, self.tiles = fn, {}

    def tile_for(self, lat, lon):
        key = (math.floor(lat), math.floor(lon))
        if key not in self.tiles:
            px = 1.0 / self.N
            rows = (key[0] + 1) - px * np.arange(self.N)
            cols = key[1] + px * np.arange(self.N)
            grid = self.fn(rows[:, None] + 0 * cols[None, :],
                           cols[None, :] + 0 * rows[:, None])
            self.tiles[key] = _TileData(grid.astype(np.float32),
                                        key[0] + 1, key[1], px, px)
        return self.tiles[key], "ok"


# The first row of every square at the bottom of a degree lies exactly on
# its whole latitude. With the aircraft parked in that band - 1.1 km north
# of the 40th parallel - and an area across the line, everything loads
# (found 2026-10-13: those squares were refused as having no map, and the
# aircraft, asking for its own square first, then took nothing at all).
band = (int(40.01 * 1e7), int(LON0 * 1e7))
own = T.square_at(band[0], band[1], 100)[0]
r = Rig(FakeTerrainPlane(position=band), box(40.01, LON0, 1.0),
        world=DemMap())
done = r.run()
lowest = [sq for sq in r.session._squares if sq.lat_deg == 40 and sq.gx == 0]
note("(the aircraft is parked in the first row of squares of its degree)",
     own.gx == 0 and own.lat_deg == 40, own)
note("parked just north of a whole degree of latitude: done, all of it",
     done is not None and done[0] and "no map" not in done[1], done)
note("the squares at the bottom of the degree are on the card, whole",
     lowest and all(r.plane.on_card(sq) is not None
                    and r.plane.on_card(sq)[0] == (1 << 56) - 1
                    for sq in lowest), "%d squares" % len(lowest))

# Ground near the aircraft that the map has no data for: the tile south of
# the 40th parallel missing (sea, or ground the map leaves out), the
# aircraft parked 1.1 km north of it with a position fix. It holds the
# squares round itself in memory and asks for the first one missing, the
# same one, for ever - so squares after it are never taken. Found on
# ArduPilot's simulator (2026-10-13): two of twenty never loaded, after
# four minutes, and the pilot was asked whether the SD card was in.
r = Rig(FakeTerrainPlane(position=band), box(40.04, LON0, 1.0),
        world=Map(planar, absent={(39, math.floor(LON0))}))
done = r.run(until=3000.0)
note("map-less ground beside the aircraft: said plainly, not the SD card",
     done and not done[0] and "keeps asking first for ground near itself"
     in done[1] and "SD card" not in done[1], done)
note("said while it happens, too",
     any("squares are coming slowly" in n for n in r.notes), r.notes[-1:])
taken = r.session.counts().get("loaded", 0)
note("most of it loaded anyway, and checked, within minutes",
     done and r.t < 240 and taken * 2 > len(r.session._usable),
     "%.0f s, %s" % (r.t, r.session.counts()))

# Across the 30th meridian at 36.8 N, where the last squares of two rows
# west of it are such strips, onto an empty card: all of it loaded, and
# nothing said to be on the card already.
r = Rig(FakeTerrainPlane(), box(36.785, 30.0, 2.5), world=Map(hills))
done = r.run()
strips = [sq for sq in r.session._usable
          if sq.lon_deg == 29 and sq.gy == 32 and sq.gx in (36, 37)]
note("across a meridian with strips at its edge: done, none 'already there'",
     done and done[0] and "already on the card" not in done[1], done)
note("and the strips are on the card, whole",
     len(strips) == 2 and all(r.plane.on_card(sq) is not None
                              and r.plane.on_card(sq)[0] == (1 << 56) - 1
                              for sq in strips), strips)

r = Rig(FakeTerrainPlane(), box(LAT0, LON0, 30.0), max_squares=50)
done = r.run()
note("too big an area: refused before anything is sent",
     done and not done[0] and "Draw a smaller area" in done[1]
     and not r.sent("TERRAIN_CHECK"), done)

plane = FakeTerrainPlane()
plane.drop_echo_every = 2
r = Rig(plane, AREA)
done = r.run()
note("half the settings' answers lost: asked again, done",
     done and done[0], done)

r = Rig(FakeTerrainPlane(position=away), AREA, world=Map(hills))
done = r.run()
note("on hills, with a fix: done, every square read back as sent",
     done and done[0] and "DIFFERENT" not in done[1], done)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
