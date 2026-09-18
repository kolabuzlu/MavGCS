"""Does the terrain radar say what it is doing, or only that it has nothing?

The radar showed "Terrain Radar - no data" for the entire duration of a
tile download, and the Live AGL panel hid itself completely. A Copernicus
tile is up to 41MB; on a fresh install, on a slow link, that is minutes
of an app that looks broken while it is in fact working.

It cost a real half-hour: a macOS build was reported as faulty, three
hypotheses were investigated - a packaging failure, a certificate
failure, a missing codec - and the answer was that the cache was empty
and the ground was still arriving.

The download reports itself now. These are the sentences it can say.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import terrain_provider as tp

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


print("")
print("1. what it says, and when")

p = tp.TerrainProvider()

note("silent when there is nothing to report", p.fetch_state() is None,
     repr(p.fetch_state()))

# Mid-download, size known from Content-Length.
p._fetching["tile"] = (10_000_000, 41_000_000)
state = p.fetch_state()
note("a download in flight says so", state is not None and "Download" in state,
     repr(state))
note("and how far along it is", "24%" in (state or ""), repr(state))
note("in megabytes, not bytes", "41 MB" in (state or ""), repr(state))

# Some servers send no Content-Length.
p._fetching["tile"] = (10_000_000, 0)
state = p.fetch_state()
note("an unknown size still reports progress",
     state is not None and "10 MB" in state and "%" not in state, repr(state))

# Two tiles at once - the fan and the track profile can straddle a
# boundary, which is what the seam fix was about.
p._fetching = {"a": (5_000_000, 10_000_000), "b": (5_000_000, 30_000_000)}
state = p.fetch_state()
note("two tiles at once are reported as one job",
     "25%" in (state or ""), repr(state))

p._fetching = {}
p._failed = {"tile": time.monotonic() + 15.0}
state = p.fetch_state()
note("a failure waiting to retry is distinguished from nothing",
     state is not None and "retry" in state.lower(), repr(state))

p._failed = {}
note("and silence returns when it is over", p.fetch_state() is None)

print("")
print("2. telling somebody")

said = []
q = tp.TerrainProvider(on_status=said.append)

q._fetching["tile"] = (1_000_000, 41_000_000)
q._notify(force=True)
note("the callback is called", len(said) == 1, repr(said[-1] if said else None))

# Throttled: 164 chunks arrive for one tile and the text barely changes
# between them.
before = len(said)
for mb in range(2, 12):
    q._fetching["tile"] = (mb * 1_000_000, 41_000_000)
    q._notify()
note("but not once per chunk", len(said) - before <= 1,
     "%d calls for 10 chunks" % (len(said) - before))

time.sleep(0.3)
q._fetching["tile"] = (20_000_000, 41_000_000)
q._notify()
note("it does keep up over time", len(said) - before == 1,
     "%d call after the throttle window, from 11 notifies"
     % (len(said) - before))

# The end of the job matters more than the middle of it.
q._fetching = {}
q._notify(force=True)
note("and says when there is nothing left to report", said[-1] is None,
     repr(said[-1]))

print("")
print("3. a callback that throws cannot stop a download")

boom = tp.TerrainProvider(on_status=lambda _s: 1 / 0)
boom._fetching["tile"] = (1, 2)
try:
    boom._notify(force=True)
    note("a broken listener is survived", True)
except Exception as exc:
    note("a broken listener is survived", False, repr(exc))

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
