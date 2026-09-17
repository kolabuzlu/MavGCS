"""Does tapping a speed switch both of them, and keep the choice?

Drives the real TelemetryPanel. Settings are held in memory, so running
this never touches the settings.json of whoever runs it.
"""

import os
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

import main as app_main

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


app = QApplication.instance() or QApplication([])

# In-memory settings: the panel reads one at construction and writes one
# on every tap, and neither should reach the real file.
store = {}
app_main.load_settings = lambda: dict(store)
app_main.save_setting = lambda k, v: store.__setitem__(k, v)


def panel():
    return app_main.TelemetryPanel()


def caption(p, key):
    return p._speed_names[key].text()


def shown(p, key):
    return p.labels[key].text()


print("")
print("it starts in m/s")
p = panel()
note("air caption", caption(p, "airspeed") == "AirSpeed (m/s)",
     caption(p, "airspeed"))
note("ground caption", caption(p, "groundspeed") == "GroundSpeed (m/s)",
     caption(p, "groundspeed"))
note("no reading yet", shown(p, "airspeed") == "--")

print("")
print("a reading arrives, in m/s as the aircraft sends it")
p.set_speed("airspeed", 18.0)
p.set_speed("groundspeed", 22.5)
note("airspeed shown as m/s", shown(p, "airspeed") == "18.00",
     shown(p, "airspeed"))
note("groundspeed shown as m/s", shown(p, "groundspeed") == "22.50",
     shown(p, "groundspeed"))

print("")
print("tap: both switch to kph, without waiting for new telemetry")
p.toggle_speed_unit()
note("airspeed converted", shown(p, "airspeed") == "64.80",
     "%s  (18 * 3.6)" % shown(p, "airspeed"))
note("groundspeed converted", shown(p, "groundspeed") == "81.00",
     "%s  (22.5 * 3.6)" % shown(p, "groundspeed"))
note("air caption follows", caption(p, "airspeed") == "AirSpeed (kph)",
     caption(p, "airspeed"))
note("ground caption follows",
     caption(p, "groundspeed") == "GroundSpeed (kph)",
     caption(p, "groundspeed"))

print("")
print("tap again: back to m/s, with the original numbers")
p.toggle_speed_unit()
note("airspeed back", shown(p, "airspeed") == "18.00", shown(p, "airspeed"))
note("groundspeed back", shown(p, "groundspeed") == "22.50",
     shown(p, "groundspeed"))
note("captions back", caption(p, "airspeed") == "AirSpeed (m/s)")

print("")
print("new telemetry respects the choice")
p.toggle_speed_unit()              # into kph
p.set_speed("airspeed", 10.0)
note("a fresh reading arrives already converted",
     shown(p, "airspeed") == "36.00", shown(p, "airspeed"))

print("")
print("the choice is remembered")
note("it was written to settings", store.get("speed_unit_kph") is True,
     repr(store))
fresh = panel()                    # as if the program restarted
note("a new panel opens in kph", caption(fresh, "airspeed") == "AirSpeed (kph)",
     caption(fresh, "airspeed"))
fresh.set_speed("groundspeed", 20.0)
note("and converts from the first reading",
     shown(fresh, "groundspeed") == "72.00", shown(fresh, "groundspeed"))

print("")
print("tapping any of the four widgets does it")
for who in ("airspeed name", "airspeed value",
            "groundspeed name", "groundspeed value"):
    # Cleared per case, not once: the choice persists, so a panel built
    # after a case that switched to kph would start in kph and the tap
    # under test would be switching back. That is the feature working,
    # and it made this loop pass and fail alternately until the state
    # was reset each time.
    store.clear()
    q = panel()
    q.set_speed("airspeed", 100.0)
    key = "airspeed" if who.startswith("airspeed") else "groundspeed"
    widget = q._speed_names[key] if who.endswith("name") else q.labels[key]
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                     QPointF(widget.rect().center()),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    widget.mouseReleaseEvent(ev)
    note("tapping the %s switches both" % who,
         shown(q, "airspeed") == "360.00"
         and caption(q, "groundspeed") == "GroundSpeed (kph)",
         shown(q, "airspeed"))

print("")
print("a press that wanders off the label does nothing")
store.clear()
q = panel()
q.set_speed("airspeed", 100.0)
widget = q.labels["airspeed"]
# Asserted as "unchanged from whatever it was", not against a fixed
# number: the unit is remembered between panels, so hard-coding the
# expected reading here only tests what the previous case left behind.
was = shown(q, "airspeed")
outside = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                      QPointF(widget.rect().right() + 40.0, 5.0),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
widget.mouseReleaseEvent(outside)
note("nothing changed", shown(q, "airspeed") == was,
     "%s -> %s" % (was, shown(q, "airspeed")))

print("")
print("vertical speed is deliberately left in m/s")
note("not among the switchable ones",
     "vspeed_mps" not in app_main.TelemetryPanel.SPEED_KEYS)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
