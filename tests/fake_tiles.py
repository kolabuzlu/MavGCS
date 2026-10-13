"""The Copernicus tile store, as terrain_provider sees it - for testing the
map's tiles without the network, and without waiting for one.

Not a test itself (run_all.py runs test_*.py only).

FakeStore stands in for urllib.request.urlopen while it is in use, and
points terrain_provider's disk cache at a folder of its own. Tiles are
real GeoTIFFs, written the way Copernicus writes them (PixelIsPoint, the
tiepoint on the north-west sample, one degree each), so they are read by
the provider's own decoder.
"""

import io
import os
import shutil
import tempfile
import urllib.error
import urllib.request

import numpy as np
import tifffile

import terrain_provider as P

URL_30 = P.BASE_URL
URL_90 = P.BASE_URL_90


def tile_bytes(lat_deg, lon_deg, fn, per_degree=120):
    """A one-degree tile's GeoTIFF, heights from fn(lats, lons)."""
    px = 1.0 / per_degree
    rows = (lat_deg + 1) - px * np.arange(per_degree)
    cols = lon_deg + px * np.arange(per_degree)
    grid = fn(rows[:, None] + 0 * cols[None, :],
              cols[None, :] + 0 * rows[:, None]).astype(np.float32)
    buf = io.BytesIO()
    tifffile.imwrite(buf, grid, extratags=[
        (33550, "d", 3, (px, px, 0.0), False),
        (33922, "d", 6, (0.0, 0.0, 0.0, float(lon_deg),
                         float(lat_deg + 1), 0.0), False)])
    return buf.getvalue()


def name_of(resolution, lat_deg, lon_deg):
    key = P.tile_name(lat_deg + 0.5, lon_deg + 0.5)
    return key if resolution == 30 else key.replace("_COG_10_", "_COG_30_", 1)


def url_of(resolution, lat_deg, lon_deg):
    name = name_of(resolution, lat_deg, lon_deg)
    return "%s/%s/%s.tif" % (URL_30 if resolution == 30 else URL_90, name, name)


class _Response:
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeStore:
    """The two buckets: tiles put in it are served, and anything else is
    answered as `otherwise` says - "missing" (S3's 404), "down" (no
    network at all) or "portal" (every address answered 404, as a sign-in
    page can). Everything asked for is kept in .asked."""

    def __init__(self, otherwise="missing"):
        self.tiles = {}             # url -> bytes
        self.otherwise = otherwise
        self.asked = []
        self._dir = None
        self._saved = None

    def put(self, resolution, lat_deg, lon_deg, fn, per_degree=120):
        self.tiles[url_of(resolution, lat_deg, lon_deg)] = tile_bytes(
            lat_deg, lon_deg, fn, per_degree)

    def urlopen(self, url, timeout=None):
        url = url if isinstance(url, str) else url.full_url
        self.asked.append(url)
        if self.otherwise == "down":
            raise urllib.error.URLError("no network")
        if url in self.tiles and self.otherwise != "portal":
            return _Response(self.tiles[url])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def asked_for(self, resolution, lat_deg, lon_deg):
        return url_of(resolution, lat_deg, lon_deg) in self.asked

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="mavgcs_tiles_")
        self._saved = (urllib.request.urlopen, P.CACHE_DIR)
        urllib.request.urlopen = self.urlopen
        P.CACHE_DIR = P.Path(self._dir)
        return self

    def __exit__(self, *exc):
        urllib.request.urlopen, P.CACHE_DIR = self._saved
        shutil.rmtree(self._dir, ignore_errors=True)
        return False

    def cached(self):
        """The tile files the provider kept on disk."""
        return sorted(os.listdir(self._dir))
