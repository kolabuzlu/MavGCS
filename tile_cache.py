"""
Offline map support: a tiny local HTTP server that sits between Leaflet and
the online tile providers.

The map's layers point at this server rather than straight at Google/OSM/
Esri. For every tile it:

  1. serves it from disk if we already have it, so a previously-viewed area
     keeps working with no internet at all; otherwise
  2. fetches it upstream, hands it over, and (when caching is enabled)
     writes it to disk for next time.

A proxy rather than the browser's own HTTP cache: that cache obeys the
providers' expiry headers and evicts whatever it likes, so it can't be
relied on to still hold your flying site next weekend. Here, a tile that
has been saved stays saved until deleted.

Tiles land in <data_dir>/map_cache/<layer>/<z>/<x>/<y>.tile - one small
file each (typically 10-30 KB), so a working area over several zoom levels
runs to tens or low hundreds of MB.
"""

import shutil
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app_paths import data_dir, resource_path

CACHE_ROOT = data_dir() / "map_cache"
# Leaflet, shipped with the app rather than fetched from a CDN, so the map
# works with no internet at all.
LIB_ROOT = Path(resource_path("vendor")) / "leaflet"

# Upstream templates, keyed by the short name used in the map's tile URLs.
# {s} is a subdomain slot, filled from SUBDOMAINS below.
PROVIDERS = {
    "google":  "https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
    "osm":     "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "esri":    "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "esriref": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
}
SUBDOMAINS = {"google": ["0", "1", "2", "3"], "osm": ["a", "b", "c"]}

# OSM's tile policy expects a real identifying User-Agent.
USER_AGENT = "MavGCS (https://github.com/kolabuzlu/MavGCS)"
FETCH_TIMEOUT_S = 10

# 1x1 transparent PNG, returned for a tile we have neither cached nor can
# reach. Leaflet treats a 404 as an error tile and retries; handing back a
# valid empty image keeps an offline map quiet and clean instead.
_BLANK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _content_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


