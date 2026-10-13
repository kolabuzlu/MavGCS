"""ArduPilot's terrain system, as far as a ground station can tell - for
testing the terrain upload without an aircraft, and without timing luck.

Not a test itself (run_all.py runs test_*.py only). Mirrors AP_Terrain as
read from ArduPilot master on 2026-10-12, piece by piece:

  - the cache: TERRAIN_CACHE_SZ squares in memory, the least recently used
    one reused for a square not held (TerrainUtil.cpp find_grid_cache);
  - the card: a square not in memory is read from it first, and while that
    read is pending the square is not asked for (TerrainGCS.cpp
    request_missing); one read or write per two updates, as
    schedule_disk_io hands work to the IO thread and collects it on the
    next pass; with no card a read never finishes;
  - asking: once per TERRAIN_REQUEST slot, and with a position fix no more
    than once every 2 s, its own square first and then any square in
    memory with parts missing, in memory order (send_request);
  - with a position, the squares round it held in memory as well, 0.7 of
    a square away each way (update_surrounding_tiles, added 2026-10-13);
  - TERRAIN_DATA taken only for a square in memory (handle_terrain_data);
  - TERRAIN_CHECK answered at once with a TERRAIN_REPORT for that place,
    spacing 0 where it has no height, and the pending/loaded counts
    (send_terrain_report, get_statistics);
  - TERRAIN_OPTIONS Disable Download (request_missing), the height
    correction from arming (height_amsl, corrected).

Time is the caller's: step() moves it on.
"""

import os

import numpy as np

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil

import terrain_upload as T

M = mavutil.mavlink

DISKWAIT, VALID, DIRTY = "diskwait", "valid", "dirty"
FULL = (1 << T.GRIDBITS) - 1


