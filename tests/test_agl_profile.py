"""Does the AGL panel say the right thing about the ground ahead?

Two halves, tested separately because they live in different languages.

The sampling is Python: track_profile walks the course from behind the
aircraft to well ahead of it, and the distances it picks are what decides
whether the picture is of the right piece of ground.

The arithmetic that matters is JavaScript: which number is shown as the
height above ground, which as the smallest gap ahead, and whether that
one is coloured as a warning. Rather than reimplementing it here and
testing the copy, the real functions are lifted out of the page the app
serves and run in a headless browser against a stub of the SVG they
expect. If the page changes, this follows it.
"""

import os
import re
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import map_view
from terrain_provider import TerrainProvider, dest_point

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- python

class FlatGround(TerrainProvider):
    """Elevation that depends only on how far east you are, so the
    distance a sample was taken at can be read back out of its height."""

    def __init__(self):
        pass

    def elevation(self, lat, lon):
        # cos(lat), because a degree of longitude is that much shorter
        # away from the equator - without it this "ruler" reads about 29%
        # long at lat 39 and the span looks wrong when it is not.
        import math
        return round((lon - 32.0) * 111320.0 * math.cos(math.radians(39.0)), 3)


print("")
print("where the ground is sampled")
p = FlatGround()
prof = p.track_profile(39.0, 32.0, 90.0, behind_m=400.0, ahead_m=1600.0,
                       samples=41)
note("one value per sample", len(prof) == 41, len(prof))
note("the first is astern", prof[0] < -390, "%.0f m" % prof[0])
note("the last is ahead", prof[-1] > 1590, "%.0f m" % prof[-1])
note("the aircraft's own position is in there",
     any(abs(v) < 1.0 for v in prof),
     "closest to zero: %.1f m" % min(prof, key=abs))
gaps = [prof[i + 1] - prof[i] for i in range(len(prof) - 1)]
note("evenly spaced", max(gaps) - min(gaps) < 1.0,
     "%.1f..%.1f m apart" % (min(gaps), max(gaps)))
# A metre or two out over two kilometres: track_profile walks a great
# circle, the ruler above is flat. 0.1% is the projection, not a fault.
note("spanning what was asked, within a tenth of a percent",
     abs((prof[-1] - prof[0]) - 2000.0) < 10.0,
     "%.0f m" % (prof[-1] - prof[0]))

print("")
print("degenerate requests do not explode")
note("one sample", p.track_profile(39.0, 32.0, 90.0, 100.0, 100.0, 1) == [])
note("no span", p.track_profile(39.0, 32.0, 90.0, 0.0, 0.0, 20) == [])

# ------------------------------------------------------------ javascript

SRC = map_view.LEAFLET_HTML
WANT = ("apNiceStep", "setAglProfile", "setAglAltitude", "clearAglProfile",
        "drawAglProfile", "apHighPath")


def lift(name):
    """The real function, out of the real page."""
    m = re.search(r"\nfunction %s\s*\([^)]*\)\s*\{" % re.escape(name), SRC)
    if not m:
        raise AssertionError("could not find function %s" % name)
    i = SRC.index("{", m.start())
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[m.start():j + 1]
    raise AssertionError("unbalanced braces in %s" % name)


js = "\n".join(lift(n) for n in WANT)
# The state those functions share, declared exactly as the page declares
# it. Without this, reading apAmsl before anything assigns it throws a
# ReferenceError and the first draw dies silently.
js += ("\nvar apElevs = null, apBehind = 0, apAhead = 0, apAmsl = null;"
       "\nvar AP_L = 34, AP_R = 292, AP_T = 30, AP_B = 116;\n")

# The elements the drawing writes into. Same ids as the page, so the real
# code runs unmodified.
PAGE = """<!doctype html><html><body>
<div id="agl-profile" style="display:none">
 <svg id="ap-svg" viewBox="0 0 300 140">
  <g id="ap-grid"></g>
  <path id="ap-ground" d=""/><path id="ap-ground-high" d=""/>
  <path id="ap-level-back" d=""/><line id="ap-level-fwd"/><line id="ap-now"/>
  <circle id="ap-uav-ring"/><circle id="ap-uav-dot"/>
  <g id="ap-labels"></g>
  <text id="ap-agl"></text><text id="ap-ahead" class="ap-ahead"></text>
 </svg></div>
<script>%s</script></body></html>""" % js

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView

