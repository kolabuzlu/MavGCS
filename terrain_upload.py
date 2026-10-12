"""
Terrain Upload: the terrain of an area the pilot draws, put onto the
aircraft's SD card - over the flight controller's own USB cable only, only
while it is disarmed, and only when the pilot asks.

Why this is a rule and not a convenience
----------------------------------------
ArduPilot with its terrain system on asks every ground station it can hear
for the terrain it lacks, in the air as on the ground. MAVProxy answers by
default, and Mission Planner does too. The pilot of this app plans missions
in the air and does not want terrain arriving at a flying aircraft
(2026-10-12), so MavGCS answers none of it - except here, in an upload the
pilot starts on the bench, over the cable. This is the one module of the
app that may send terrain, and tests/test_no_terrain_upload.py holds it to
that and to its conditions.

ArduPilot's simulator, SITL, counts as the cable does: it talks over the
network but cannot fly, and the pilot rehearses with it. It is recognised
by a setting only the simulator has - see
MavlinkLink._request_simulator_check - so a real aircraft on a radio, or on
LTE arriving at 127.0.0.1, is still refused.

How it loads
------------
ArduPilot's own terrain protocol, not a file transfer. The aircraft writes
its own files in its own format, so nothing here has to reproduce that
format, and a square goes onto the card through the same code that would
put it there in flight.

1. Every square of ArduPilot's terrain grid that the area touches, plus a
   margin all round, is asked about with TERRAIN_CHECK. A square is 28 x 32
   points: 2.7 x 3.2 km at the default 100 m spacing.
2. The aircraft looks on its card. A square it does not have, it asks for
   with TERRAIN_REQUEST; the answer is TERRAIN_DATA from the Copernicus map
   the app already draws its terrain from. A square that is already there
   is left as it is - the aircraft does not ask for it.
3. When every square is in, each is asked about again, and the height the
   aircraft reports is compared with the height that was sent.

The aircraft also asks for squares outside the area - where it is sitting,
its home, its stored mission - and asks for those first: while one of them
is missing it asks for nothing else. So those are answered too, while the
upload runs, and counted separately.

Where a point of a square is
----------------------------
Exactly where ArduPilot takes it to be. It finds a height by measuring from
the whole degree the point is in, with Location::get_distance_NE, and
reading the 4 points around the result; so point (x, y) of a square is
wherever that measurement gives (x, y) whole steps. Mission Planner
measures each point from the square's own corner instead, which puts the
far side of a degree several metres away from where the aircraft will read
it. Here the plane's own arithmetic is mirrored, and the check in step 3
proves it on the aircraft.
"""

import math
import re
import threading
import time
from collections import deque, namedtuple

import numpy as np
from pymavlink import mavutil


# --------------------------------------------------------------------------
# ArduPilot's terrain grid: libraries/AP_Terrain/AP_Terrain.h

GRID_MAVLINK_SIZE = 4                       # one TERRAIN_DATA is 4 x 4 points
GRID_BLOCK_MUL_X = 7
GRID_BLOCK_MUL_Y = 8
# A square starts every 24 x 28 points and is 28 x 32 points big: each
# overlaps the next by a row, so any point's 4 neighbours are in one square.
BLOCK_SPACING_X = (GRID_BLOCK_MUL_X - 1) * GRID_MAVLINK_SIZE    # 24, north
BLOCK_SPACING_Y = (GRID_BLOCK_MUL_Y - 1) * GRID_MAVLINK_SIZE    # 28, east
BLOCK_SIZE_X = GRID_MAVLINK_SIZE * GRID_BLOCK_MUL_X             # 28
BLOCK_SIZE_Y = GRID_MAVLINK_SIZE * GRID_BLOCK_MUL_Y             # 32
GRIDBITS = GRID_BLOCK_MUL_X * GRID_BLOCK_MUL_Y                  # 56 parts

# Location's scaling as ArduPilot holds it: LATLON_TO_M is a double in
# AP_Math, but Location keeps it as a constexpr float, and that float is
# what every distance and offset is computed with.
LATLON_TO_M = 0.011131884530186653
LATLON_TO_M_INV = 89.83204650878906

# The margin all round the drawn area. More than the 2 km ArduPlane looks
# ahead when following terrain (TERRAIN_LOOKAHD), and about one square.
MARGIN_M = 3000.0
# About ninety minutes of loading at the aircraft's pace. An area bigger
# than this was almost certainly not meant.
MAX_SQUARES = 2500
# The aircraft asks for one square every 2 s while it knows where it is
# (AP_Terrain::send_request), which is what an estimate has to assume.
SECONDS_PER_SQUARE = 2.0

# Where a square is asked about, in grid steps into it. Not its middle: at
# a point a quarter of the way one way and three quarters the other, the
# four heights around it count differently, so a square that arrived
# turned or mirrored reads differently from one that arrived right.
CHECK_AT = (12.25, 14.75)

# What the link hands over to the upload. The upload also reads PARAM_VALUE,
# which the link passes on by name.
HEARD = frozenset({"TERRAIN_REQUEST", "TERRAIN_REPORT"})

Square = namedtuple("Square", "lat_deg lon_deg gx gy")