class _Handler(BaseHTTPRequestHandler):
    # Injected by TileCacheServer.
    server_version = "MavGCSTiles/1.0"

    def log_message(self, fmt, *args):
        pass  # stay out of stdout; tiles are far too chatty to log

    def do_GET(self):
        parts = self.path.strip("/").split("/")

        # /lib/... - Leaflet itself, served locally. It used to come from a
        # CDN, which meant that with no internet the library never loaded
        # and the whole map page died: no tiles, no layer switcher, no
        # terrain radar. Cached tiles are worthless if the thing that draws
        # them is missing.
        if parts and parts[0] == "lib":
            self._serve_lib(parts[1:])
            return

        # /t/<layer>/<z>/<x>/<y>
        if len(parts) != 5 or parts[0] != "t" or parts[1] not in PROVIDERS:
            self.send_error(404)
            return
        _, layer, z, x, y = parts
        if not (z.isdigit() and x.isdigit() and y.isdigit()):
            self.send_error(404)
            return

        blob = self.server.tile_cache.get_tile(layer, int(z), int(x), int(y))
        if blob is None:
            blob = _BLANK_PNG
        self.send_response(200)
        self.send_header("Content-Type", _content_type(blob))
        self.send_header("Content-Length", str(len(blob)))
        # Let the browser keep its own short-term copy too, so panning back
        # and forth doesn't even reach this server.
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Leaflet abandons tiles as you pan; not an error

    def _serve_lib(self, rel_parts):
        """Serve a file from the bundled vendor/leaflet directory."""
        # Refuse anything that tries to climb out of the vendor directory.
        if not rel_parts or any(p in ("", ".", "..") for p in rel_parts):
            self.send_error(404)
            return
        path = LIB_ROOT.joinpath(*rel_parts)
        try:
            path = path.resolve()
            path.relative_to(LIB_ROOT.resolve())
            blob = path.read_bytes()
        except (OSError, ValueError):
            self.send_error(404)
            return
        types = {".js": "application/javascript", ".css": "text/css",
                 ".png": "image/png", ".svg": "image/svg+xml"}
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TileCacheServer:
    """
    Local tile proxy. Starts on an ephemeral port on 127.0.0.1; the port is
    handed to the map so its layer URLs can point here.
    """

    def __init__(self):
        self._httpd = None
        self._thread = None
        self.port = None
        # Reads from the cache always happen - that's what makes a
        # previously-visited area work offline. The limit only governs
        # whether newly-fetched tiles are WRITTEN, and how much we keep.
        # 0 means "No Cache": serve what's already saved, save nothing new.
        self.size_limit_bytes = 0
        self._lock = threading.Lock()
        self._bytes = 0
        self._count = 0
        self._scanned = False

    @property
    def caching_enabled(self) -> bool:
        return self.size_limit_bytes > 0

    def set_size_limit(self, limit_bytes: int):
        self.size_limit_bytes = max(0, int(limit_bytes))
        if self.caching_enabled:
            self._enforce_limit()

    def _scan_existing(self):
        """Total up what's already on disk. Done once, off the GUI thread -
        a full cache can hold tens of thousands of files."""
        count = size = 0
        if CACHE_ROOT.exists():
            for p in CACHE_ROOT.rglob("*.tile"):
                try:
                    size += p.stat().st_size
                    count += 1
                except OSError:
                    pass
        with self._lock:
            self._bytes, self._count, self._scanned = size, count, True

    def stats(self):
        """(tile count, bytes used) - cheap, from the running totals."""
        with self._lock:
            return self._count, self._bytes

    def clear(self):
        """Delete every cached tile."""
        try:
            shutil.rmtree(CACHE_ROOT, ignore_errors=True)
        except OSError:
            pass
        with self._lock:
            self._bytes = self._count = 0

    def _enforce_limit(self):
        """
        Trim the cache back under its limit, oldest tile first.

        Only runs when actually over the limit, and then trims to 90% so a
        cache sitting exactly at the boundary doesn't re-scan on every
        single tile that follows.
        """
        with self._lock:
            over = self._bytes > self.size_limit_bytes > 0
        if not over:
            return
        try:
            entries = []
            for p in CACHE_ROOT.rglob("*.tile"):
                try:
                    st = p.stat()
                    entries.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
        except OSError:
            return
        entries.sort()  # oldest first
        target = int(self.size_limit_bytes * 0.9)
        freed = removed = 0
        with self._lock:
            current = self._bytes
        for _, size, path in entries:
            if current - freed <= target:
                break
            try:
                path.unlink()
                freed += size
                removed += 1
            except OSError:
                pass
        with self._lock:
            self._bytes = max(0, self._bytes - freed)
            self._count = max(0, self._count - removed)

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.tile_cache = self
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="tile-cache", daemon=True)
        self._thread.start()
        # Totalling up an existing cache means stat()-ing potentially tens
        # of thousands of files, so keep it off the GUI thread.
        threading.Thread(target=self._scan_existing,
                         name="tile-cache-scan", daemon=True).start()
        return self.port

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @staticmethod
    def _path_for(layer, z, x, y) -> Path:
        return CACHE_ROOT / layer / str(z) / str(x) / f"{y}.tile"

    def get_tile(self, layer, z, x, y):
        """Cached bytes if we have them, else fetch upstream. None if the
        tile is neither on disk nor reachable (i.e. offline and not saved)."""
        path = self._path_for(layer, z, x, y)
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            pass

        blob = self._fetch(layer, z, x, y)
        if blob and self.caching_enabled:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Write-then-rename: a half-written tile would otherwise be
                # served as a corrupt image forever after.
                tmp = path.with_suffix(".part")
                tmp.write_bytes(blob)
                tmp.replace(path)
                with self._lock:
                    self._bytes += len(blob)
                    self._count += 1
            except OSError:
                pass
            self._enforce_limit()
        return blob

    def _fetch(self, layer, z, x, y):
        subs = SUBDOMAINS.get(layer)
        sub = subs[(x + y) % len(subs)] if subs else ""
        url = PROVIDERS[layer].format(s=sub, z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, socket.timeout):
            return None  # offline, or the provider refused this tile

