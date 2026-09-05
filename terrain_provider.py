"""
Forward-looking terrain radar - background elevation lookups against the
free Copernicus GLO-30 DEM (public AWS Open Data bucket, no API key or
account needed - the same data source and tile format KiteGCS's own
terrain radar uses under the hood, confirmed by cross-checking a real tile
against a vehicle's own TERRAIN_REPORT reading: the two agreed to ~1m).

TerrainProvider does the actual tile fetch/decode/sample work and is Qt-
agnostic. TerrainRadarWorker wraps it in a QThread that watches telemetry
and (re)samples a forward "fan" of points ahead of the aircraft whenever
position/heading/range have moved enough to matter (ported from KiteGCS's
own re-sample-only-on-meaningful-change logic) - tile downloads take real
time (a few seconds each), so this must never run on the GUI thread.
"""

import io
import math
import os
import time
import threading
import urllib.request
import urllib.error
from collections import OrderedDict
from pathlib import Path

import numpy as np
import tifffile

from PySide6.QtCore import QThread, Signal

from app_paths import data_dir

BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
# Not next to the code: in a packaged build that's inside the unpacked
# bundle, which is replaced wholesale on upgrade (and may not be writable).
# See app_paths.data_dir().
CACHE_DIR = data_dir() / "terrain_cache"
MAX_CACHED_TILES = 4  # each decoded tile is ~50MB (3600x3600 float32)

EARTH_R = 6371000.0

# Terrain tiles are ~40MB each, so this cache grows far faster per tile than
# the map's. It used to be unbounded; a limit keeps it in check while still
# defaulting to caching, since a terrain radar that re-downloads 40MB for
# every fix would be unusable. 0 means "No Cache": use what's already saved,
# save nothing new.
_DEFAULT_CACHE_LIMIT = 2 * 1024 ** 3   # 2 GB
_cache_lock = threading.Lock()
_cache_limit_bytes = _DEFAULT_CACHE_LIMIT


def cache_limit_bytes() -> int:
    with _cache_lock:
        return _cache_limit_bytes


def set_cache_limit(limit_bytes: int):
    global _cache_limit_bytes
    with _cache_lock:
        _cache_limit_bytes = max(0, int(limit_bytes))
    if cache_limit_bytes() > 0:
        enforce_cache_limit()


def cache_stats():
    """(tile count, bytes on disk). Cheap to scan directly - even a large
    terrain cache is only tens of files, unlike the map's thousands."""
    count = size = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.tif"):
            try:
                size += p.stat().st_size
                count += 1
            except OSError:
                pass
    return count, size


def clear_cache():
    """Delete every cached elevation tile."""
    if not CACHE_DIR.exists():
        return
    for p in CACHE_DIR.glob("*.tif"):
        try:
            p.unlink()
        except OSError:
            pass


def enforce_cache_limit():
    """Trim back under the limit, oldest tile first (to 90%, so a cache
    sitting on the boundary doesn't re-scan after every single tile)."""
    limit = cache_limit_bytes()
    if limit <= 0:
        return
    entries = []
    try:
        for p in CACHE_DIR.glob("*.tif"):
            try:
                st = p.stat()
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
    except OSError:
        return
    total = sum(e[1] for e in entries)
    if total <= limit:
        return
    entries.sort()  # oldest first
    target = int(limit * 0.9)
    for _, size, path in entries:
        if total <= target:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def tile_name(lat: float, lon: float) -> str:
    """Copernicus GLO-30 tile name for the 1-degree cell containing lat/lon,
    e.g. (37.98, 41.84) -> 'Copernicus_DSM_COG_10_N37_00_E041_00_DEM'."""
    lat_i = math.floor(lat)
    lon_i = math.floor(lon)
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_i):02d}_00_{ew}{abs(lon_i):03d}_00_DEM"