def _c_div(a, b):
    """C's integer division, which truncates toward zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def whole_degree(v_e7):
    """The whole degree ArduPilot files a coordinate under: the floor."""
    return _c_div(v_e7 - 9999999 if v_e7 < 0 else v_e7, 10_000_000)


def longitude_scale(lat_e7):
    """Location::longitude_scale."""
    return max(math.cos(lat_e7 * (1.0e-7 * math.pi / 180.0)), 0.01)


def _diff_longitude(lon1, lon2):
    d = lon1 - lon2
    if d > 1800000000:
        d -= 3600000000
    elif d < -1800000000:
        d += 3600000000
    return d


def _wrap_longitude(lon):
    if lon > 1800000000:
        lon -= 3600000000
    elif lon < -1800000000:
        lon += 3600000000
    return lon


def distance_ne(lat1, lon1, lat2, lon2):
    """Location::get_distance_NE from point 1 to point 2, in metres north
    and east. Coordinates in degrees x 1e7, as ArduPilot holds them."""
    return ((lat2 - lat1) * LATLON_TO_M,
            _diff_longitude(lon2, lon1) * LATLON_TO_M
            * longitude_scale(_c_div(lat2 + lat1, 2)))


def offset(lat, lon, north, east):
    """Location::offset_latlng: the point north and east metres away."""
    dlat = int(north * LATLON_TO_M_INV)
    dlng = int((east * LATLON_TO_M_INV)
               / longitude_scale(lat + _c_div(dlat, 2)))
    return lat + dlat, _wrap_longitude(lon + dlng)


def square_at(lat_e7, lon_e7, spacing):
    """AP_Terrain::calculate_grid_info: the square a point is filed under,
    and where in it - (square, ix, iy, fx, fy), ix/iy the grid step below
    the point and fx/fy how far on towards the next."""
    lat_deg, lon_deg = whole_degree(lat_e7), whole_degree(lon_e7)
    north, east = distance_ne(lat_deg * 10_000_000, lon_deg * 10_000_000,
                              lat_e7, lon_e7)
    idx_x = int(north / spacing)
    idx_y = int(east / spacing)
    square = Square(lat_deg, lon_deg,
                    idx_x // BLOCK_SPACING_X, idx_y // BLOCK_SPACING_Y)
    return (square, idx_x % BLOCK_SPACING_X, idx_y % BLOCK_SPACING_Y,
            (north - idx_x * spacing) / spacing,
            (east - idx_y * spacing) / spacing)


def square_corner(square, spacing):
    """The south-west corner a square is named by in TERRAIN_REQUEST and
    TERRAIN_DATA, worked out as the aircraft works it out."""
    return offset(square.lat_deg * 10_000_000, square.lon_deg * 10_000_000,
                  square.gx * BLOCK_SPACING_X * float(spacing),
                  square.gy * BLOCK_SPACING_Y * float(spacing))


def square_of_corner(lat_e7, lon_e7, spacing):
    """The square a TERRAIN_REQUEST names, from the corner it gives."""
    lat_deg, lon_deg = whole_degree(lat_e7), whole_degree(lon_e7)
    north, east = distance_ne(lat_deg * 10_000_000, lon_deg * 10_000_000,
                              lat_e7, lon_e7)
    return Square(lat_deg, lon_deg,
                  int(round(north / (BLOCK_SPACING_X * spacing))),
                  int(round(east / (BLOCK_SPACING_Y * spacing))))


def _place(square, north_steps, east_steps, spacing):
    """Degrees x 1e7 (as floats) of the point that many grid steps from the
    square's degree corner - where get_distance_NE from that corner comes
    out at exactly those steps. Works on numpy arrays as well as numbers."""
    ref_lat = square.lat_deg * 1e7
    ref_lon = square.lon_deg * 1e7
    lat = ref_lat + (np.asarray(north_steps, dtype=np.float64)
                     * spacing) / LATLON_TO_M
    scale = np.maximum(np.cos((ref_lat + lat) / 2.0
                              * (1.0e-7 * math.pi / 180.0)), 0.01)
    lon = ref_lon + (np.asarray(east_steps, dtype=np.float64)
                     * spacing) / (LATLON_TO_M * scale)
    return lat, lon


def square_points(square, spacing):
    """Latitude and longitude, in degrees, of a square's 28 x 32 points:
    two arrays indexed [x][y], x counting north and y east."""
    xs = square.gx * BLOCK_SPACING_X + np.arange(BLOCK_SIZE_X)
    ys = square.gy * BLOCK_SPACING_Y + np.arange(BLOCK_SIZE_Y)
    lat, lon = _place(square, xs[:, None] + 0 * ys[None, :],
                      ys[None, :] + 0 * xs[:, None], spacing)
    return lat / 1e7, lon / 1e7


def check_point(square, spacing):
    """Where a square is asked about: (lat, lon) in degrees x 1e7.

    CHECK_AT where the square has room for it. The last square of a
    degree, north or east, is mostly in the next degree - the aircraft
    files a point there under that degree's squares, not this one - so
    for those the point moves back towards the corner until it is a point
    of this square again, keeping the quarter-step offsets.
    """
    point = None
    for cx in (CHECK_AT[0], 6.25, 3.25, 1.25, 0.25):
        for cy in (CHECK_AT[1], 7.75, 3.75, 1.75, 0.75, 0.25):
            lat, lon = _place(square, square.gx * BLOCK_SPACING_X + cx,
                              square.gy * BLOCK_SPACING_Y + cy, spacing)
            point = int(round(float(lat))), int(round(float(lon)))
            if square_at(point[0], point[1], spacing)[0] == square:
                return point
    return point


def interpolate(heights, ix, iy, fx, fy):
    """AP_Terrain::height_amsl's interpolation between the four points
    around a place - the height the aircraft will report for it."""
    h00 = float(heights[ix][iy])
    h01 = float(heights[ix][iy + 1])
    h10 = float(heights[ix + 1][iy])
    h11 = float(heights[ix + 1][iy + 1])
    avg1 = (1.0 - fx) * h00 + fx * h10
    avg2 = (1.0 - fx) * h01 + fx * h11
    return (1.0 - fy) * avg1 + fy * avg2


def expected_report(heights, square, spacing):
    """The terrain height the aircraft should report at check_point once it
    holds these heights for the square."""
    lat, lon = check_point(square, spacing)
    _sq, ix, iy, fx, fy = square_at(lat, lon, spacing)
    return interpolate(heights, ix, iy, fx, fy)


def gridbit_data(heights, gridbit):
    """The sixteen heights of one TERRAIN_DATA, in the order the aircraft
    stores them (AP_Terrain::handle_terrain_data: part gridbit starts at
    row (gridbit / 8) * 4 north and column (gridbit % 8) * 4 east, and
    data[n * 4 + e] is the point n north and e east of that)."""
    x0 = (gridbit // GRID_BLOCK_MUL_Y) * GRID_MAVLINK_SIZE
    y0 = (gridbit % GRID_BLOCK_MUL_Y) * GRID_MAVLINK_SIZE
    return [int(heights[x0 + n][y0 + e])
            for n in range(GRID_MAVLINK_SIZE)
            for e in range(GRID_MAVLINK_SIZE)]


# --------------------------------------------------------------------------
# The area

def _flat(polygon):
    """A flat frame around the polygon: metres east and north of its middle.
    Ample for an area a pilot draws; nothing here needs better than a
    hundred metres, and it is only used to decide which squares to load."""
    lat0 = sum(p[0] for p in polygon) / len(polygon)
    lon0 = sum(p[1] for p in polygon) / len(polygon)
    ky = 111319.49
    kx = ky * max(math.cos(math.radians(lat0)), 0.01)

    def to_xy(lat, lon):
        return (lon - lon0) * kx, (lat - lat0) * ky

    def to_latlon(x, y):
        return lat0 + y / ky, lon0 + x / kx

    return to_xy, to_latlon


def _inside(pts, x, y):
    """Point in polygon, by ray casting."""
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _edge_distance(pts, x, y):
    """How far a point is from the polygon's outline."""
    best = float("inf")
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(
            0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
        px, py = x1 + t * dx - x, y1 + t * dy - y
        best = min(best, math.hypot(px, py))
    return best


def _segment_distance(a, b, c, d):
    """How far apart two line segments are."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2 = cross(a, b, c), cross(a, b, d)
    d3, d4 = cross(c, d, a), cross(c, d, b)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
        return 0.0
    return min(_edge_distance([c, d], a[0], a[1]),
               _edge_distance([c, d], b[0], b[1]),
               _edge_distance([a, b], c[0], c[1]),
               _edge_distance([a, b], d[0], d[1]))


def _within(outline, pts, margin_m):
    """Does the outline come within margin_m of the polygon?"""
    if any(_inside(pts, x, y) for x, y in outline):
        return True
    if any(_inside(outline, x, y) for x, y in pts):
        return True
    for i in range(len(outline)):
        a, b = outline[i], outline[(i + 1) % len(outline)]
        for j in range(len(pts)):
            if _segment_distance(a, b, pts[j],
                                 pts[(j + 1) % len(pts)]) <= margin_m:
                return True
    return False


def squares_for_area(polygon, spacing, margin_m=MARGIN_M):
    """Every square the area comes within margin_m of, grouped by whole
    degree so each map tile is read once.

    Worked out square by square rather than by sampling places: the last
    square of a degree can be a strip a few hundred metres wide - the rest
    of it lies in the next degree, whose own squares the aircraft uses
    there - and sampling at any spacing can step over a strip like that,
    leaving ground inside the area with no terrain on the aircraft. So
    every square in reach is taken in turn, outlined as the part of the
    ground the aircraft files under it, and kept if that outline comes
    within the margin of the area.
    """
    to_xy, to_latlon = _flat(polygon)
    pts = [to_xy(lat, lon) for lat, lon in polygon]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = margin_m + max(BLOCK_SPACING_X, BLOCK_SPACING_Y) * spacing
    lat_lo, lon_lo = to_latlon(min(xs) - pad, min(ys) - pad)
    lat_hi, lon_hi = to_latlon(max(xs) + pad, max(ys) + pad)
    rows_per_degree = 1e7 * LATLON_TO_M / spacing
    found = []
    for lat_deg in range(math.floor(lat_lo), math.floor(lat_hi) + 1):
        band_lo = max(lat_lo, float(lat_deg))
        band_hi = min(lat_hi, lat_deg + 1.0)
        if band_hi < band_lo:
            continue
        # A degree is narrower towards the pole: the columns in reach are
        # bounded with the widest and the narrowest it gets in this band.
        cosines = [math.cos(math.radians(v)) for v in (band_lo, band_hi)]
        if band_lo < 0.0 < band_hi:
            cosines.append(1.0)
        wide, narrow = max(cosines), max(min(cosines), 0.01)
        n_lo = (band_lo - lat_deg) * rows_per_degree
        n_hi = (band_hi - lat_deg) * rows_per_degree
        for lon_deg in range(math.floor(lon_lo), math.floor(lon_hi) + 1):
            e_lo = max(0.0, (lon_lo - lon_deg) * rows_per_degree * narrow)
            e_hi = min(rows_per_degree * wide,
                       (lon_hi - lon_deg) * rows_per_degree * wide)
            if e_hi < e_lo:
                continue
            for gx in range(int(n_lo // BLOCK_SPACING_X),
                            int(n_hi // BLOCK_SPACING_X) + 1):
                n0 = gx * BLOCK_SPACING_X
                n1 = min(n0 + BLOCK_SPACING_X, rows_per_degree)
                if n1 <= n0:
                    continue
                for gy in range(int(e_lo // BLOCK_SPACING_Y),
                                int(e_hi // BLOCK_SPACING_Y) + 2):
                    square = Square(lat_deg, lon_deg, gx, gy)
                    outline = _outline(square, n0, n1, spacing, to_xy)
                    if outline and _within(outline, pts, margin_m):
                        found.append(square)
    return sorted(set(found))


def _outline(square, n0, n1, spacing, to_xy):
    """The corners, in the flat frame, of the ground the aircraft files
    under this square: its own 24 x 28 steps, cut off where its degree
    ends. None when its degree ends before it begins."""
    e0 = square.gy * BLOCK_SPACING_Y
    corners, widest = [], 0.0
    for n, east_side in ((n0, False), (n0, True), (n1, True), (n1, False)):
        lat, _lon = _place(square, n, 0, spacing)
        # Where this degree ends to the east, at this latitude, in steps.
        edge = (1e7 * LATLON_TO_M * longitude_scale(
            int((square.lat_deg * 1e7 + float(lat)) / 2.0))) / spacing
        e1 = min(e0 + BLOCK_SPACING_Y, edge)
        widest = max(widest, e1 - e0)
        lat, lon = _place(square, n, max(e1, e0) if east_side else e0,
                          spacing)
        corners.append(to_xy(float(lat) / 1e7, float(lon) / 1e7))
    return corners if widest > 0 else None


def describe_minutes(seconds):
    minutes = int(math.ceil(seconds / 60.0))
    return "about a minute" if minutes <= 1 else "about %d minutes" % minutes


# --------------------------------------------------------------------------
# Which link is the flight controller's own USB

# What a flight controller's own USB port calls itself. ArduPilot's boards
# use 1209:5740 or 1209:5741 unless the maker sets their own - the user's
# SpeedyBee F405 Wing does not - and the makers below use their own vendor
# number for nothing but flight controllers.
_FC_USB_IDS = {(0x1209, 0x5740), (0x1209, 0x5741)}
_FC_USB_VENDORS = {0x2DAE: "CubePilot", 0x3162: "Holybro",
                   0x26AC: "3D Robotics", 0x3163: "CUAV"}
# USB-to-serial chips. Telemetry radios and plain cables are built on these;
# a flight controller's own port never is.
_BRIDGE_VENDORS = {0x0403: "FTDI", 0x10C4: "Silicon Labs", 0x1A86: "WCH",
                   0x067B: "Prolific"}

_NETWORK_PREFIXES = ("tcp:", "tcpin:", "udp:", "udpin:", "udpout:",
                     "udpbcast:", "mcast:", "ws:", "wss:", "wsserver:")


def classify_usb_port(vid, pid, manufacturer=""):
    """(is it a flight controller's own USB port, what it is), from the
    USB identity of one serial port. Anything not known to be a flight
    controller is not one: the upload is refused rather than guessed at."""
    if vid is None:
        return False, "not a USB port"
    if (vid, pid) in _FC_USB_IDS:
        return True, "the flight controller's USB"
    if vid in _FC_USB_VENDORS:
        return True, "the flight controller's USB (%s)" % _FC_USB_VENDORS[vid]
    if (manufacturer or "").strip().lower() == "ardupilot":
        return True, "the flight controller's USB"
    if vid in _BRIDGE_VENDORS:
        return False, ("a radio or a cable on a USB-serial adapter (%s)"
                       % _BRIDGE_VENDORS[vid])
    return False, "a USB device MavGCS does not know (%04X:%04X)" % (
        vid, pid or 0)


def serial_device(connection_string):
    """The serial device a connection string opens, or None for a network
    link."""
    s = (connection_string or "").strip()
    if s.lower().startswith(_NETWORK_PREFIXES):
        return None
    device, _, baud = s.rpartition(":")
    if device and baud.isdigit():
        return device
    return s or None


def _same_device(a, b):
    def norm(d):
        d = d.strip().lower().replace("\\\\.\\", "")
        # macOS lists a port as /dev/cu.X; a link may have opened /dev/tty.X.
        return d.replace("/dev/tty.", "/dev/cu.")
    return norm(a) == norm(b)


def flight_controller_usb(connection_string, comports=None):
    """(True, what it is) when this link is a flight controller's own USB
    port; (False, what it is instead) when it is anything else."""
    device = serial_device(connection_string)
    if device is None:
        return False, "a network link"
    try:
        if comports is None:
            import serial.tools.list_ports
            comports = serial.tools.list_ports.comports
        ports = list(comports())
    except Exception:
        return False, "a port whose USB identity could not be read"
    for p in ports:
        if _same_device(p.device, device):
            return classify_usb_port(getattr(p, "vid", None),
                                     getattr(p, "pid", None),
                                     getattr(p, "manufacturer", "") or "")
    return False, "a port that is not plugged in"


# --------------------------------------------------------------------------
# Heights from the map

def square_heights(provider, square, spacing):
    """(the square's heights as int16 [28][32], "ok"), or (None, why).

    Rounded up to the whole metre the aircraft stores: what it is told is
    never lower than the map. Never a made-up height - a square with any
    point the map has no ground for is not loaded at all, because the
    aircraft would take a guess for the truth. "absent" is ground the
    dataset does not have (sea, or a few withheld areas); "failed" is a
    tile that could not be fetched just now.
    """
    lats, lons = square_points(square, spacing)
    out = np.full(lats.shape, np.nan)
    tlat = np.floor(lats).astype(np.int64)
    tlon = np.floor(lons).astype(np.int64)
    for a, b in sorted(set(zip(tlat.ravel().tolist(), tlon.ravel().tolist()))):
        here = (tlat == a) & (tlon == b)
        tile, state = provider.tile_for(a + 0.5, b + 0.5)
        if tile is None:
            return None, state
        out[here] = tile.sample_many(lats[here], lons[here])
    if not np.all(np.isfinite(out)) or out.min() < -500 or out.max() > 9000:
        return None, "absent"
    return np.ceil(out).astype(np.int16), "ok"


class MapReader:
    """Heights for squares, read on a thread of its own.

    A map tile is 40 MB and can take minutes to download, and the link
    thread, which the upload runs on, must never wait for one: it is the
    thread that keeps the aircraft's telemetry coming. So squares are asked
    for here and collected when they are ready. A provider of its own,
    because TerrainProvider is not safe to share between threads.

    threaded=False works each square out when it is first asked for, on
    the caller's thread - for tests, which want no timing in them.
    """

    TRIES = 3
    RETRY_S = 16.0      # a failed tile is left alone 15 s by the provider

    def __init__(self, spacing, provider_factory, threaded=True):
        self._spacing = spacing
        self._factory = provider_factory
        self._threaded = threaded
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue = deque()           # (square, not before)
        self._queued = set()
        self._done = {}
        self._tries = {}
        self._status = None
        self._stop = False
        self._provider = None
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="terrain-upload-map")
            self._thread.start()

    def want(self, squares):
        with self._lock:
            for s in squares:
                if s not in self._queued:
                    self._queued.add(s)
                    self._queue.append((s, 0.0))
        self._wake.set()

    def get(self, square):
        """(heights or None, state), or None while not worked out yet."""
        with self._lock:
            if square in self._done:
                return self._done[square]
        if self._threaded:
            return None
        if self._provider is None:
            self._provider = self._factory(None)
        result = square_heights(self._provider, square, self._spacing)
        with self._lock:
            self._done[square] = result
        return result

    def status(self):
        with self._lock:
            return self._status

    def stop(self):
        self._stop = True
        self._wake.set()
        if not self._threaded:
            self._provider = None

    def _set_status(self, text):
        with self._lock:
            self._status = text

    def _run(self):
        try:
            self._provider = self._factory(self._set_status)
        except Exception as exc:
            self._provider = None
            failure = "the map could not be opened: %s" % (exc,)
        else:
            failure = None
        while not self._stop:
            now = time.monotonic()
            square = None
            with self._lock:
                # The first square due. One whose tile just failed waits
                # its turn at the back, so the rest of the area - most of
                # it usually in other tiles - is not held up behind it.
                for _ in range(len(self._queue)):
                    candidate, not_before = self._queue.popleft()
                    if not_before <= now:
                        square = candidate
                        break
                    self._queue.append((candidate, not_before))
            if square is None:
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            if failure is not None:
                result = (None, "failed")
            else:
                try:
                    result = square_heights(self._provider, square,
                                            self._spacing)
                except Exception:
                    result = (None, "failed")
            if result[1] == "failed" and failure is None:
                tries = self._tries.get(square, 0) + 1
                self._tries[square] = tries
                if tries < self.TRIES:
                    # The network may come back; the provider leaves a
                    # failed tile alone for 15 s, so this waits as long.
                    with self._lock:
                        self._queue.append((square, now + self.RETRY_S))
                    continue
            with self._lock:
                self._done[square] = result
        # Let the map go the moment the upload is done with it. The provider
        # holds this reader's _set_status, and this reader holds the
        # provider: a loop Python frees only at its occasional full sweep,
        # and until then a decoded tile - 50 MB of it - stayed in memory.
        # Seen in the user's run, 2026-10-12: about 85 MB more after the
        # upload than before it.
        self._provider = None


# --------------------------------------------------------------------------
# The upload

class TerrainUpload:
    """One upload, from Done to its summary.

    Everything here runs on the link thread: tick() from its loop,
    on_message() and on_param() as frames arrive. What it needs from the
    link is io:

      io.where()          -> (is the link in use the flight controller's
                              own USB, what it is)
      io.armed()          -> whether the aircraft is armed
      io.send_terrain(fn) -> fn(mav, sysid, compid) on the link in use; the
                             link itself refuses unless it is the USB and the
                             aircraft is disarmed
      io.send_setting(fn) -> the same for a parameter or a message rate;
                             refused unless it is the USB
      io.progress(done, total, text)
      io.note(text)       -> a line for the pilot
      io.finished(ok, summary)

    Terrain goes out only through send_terrain, and the same two conditions
    are checked here before every send as well, so either alone would hold.
    """

    PARAM_ASK_EVERY_S = 1.5
    PARAM_WAIT_S = 9.0
    # TERRAIN_OPTIONS and TERRAIN_CACHE_SZ are newer than the rest, so an
    # older firmware may not have them: this long after the two that matter,
    # they are taken as their defaults.
    OPTIONAL_WAIT_S = 3.0
    CHECK_EVERY_S = 1.0
    SQUARE_WAIT_S = 45.0
    SQUARE_TRIES = 3
    IN_FLIGHT = 6
    SAVE_WAIT_S = 20.0
    # Between batches: the card has caught up when the count of what is
    # still to be written is nothing - or has not moved for this long,
    # which is what is left when the rest is ground the aircraft wants
    # and the map has not got.
    SETTLED_S = 2.0
    SETTLE_WAIT_S = 15.0
    VERIFY_EVERY_S = 0.5
    VERIFY_WAIT_S = 15.0
    # A square found missing at the check is loaded once more before it is
    # called lost: the aircraft can push a square out of its memory before
    # its card has taken it, and that is not a broken card.
    RELOAD_ROUNDS = 1
    REQUEST_INTERVAL_US = 100_000
    PROGRESS_EVERY_S = 0.25
    # Within this of what was sent, the height the aircraft reports agrees:
    # whole metres, interpolated in float.
    AGREE_M = 0.3
    # Once armed on the ground the aircraft shifts every height it reports
    # by one amount (TERRAIN_OFS_MAX, at most 50 m). The same for every
    # square, so it is allowed for as one common offset.
    COMMON_OFFSET_MAX_M = 50.0

    SETTINGS = ("TERRAIN_ENABLE", "TERRAIN_SPACING", "TERRAIN_OPTIONS",
                "TERRAIN_CACHE_SZ")
    REQUIRED = ("TERRAIN_ENABLE", "TERRAIN_SPACING")
    OPTION_DISABLE_DOWNLOAD = 1
    OPTION_DISABLE_DISK = 2

    def __init__(self, polygon, io, provider_factory=None, clock=None,
                 margin_m=MARGIN_M, max_squares=MAX_SQUARES, threaded=True):
        self.polygon = [(float(a), float(b)) for a, b in polygon]
        self._io = io
        self._factory = provider_factory or _default_provider
        self._clock = clock or time.monotonic
        self._margin = margin_m
        self._margin_used = margin_m
        self._max_squares = max_squares
        self._threaded = threaded
        self.state = "settings"
        self.ended = False
        self._entered_at = None
        self._params = {}
        self._param_asked_at = float("-inf")
        self._required_at = None
        self._spacing = None
        self._in_flight_max = self.IN_FLIGHT
        self._options = 0
        self._options_wanted = None
        self._options_touched = False
        self._options_sent_at = float("-inf")
        self._restore_options = None
        self._options_outcome = None
        self._reader = None
        self._squares = []          # the area, in order
        self._usable = []           # those the map has ground for
        self._map_waiting = set()
        self._round = []            # the squares this pass loads and checks
        self._reload_round = 0
        self._queue = deque()
        self._in_flight = {}        # square -> [since, asked_at]
        self._tries = {}
        self._point_of = {}         # check point -> square
        self._area = set()          # the area's squares with ground
        self._result = {}           # square -> loaded / there / failed /
                                    #           nomap / lost / mismatch
        self._why_nomap = {}
        self._answered = set()      # area squares this upload sent
        self._extra = set()         # squares outside it, answered
        self._reported = {}         # square -> height read back at the end
        self._pushing = set()       # asked about only to empty its memory
        self._pending = None
        self._pending_changed_at = float("-inf")
        self._settling_since = None
        self._settle_asked_at = float("-inf")
        self._last_loaded = None
        self._save_asked_at = float("-inf")
        self._interval_set = False
        self._stop_reason = None
        self._fail_text = None
        self._notes = []
        self._progress_at = float("-inf")
        self._last_progress = None

    # ---- from the link --------------------------------------------------

    def stop(self, reason):
        """End it early: the pilot pressed Stop, armed, or the link went."""
        if self.ended or self._stop_reason is not None:
            return
        self._stop_reason = reason
        self._wind_down(self._clock())

    def close(self, reason):
        """The link is closing. Stop, send what puts the aircraft back as it
        was while there is still a link to send it on, and end now - there
        will be nothing left to hear an answer with."""
        if self.ended:
            return
        self.stop(reason)
        if not self.ended:
            if (self._restore_options is not None
                    and self._options_outcome is None):
                self._options_outcome = "sent"
            self._finish_with_summary()

    def tick(self, now=None):
        now = self._clock() if now is None else now
        if self.ended:
            return
        if self._entered_at is None:
            self._entered_at = now
        if self.state != "restoring" and self._stop_reason is None:
            usb, what = self._io.where()
            if not usb:
                self.stop("the link in use is %s, not the flight "
                          "controller's own USB" % what)
                return
            if self._io.armed():
                self.stop("the aircraft was armed")
                return
        getattr(self, "_tick_" + self.state)(now)
        self._report_progress(now)

    def on_param(self, name, value):
        if name in self.SETTINGS:
            self._params[name] = float(value)

    def on_message(self, msg, mtype):
        """A TERRAIN_REQUEST or TERRAIN_REPORT from the link in use.

        Returns True for a report that answers one of this upload's own
        questions. That one is about a square of the area, not about the
        ground under the aircraft, and must not be shown as if it were.
        """
        if self.ended:
            return False
        if mtype == "TERRAIN_REPORT":
            return self._on_report(msg)
        if mtype == "TERRAIN_REQUEST":
            self._on_request(msg)
        return False

    # ---- one state at a time ----------------------------------------------

    def _enter(self, state, now):
        self.state = state
        self._entered_at = now

    def _note(self, text):
        note = getattr(self._io, "note", None)
        if note is not None:
            note(text)

    def _give_up(self, text, now):
        """Fail, but still put back whatever was changed on the aircraft."""
        self._fail_text = text
        self._wind_down(now)

    def _ask_settings(self, now, names):
        if now - self._param_asked_at < self.PARAM_ASK_EVERY_S:
            return
        self._param_asked_at = now

        def ask(mav, sysid, compid):
            for name in names:
                mav.param_request_read_send(sysid, compid,
                                            name.encode("ascii"), -1)
        self._io.send_setting(ask)

    def _tick_settings(self, now):
        need = [n for n in self.REQUIRED if n not in self._params]
        missing = [n for n in self.SETTINGS if n not in self._params]
        if need:
            if now - self._entered_at > self.PARAM_WAIT_S:
                self._give_up("Terrain upload: no answer from the aircraft "
                              "about its terrain settings - is terrain built "
                              "into its firmware?", now)
                return
            self._ask_settings(now, missing)
            return
        if self._required_at is None:
            self._required_at = now
        if missing and now - self._required_at < self.OPTIONAL_WAIT_S:
            self._ask_settings(now, missing)
            return
        if int(self._params["TERRAIN_ENABLE"]) == 0:
            self._give_up("Terrain upload: terrain is switched off on the "
                          "aircraft (TERRAIN_ENABLE is 0). Switch it on and "
                          "try again.", now)
            return
        spacing = int(round(self._params["TERRAIN_SPACING"]))
        if spacing <= 0:
            self._give_up("Terrain upload: the aircraft's TERRAIN_SPACING is "
                          "%d." % spacing, now)
            return
        self._options = int(self._params.get("TERRAIN_OPTIONS", 0))
        if self._options & self.OPTION_DISABLE_DISK:
            self._give_up("Terrain upload: the aircraft is set not to keep "
                          "terrain on its card (TERRAIN_OPTIONS, Disable "
                          "Disk), so it would lose it at power-off. Clear "
                          "that option and try again.", now)
            return
        cache = int(self._params.get("TERRAIN_CACHE_SZ", 12) or 12)
        # Its own position and its home take two places in its memory, and
        # a stored mission more. Asking about more squares than fit would
        # push the upload's own out before they were loaded.
        self._in_flight_max = max(1, min(self.IN_FLIGHT, cache - 4))
        self._spacing = spacing
        self._squares = squares_for_area(self.polygon, spacing, self._margin)
        # The check at the end must read each square back from the card,
        # not from the aircraft's memory, or a card that never saved it
        # would pass. A square leaves memory only when others push it out,
        # so the area needs twice the memory's worth of squares: by the
        # time the check reaches the last one loaded, as many have been
        # asked about since. A small area gets a wider margin to make up
        # the number - more ground round it, which is no loss.
        enough = 2 * cache + 2
        while len(self._squares) < enough and self._margin_used < 20000.0:
            self._margin_used += 1000.0
            self._squares = squares_for_area(self.polygon, spacing,
                                             self._margin_used)
        if len(self._squares) > self._max_squares:
            self._give_up("Terrain upload: that area is %d squares of %s - "
                          "at most %d can be loaded at once. Draw a smaller "
                          "area." % (len(self._squares), self._square_size(),
                                     self._max_squares), now)
            return
        self._reader = MapReader(spacing, self._factory, self._threaded)
        self._reader.want(self._squares)
        self._map_waiting = set(self._squares)
        self._enter("map", now)

    def _tick_map(self, now):
        for sq in list(self._map_waiting):
            if self._reader.get(sq) is not None:
                self._map_waiting.discard(sq)
        if self._map_waiting:
            return
        for sq in self._squares:
            heights, state = self._reader.get(sq)
            if heights is None:
                self._result[sq] = "nomap"
                self._why_nomap[sq] = state
            else:
                self._usable.append(sq)
        if not self._usable:
            self._give_up("Terrain upload: %s" % (
                "the map could not be downloaded - check the internet "
                "connection and try again."
                if set(self._why_nomap.values()) == {"failed"} else
                "MavGCS's map has no ground for this area (sea, or ground "
                "the map does not cover). Nothing was loaded."), now)
            return
        self._area = set(self._usable)
        for sq in self._usable:
            self._point_of[check_point(sq, self._spacing)] = sq
        self._round = list(self._usable)
        self._queue = deque(self._round)
        if self._options & self.OPTION_DISABLE_DOWNLOAD:
            # Disable Download stops the aircraft asking anybody for
            # terrain, this upload included. Switched off for the upload,
            # said so, and put back at the end whatever happens.
            self._options_wanted = (self._options
                                    & ~self.OPTION_DISABLE_DOWNLOAD)
            self._note("Terrain upload: switching off TERRAIN_OPTIONS "
                       "Disable Download for the upload - it is put back at "
                       "the end")
            self._enter("options", now)
        else:
            self._begin_loading(now)

    def _set_options(self, now, value):
        if now - self._options_sent_at < self.PARAM_ASK_EVERY_S:
            return
        self._options_sent_at = now
        self._options_touched = True

        def write(mav, sysid, compid):
            mav.param_set_send(sysid, compid, b"TERRAIN_OPTIONS",
                               float(value),
                               mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
        self._io.send_setting(write)

    def _tick_options(self, now):
        if int(self._params.get("TERRAIN_OPTIONS", -1)) == self._options_wanted:
            self._begin_loading(now)
            return
        if now - self._entered_at > self.PARAM_WAIT_S:
            self._give_up("Terrain upload: the aircraft did not take the "
                          "change to TERRAIN_OPTIONS that lets it load "
                          "terrain (Disable Download is on). Nothing was "
                          "loaded.", now)
            return
        self._set_options(now, self._options_wanted)

    def _begin_loading(self, now):
        def faster(mav, sysid, compid):
            # The aircraft asks for one square each time it sends this, and
            # with no position fix nothing else holds it back. Put back at
            # the end.
            mav.command_long_send(
                sysid, compid, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0, mavutil.mavlink.MAVLINK_MSG_ID_TERRAIN_REQUEST,
                self.REQUEST_INTERVAL_US, 0, 0, 0, 0, 0)
        if self._reload_round == 0:
            if self._io.send_setting(faster):
                self._interval_set = True
            self._note("Terrain upload: %d squares of %s (the area and %.0f km "
                       "round it) - %s at most" % (
                           len(self._usable), self._square_size(),
                           self._margin_used / 1000.0, describe_minutes(
                               len(self._usable) * SECONDS_PER_SQUARE)))
        self._enter("loading", now)

    def _tick_loading(self, now):
        # In batches, with a pause for the card between them. The aircraft
        # reads its card before it writes to it, and a new square to ask
        # about is always a read - so while squares keep coming, finished
        # ones wait in its memory unwritten, and the next new square can
        # push one out before its turn (find_grid_cache takes the oldest,
        # written or not). Measured on the mirror: four of thirty lost that
        # way when squares were simply kept coming. So a batch is asked
        # about, loaded, and then nothing new until the card has it all.
        if self._settling_since is not None:
            if (self._pending == 0
                    or now - self._pending_changed_at >= self.SETTLED_S
                    or now - self._settling_since > self.SETTLE_WAIT_S):
                self._settling_since = None
            else:
                # The count of what is still to be written comes with every
                # answer. A square just loaded is in its memory, so asking
                # about it costs the aircraft no read.
                if (self._last_loaded is not None
                        and now - self._settle_asked_at >= 0.5):
                    self._settle_asked_at = now
                    self._check(self._last_loaded)
                return
        if not self._in_flight:
            if not self._queue:
                self._enter("saving", now)
                return
            for _ in range(min(self._in_flight_max, len(self._queue))):
                self._in_flight[self._queue.popleft()] = [now, float("-inf")]
        for sq, entry in list(self._in_flight.items()):
            since, asked_at = entry
            if now - since > self.SQUARE_WAIT_S:
                del self._in_flight[sq]
                tries = self._tries.get(sq, 0) + 1
                self._tries[sq] = tries
                if tries >= self.SQUARE_TRIES:
                    self._result[sq] = "failed"
                else:
                    self._queue.append(sq)
                continue
            if now - asked_at >= self.CHECK_EVERY_S:
                entry[1] = now
                self._check(sq)
        if not self._in_flight:
            self._settling_since = now

    def _tick_saving(self, now):
        # Everything is in the aircraft's memory; this waits for its card.
        # A square counts as pending until it is written, so the count
        # coming back to nothing is the card having taken it all.
        if self._pending == 0 or now - self._entered_at > self.SAVE_WAIT_S:
            self._start_verifying(now)
            return
        # Each answer to a question carries the count; ask about one square
        # once a second to keep it coming.
        if now - self._save_asked_at >= 1.0:
            self._save_asked_at = now
            done = [sq for sq in self._round
                    if self._result.get(sq) in ("loaded", "there")]
            if done:
                self._check(done[0])

    def _start_verifying(self, now):
        check = [sq for sq in self._round
                 if self._result.get(sq) in ("loaded", "there")]
        push = []
        if self._reload_round:
            # A square loaded again is still in the aircraft's memory. Asked
            # about first, squares already read back whole from the card
            # push it out - and, being whole on the card, they make the
            # aircraft ask for nothing.
            cache = int(self._params.get("TERRAIN_CACHE_SZ", 12) or 12)
            push = [sq for sq in self._usable
                    if sq not in self._round and sq in self._reported
                    ][:cache + 2]
        self._pushing = set(push)
        self._queue = deque(push + check)
        self._in_flight = {}
        self._enter("verifying", now)

    def _tick_verifying(self, now):
        while self._queue and len(self._in_flight) < self._in_flight_max:
            self._in_flight[self._queue.popleft()] = [now, float("-inf")]
        for sq, entry in list(self._in_flight.items()):
            since, asked_at = entry
            if now - since > self.VERIFY_WAIT_S:
                del self._in_flight[sq]
                if sq not in self._pushing:
                    self._result[sq] = "lost"
                continue
            if sq in self._pushing and asked_at > float("-inf"):
                del self._in_flight[sq]     # asked once: that is its job
                continue
            if now - asked_at >= self.VERIFY_EVERY_S:
                entry[1] = now
                self._check(sq)
        if self._queue or self._in_flight:
            return
        lost = [sq for sq in self._round if self._result.get(sq) == "lost"]
        if lost and self._reload_round < self.RELOAD_ROUNDS:
            self._reload_round += 1
            for sq in lost:
                del self._result[sq]
                self._answered.discard(sq)
                self._reported.pop(sq, None)
                self._tries.pop(sq, None)
            self._round = lost
            self._queue = deque(lost)
            self._begin_loading(now)
            return
        self._judge()
        self._wind_down(now)

    def _tick_restoring(self, now):
        if self._restore_options is not None:
            if (int(self._params.get("TERRAIN_OPTIONS", -1))
                    == self._restore_options):
                self._options_outcome = "restored"
            elif (not self._io.where()[0]
                  or now - self._entered_at > self.PARAM_WAIT_S):
                self._options_outcome = "not restored"
            else:
                self._set_options(now, self._restore_options)
                return
        self._finish_with_summary()

    # ---- talking to the aircraft --------------------------------------------

    def _may_send_terrain(self):
        usb, _what = self._io.where()
        return usb and not self._io.armed() and self._stop_reason is None

    def _check(self, sq):
        if not self._may_send_terrain():
            return
        lat, lon = check_point(sq, self._spacing)

        def ask(mav, sysid, compid):
            mav.terrain_check_send(lat, lon)
        self._io.send_terrain(ask)

    def _on_report(self, msg):
        pending = int(getattr(msg, "pending", 0))
        if pending != self._pending:
            self._pending = pending
            self._pending_changed_at = self._clock()
        sq = self._point_of.get((int(msg.lat), int(msg.lon)))
        if sq is None:
            return False
        if int(getattr(msg, "spacing", 0)) == 0:
            return True                 # not there (yet)
        if sq in self._in_flight:
            if self.state == "loading":
                del self._in_flight[sq]
                self._result[sq] = ("loaded" if sq in self._answered
                                    else "there")
                self._last_loaded = sq
            elif self.state == "verifying":
                del self._in_flight[sq]
                if sq not in self._pushing:
                    self._reported[sq] = float(msg.terrain_height)
        return True

    def _on_request(self, msg):
        if self._spacing is None or int(msg.grid_spacing) != self._spacing:
            return
        sq = square_of_corner(int(msg.lat), int(msg.lon), self._spacing)
        if self.state == "verifying":
            # Asked for again once the check has begun: the aircraft does
            # not have it after all. Finding that out is what the check is
            # for, so it is not quietly sent again here.
            if (sq in self._area
                    and self._result.get(sq) in ("loaded", "there")):
                self._in_flight.pop(sq, None)
                if sq in self._queue:
                    self._queue.remove(sq)
                self._result[sq] = "lost"
            return
        if self.state not in ("loading", "saving"):
            return
        if sq in self._area:
            heights = self._reader.get(sq)[0]
            self._answered.add(sq)
        else:
            # The aircraft's own: where it is, its home, its mission. It
            # asks for these before anything else, and while one is missing
            # it asks for nothing else - so they are answered too, from the
            # same map by the same rules, while the upload runs.
            self._reader.want([sq])
            got = self._reader.get(sq)
            if got is None or got[0] is None:
                return
            heights = got[0]
            self._extra.add(sq)
        self._answer(msg, heights)

    def _answer(self, msg, heights):
        if not self._may_send_terrain():
            return
        lat, lon, spacing = int(msg.lat), int(msg.lon), self._spacing
        bits = [b for b in range(GRIDBITS) if int(msg.mask) >> b & 1]

        def send(mav, sysid, compid):
            for bit in bits:
                mav.terrain_data_send(lat, lon, spacing, bit,
                                      gridbit_data(heights, bit))
        self._io.send_terrain(send)

    # ---- the end ------------------------------------------------------------

    def _judge(self):
        """Compare what the aircraft reports with what it was sent."""
        diffs = {}
        for sq, reported in self._reported.items():
            heights = self._reader.get(sq)[0]
            diffs[sq] = reported - expected_report(heights, sq, self._spacing)
        loaded = [sq for sq in diffs if self._result.get(sq) == "loaded"]
        common = 0.0
        if loaded:
            common = float(np.median([diffs[sq] for sq in loaded]))
            if abs(common) > self.COMMON_OFFSET_MAX_M:
                common = 0.0
        for sq in loaded:
            if abs(diffs[sq] - common) > self.AGREE_M:
                self._result[sq] = "mismatch"
        there = [sq for sq in diffs if self._result.get(sq) == "there"]
        if there:
            worst = max(abs(diffs[sq] - common) for sq in there)
            if worst >= 5.0:
                self._notes.append(
                    "Of the squares already on the card, some differ from "
                    "MavGCS's map by up to %.0f m (older or other terrain); "
                    "to replace them, delete the TERRAIN folder on the card "
                    "and upload again." % worst)
        if abs(common) >= 0.5:
            self._notes.append(
                "The aircraft adds %+.1f m to every height it reports - its "
                "correction from where it was last armed." % common)

    def _wind_down(self, now):
        """Stop sending terrain, put back what was changed, then finish."""
        if self._reader is not None:
            self._reader.stop()
        if self._interval_set and self._io.where()[0]:
            def default_rate(mav, sysid, compid):
                mav.command_long_send(
                    sysid, compid,
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    mavutil.mavlink.MAVLINK_MSG_ID_TERRAIN_REQUEST, 0,
                    0, 0, 0, 0, 0)
            self._io.send_setting(default_rate)
            self._interval_set = False
        if self._options_touched:
            # Put back on a fresh echo, not on the value last seen: a write
            # can land without its echo arriving, so the old reading proves
            # nothing either way.
            self._restore_options = self._options
            self._params.pop("TERRAIN_OPTIONS", None)
            self._options_sent_at = float("-inf")
        self._enter("restoring", now)
        self._tick_restoring(now)

    def _square_size(self):
        s = self._spacing or 100
        return "%.1f x %.1f km" % (BLOCK_SPACING_X * s / 1000.0,
                                   BLOCK_SPACING_Y * s / 1000.0)

    def counts(self):
        tally = {}
        for result in self._result.values():
            tally[result] = tally.get(result, 0) + 1
        return tally

    def _finish_with_summary(self):
        c = self.counts()
        loaded, there = c.get("loaded", 0), c.get("there", 0)
        failed, lost = c.get("failed", 0), c.get("lost", 0)
        mismatch, nomap = c.get("mismatch", 0), c.get("nomap", 0)
        parts = []
        if self._fail_text is not None:
            ok, head = False, self._fail_text.rstrip(".")
        elif self._stop_reason is not None:
            ok = False
            if loaded + there == 0:
                head = ("Terrain upload stopped - %s. Nothing was loaded"
                        % self._stop_reason)
            else:
                head = ("Terrain upload stopped - %s. %d of %d squares were "
                        "loaded; press Terrain Upload again to finish "
                        "(squares already on the card are skipped)"
                        % (self._stop_reason, loaded + there,
                           len(self._usable)))
        else:
            ok = not (failed or lost or mismatch)
            head = ("Terrain upload %s: %d square%s of %s loaded and checked"
                    % ("done" if ok else "finished with problems", loaded,
                       "" if loaded == 1 else "s", self._square_size()))
            if there:
                parts.append("%d already on the card" % there)
            if self._extra:
                parts.append("%d more the aircraft asked for itself"
                             % len(self._extra))
            if failed:
                parts.append("%d never taken by the aircraft - is its SD "
                             "card in?" % failed)
            if lost:
                parts.append("%d NOT kept by the aircraft - is its SD card "
                             "in and working?" % lost)
            if mismatch:
                parts.append("%d read back DIFFERENT from what was sent"
                             % mismatch)
            if nomap:
                parts.append("%d not loaded: %s" % (
                    nomap, "the map could not be downloaded"
                    if set(self._why_nomap.values()) == {"failed"} else
                    "no map data there (sea, or ground the map does not "
                    "cover)"))
        text = head
        if parts:
            text += " - " + "; ".join(parts)
        if not text.endswith((".", "?", "!")):
            text += "."
        if self._options_outcome == "restored":
            text += (" TERRAIN_OPTIONS Disable Download was switched off for "
                     "the upload and is back on.")
        elif self._options_outcome == "not restored":
            ok = False
            text += (" TERRAIN_OPTIONS Disable Download was switched off for "
                     "the upload and could NOT be put back - set "
                     "TERRAIN_OPTIONS to %d in Params." % self._options)
        elif self._options_outcome == "sent":
            ok = False
            text += (" TERRAIN_OPTIONS Disable Download was switched off for "
                     "the upload; putting it back was sent as the link "
                     "closed, unconfirmed - check TERRAIN_OPTIONS is %d."
                     % self._options)
        if self._notes:
            text += " " + " ".join(self._notes)
        self._finish(ok, text)

    def _finish(self, ok, text):
        if self.ended:
            return
        if self._reader is not None:
            self._reader.stop()
        self.ended = True
        self.state = "ended"
        self._io.finished(ok, text)

    def _report_progress(self, now):
        if self.ended:
            return
        # Short: the line sits in a narrow box under the map's overlay
        # switches, with the zoom readout just beside it, and the box's own
        # title already says what is running.
        total = len(self._round) or len(self._squares)
        if self.state == "settings":
            text, done = "Reading settings", 0
        elif self.state == "map":
            status = self._reader.status() if self._reader else None
            done = len(self._squares) - len(self._map_waiting)
            percent = re.search(r"(\d+)%", status or "")
            if percent:
                text = "Map %s%%" % percent.group(1)
            else:
                text = "Map %d/%d" % (done, len(self._squares))
        elif self.state == "options":
            done, text = 0, "Preparing"
        elif self.state == "loading":
            done = sum(1 for sq in self._round if sq in self._result)
            text = "Loading %d/%d" % (done, total)
        elif self.state == "saving":
            done, text = total, "Saving"
        elif self.state == "verifying":
            done = sum(1 for sq in self._round if sq in self._reported
                       or self._result.get(sq) == "lost")
            text = "Checking %d/%d" % (done, total)
        else:
            done, text = total, "Finishing"
        if (text, done, total) == self._last_progress:
            return
        if now - self._progress_at < self.PROGRESS_EVERY_S:
            return
        self._progress_at = now
        self._last_progress = (text, done, total)
        self._io.progress(done, total, text)


def _default_provider(on_status):
    from terrain_provider import TerrainProvider
    return TerrainProvider(on_status=on_status)