app = QApplication.instance() or QApplication([])
view = QWebEngineView()
loop = QEventLoop()
view.loadFinished.connect(lambda ok: loop.quit())
view.setHtml(PAGE)
QTimer.singleShot(15000, loop.quit)
loop.exec()


def run(code):
    box = {}
    l = QEventLoop()
    view.page().runJavaScript(code, lambda v: (box.__setitem__("v", v), l.quit()))
    QTimer.singleShot(10000, l.quit)
    l.exec()
    return box.get("v")


READ = ("JSON.stringify({agl:document.getElementById('ap-agl').textContent,"
        "ahead:document.getElementById('ap-ahead').textContent,"
        "cls:document.getElementById('ap-ahead').getAttribute('class'),"
        "shown:document.getElementById('agl-profile').style.display,"
        "high:(document.getElementById('ap-ground-high').getAttribute('d')||'').length})")


def show(elevs, behind, ahead, amsl, slope=0.0):
    run("setAglProfile(%s,%s,%s); setAglAltitude(%s,%s);"
        % (json.dumps(elevs), behind, ahead, amsl, slope))
    return json.loads(run(READ) or "{}")


print("")
print("the page's own code, run headlessly")
note("every function was found", len(js) > 500, "%d chars lifted" % len(js))

# Ground at 900 m, aircraft at 1000 m: 100 m of air, flat all the way.
flat = show([900.0] * 21, 500.0, 1500.0, 1000.0)
note("height above ground", flat.get("agl") == "100 m", flat.get("agl"))
note("and the same ahead", "100 m" in (flat.get("ahead") or ""),
     flat.get("ahead"))
note("no warning on flat ground", flat.get("cls") == "ap-ahead",
     flat.get("cls"))
note("the panel shows itself", flat.get("shown") == "block",
     repr(flat.get("shown")))
note("nothing drawn above the aircraft", flat.get("high") == 0)

# A hill ahead reaching 970: 30 m of clearance, which is not much.
hill = [900.0] * 11 + [910, 925, 940, 955, 965, 970, 965, 950, 930, 910]
warn = show(hill, 500.0, 1500.0, 1000.0)
note("still 100 m right here", warn.get("agl") == "100 m", warn.get("agl"))
note("but the gap ahead is the hill", "30 m" in (warn.get("ahead") or ""),
     warn.get("ahead"))
note("and it is flagged", "warn" in (warn.get("cls") or ""), warn.get("cls"))

# The same hill, with the aircraft 40 m lower: it is now above us.
bad = show(hill, 500.0, 1500.0, 930.0)
note("negative clearance is shown as such",
     "-40 m" in (bad.get("ahead") or ""), bad.get("ahead"))
note("and coloured as danger", "bad" in (bad.get("cls") or ""),
     bad.get("cls"))
note("the part above the aircraft is drawn separately",
     bad.get("high") > 0, "%s chars" % bad.get("high"))

print("")
print("ground behind is not counted as a hazard ahead")
# 21 samples over -500..1500, so 100 m apart and sample 5 is the aircraft
# itself. The mountain is strictly astern - samples 0 to 4 - because
# distance zero counts as ahead, and rightly: the smallest gap between
# here and there includes here.
behind_hill = [990.0] * 5 + [900.0] * 16
past = show(behind_hill, 500.0, 1500.0, 1000.0)
note("the gap ahead ignores it", "100 m" in (past.get("ahead") or ""),
     past.get("ahead"))
# And the converse: ground right under the aircraft is not excused.
under = [900.0] * 5 + [985.0] + [900.0] * 15
now = show(under, 500.0, 1500.0, 1000.0)
note("but ground underfoot still counts", "15 m" in (now.get("ahead") or ""),
     now.get("ahead"))

print("")
print("missing tiles")
holes = [None] * 8 + [900.0] * 13
gap = show(holes, 500.0, 1500.0, 1000.0)
note("it still draws what it has", gap.get("shown") == "block")
note("and still reports the gap ahead", "100 m" in (gap.get("ahead") or ""),
     gap.get("ahead"))
