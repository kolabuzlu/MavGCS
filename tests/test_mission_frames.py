"""What a mission's altitudes are measured from, and what goes on the wire.

Every mission this sent went out in one frame - relative to home - and
the number was written into the MISSION_ITEM_INT call as a constant. The
Start Mission dialog now asks, the way Mission Planner's Frame column
does, and the answer has to survive three hops: the dialog, the waypoint
dicts, and the item the vehicle requests one at a time.

The frame is not cosmetic. 120 m relative to home over ground that is
already 900 m up is a different place from 120 m above sea level, and
the second one is underground. So the two things worth being sure of are
that the chosen frame reaches the wire, and that a mission planned
without touching the dialog's new field goes out exactly as it always
did.

The ground clearance drawn on the map has to follow the same rule. A
waypoint measured from the terrain beneath it IS its own clearance -
no home position needed, no tile needed - and a leg between two of them
is flown over the ground rather than through a straight line in the air,
so walking the terrain under it would invent a hill the aeroplane climbs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pymavlink import mavutil

import terrain_provider as tp
from mavlink_link import MavlinkLink

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


print("")
print("1. the three frames, as the wire spells them")

note("relative is the frame it always sent",
     MavlinkLink.MISSION_FRAMES["RELATIVE"]
     == mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT)
note("absolute is above sea level",
     MavlinkLink.MISSION_FRAMES["ABSOLUTE"]
     == mavutil.mavlink.MAV_FRAME_GLOBAL_INT)
note("terrain is above the ground",
     MavlinkLink.MISSION_FRAMES["TERRAIN"]
     == mavutil.mavlink.MAV_FRAME_GLOBAL_TERRAIN_ALT_INT)
# The _INT frames go with MISSION_ITEM_INT, whose coordinates are 1e7
# integers. Sending a float-frame number on that message is how a
# waypoint ends up a thousand kilometres away.
note("all three are the _INT variants",
     set(MavlinkLink.MISSION_FRAMES.values()) == {5, 6, 11},
     sorted(MavlinkLink.MISSION_FRAMES.values()))
note("and relative is the default",
     MavlinkLink.DEFAULT_MISSION_FRAME == "RELATIVE")


print("")
print("2. what the vehicle is actually sent")


class FakeMav:
    """Records mission_item_int_send instead of transmitting it."""

    def __init__(self):
        self.items = []

    def mission_clear_all_send(self, *a, **k):
        pass

    def mission_item_int_send(self, _sys, _comp, seq, frame, command,
                              _current, _auto, _p1, _p2, _p3, _p4,
                              lat, lon, alt, *rest):
        self.items.append({"seq": seq, "frame": frame, "command": command,
                           "lat": lat, "lon": lon, "alt": alt})

    def mission_count_send(self, *a, **k):
        pass


class FakeMaster:
    target_system = 1
    target_component = 1

    def __init__(self):
        self.mav = FakeMav()


def upload(waypoints, default_alt=30.0):
    """Run one mission through the uploader and return the items sent.

    The link is built without __init__ on purpose: a real one opens a
    socket and starts a thread, and neither has anything to do with what
    number lands in the frame field.
    """
    link = MavlinkLink.__new__(MavlinkLink)
    link.master = FakeMaster()
    link._mission_state = None
    link._mission_pending = None
    link._mission_deadline = None
    link._mission_kind = "mission"
    link._home_lat, link._home_lon = 40.0, 29.0
    import threading
    link._send_lock = threading.Lock()
    said = []
    link.command_feedback = type("Sig", (), {"emit": lambda _s, t: said.append(t)})()

    link.upload_and_start_mission(waypoints, default_alt)
    link._mission_state = "uploading"

    # The vehicle asks for each item in turn; answer for all of them.
    items = []
    for seq in range(len(link._mission_pending)):
        item = link._mission_pending[seq]
        frame = item[4] if len(item) >= 5 else "RELATIVE"
        cmd = item[3] if len(item) >= 4 else "WAYPOINT"
        link.master.mav.mission_item_int_send(
            1, 1, seq,
            link.MISSION_FRAMES.get(
                frame, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT),
            link.MISSION_COMMANDS.get(cmd,
                                      mavutil.mavlink.MAV_CMD_NAV_WAYPOINT),
            0, 1, 0, 0, 0, 0,
            int(item[0] * 1e7), int(item[1] * 1e7), float(item[2]))
    items = link.master.mav.items
    return items, link._mission_pending


# A mission planned the way every mission was planned before the dialog
# asked. This is the one that must not have moved.
items, pending = upload([(40.1, 29.1, 100.0, "WAYPOINT")])
note("a mission with no frame given goes out relative",
     all(i["frame"] == mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
         for i in items),
     [i["frame"] for i in items])

items, pending = upload([(40.1, 29.1, 100.0, "WAYPOINT", "ABSOLUTE"),
                         (40.2, 29.2, 120.0, "WAYPOINT", "ABSOLUTE")])
# Item 0 is the home placeholder ArduPilot reads and never flies to. It
# keeps the frame it has always had; making it interesting is how a
# mission starts by diving at the ground.
note("the home placeholder stays relative",
     items[0]["frame"] == mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
     items[0]["frame"])
note("an absolute mission sends absolute for every real point",
     all(i["frame"] == mavutil.mavlink.MAV_FRAME_GLOBAL_INT
         for i in items[1:]),
     [i["frame"] for i in items])
note("and the altitudes are untouched by the frame",
     [i["alt"] for i in items[1:]] == [100.0, 120.0],
     [i["alt"] for i in items[1:]])

items, _ = upload([(40.1, 29.1, 80.0, "WAYPOINT", "TERRAIN")])
note("a terrain mission sends the terrain frame",
     items[1]["frame"] == mavutil.mavlink.MAV_FRAME_GLOBAL_TERRAIN_ALT_INT,
     items[1]["frame"])

# A frame nobody has heard of must not shorten a mission, for the same
# reason an unknown command does not: the aeroplane gets every point it
# was given, flown the safest way this knows.
items, _ = upload([(40.1, 29.1, 80.0, "WAYPOINT", "ORBITAL")])
note("an unknown frame falls back rather than dropping the point",
     len(items) == 2
     and items[1]["frame"]
     == mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
     "%d items" % len(items))

# The landing still lands: choosing a frame must not disturb the rule
# that a landing's height is the ground rather than the cruise default.
items, _ = upload([(40.1, 29.1, None, "WAYPOINT", "TERRAIN"),
                   (40.2, 29.2, None, "LAND", "TERRAIN")], default_alt=90.0)
note("a point with no altitude takes the mission default",
     items[1]["alt"] == 90.0, items[1]["alt"])
note("but a landing still touches down at zero",
     items[2]["alt"] == 0.0 and items[2]["command"]
     == mavutil.mavlink.MAV_CMD_NAV_LAND,
     items[2]["alt"])


print("")
print("3. the dialog that asks")

from PySide6.QtWidgets import QApplication
import main as app_main

app = QApplication.instance() or QApplication([])
dialog = app_main.MissionStartDialog(3, 45.0)

note("it offers exactly the three frames",
     [dialog.frame_combo.itemText(i)
      for i in range(dialog.frame_combo.count())]
     == ["Relative", "Absolute", "Terrain"],
     [dialog.frame_combo.itemText(i)
      for i in range(dialog.frame_combo.count())])
note("named as the link names them",
     [dialog.frame_combo.itemData(i)
      for i in range(dialog.frame_combo.count())]
     == list(MavlinkLink.MISSION_FRAMES),
     [dialog.frame_combo.itemData(i)
      for i in range(dialog.frame_combo.count())])
note("relative is what it opens on",
     dialog.frame_combo.currentData() == "RELATIVE")
note("and it carries the altitude it was given",
     dialog.alt_spin.value() == 45.0, dialog.alt_spin.value())
# The same range and decimals as the QInputDialog it replaced, so the
# same answers are possible.
note("the altitude range is the one it always had",
     (dialog.alt_spin.minimum(), dialog.alt_spin.maximum(),
      dialog.alt_spin.decimals()) == (0.0, 1000.0, 0),
     "%s to %s, %d decimals"
     % (dialog.alt_spin.minimum(), dialog.alt_spin.maximum(),
        dialog.alt_spin.decimals()))

# Relative and Absolute both name a datum without saying what it is, so
# the dialog says it underneath. Getting those two the wrong way round is
# the mistake this is here to stop.
for index, word in ((0, "home"), (1, "sea level"), (2, "ground")):
    dialog.frame_combo.setCurrentIndex(index)
    note("choosing %s explains itself" % dialog.frame_combo.currentText(),
         word in dialog.note.text(), dialog.note.text())

# Reopened on a previous choice - a terrain survey is planned several
# missions in a row, and re-picking it every time is how one gets missed.
dialog = app_main.MissionStartDialog(1, 30.0, "TERRAIN")
note("it reopens on the frame last used",
     dialog.frame_combo.currentData() == "TERRAIN",
     dialog.frame_combo.currentData())
dialog = app_main.MissionStartDialog(1, 30.0, "NONSENSE")
note("and falls back to relative rather than empty",
     dialog.frame_combo.currentData() == "RELATIVE",
     dialog.frame_combo.currentData())


print("")
print("4. the ground clearance the map draws")

# A provider that answers with flat ground at a known height, so the
# arithmetic is the only thing under test.
class FlatGround:
    def __init__(self, metres):
        self.metres = metres
        self.asked = 0

    def elevation(self, _lat, _lon):
        self.asked += 1
        return self.metres

    def retry_pending(self):
        return False


worker = tp.WaypointTerrainWorker.__new__(tp.WaypointTerrainWorker)
worker._provider = FlatGround(900.0)

# Home at 910 m, a waypoint 120 m above it: 1030 m, over ground at 900.
note("relative is measured from home",
     worker._clearance(910.0, 40.0, 29.0, 120.0, "RELATIVE") == 130.0,
     worker._clearance(910.0, 40.0, 29.0, 120.0, "RELATIVE"))
# The same 120, above sea level, is 780 m underground.
note("absolute is measured from sea level",
     worker._clearance(910.0, 40.0, 29.0, 120.0, "ABSOLUTE") == -780.0,
     worker._clearance(910.0, 40.0, 29.0, 120.0, "ABSOLUTE"))
note("terrain is already a clearance",
     worker._clearance(910.0, 40.0, 29.0, 120.0, "TERRAIN") == 120.0)

before = worker._provider.asked
worker._clearance(None, 40.0, 29.0, 120.0, "TERRAIN")
note("and needs neither home nor a terrain tile",
     worker._provider.asked == before,
     "%d lookups" % (worker._provider.asked - before))

note("a relative point with no home cannot be judged",
     worker._clearance(None, 40.0, 29.0, 120.0, "RELATIVE") is None)
note("an absolute one can",
     worker._clearance(None, 40.0, 29.0, 1000.0, "ABSOLUTE") == 100.0)

worker._provider = FlatGround(None)   # no tile here
note("no terrain data leaves a relative point unjudged",
     worker._clearance(910.0, 40.0, 29.0, 120.0, "RELATIVE") is None)


print("")
print("5. legs, and the one that follows the ground")

worker._provider = FlatGround(900.0)
worker._running = True

# Two terrain waypoints 50 m and 80 m above the ground. The aeroplane
# holds that height over whatever is beneath it, so the lowest the leg
# ever gets is the lower end - not something read off a hill in between.
a = (1, 40.0, 29.0, 50.0, "TERRAIN")
b = (2, 40.05, 29.05, 80.0, "TERRAIN")
before = worker._provider.asked
leg = worker._leg_clearance(None, a, b)
note("a terrain leg is the lower of its two ends", leg == 50.0, leg)
note("and reads no terrain to say so",
     worker._provider.asked == before,
     "%d lookups" % (worker._provider.asked - before))

# Relative to home at 910: both ends 1030 m over ground at 900.
a = (1, 40.0, 29.0, 120.0, "RELATIVE")
b = (2, 40.05, 29.05, 120.0, "RELATIVE")
leg = worker._leg_clearance(910.0, a, b)
note("a relative leg is still walked against the ground",
     leg == 130.0, leg)

# Mixed: absolute 1000 m at one end, terrain 50 m (so 950 m) at the
# other, over ground at 900. The straight line between them never gets
# below the lower end.
a = (1, 40.0, 29.0, 1000.0, "ABSOLUTE")
b = (2, 40.05, 29.05, 50.0, "TERRAIN")
leg = worker._leg_clearance(None, a, b)
note("a mixed leg is brought onto one datum first", leg == 50.0, leg)

a = (1, 40.0, 29.0, 120.0, "RELATIVE")
b = (2, 40.05, 29.05, 120.0, "RELATIVE")
note("a leg with no home cannot be judged",
     worker._leg_clearance(None, a, b) is None)


print("")
print("6. what the worker is handed")

note("a five-field point keeps its frame",
     tp.WaypointTerrainWorker._normalise((1, 40.0, 29.0, 100.0, "terrain"))
     == (1, 40.0, 29.0, 100.0, "TERRAIN"))
# Four fields is what every caller passed before the frame existed.
note("a four-field point is relative, as it always was",
     tp.WaypointTerrainWorker._normalise((1, 40.0, 29.0, 100.0))
     == (1, 40.0, 29.0, 100.0, "RELATIVE"))
note("and an unknown frame is read as relative too",
     tp.WaypointTerrainWorker._normalise((1, 40.0, 29.0, 100.0, "ORBITAL"))
     == (1, 40.0, 29.0, 100.0, "RELATIVE"))


print("")
print("7. one answer, applied to every point drawn")


class Recorder:
    """Stands in for the window, recording what Start Mission does.

    Built as a stub rather than a real MainWindow on purpose: a window
    brings up the browser engine, and this is about which frame lands on
    which dict. Everything on_start_mission touches is here, so a field
    it starts using that this does not have fails loudly rather than
    silently passing.
    """

    _last_alt = 30.0
    _mission_frame = "RELATIVE"
    _mission_default_alt = None
    MISSION_FRAME_NAMES = app_main.MainWindow.MISSION_FRAME_NAMES

    def __init__(self, queue):
        self._waypoint_queue = queue
        self._sent_mission = []
        self.uploaded = []
        self.said = []
        self.warned = 0
        self.frame_told_to_map = None
        self.link = object()
        self.map_view = self
        self.waypoint_panel = self

    # The link, and the one call that matters on it.
    def _require_link(self):
        return self

    def upload_and_start_mission(self, waypoints, alt, restart=True):
        self.uploaded.append((list(waypoints), alt, restart))

    # Everything else is noise for this test, but it has to exist.
    def on_command_feedback(self, text):
        self.said.append(text)

    def _warn_if_vehicle_has_no_terrain(self):
        self.warned += 1

    def _recheck_waypoint_terrain(self):
        pass

    def _recheck_fence_containment(self):
        pass

    def set_waypoint_frame(self, frame):
        self.frame_told_to_map = frame

    def set_waypoint_default_alt(self, alt):
        pass

    def clear_target(self):
        pass

    def commit_waypoints(self):
        pass

    def set_count(self, _n):
        pass

    def set_can_update(self, _ok):
        pass


def start_mission(answer, queue=None):
    """Press Start Mission and answer the dialog with `answer`."""
    queue = queue if queue is not None else [
        {"id": 1, "lat": 40.1, "lon": 29.1, "alt": None, "cmd": "WAYPOINT",
         "frame": "RELATIVE"},
        {"id": 2, "lat": 40.2, "lon": 29.2, "alt": 150.0, "cmd": "WAYPOINT",
         "frame": "RELATIVE"},
        {"id": 3, "lat": 40.3, "lon": 29.3, "alt": None, "cmd": "LAND",
         "frame": "RELATIVE"},
    ]
    window = Recorder(queue)
    original = app_main.MissionStartDialog.ask
    app_main.MissionStartDialog.ask = staticmethod(lambda *a, **k: answer)
    try:
        app_main.MainWindow.on_start_mission(window)
    finally:
        app_main.MissionStartDialog.ask = original
    return window, queue


window, queue = start_mission((120.0, "ABSOLUTE", True))
sent = window.uploaded[0][0]
note("the chosen frame reaches every point sent",
     [wp[4] for wp in sent] == ["ABSOLUTE"] * 3,
     [wp[4] for wp in sent])
note("including the landing",
     sent[2][3] == "LAND" and sent[2][4] == "ABSOLUTE",
     "%s %s" % (sent[2][3], sent[2][4]))
note("the altitude answered is the mission default",
     window.uploaded[0][1] == 120.0, window.uploaded[0][1])
note("a point that had its own altitude keeps it",
     sent[1][2] == 150.0, sent[1][2])
note("the map is told what to label the heights with",
     window.frame_told_to_map == "ABSOLUTE", window.frame_told_to_map)
note("and the choice is remembered for the next mission",
     window._mission_frame == "ABSOLUTE", window._mission_frame)
note("a frame other than relative is said out loud",
     any("above sea level" in t for t in window.said), window.said)

# Cancelling must send nothing at all. The dialog hands back an altitude
# either way, and a cancelled mission read as "0 m" would fly a plan into
# the ground.
window, queue = start_mission((120.0, "TERRAIN", False))
note("cancelling sends no mission", window.uploaded == [])
note("and changes nothing about the points",
     [wp["frame"] for wp in queue] == ["RELATIVE"] * 3,
     [wp["frame"] for wp in queue])
note("nor what the next dialog will open on",
     window._mission_frame == "RELATIVE", window._mission_frame)

# Terrain is the frame the aircraft can quietly fail to honour: without
# terrain data of its own ArduPilot flies the height relative to home
# instead, and the mission uploads either way.
window, _ = start_mission((80.0, "TERRAIN", True))
note("choosing terrain checks the aircraft has terrain data",
     window.warned == 1, "%d warnings" % window.warned)
window, _ = start_mission((80.0, "RELATIVE", True))
note("choosing relative does not", window.warned == 0)
note("and relative says nothing extra either",
     not any("above" in t for t in window.said), window.said)


print("")
print("8. the map labels the height with its datum")

# "120m" on its own means three different places, so a non-relative
# mission says which. Relative adds nothing, because relative is what the
# map always showed and a mission planned as they always were has to look
# as it always did.
import map_view

page = map_view.LEAFLET_HTML
note("the page can be told the mission's frame",
     "function setWaypointFrame(" in page)
note("it tags a sea-level height", "ABSOLUTE: ' amsl'" in page)
note("and a terrain one", "TERRAIN: ' terr'" in page)
note("relative gets no tag",
     "RELATIVE" not in page.split("WP_FRAME_TAGS = ")[1].split("}")[0],
     page.split("WP_FRAME_TAGS = ")[1].split("}")[0])
# The tag goes on the altitude label, which is the number it qualifies.
note("the tag is on the altitude label",
     "wpFrameTag(m)" in page.split("function wpAltText")[1].split("}")[0])

# The popup spells it out where a height is typed, rather than only
# abbreviating it on the marker.
note("and the popup says it in words",
     "above sea level" in page and "above the terrain" in page)


print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