def dest_point(lat, lon, bearing_deg, dist_m):
    """Great-circle destination point given a start, bearing, and distance."""
    ang_dist = dist_m / EARTH_R
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist) + math.cos(lat1) * math.sin(ang_dist) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


class _TileData:
    """A decoded DEM tile: elevation grid + geographic transform."""

    __slots__ = ("grid", "height", "width", "origin_lat", "origin_lon", "px_lat", "px_lon")

    def __init__(self, grid, origin_lat, origin_lon, px_lat, px_lon):
        self.grid = grid
        self.height, self.width = grid.shape
        self.origin_lat = origin_lat
        self.origin_lon = origin_lon
        self.px_lat = px_lat
        self.px_lon = px_lon

    def sample(self, lat, lon):
        """Bilinear sample at lat/lon, or None if outside this tile."""
        col = (lon - self.origin_lon) / self.px_lon
        row = (self.origin_lat - lat) / self.px_lat
        if col < 0 or row < 0 or col > self.width - 1 or row > self.height - 1:
            return None
        c0, r0 = int(col), int(row)
        c1, r1 = min(c0 + 1, self.width - 1), min(r0 + 1, self.height - 1)
        fc, fr = col - c0, row - r0
        top = self.grid[r0, c0] * (1 - fc) + self.grid[r0, c1] * fc
        bot = self.grid[r1, c0] * (1 - fc) + self.grid[r1, c1] * fc
        return float(top * (1 - fr) + bot * fr)


def _decode_tile(raw: bytes) -> _TileData:
    """Decode a Copernicus GLO-30 GeoTIFF tile into a _TileData."""
    with tifffile.TiffFile(io.BytesIO(raw)) as tif:
        page = tif.pages[0]
        grid = page.asarray().astype(np.float32)
        tags = page.tags
        scale = tags[33550].value  # ModelPixelScaleTag: (scaleX, scaleY, scaleZ)
        tie = tags[33922].value    # ModelTiepointTag: (i, j, k, x, y, z)
    px_lon, px_lat = abs(scale[0]), abs(scale[1])
    origin_lon = tie[3] - tie[0] * px_lon
    origin_lat = tie[4] + tie[1] * px_lat
    return _TileData(grid, origin_lat, origin_lon, px_lat, px_lon)


class TerrainProvider:
    """
    Elevation lookups against Copernicus GLO-30 (EGM2008 geoid, ~= MSL - the
    same reference GPS altitude uses, so no geoid conversion is needed to
    compare against a vehicle's AMSL altitude). Tiles are ~40MB 1x1-degree
    GeoTIFFs, disk-cached under CACHE_DIR and kept as decoded numpy arrays
    in a small in-memory LRU (a handful of tiles is already tens of MB).

    Not thread-safe for concurrent callers - used exclusively from
    TerrainRadarWorker's own background thread.
    """

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._tiles = OrderedDict()  # key -> _TileData, most-recently-used last
        self._missing = set()  # keys known to not exist (ocean) or that failed to fetch

    def _load_tile(self, key: str):
        if key in self._tiles:
            self._tiles.move_to_end(key)
            return self._tiles[key]
        if key in self._missing:
            return None

        path = CACHE_DIR / f"{key}.tif"
        raw = None
        if path.exists():
            try:
                raw = path.read_bytes()
            except OSError:
                raw = None
        if raw is None:
            url = f"{BASE_URL}/{key}/{key}.tif"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    raw = resp.read()
                if cache_limit_bytes() > 0:
                    # Write via a temporary file and rename: a tile is ~40MB,
                    # so a stop (or a crash) part-way through a direct write
                    # would leave a truncated file that then fails to decode
                    # forever.
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(raw)
                    os.replace(tmp, path)
                    enforce_cache_limit()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError):
                self._missing.add(key)
                return None

        try:
            tile = _decode_tile(raw)
        except Exception:
            self._missing.add(key)
            return None

        self._tiles[key] = tile
        while len(self._tiles) > MAX_CACHED_TILES:
            self._tiles.popitem(last=False)
        return tile

    def elevation(self, lat: float, lon: float):
        """Terrain elevation (m, ~= MSL) at lat/lon, or None if unavailable."""
        tile = self._load_tile(tile_name(lat, lon))
        return tile.sample(lat, lon) if tile else None

    def fan(self, lat, lon, heading_deg, half_angle_deg, range_m, ang_cells, rad_cells):
        """
        Elevations over a forward polar fan ahead of (lat, lon): angularly
        centred on heading_deg, spanning +/- half_angle_deg, out to
        range_m, sampled at each of ang_cells x rad_cells cell centres.
        Returns a flat list (row-major [a * rad_cells + b]) of elevation-or-
        None, matching the cell layout the map overlay's JS expects.
        """
        out = []
        for a in range(ang_cells):
            frac = (a + 0.5) / ang_cells
            bearing = heading_deg - half_angle_deg + 2 * half_angle_deg * frac
            for b in range(rad_cells):
                dist = range_m * (b + 0.5) / rad_cells
                clat, clon = dest_point(lat, lon, bearing, dist)
                out.append(self.elevation(clat, clon))
        return out