nothing = show([None] * 21, 500.0, 1500.0, 1000.0)
note("no ground at all hides the panel", nothing.get("shown") == "none",
     repr(nothing.get("shown")))

print("")
print("the flight path is drawn at the gradient being flown")
# The line behind is a path now, because it traces the flown track; only
# the projection ahead is still a plain segment.
LINE = ("JSON.stringify({back:document.getElementById('ap-level-back').getAttribute('d')||'',"
        "fwdY1:+document.getElementById('ap-level-fwd').getAttribute('y1'),"
        "fwdY2:+document.getElementById('ap-level-fwd').getAttribute('y2')})")


def back_ends(d):
    """First and last y of the path behind."""
    ys = [float(v.split(",")[1]) for v in
          d.replace("M", " ").replace("L", " ").split() if "," in v]
    return (ys[0], ys[-1]) if ys else (0.0, 0.0)

show([900.0] * 21, 500.0, 1500.0, 1000.0, 0.0)
lvl = json.loads(run(LINE) or "{}")
note("level flight draws a level line",
     abs(lvl["fwdY1"] - lvl["fwdY2"]) < 0.01,
     "%.1f -> %.1f" % (lvl["fwdY1"], lvl["fwdY2"]))

show([900.0] * 21, 500.0, 1500.0, 1000.0, 0.1)      # climbing
up = json.loads(run(LINE) or "{}")
# Screen y grows downward, so climbing means the far end is a smaller y.
note("climbing tilts it up", up["fwdY2"] < up["fwdY1"] - 1,
     "%.1f -> %.1f" % (up["fwdY1"], up["fwdY2"]))
b0, b1 = back_ends(up["back"])
note("and with no track flown yet, behind falls back to the gradient too",
     b0 > b1 + 1, "%.1f -> %.1f" % (b0, b1))

show([900.0] * 21, 500.0, 1500.0, 1000.0, -0.1)     # descending
dn = json.loads(run(LINE) or "{}")
note("descending tilts it down", dn["fwdY2"] > dn["fwdY1"] + 1,
     "%.1f -> %.1f" % (dn["fwdY1"], dn["fwdY2"]))

print("")
print("but the projection does not get to squash the ground")
GROUND_BAND = ("(function(){var d=document.getElementById('ap-ground')"
               ".getAttribute('d')||'';"
               "var ys=(d.match(/,[0-9.]+/g)||[]).map(function(s){return +s.slice(1)});"
               "return JSON.stringify({lo:Math.min.apply(null,ys),"
               "hi:Math.max.apply(null,ys)});})()")


def ground_band(slope):
    run("setAglProfile(%s,500,3500); setAglAltitude(1000,%s,[]);"
        % (json.dumps([600.0] * 21), slope))
    b = json.loads(run(GROUND_BAND) or "{}")
    return b.get("lo"), b.get("hi")

flat_band = ground_band(0.0)
up_band = ground_band(0.3)          # a kilometre higher by the far end
down_band = ground_band(-0.3)
note("a steep climb leaves the ground where it was",
     up_band == flat_band, "%s vs %s" % (up_band, flat_band))
note("and so does a steep descent",
     down_band == flat_band, "%s vs %s" % (down_band, flat_band))
# This is the whole point: the panel is for seeing the ground, and the
# far end of a projection is not worth compressing it into a ribbon.
note("the ground keeps a usable slice of the box",
     flat_band[1] - flat_band[0] >= 0 and flat_band[0] > 90,
     "y %.0f..%.0f of the 30..116 box" % flat_band)

print("")
print("a climb runs on past the plot box, behind the readouts")
run("setAglProfile(%s,500,3500); setAglAltitude(1000,0.3,[]);"
    % json.dumps([600.0] * 21))
fwd = json.loads(run("JSON.stringify({y1:+document.getElementById('ap-level-fwd')"
                     ".getAttribute('y1'),y2:+document.getElementById('ap-level-fwd')"
                     ".getAttribute('y2')})") or "{}")
# AP_T is 30: the top of the plot box. Above it is the header row.
note("the far end climbs above the plot box", fwd["y2"] < 30,
     "y %.0f, box top is 30" % fwd["y2"])
