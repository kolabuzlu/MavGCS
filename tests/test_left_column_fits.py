"""Does the left-hand column still fit the window it is given?

This exists because of a regression that shipped to a branch and that
nothing here could have caught. The column's spacing was retuned so the
panels would sit correctly on macOS, which lays them out about 160px
shorter than Windows does. The constants were written as fixed values,
so Windows got the macOS spacing too: the column grew from 662px to
976px against a 796px viewport, its scroll area - which had never
activated on Windows before - switched on, and TelemetryPanel went
entirely below the fold. Airspeed, altitude, satellite count and battery
stopped being visible on the released platform.

It needs no display and no pixels. The window is built offscreen, given
a size, and asked where its own widgets ended up, which is the same
question a person answers by looking at it. That makes it ordinary
geometry rather than image comparison, so there is nothing to golden and
nothing to go stale on a font change.

The sizes below are heights MavGCS has to stay usable at, not a wish,
and they are per-platform because the screens are. A Windows ground
station gets flown on whatever laptop is in the case; a Mac has a floor
under it, because Apple does not sell a short one.

The Windows numbers are measured rather than chosen: 816 is what a
1920x1080 panel at 125% scaling leaves once the taskbar is taken off,
which is the machine the regression was found on, and 768 is the common
laptop panel underneath that.

The macOS numbers are provisional. They are the smallest logical
resolutions Apple has shipped recently, less the menu bar, but nobody
has flown this on such a machine - they want confirming by someone with
a Mac in front of them, and lowering them to match a real one is a
better answer than deleting the check.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
# No display on a build machine, and none needed: nothing here is shown
# to anybody. Qt still has to be told, or importing it fails outright.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as app_main

from PySide6.QtWidgets import QApplication, QScrollArea

# Never connects: the window is built for its geometry, not its data.
DEAD_ADDRESS = "udp:127.0.0.1:14999"

SIZES = {
    "win32": [(1536, 816), (1366, 768)],
    "darwin": [(1440, 875), (1280, 775)],
}.get(sys.platform)

if SIZES is None:
    # Not a platform this is shipped on. Saying so and stopping beats
    # inventing a screen size to assert against, and beats passing
    # silently - a suite that reports "ok" without having measured
    # anything is worse than one that admits it did not run.
    print("  skipped: no screen sizes defined for %r" % (sys.platform,))
    sys.exit(0)

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


def build(app, width, height):
    """A shown window at that size, laid out and settled."""
    w = app_main.MainWindow(DEAD_ADDRESS)
    w.resize(width, height)
    w.show()
    # Qt lays out on the event loop, not on resize(), so the geometry is
    # not final until it has run. Ten passes is well past settling and
    # still instant.
    for _ in range(10):
        app.processEvents()
    return w


app = QApplication.instance() or QApplication([])

for width, height in SIZES:
    print("")
    print("window %dx%d" % (width, height))
    w = build(app, width, height)

    areas = w.findChildren(QScrollArea)
    note("the left column is in a scroll area", len(areas) == 1,
         "found %d" % len(areas))

    if areas:
        area = areas[0]
        inner = area.widget()
        needs = inner.sizeHint().height() if inner is not None else 0
        has = area.viewport().height()
        # The scroll area is the fallback for a window someone has made
        # small on purpose. It is not where the column is meant to live
        # at a normal size, and a scrollbar here means the readings have
        # gone somewhere the pilot has to hunt for them.
        note("its content fits without scrolling", needs <= has,
             "needs %dpx, has %dpx, over by %+dpx" % (needs, has, needs - has))

    telemetry = getattr(w, "telemetry", None)
    note("the telemetry panel exists", telemetry is not None)
    if telemetry is not None:
        top = telemetry.mapTo(w, telemetry.rect().topLeft()).y()
        bottom = top + telemetry.height()
        # The one that matters: every flight reading is on screen. A
        # column that overflows by less than the panel's own height
        # would still pass the check above on some future layout, and
        # this is the thing that check is protecting.
        note("every flight reading is on screen", bottom <= w.height(),
             "panel ends at y=%d, window is %dpx" % (bottom, w.height()))

    w.close()

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
