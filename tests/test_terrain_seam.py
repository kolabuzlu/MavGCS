"""Does the ground exist all the way to the edge of a DEM tile?

Copernicus tiles are PixelIsPoint: the tiepoint is the CENTRE of pixel
(0, 0), so a one-degree tile of N samples has its last sample one sample
short of the next tile's first. Bounding the lookup on the last sample
rather than on the tile's extent left a strip along every edge answering
"no elevation" with good ground on both sides of it - about 24 m wide on
a real 3600-wide tile, and found on a real one at longitude 35.

Synthetic tiles here, deliberately: the shape of the bug is in the
indexing, not in the data, and a test that needs a 38 MB download is a
test that stops being run.
"""

import os
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from terrain_provider import _TileData

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


# Four samples across one degree, so the "last sample to the next tile"
# strip is a quarter of the tile and easy to read. Structurally identical
# to 3600 samples across one degree; only the scale differs.
N = 4
PX = 1.0 / N


def ramp_tile(origin_lat, origin_lon, base):
    """Elevation rising 10 m per column, flat in latitude."""
    grid = np.zeros((N, N), dtype=np.float32)
    for c in range(N):
        grid[:, c] = base + c * 10.0
    return _TileData(grid, origin_lat, origin_lon, PX, PX)


# Two tiles meeting at longitude 1.0, continuing the same ramp across the
# join the way real neighbouring tiles do.
west = ramp_tile(1.0, 0.0, 0.0)          # samples at lon 0, .25, .5, .75
east = ramp_tile(1.0, 1.0, 40.0)         # samples at lon 1, 1.25, ...
LAT = 0.5                                # comfortably inside both


def owner(lon):
    """Which tile the provider would choose - the floor of the coordinate."""
    import math
    return west if math.floor(lon) == 0 else east


print("")
print("interior sampling is unchanged")
note("exactly on a sample", west.sample(LAT, 0.25) == 10.0,
     repr(west.sample(LAT, 0.25)))
note("halfway between two", abs(west.sample(LAT, 0.375) - 15.0) < 1e-5,
     repr(west.sample(LAT, 0.375)))
note("the first sample", west.sample(LAT, 0.0) == 0.0)
note("the last sample", west.sample(LAT, 0.75) == 30.0)

print("")
print("the strip past the last sample - this was the bug")
strip = [0.7501, 0.80, 0.90, 0.99, 0.9999]
answered = [west.sample(LAT, x) for x in strip]
note("every longitude in it now answers",
     all(v is not None for v in answered),
     "%d of %d" % (sum(v is not None for v in answered), len(strip)))
# float32 grid, so a fraction of 0.9999 lands a hair under 30. The claim
# is that the strip reads the edge sample, not that arithmetic is exact.
note("and answers with the edge sample, not something invented",
     all(abs(v - 30.0) < 1e-4 for v in answered if v is not None),
     repr(answered))

print("")
print("the same strip in latitude")
# origin_lat is the NORTH edge, rows run south: last sample at 1.0 - 0.75.
lat_strip = [0.2499, 0.20, 0.10, 0.0001]
lat_answered = [west.sample(y, 0.25) for y in lat_strip]
note("every latitude in it answers",
     all(v is not None for v in lat_answered),
     "%d of %d" % (sum(v is not None for v in lat_answered), len(lat_strip)))

print("")
print("both at once - the tile's far corner")
note("the corner answers", west.sample(0.0001, 0.9999) is not None,
     repr(west.sample(0.0001, 0.9999)))

print("")
print("nothing outside the tile is claimed")
note("past the tile's own degree", west.sample(LAT, 1.0) is None)
note("well past it", west.sample(LAT, 1.5) is None)
note("before it", west.sample(LAT, -0.0001) is None)
note("north of it", west.sample(1.0001, 0.25) is None)
note("south of it", west.sample(-0.0001, 0.25) is None)

print("")
print("walking across the join, as a flight would")
lon = 0.70
holes = []
walk = []
while lon <= 1.30001:
    got = owner(lon).sample(LAT, lon)
    walk.append((lon, got))
    if got is None:
        holes.append(lon)
    lon += PX / 8.0
note("no longitude is left without ground", not holes,
     "%d holes" % len(holes))
note("the two tiles hand over exactly once",
     owner(0.9999) is west and owner(1.0) is east)
# Not a magnitude claim: with four samples to the degree, one sample IS a
# quarter of a degree, so any bound stated in metres here would be an
# artefact of the toy scale rather than of the code. On a real 3600-wide
# tile one sample is about 24 m, and that is the whole error the clamp
# can introduce. What is worth asserting is that the handover lands on
# the right samples.
note("the west tile hands over holding its edge sample",
     abs(west.sample(LAT, 0.9999) - 30.0) < 1e-4,
     repr(west.sample(LAT, 0.9999)))
note("and the east tile takes over on its first sample, exactly",
     east.sample(LAT, 1.0) == 40.0, repr(east.sample(LAT, 1.0)))
note("so the clamp is nearest-neighbour within one sample, never a hole",
     west.sample(LAT, 0.9999) is not None
     and east.sample(LAT, 1.0) is not None)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