note("and keeps going towards the top of the panel", fwd["y2"] < 10,
     "y %.0f, panel top is 0" % fwd["y2"])

order = run("(function(){var ids=[].slice.call("
            "document.querySelectorAll('#ap-svg > *')).map(function(e){return e.id});"
            "return ids.indexOf('ap-level-fwd') + ':' + ids.indexOf('ap-agl')"
            " + ':' + ids.indexOf('ap-labels');})()")
fwd_i, agl_i, lbl_i = (int(v) for v in order.split(":"))
note("the readouts are painted after the path, so they sit on top",
     agl_i > fwd_i, "path at %d, AGL text at %d" % (fwd_i, agl_i))
note("and so are the axis labels", lbl_i > fwd_i,
     "path at %d, labels at %d" % (fwd_i, lbl_i))

print("")
print("and the gap ahead is measured against that path")
# Ground rising to 960 at the far end; the aircraft at 1000. Level, that
# is 40 m of clearance. Descending at 4 m/s over 40 m/s of groundspeed -
# a tenth - it is 1500 * 0.1 = 150 m lower by then, so it does not clear.
rising = [900.0] * 11 + [905, 910, 920, 930, 940, 948, 953, 957, 959, 960]
level = show(rising, 500.0, 1500.0, 1000.0, 0.0)
note("level flight clears it", "40 m" in (level.get("ahead") or ""),
     level.get("ahead"))
# 40 m is inside the fifty-metre warning band, so amber is right here.
note("and flags it, since forty metres is not much",
     "warn" in (level.get("cls") or ""), level.get("cls"))

sinking = show(rising, 500.0, 1500.0, 1000.0, -0.1)
note("the same ground, descending, does not clear",
     "-" in (sinking.get("ahead") or ""), sinking.get("ahead"))
note("and it is coloured as danger", "bad" in (sinking.get("cls") or ""),
     sinking.get("cls"))

climbing = show(rising, 500.0, 1500.0, 1000.0, 0.05)
# Climbing lifts the far samples clear, so the tightest point becomes the
# flat ground directly below - 100 m - rather than the ridge at the end.
# Far better than the 40 m a level reading gives over the same ground.
note("climbing over it reads far better than level",
     "100 m" in (climbing.get("ahead") or ""), climbing.get("ahead"))
note("and calmly", climbing.get("cls") == "ap-ahead", climbing.get("cls"))

print("")
print("the line behind is the track flown, not a projection")
BACK = ("JSON.stringify({d:document.getElementById('ap-level-back')"
        ".getAttribute('d')||''})")

# Flat ground, level now, but the aircraft climbed 200 m to get here.
climbed = [[400.0, 800.0], [300.0, 850.0], [200.0, 920.0],
           [100.0, 980.0], [20.0, 1000.0]]
run("setAglProfile(%s,500,1500); setAglAltitude(1000,0,%s);"
    % (json.dumps([900.0] * 21), json.dumps(climbed)))
hist = json.loads(run(BACK) or "{}")["d"]
note("it is a path with a point per fix", hist.count("L") >= 5,
     "%d segments" % hist.count("L"))

# Same aircraft, same instant, told nothing about where it has been.
run("setAglProfile(%s,500,1500); setAglAltitude(1000,0,[]);"
    % json.dumps([900.0] * 21))
plain = json.loads(run(BACK) or "{}")["d"]
note("with no history it falls back to one straight segment",
     plain.count("L") == 1, "%d segments" % plain.count("L"))
note("and the two are not the same line", hist != plain)

# A climb must rise to the left of the aircraft: earlier fixes lower, and
# screen y grows downward, so the first point has the largest y.
ys = [float(v.split(",")[1]) for v in
      hist.replace("M", " ").replace("L", " ").split() if "," in v]
note("the climb reads as a climb", ys[0] > ys[-1] + 5,
     "y %.0f at the oldest fix -> %.0f at the aircraft" % (ys[0], ys[-1]))

print("")
print("recording the track, on the Python side")
from types import SimpleNamespace
import main as app_main
rec = app_main.MainWindow._record_agl_history
track = app_main.MainWindow._agl_track


