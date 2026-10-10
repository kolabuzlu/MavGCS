"""The mission being flown and the one being drawn are two routes.

Reported by the user on 2026-10-10: with a mission flying, a new draft
point placed inside a hill drew a red line to waypoint 1 of the mission
in the air. The terrain check had been handed one list - the draft, then
the mission - and judged a leg between every pair of neighbours in it,
including the one joining the two routes, which nothing will ever fly.
The map only draws a leg that crosses the ground, and a leg starting
inside a hill always does; from a point safely in the air it rarely
did, which is why it came and went. The fence check invented the same
leg.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as app_main
import terrain_provider as tp

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


M = app_main.MainWindow


def wp(i, lat, lon, alt=150.0):
    return {"id": i, "lat": lat, "lon": lon, "alt": alt, "cmd": "WAYPOINT",
            "frame": "RELATIVE"}


def window(sent, queue, **extra):
    """Just enough of MainWindow for the methods under test."""
    w = types.SimpleNamespace(_sent_mission=sent, _waypoint_queue=queue,
                              **extra)
    w._mission_routes = lambda: M._mission_routes(w)
    w._mission_points = lambda: M._mission_points(w)
    return w


def ids(routes):
    return [[p["id"] for p in r] for r in routes]


# The mission in the air, and a draft for the next one.
sent = [wp(1, 40.012, 29.010), wp(2, 40.025, 29.015), wp(3, 40.020, 29.012)]
draft = [wp(4, 40.025, 29.045), wp(5, 40.012, 29.050)]

print("")
print("1. which points make which route")
note("drawing a first mission: one route",
     ids(M._mission_routes(window([], draft))) == [[4, 5]])
note("flying one, drawing the next: two routes, the one in the air first",
     ids(M._mission_routes(window(sent, draft))) == [[1, 2, 3], [4, 5]],
     ids(M._mission_routes(window(sent, draft))))
note("while a mission is started - the same points in both - one route",
     ids(M._mission_routes(window(sent, list(sent)))) == [[1, 2, 3]])
note("nothing drawn, nothing sent: no routes",
     M._mission_routes(window([], [])) == [])
note("every point is still listed once, for what is asked of each alone",
     [p["id"] for p in M._mission_points(window(sent, draft))]
     == [1, 2, 3, 4, 5])


print("")
print("2. the terrain check is told where each route ends")


class Worker:
    def check(self, home_alt, points, ends=()):
        self.args = (home_alt, [p[0] for p in points], list(ends))


worker = Worker()
M._recheck_waypoint_terrain(window(sent, draft, _mission_default_alt=150.0,
                                   _home_alt_amsl=900.0,
                                   wp_terrain_worker=worker))
note("both routes are checked", worker.args[1] == [1, 2, 3, 4, 5],
     worker.args[1])
note("and each route's last point is marked as its end",
     worker.args[2] == [3, 5], worker.args[2])


print("")
print("3. and no leg is judged between them")


class FlatGround:
    def __init__(self, metres):
        self.metres = metres

    def elevation(self, _lat, _lon):
        return self.metres

    def retry_pending(self):
        return False


answers = []
judge = tp.WaypointTerrainWorker.__new__(tp.WaypointTerrainWorker)
judge._provider = FlatGround(1000.0)    # ground at 1000 m
judge._running = True
judge.result_ready = types.SimpleNamespace(
    emit=lambda clearances, legs: answers.append((clearances, legs)))
# Home at 900 m: every point at 150 m relative is at 1050, clear by 50 -
# except the newest draft point, placed at 50 m and so 50 m inside the
# hill: the user's case.
points = [(1, 40.012, 29.010, 150.0, "RELATIVE"),
          (2, 40.025, 29.015, 150.0, "RELATIVE"),
          (3, 40.020, 29.012, 150.0, "RELATIVE"),
          (4, 40.025, 29.045, 150.0, "RELATIVE"),
          (5, 40.012, 29.050, 50.0, "RELATIVE")]
judge._answer((900.0, tuple(points), frozenset([3, 5])))
clearances, legs = answers[-1]
pairs = [(a, b) for a, b, _m in legs]
note("legs within each route are judged", pairs == [(1, 2), (2, 3), (4, 5)],
     pairs)
note("none joins the end of one route to the start of the other",
     (3, 4) not in pairs and (5, 1) not in pairs)
note("the draft leg into the hill is still caught",
     any(a == 4 and b == 5 and m <= 0 for a, b, m in legs), legs)
judge._answer((900.0, tuple(points), frozenset()))
note("(given no ends, the old way, the joining leg appears - the bug)",
     (3, 4) in [(a, b) for a, b, _m in answers[-1][1]])


print("")
print("4. the fence check walks each route on its own")
# A U: the notch at the top, between longitudes 29.02 and 29.04, is
# outside. The mission in the air sits in the left arm, the draft in the
# right, and a straight line between the two crosses the notch.
fence = [(40.00, 29.00), (40.03, 29.00), (40.03, 29.02), (40.01, 29.02),
         (40.01, 29.04), (40.03, 29.04), (40.03, 29.06), (40.00, 29.06)]
told, said = [], []
w = window(sent, draft, _fence_points=fence, _last_fence_warning=([], []),
           map_view=types.SimpleNamespace(
               set_fence_violations=lambda o, l: told.append((o, l))),
           on_command_feedback=said.append)
M._recheck_fence_containment(w)
note("both routes inside, and no leg between them is reported",
     told[-1] == ([], []), told[-1])
note("so nothing is said about leaving the fence", not said, said)
# A draft leg that really does leave: its third point back in the left
# arm, so the leg to it crosses the notch.
w._waypoint_queue = draft + [wp(6, 40.020, 29.008)]
M._recheck_fence_containment(w)
note("a draft leg that does leave the fence is still reported",
     told[-1] == ([], [(5, 6)]), told[-1])

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
