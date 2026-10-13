"""The "Queue waypoints on map click" box says what map clicks will do.

Queueing waypoints is the window's setting, shown by its checkbox. Drawing
a fence or a terrain area takes the map's clicks, so the page turns
queueing off by itself - and the box stayed ticked over clicks that then
queued nothing and dropped Fly to Here pins instead (found 2026-10-13, for
Terrain Upload; the Geofence switch had done the same since September).
Now the page tells the window, and the box goes off with it.

1. The page's own JavaScript, taken from the page and run as it is, with
   stand-ins only for the map and the document: starting to draw a
   terrain area or a fence while queueing tells the window, once - and
   says nothing when queueing was off anyway.
2. The window: told, the box goes off, and the map hears the same through
   the box's own signal, so the setting stays in one place.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtQml import QJSEngine
from PySide6.QtWidgets import QApplication

import main as app_main
import map_view

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail != "" else ""))
    if not ok:
        fails.append(name)


app = QApplication.instance() or QApplication(sys.argv)
PAGE = map_view.LEAFLET_HTML


def function_source(name):
    """One named function, exactly as the page has it."""
    m = re.search(r"\nfunction %s\s*\(" % re.escape(name), PAGE)
    if m is None:
        raise SystemExit("the page has no function %s" % name)
    i, depth, quote = PAGE.index("{", m.start()), 0, None
    while True:
        c = PAGE[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif PAGE.startswith("//", i):
            i = PAGE.index("\n", i)
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return PAGE[m.start():i + 1]
        i += 1


STAND_INS = r"""
var told = 0;
var bridge = {waypointModeOff: function () { told += 1; }};
function element() {
    return {style: {}, textContent: '', disabled: false, checked: false,
            getBoundingClientRect: function () { return {left: 0}; }};
}
var elements = {};
var document = {getElementById: function (id) {
    if (!elements[id]) { elements[id] = element(); }
    return elements[id];
}};
var map = {removeLayer: function () {}};
var waypointMode = false, fenceDrawing = false, terrainDrawing = false;
var fencePoints = [], terrainPoints = [], terrainMarkers = [];
var terrainShape = null;
"""

engine = QJSEngine()
NAMES = ("setWaypointMode", "stopQueueingWaypoints", "setFenceDrawing",
         "refreshFenceButtons", "clearTerrainArea", "alignTerrainPanel",
         "setTerrainUploadState")
# A page without one of these (an older one) still runs what it has, so
# the checks below fail on what it does rather than on what it lacks.
code = STAND_INS + "\n".join(
    function_source(n) for n in NAMES
    if re.search(r"\nfunction %s\s*\(" % n, PAGE))
result = engine.evaluate(code)
if result.isError():
    raise SystemExit("the page's JavaScript did not load: " + result.toString())


def js(expression):
    value = engine.evaluate(expression)
    if value.isError():
        raise RuntimeError(value.toString())
    return value.toVariant()


print("")
print("1. the page tells the window when it stops queueing")
js("told = 0; waypointMode = true; setTerrainUploadState('drawing', '');")
note("drawing a terrain area while queueing: queueing off, window told once",
     js("waypointMode") is False and js("told") == 1, js("told"))
js("setTerrainUploadState('idle', '');")
js("told = 0; waypointMode = true; setFenceDrawing(true);")
note("drawing a fence while queueing: queueing off, window told once",
     js("waypointMode") is False and js("told") == 1, js("told"))
js("setFenceDrawing(false);")
js("told = 0; waypointMode = false; setTerrainUploadState('drawing', '');"
   " setTerrainUploadState('idle', ''); setFenceDrawing(true);")
note("with queueing off already: nothing said", js("told") == 0, js("told"))


print("")
print("2. told, the window's box goes off - and the map with it")


class Map:
    def __init__(self):
        self.modes = []

    def set_waypoint_mode(self, enabled):
        self.modes.append(enabled)


window = type("Window", (), {})()
window.map_view = Map()
window.waypoint_panel = app_main.WaypointMissionPanel()
window.waypoint_panel.mode_toggled.connect(
    lambda on: app_main.MainWindow.on_waypoint_mode_toggled(window, on))
box = window.waypoint_panel.queue_checkbox
box.setChecked(True)
note("(ticked, the map is told to queue)", window.map_view.modes == [True],
     window.map_view.modes)
bridge = map_view.Bridge()
handler = getattr(app_main.MainWindow, "on_map_waypoint_mode_off", None)
note("the page has a way to tell the window, and the window listens",
     hasattr(bridge, "waypointModeOff") and handler is not None)
if hasattr(bridge, "waypointModeOff") and handler is not None:
    bridge.waypoint_mode_off.connect(lambda: handler(window))
    bridge.waypointModeOff()
    note("the page saying so unticks the box", not box.isChecked())
    note("and the map hears it through the box, as from a click",
         window.map_view.modes == [True, False], window.map_view.modes)
    bridge.waypointModeOff()
    note("said again, nothing changes", not box.isChecked()
         and window.map_view.modes == [True, False], window.map_view.modes)
    box.setChecked(True)
    note("ticking it again queues again", box.isChecked()
         and window.map_view.modes == [True, False, True],
         window.map_view.modes)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