def flyer():
    s = SimpleNamespace(
        AGL_HISTORY_MAX_M=app_main.MainWindow.AGL_HISTORY_MAX_M,
        AGL_HISTORY_STEP_M=app_main.MainWindow.AGL_HISTORY_STEP_M)
    from collections import deque
    s._agl_history = deque()
    s._agl_flown_m = 0.0
    s._agl_last_fix = None
    s._agl_behind_m = 1000.0
    s._last_amsl_alt = 1000.0
    return s


f = flyer()
rec(f, 39.0, 32.0)
note("the first fix only sets the origin", len(f._agl_history) == 0)

# Due east in 100 m steps. A degree of longitude here is 111320*cos(39).
step_deg = 100.0 / (111320.0 * __import__("math").cos(__import__("math").radians(39.0)))
for k in range(1, 21):
    f._last_amsl_alt = 1000.0 + k
    rec(f, 39.0, 32.0 + step_deg * k)
note("one point per fix", len(f._agl_history) == 20, len(f._agl_history))
note("distance adds up", abs(f._agl_flown_m - 2000.0) < 20.0,
     "%.0f m" % f._agl_flown_m)

pts = track(f)
note("only what fits the window comes back", len(pts) <= 11,
     "%d of 20 points, window %.0f m" % (len(pts), f._agl_behind_m))
note("oldest first", pts[0][0] > pts[-1][0],
     "%.0f m astern -> %.0f m" % (pts[0][0], pts[-1][0]))
note("nothing further back than asked", max(p[0] for p in pts) <= 1000.0)
note("and it carries the altitude of the moment",
     pts[-1][1] > pts[0][1], "%.0f -> %.0f m" % (pts[0][1], pts[-1][1]))

# Standing still must not stack up points on one spot.
before = len(f._agl_history)
for _ in range(10):
    rec(f, 39.0, 32.0 + step_deg * 20)
note("a stationary aircraft records nothing",
     len(f._agl_history) == before, len(f._agl_history) - before)

# And the buffer must not grow without bound over a long flight.
f2 = flyer()
rec(f2, 39.0, 32.0)
for k in range(1, 120):
    rec(f2, 39.0, 32.0 + step_deg * k)
note("old track is dropped",
     f2._agl_history[0][0] >= f2._agl_flown_m - f2.AGL_HISTORY_MAX_M,
     "oldest kept is %.0f m back of %.0f m flown"
     % (f2._agl_flown_m - f2._agl_history[0][0], f2._agl_flown_m))

print("")
print("the slope, and its guards, on the Python side")
from types import SimpleNamespace
import main as app_main
calc = app_main.MainWindow._agl_slope
stub = SimpleNamespace(AGL_MAX_SLOPE=app_main.MainWindow.AGL_MAX_SLOPE,
                       AGL_MIN_GROUNDSPEED=app_main.MainWindow.AGL_MIN_GROUNDSPEED)
stub._last_groundspeed, stub._last_climb = 40.0, 4.0
note("4 m/s up at 40 m/s is a tenth", abs(calc(stub) - 0.1) < 1e-9, calc(stub))
stub._last_climb = -4.0
note("and downwards is negative", abs(calc(stub) + 0.1) < 1e-9, calc(stub))
stub._last_groundspeed, stub._last_climb = 0.2, 5.0
note("standing still gives no gradient at all", calc(stub) == 0.0, calc(stub))
stub._last_groundspeed, stub._last_climb = 4.0, 40.0
note("an absurd climb is capped", calc(stub) == app_main.MainWindow.AGL_MAX_SLOPE,
     calc(stub))
stub._last_groundspeed, stub._last_climb = 40.0, None
note("a missing climb rate is level", calc(stub) == 0.0, calc(stub))

print("")
print("rounding to sensible gridlines")
# 12 m: a fifth is 2.4, which on the 1/2/5 ladder rounds up to 5, not
# down to 2. Worked through rather than guessed, after guessing wrong.
for span, want in ((190.0, 50.0), (4860.0, 1000.0), (12.0, 5.0)):
    got = run("apNiceStep(%s)" % span)
    note("%g m span -> %g m steps" % (span, want), abs(got - want) < 1e-9,
         "got %s" % got)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
