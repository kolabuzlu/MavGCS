"""Does what the app pushes at the map survive the page not existing yet?

MapView loads its page with setHtml at the end of its constructor, and
the application starts pushing to it immediately. Everything sent before
the page's script has run used to hit a ReferenceError and vanish into a
console nobody reads. Four calls land inside that window on a normal
launch:

    setReturnHome   setCogStatus   setTileCacheStats   setTerrainCacheStats

All four of them self-correct, and the first version of this file said
otherwise. It claimed the cache readouts were pushed once at startup and
never again, making a lost call permanent for the session. Wrong:
_tile_stats_timer in main.py fires every 2000ms and pushes the cache
figures and the centre of gravity, and the return-home badge arrives with
telemetry. The claim came from grepping for a timer under the wrong name,
finding none, and believing it.

So the cost was four errors in a console nobody reads and up to two
seconds of blank readouts at startup. Cosmetic.

Which is the honest reason these checks exist: not that anything visible
was broken, but that a statement sent to the page could vanish without
trace. The next one somebody adds may not have a timer behind it, and
then it is gone for good with nothing on screen to say so.

It was intermittent because it is a race: whether the push beats the
load varies run to run. That is also why nobody had found it by using
the program.

And the first version of the fix broke something real. Section 6 then
demanded that every call to the page go through the queue, so the two
questions the app asks the page went through it too - and the queue
takes no callback. Both raised on every call, main.py swallowed it, and
V2.3.2 never learned which graphics card it was on: the first launch
never restarted onto the discrete card. The user noticed; no check here
did, because none of them ever asked the page a question. Sections 7
and 8 do.
"""

import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from map_view import MapView

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


class FakePage:
    """Records what would have reached the page, and answers questions."""

    answer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU)"

    def __init__(self):
        self.ran = []
        self.asked = []

    def runJavaScript(self, script, callback=None):
        if callback is None:
            self.ran.append(script)
        else:
            self.asked.append(script)
            callback(self.answer)


def fresh():
    """A MapView with the queue in its just-constructed state.

    Built without __init__ deliberately: a real one starts a browser
    engine and loads a page over a tile proxy, and none of that is what
    is under test. The three attributes below are exactly what the
    constructor sets up for this, so the object is in the state the race
    happens in.
    """
    view = MapView.__new__(MapView)
    view._page_ready = False
    view._pending_js = []
    page = FakePage()
    view.page = lambda: page
    return view, page


print("")
print("1. before the page exists")

view, page = fresh()
view.update_position(41.0, 29.0, 90.0)
note("nothing is sent to a page that cannot run it", page.ran == [],
     "%d statements" % len(page.ran))
note("and nothing is thrown away either", len(view._pending_js) == 1,
     "%d held" % len(view._pending_js))

print("")
print("2. the four that were being lost")

view, page = fresh()
view.set_return_home("home", "Home", "410 of 900 mAh", "62")
view.set_cog_status("ok", "Balanced", 0.4)
view.update_tile_cache_stats(1200, 50_000_000, 200_000_000)
view.update_terrain_cache_stats(41, 528_000_000, 1_000_000_000)
held = " ".join(view._pending_js)
for fn in ("setReturnHome", "setCogStatus",
           "setTileCacheStats", "setTerrainCacheStats"):
    note("%s is held rather than lost" % fn, fn in held)

view._on_load_finished(True)
sent = " ".join(page.ran)
for fn in ("setReturnHome", "setCogStatus",
           "setTileCacheStats", "setTerrainCacheStats"):
    note("%s reaches the page once it loads" % fn, fn in sent)
note("and the queue is emptied behind them", view._pending_js == [],
     "%d left" % len(view._pending_js))

print("")
print("3. order, and what happens after")

view, page = fresh()
for lat in (1.0, 2.0, 3.0):
    view.update_position(lat, 0.0, 0.0)
