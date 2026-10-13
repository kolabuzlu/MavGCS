"""Where the map's heights come from: 30 m, 90 m, or open sea at 0 m.

What the pilot asked for (2026-10-13), as the two places Copernicus's 30 m
map has no tile: open sea, where Mission Planner and ArduPilot's own
terrain server put 0 m and MavGCS loaded nothing; and the cells whose 30 m
tiles the Copernicus programme withholds - Armenia, Azerbaijan, and every
cell touching them, which in Turkey is Kars, Igdir and Cildir.

1. The built-in list of cells (copernicus_cells, from Copernicus's own
   tile lists) says which is which.
2. The provider answers from it: sea without asking the network at all,
   and never sea for land - not when the store says "not found" to
   everything, as a hotel's sign-in page can, nor with no network. A
   withheld cell is read from the 90 m tile, kept on disk like any other.
3. A square's heights say where they came from.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("MAVLINK20", "1")

import numpy as np

import copernicus_cells as C
import terrain_provider as P
import terrain_upload as T
from fake_tiles import FakeStore, name_of

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail != "" else ""))
    if not ok:
        fails.append(name)


# Tilted planes: exact under interpolation, and continuous across degree
# lines as real ground is - a tile's first row lies on its north line.
def ground_30(lats, lons):
    return 1100.0 + 300.0 * (np.asarray(lats) - 39.0) \
        + 200.0 * (np.asarray(lons) - 31.0)


def ground_90(lats, lons):
    # A different ground, so which tile answered can be told.
    return 2000.0 + 250.0 * (np.asarray(lats) - 40.0) \
        + 150.0 * (np.asarray(lons) - 43.0)


# --------------------------------------------------------------------------
print("")
print("1. the cells Copernicus covers, and how")
land = sum(bin(b).count("1") for b in C._LAND)
note("the list holds every cell of the 90 m set", land == C.CELLS_90M,
     "%d bits, %d cells" % (land, C.CELLS_90M))
note("and the 30 m set is all of it but the withheld cells",
     C.CELLS_30M + len(C.WITHHELD_30M) == C.CELLS_90M
     and len(C.WITHHELD_30M) == 25,
     "%d + %d" % (C.CELLS_30M, len(C.WITHHELD_30M)))
note("every withheld cell is land",
     all(C.kind(a, b) == "90 m" for a, b in C.WITHHELD_30M))
want = {(39, 32): "30 m",       # Ankara
        (41, 28): "30 m",       # Istanbul: land, the Black Sea, Marmara
        (36, 31): "30 m",       # the coast at Alanya
        (46, 51): "30 m",       # the Caspian: inland water has tiles
        (40, 43): "90 m",       # Kars
        (39, 44): "90 m",       # Igdir and Ararat
        (41, 43): "90 m",       # Cildir
        (42, 31): "sea",        # the Black Sea off Zonguldak
        (35, 31): "sea",        # the Mediterranean south of Antalya
        (0, -30): "sea"}        # the Atlantic
wrong = {c: C.kind(*c) for c, k in want.items() if C.kind(*c) != k}
note("each cell as Copernicus's lists say", not wrong, wrong)
note("longitude wraps round, and there is no cell past the pole",
     C.kind(41, -332) == "30 m" and C.kind(41, 388) == "30 m"
     and C.kind(90, 0) == "sea")


# --------------------------------------------------------------------------
print("")
print("2. the provider")
with FakeStore(otherwise="down") as store:
    p = P.TerrainProvider()
    sea = p.elevation(42.5, 31.5)
    tile, state = p.tile_for(42.5, 31.5)
    note("open sea is 0 m, with no network at all",
         sea == 0.0 and state == "ok" and tile.source == "sea",
         (sea, state, getattr(tile, "source", None)))
    note("and nothing was asked of the network for it", not store.asked,
         store.asked)
    note("with no network, land is not known - and never 0 m",
         p.elevation(39.5, 32.5) is None
         and p.tile_for(39.5, 32.5) == (None, "failed"))

with FakeStore(otherwise="portal") as store:
    # Every address answered "not found", as a sign-in page can: land must
    # not turn into sea, which would be 0 m under the aircraft over a hill.
    store.put(30, 39, 32, ground_30)
    p = P.TerrainProvider()
    got = p.tile_for(39.5, 32.5)
    note("a store answering 'not found' to everything: land has no data, "
         "and is not sea", got == (None, "absent")
         and p.elevation(39.5, 32.5) is None, got)
    note("(it asked for the 30 m tile and then the 90 m one)",
         store.asked_for(30, 39, 32) and store.asked_for(90, 39, 32),
         store.asked)

with FakeStore() as store:
    store.put(90, 40, 43, ground_90)
    p = P.TerrainProvider()
    lat, lon = 40.6013, 43.0975     # Kars
    tile, state = p.tile_for(lat, lon)
    h = p.elevation(lat, lon)
    note("a withheld cell is read from the 90 m tile",
         state == "ok" and tile.source == "90 m"
         and abs(h - float(ground_90(lat, lon))) < 0.01,
         (state, getattr(tile, "source", None), h))
    note("without asking for the 30 m tile it is known not to have",
         not store.asked_for(30, 40, 43), store.asked)
    note("and the 90 m tile is kept on disk like any other",
         store.cached() == [name_of(90, 40, 43) + ".tif"], store.cached())
    store.otherwise = "down"
    store.tiles.clear()
    p2 = P.TerrainProvider()
    h2 = p2.elevation(lat, lon)
    note("so it is there with no network, next time",
         h2 is not None and h2 == h, (h2, h))

with FakeStore() as store:
    store.put(30, 39, 32, ground_30)
    store.put(90, 39, 32, ground_90)
    p = P.TerrainProvider()
    tile, state = p.tile_for(39.5, 32.5)
    note("where the 30 m tile is there, it is the one read",
         tile.source == "30 m" and not store.asked_for(90, 39, 32)
         and abs(p.elevation(39.5, 32.5)
                 - float(ground_30(39.5, 32.5))) < 0.01,
         (tile.source, store.asked))

with FakeStore() as store:
    # A cell the 30 m list names, whose tile the 30 m store no longer has.
    store.put(90, 39, 32, ground_90)
    p = P.TerrainProvider()
    tile, state = p.tile_for(39.5, 32.5)
    note("and should the 30 m store lose a tile, the 90 m one answers",
         state == "ok" and tile.source == "90 m", (state, store.asked))


# --------------------------------------------------------------------------
print("")
print("3. a square's heights, and where they came from")
with FakeStore() as store:
    store.put(90, 40, 43, ground_90)
    store.put(30, 41, 31, ground_30)
    p = P.TerrainProvider()

    sq = T.square_at(int(42.5e7), int(31.5e7), 100)[0]
    heights, state = T.square_heights(p, sq, 100)
    note("a square over open sea: 0 m throughout, and said to be sea",
         heights is not None and state == "ok, sea"
         and not heights.any(), state)

    sq = T.square_at(int(40.6e7), int(43.1e7), 100)[0]
    heights, state = T.square_heights(p, sq, 100)
    lats, lons = T.square_points(sq, 100)
    note("a square in Kars: the 90 m map's heights, rounded up, and said",
         heights is not None and state == "ok, 90 m"
         and np.array_equal(heights, np.ceil(ground_90(lats, lons))
                            .astype(np.int16)), state)

    # The last row of squares of a degree reaches past its north line: at
    # 42 N off Zonguldak that is the open sea.
    sq = T.square_at(int(41.995e7), int(31.5e7), 100)[0]
    heights, state = T.square_heights(p, sq, 100)
    lats, lons = T.square_points(sq, 100)
    north = lats >= 42.0
    note("a square across the coast's degree line: land, and 0 m past it",
         heights is not None and state == "ok, sea" and north.any()
         and (~north).any() and not heights[north].any()
         and np.array_equal(heights[~north], np.ceil(
             ground_30(lats[~north], lons[~north])).astype(np.int16)),
         (state, int(north.sum()), int((~north).sum())))

with FakeStore(otherwise="portal") as store:
    p = P.TerrainProvider()
    sq = T.square_at(int(39.5e7), int(32.5e7), 100)[0]
    note("land the store will not give is still not loaded, never as sea",
         T.square_heights(p, sq, 100) == (None, "absent"))

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