def _bit_of(x, y):
    return (x // T.GRID_MAVLINK_SIZE) * T.GRID_BLOCK_MUL_Y + (
        y // T.GRID_MAVLINK_SIZE)


class _Entry:
    def __init__(self):
        self.square = None
        self.lat = self.lon = 0
        self.bitmap = 0
        self.heights = np.zeros((T.BLOCK_SIZE_X, T.BLOCK_SIZE_Y),
                                dtype=np.int16)
        self.state = None
        self.last_access = -1.0


class FakeTerrainPlane:
    """AP_Terrain on one telemetry port."""

    def __init__(self, spacing=100, cache_size=12, options=0, enable=1,
                 card=True, position=None, offset=0.0, sim_speedup=None):
        self.params = {"TERRAIN_ENABLE": float(enable),
                       "TERRAIN_SPACING": float(spacing),
                       "TERRAIN_OPTIONS": float(options),
                       "TERRAIN_CACHE_SZ": float(cache_size)}
        # ArduPilot's simulator has SIM_ settings; a flight controller
        # has none, and does not answer for them.
        if sim_speedup is not None:
            self.params["SIM_SPEEDUP"] = float(sim_speedup)
        self.cache = [_Entry() for _ in range(cache_size)]
        self.disk = {}                  # square -> (bitmap, heights)
        self.card = card
        self.position = position        # (lat_e7, lon_e7) or None: no fix
        self.offset = offset            # added to every height reported
        self.now = 0.0
        self.outbox = []                # (kind, message) for the GCS
        self.request_interval = 1.0     # TERRAIN_REQUEST slot, default 1 Hz
        self.interval_set = False
        self.next_request_slot = 0.0
        self.next_update = 0.0
        self.next_report = 0.0
        self.last_request = float("-inf")
        self.io = None                  # ("read"|"write", entry, block)
        self.io_done = False
        # What arrived, for the tests to look at.
        self.got = []                   # (type, what, when)
        self.data_after = []            # (time, square) of TERRAIN_DATA
        # Faults to inject.
        self.drop_echo_every = 0
        self._echo_count = 0
        self.drop_writes = set()        # squares whose next write is lost
        self.always_drop = set()        # squares the card never keeps

    # ---- what the ground station sends ----------------------------------

    @property
    def spacing(self):
        return int(self.params["TERRAIN_SPACING"])

    def options(self):
        return int(self.params["TERRAIN_OPTIONS"])

    def active(self):
        return int(self.params["TERRAIN_ENABLE"]) != 0

    # pymavlink's send methods, called through io.send_terrain/send_setting.
    def terrain_check_send(self, lat, lon):
        self.got.append(("TERRAIN_CHECK", (lat, lon), self.now))
        self._report((lat, lon), extrapolate=False)

    def terrain_data_send(self, lat, lon, grid_spacing, gridbit, data):
        sq = T.square_of_corner(lat, lon, grid_spacing)
        self.got.append(("TERRAIN_DATA", sq, self.now))
        self.data_after.append((self.now, sq))
        for e in self.cache:
            if (e.state is not None and abs(e.lat - lat) <= 5
                    and abs(e.lon - lon) <= 5
                    and grid_spacing == self.spacing and gridbit < 56):
                break
        else:
            return                      # not a square in memory: ignored
        x0 = (gridbit // T.GRID_BLOCK_MUL_Y) * T.GRID_MAVLINK_SIZE
        y0 = (gridbit % T.GRID_BLOCK_MUL_Y) * T.GRID_MAVLINK_SIZE
        for n in range(4):
            for k in range(4):
                e.heights[x0 + n][y0 + k] = data[n * 4 + k]
        e.bitmap |= 1 << gridbit
        if self.card:
            e.state = DIRTY

    def param_request_read_send(self, sysid, compid, name, index):
        name = name.decode() if isinstance(name, bytes) else name
        self.got.append(("PARAM_REQUEST_READ", name, self.now))
        if name in self.params:
            self._echo(name)

    def param_set_send(self, sysid, compid, name, value, ptype):
        name = name.decode() if isinstance(name, bytes) else name
        self.got.append(("PARAM_SET", (name, value), self.now))
        if name in self.params:
            self.params[name] = float(int(value))
            self._echo(name)

    def command_long_send(self, sysid, compid, command, conf, p1, p2, *rest):
        self.got.append(("COMMAND_LONG", (command, p1, p2), self.now))
        if (command == M.MAV_CMD_SET_MESSAGE_INTERVAL
                and int(p1) == M.MAVLINK_MSG_ID_TERRAIN_REQUEST):
            if int(p2) > 0:
                self.request_interval = p2 / 1e6
                self.interval_set = True
            else:
                self.request_interval = 1.0
                self.interval_set = False

    def _echo(self, name):
        self._echo_count += 1
        if self.drop_echo_every and self._echo_count % self.drop_echo_every == 0:
            return
        self.outbox.append(("PARAM_VALUE", (name, self.params[name])))

    # ---- AP_Terrain -------------------------------------------------------

    def _find(self, square):
        """find_grid_cache."""
        oldest = self.cache[0]
        for e in self.cache:
            if e.square == square and e.state is not None:
                e.last_access = self.now
                return e
            if e.last_access < oldest.last_access:
                oldest = e
        e = oldest
        e.square = square
        e.lat, e.lon = T.square_corner(square, self.spacing)
        e.bitmap = 0
        e.heights = np.zeros((T.BLOCK_SIZE_X, T.BLOCK_SIZE_Y), dtype=np.int16)
        e.state = DISKWAIT
        e.last_access = self.now
        return e

    def height_amsl(self, lat, lon, corrected=True):
        if not self.active():
            return None
        sq, ix, iy, fx, fy = T.square_at(lat, lon, self.spacing)
        e = self._find(sq)
        for x, y in ((ix, iy), (ix, iy + 1), (ix + 1, iy), (ix + 1, iy + 1)):
            if not e.bitmap >> _bit_of(x, y) & 1:
                return None
        h = T.interpolate(e.heights, ix, iy, fx, fy)
        return h + (self.offset if corrected else 0.0)

    def statistics(self):
        pending = loaded = 0
        for e in self.cache:
            if e.state is None:
                continue
            if e.state == DISKWAIT:
                pending += 56
                continue
            if e.state == DIRTY:
                pending += 1
            count = bin(e.bitmap).count("1")
            pending += 56 - count
            loaded += count
        return pending, loaded

    def _report(self, place, extrapolate):
        height = self.height_amsl(place[0], place[1])
        pending, loaded = self.statistics()
        self.outbox.append(("TERRAIN_REPORT", M.MAVLink_terrain_report_message(
            place[0], place[1], self.spacing if height is not None else 0,
            height if height is not None else 0.0, 0.0, pending, loaded)))

    def _request_missing(self, e):
        if self.options() & 1:
            return False
        if e.state == DISKWAIT:
            return False
        if e.bitmap == FULL:
            return False
        self.outbox.append(("TERRAIN_REQUEST", M.MAVLink_terrain_request_message(
            e.lat, e.lon, self.spacing, FULL & ~e.bitmap)))
        self.got.append(("asked", e.square, self.now))
        self.last_request = self.now
        return True

    def send_request(self):
        if not self.active():
            return
        self._schedule_disk_io()
        if self.position is None:
            for e in self.cache:
                if e.state in (VALID, DIRTY) and self._request_missing(e):
                    return
            return
        if self.now - self.last_request < 2.0:
            return
        own = self._find(T.square_at(self.position[0], self.position[1],
                                     self.spacing)[0])
        if self._request_missing(own):
            return
        for e in self.cache:
            if e.state in (VALID, DIRTY) and self._request_missing(e):
                return

    def update(self):
        """AP_Terrain::update and schedule_disk_io, ten times a second."""
        if not self.active():
            return
        if self.position is not None:
            self.height_amsl(self.position[0], self.position[1])
            # update_surrounding_tiles: the squares round it as well, 0.7
            # of a square away each way - held in memory, and so asked for
            # like any other.
            for x in (-1, 0, 1):
                for y in (-1, 0, 1):
                    lat, lon = T.offset(
                        self.position[0], self.position[1],
                        x * T.BLOCK_SIZE_X * 0.7 * self.spacing,
                        y * T.BLOCK_SIZE_Y * 0.7 * self.spacing)
                    self.height_amsl(lat, lon)
        self._schedule_disk_io()

    def _schedule_disk_io(self):
        """schedule_disk_io: one read or write handed to the IO thread, and
        collected on the next call. ArduPilot calls it from update() and
        from send_request() both (TerrainGCS.cpp), so with requests
        streamed its card works about twice as often as update() alone
        would make it - which the mirror missed until 2026-10-13, and lost
        squares a real one does not."""
        if self.io is not None and self.io_done:
            kind, e, block = self.io
            if kind == "read":
                if e.state == DISKWAIT and e.square == block[0]:
                    if block[1]:
                        e.bitmap, e.heights = block[1], block[2].copy()
                    e.state = VALID
            else:
                if e.state == DIRTY and e.bitmap == block[1]:
                    e.state = VALID
            self.io = None
            self.io_done = False
            return
        if self.io is not None:
            return
        for e in self.cache:
            if e.state == DISKWAIT:
                if not self.card:
                    return              # the card never answers
                stored = self.disk.get(e.square)
                self.io = ("read", e, (e.square,) + (
                    stored if stored else (0, None)))
                self.io_done = True     # the IO thread is quick
                return
        for e in self.cache:
            if e.state == DIRTY:
                if e.square in self.always_drop:
                    pass
                elif e.square in self.drop_writes:
                    self.drop_writes.discard(e.square)
                else:
                    self.disk[e.square] = (e.bitmap, e.heights.copy())
                self.io = ("write", e, (e.square, e.bitmap))
                self.io_done = True
                return

    def step(self, now):
        """Everything the aircraft does up to this time."""
        self.now = now
        if now >= self.next_update:
            self.next_update = now + 0.1
            self.update()
        if now >= self.next_request_slot:
            self.next_request_slot = now + self.request_interval
            self.send_request()
        if now >= self.next_report and self.position is not None:
            self.next_report = now + 1.0
            self._report(self.position, extrapolate=True)

    def take(self):
        out, self.outbox = self.outbox, []
        return out

    # ---- for the tests ------------------------------------------------------

    def on_card(self, square):
        """(bitmap, heights) the card holds for a square, or None."""
        return self.disk.get(square)