view._on_load_finished(True)
note("held statements replay in the order they were made",
     [s.split("(")[1].split(",")[0] for s in page.ran] == ["1.0", "2.0", "3.0"],
     " then ".join(s.split("(")[1].split(",")[0] for s in page.ran))
# Replayed rather than collapsed to the last one. Collapsing is sound for
# setters, which is all this page's API is today, and a silent fault the
# day somebody adds a call that accumulates.
note("all of them, not just the newest", len(page.ran) == 3,
     "%d statements" % len(page.ran))

before = len(page.ran)
view.update_position(4.0, 0.0, 0.0)
note("once ready, statements go straight through",
     len(page.ran) == before + 1 and view._pending_js == [])

print("")
print("4. when the load fails")

view, page = fresh()
view.update_tile_cache_stats(1, 2, 3)
view._on_load_finished(False)
note("a failed load runs nothing", page.ran == [])
# Dropping the queue here would turn a page that recovers on a reload
# into one that comes back blank.
note("and holds on to what it has, for the reload",
     len(view._pending_js) == 1, "%d held" % len(view._pending_js))
note("the view does not call itself ready", view._page_ready is False)

print("")
print("5. a page that never loads at all")

view, page = fresh()
for i in range(MapView.MAX_PENDING_JS + 250):
    view.update_position(float(i), 0.0, 0.0)
note("the queue is bounded",
     len(view._pending_js) == MapView.MAX_PENDING_JS,
     "%d held, cap %d" % (len(view._pending_js), MapView.MAX_PENDING_JS))
# Oldest dropped, so what survives is the most recent state - which for
# a page of setters is the state worth having.
note("and what it keeps is the newest",
     view._pending_js[-1].startswith("updatePosition(%.1f" %
                                     (MapView.MAX_PENDING_JS + 249)),
     view._pending_js[-1][:40])

print("")
print("6. every statement goes through it, and nothing else does")

# The fix is only worth as much as its coverage: one statement still
# reaching for the page directly is one readout that still races. So
# exactly four methods may call the page directly: the runner's two, and
# the two questions, which must not be held (see 7). Named, not counted -
# a bare count is how the questions were forced into the queue before.
source = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "map_view.py"), encoding="utf-8").read()
tree = ast.parse(source)
direct = set()
for fn in ast.walk(tree):
    if isinstance(fn, ast.FunctionDef):
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "runJavaScript"):
                direct.add(fn.name)
note("only the runner and the two questions talk to the page directly",
     direct == {"_run_js", "_on_load_finished",
                "dump_graphics_adapter", "dump_draw_state"},
     ", ".join(sorted(direct)))
note("and every statement goes through _run_js",
     source.count("self._run_js(") >= 40,
     "%d routed" % source.count("self._run_js("))

print("")
print("7. the two questions get their answers")

# Before the page is up as well as after: asked early, a question goes
# to the page anyway and comes back empty, and the app asks again. What
# it must never do is raise, or sit in a queue that cannot answer it.
for label, ask in (("which graphics card", MapView.dump_graphics_adapter),
                   ("what is being drawn", MapView.dump_draw_state)):
    for ready in (False, True):
        view, page = fresh()
        view._page_ready = ready
        got = []
        try:
            ask(view, got.append)
            error = None
        except Exception as exc:
            error = exc
        note("'%s', asked %s, is answered" % (
                 label, "once the page is up" if ready else "early"),
             error is None and got == [FakePage.answer],
             repr(error) if error else "")
        note("and is not left waiting in the queue", view._pending_js == [],
             "%d held" % len(view._pending_js))

print("")
print("8. nothing hands the runner a callback it would drop")

# The shape of the V2.3.2 mistake, checked everywhere at once: _run_js
# takes one statement and returns nothing, so a call passing it anything
# more is a question that will never be answered.
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "_run_js"]
wrong = ["line %d" % n.lineno for n in calls
         if len(n.args) != 1 or n.keywords]
note("every _run_js call passes exactly one statement", not wrong,
     ", ".join(wrong) if wrong else "%d calls" % len(calls))

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