class TerrainRadarWorker(QThread):
    """
    Background thread that (re)samples a forward terrain fan as the vehicle
    moves, and emits it for the map overlay to render. Tile downloads take
    real time (a few seconds each), so this must never run on the GUI
    thread - update_telemetry() is the only method safe to call from there.
    """

    # elevations (flat list, row-major), range_m, ang_cells, rad_cells
    fan_ready = Signal(list, float, int, int)

    HALF_ANGLE_DEG = 60.0  # +/- -> 120 deg forward fan
    ANG_CELLS = 32
    RAD_CELLS = 16
    RANGE_STEPS = [300.0, 900.0, 1800.0, 3600.0]  # m, speed-driven
    LOOKAHEAD_S = 120.0  # pick the smallest range step covering speed * 120s
    STEP_DOWN_HYST = 0.7
    POLL_INTERVAL_S = 0.2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._lock = threading.Lock()
        self._telem = None  # (lat, lon, heading_deg, speed_mps)
        self._range_m = self.RANGE_STEPS[0]
        self._last_sample = None  # (lat, lon, heading, range, time)

    def update_telemetry(self, lat, lon, heading_deg, speed_mps):
        """Thread-safe - call from the GUI thread on every position/vfr update."""
        with self._lock:
            self._telem = (lat, lon, heading_deg, speed_mps)

    def _next_range(self, speed_mps):
        need = speed_mps * self.LOOKAHEAD_S
        steps = self.RANGE_STEPS
        i = steps.index(self._range_m) if self._range_m in steps else 0
        target = steps[-1]
        for s in steps:
            if s >= need:
                target = s
                break
        if target > self._range_m:
            return target
        if target < self._range_m and i > 0 and need < steps[i - 1] * self.STEP_DOWN_HYST:
            return target
        return self._range_m

    @staticmethod
    def _haversine_m(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * EARTH_R * math.asin(math.sqrt(a))

    @staticmethod
    def _angle_diff(a, b):
        return abs(((a - b + 540) % 360) - 180)

    def run(self):
        provider = TerrainProvider()
        while self._running:
            with self._lock:
                telem = self._telem
            if telem is None:
                time.sleep(self.POLL_INTERVAL_S)
                continue
            lat, lon, heading, speed = telem
            self._range_m = self._next_range(speed)

            cell_m = self._range_m / self.RAD_CELLS
            stale = self._last_sample is None or (time.time() - self._last_sample[4]) > 5.0
            moved = self._last_sample is not None and self._haversine_m(
                self._last_sample[0], self._last_sample[1], lat, lon
            ) > cell_m * 0.5
            turned = self._last_sample is not None and self._angle_diff(
                heading, self._last_sample[2]
            ) > 2.0
            rescaled = self._last_sample is not None and self._last_sample[3] != self._range_m

            if stale or moved or turned or rescaled:
                try:
                    elev = provider.fan(
                        lat, lon, heading, self.HALF_ANGLE_DEG, self._range_m,
                        self.ANG_CELLS, self.RAD_CELLS,
                    )
                    self.fan_ready.emit(elev, self._range_m, self.ANG_CELLS, self.RAD_CELLS)
                except Exception:
                    pass
                self._last_sample = (lat, lon, heading, self._range_m, time.time())

            time.sleep(self.POLL_INTERVAL_S)

    def clear_telemetry(self):
        """Forget the last fix so the radar stops re-sampling once the
        vehicle link is gone - otherwise it keeps refreshing off a stale
        position and looks live after a disconnect."""
        with self._lock:
            self._telem = None
        self._last_sample = None

    def stop(self):
        # A tile fetch can hold this thread for up to the 15s HTTP timeout,
        # so give it real time to unwind. terminate() is the last resort: a
        # QThread still running when Qt destroys it aborts the process, and
        # tile writes are atomic (see _load_tile), so a killed download
        # cannot leave a corrupt cache behind.
        self._running = False
        if not self.wait(5000):
            self.terminate()
            self.wait(1000)


class WaypointTerrainWorker(QThread):
    """
    Checks each waypoint against the ground underneath it, off the GUI
    thread.

    A waypoint carries a height relative to home. What decides whether it
    clears the ground is that height added to home's own height above sea
    level, compared with the terrain elevation at the waypoint - three
    numbers that live in three different places, which is why this is
    worth doing in one spot rather than at each call site.

    Nothing here changes what the aircraft flies. It answers one
    question for the map: how far above the ground each of these points
    would put the aeroplane, and so which of them would put it inside a
    hill.
    """

    # [(waypoint id, clearance in metres)] for every point it could
    # judge. Negative means the point is inside the hill. Points with no
    # terrain data are simply absent - which is not the same as a
    # clearance of zero, and must not be shown as one.
    result_ready = Signal(list)

    # A waypoint exactly at terrain height is already wrong, so the test
    # is "at or below". This adds nothing on top - no invented safety
    # buffer, because the number a pilot can reason about is the one they
    # typed, not that number plus a margin somebody chose for them.
    CLEARANCE_M = 0.0

    POLL_INTERVAL_S = 0.25

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._lock = threading.Lock()
        self._request = None        # (home_alt_amsl, [(id, lat, lon, rel_alt)])
        self._last_done = None      # the request last answered, to avoid rework
        self._provider = TerrainProvider()

    def check(self, home_alt_amsl, waypoints):
        """Thread-safe; call from the GUI thread whenever anything moves.

        waypoints is [(id, lat, lon, relative_alt_m)]. Points whose
        altitude is not yet decided should simply be left out.
        """
        with self._lock:
            self._request = (home_alt_amsl, tuple(waypoints))

    def clear(self):
        with self._lock:
            self._request = (None, ())
            self._last_done = None

    def run(self):
        while self._running:
            with self._lock:
                req = self._request
            if req is None or req == self._last_done:
                self.msleep(int(self.POLL_INTERVAL_S * 1000))
                continue

            home_alt, points = req
            clearances = []
            # Without home's height above sea level there is nothing to
            # compare against: a relative altitude alone says nothing
            # about the ground.
            if home_alt is not None:
                for wp_id, lat, lon, rel_alt in points:
                    if not self._running:
                        break
                    ground = self._provider.elevation(lat, lon)
                    if ground is None:
                        # No tile for this spot. Left out rather than
                        # guessed at: an unjudged waypoint reads as "not
                        # checked", where a number would read as known.
                        continue
                    clearances.append(
                        (int(wp_id), float(home_alt + rel_alt - ground)))

            if self._running:
                self._last_done = req
                self.result_ready.emit(clearances)

    def stop(self):
        self._running = False
        if not self.wait(5000):
            self.terminate()
            self.wait(1000)
