"""Does the window fit itself to the screen, and does Windows stay out of it?

The macOS column is fixed-height panels stacked above a HUD that carries
the only stretch factor, which makes the HUD the residual: every pixel a
shorter screen takes comes out of it alone. Measured on a 13.6-inch Air -
viewport 803 against the 16-inch's 942, a difference of 139, and the HUD
260 against 121, a difference of 139. Nothing else moved at all. At 121
the pitch ladder is clipped, so the instrument is losing information
rather than looking cramped.

The fix asked for was to treat the layout as a photograph and size it to
the screen. What is checked here is the arithmetic that decides the
factor, because that is the part with a wrong answer available to it -
and, more importantly, that none of it can reach Windows, whose layout
is frozen and must not move by one pixel over a Mac's problem.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as app_main

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


scale_for = app_main.display_scale_for
DESIGN = app_main.DESIGN_SCREEN_POINTS

print("")
print("1. the screen it was drawn for")

note("the design screen is the 16-inch MacBook Pro", DESIGN == (1792, 1120),
     str(DESIGN))
note("and it is left alone", scale_for(DESIGN) is None)
# 1792x1121 is the design screen for every purpose that matters. Scaling
# by 0.9991 would put every widget on a fractional boundary to no end.
note("so is one a pixel off it", scale_for((1792, 1121)) is None,
     str(scale_for((1792, 1121))))

print("")
print("2. the screen that started this")

# 13.6-inch MacBook Air, default scaled mode.
air = scale_for((1470, 956))
note("a 13.6-inch Air is scaled down", air is not None and air < 1.0,
     "%.4f" % air if air else "None")
note("by the tighter of the two ratios", abs(air - 1470 / 1792) < 1e-9,
     "%.4f, width-bound" % air)
# Height would have given 0.854 and pushed the picture off the side.
note("not the looser one, which would run off the edge",
     air < 956 / 1120, "%.3f < %.3f" % (air, 956 / 1120))

# What that buys the HUD. The layout works in logical pixels, so a window
# filling the screen is screen/scale logical pixels tall - MORE than the
# 16-inch's 968, which is why the slack from keeping the aspect ratio is
# not lost: it goes where leftover height in this column always goes.
logical_h = 829 / air
note("the column gets back the height it was starving for",
     logical_h > 968, "%.0f logical px against the 16-inch's 968" % logical_h)

print("")
print("3. other screens, including ones nobody here owns")

# The point of scaling over tuning constants: screens nobody has tested.
for name, points, smaller in (
        ("13-inch Air at 1440x900", (1440, 900), True),
        ("15-inch Air", (1710, 1112), True),
        ("14-inch Pro", (1512, 982), True),
        ("1080p external", (1920, 1080), True),
):
    got = scale_for(points)
    note("%s is fitted" % name,
         got is not None and (got < 1.0) == smaller,
         "%.3f" % got if got else "None")

# A screen bigger on both axes scales UP, which is what "four corners to
# four corners" means in the direction nobody was complaining about.
big = scale_for((2560, 1440))
note("a larger screen scales up rather than leaving a margin",
     big is not None and big > 1.0, "%.3f" % big if big else "None")

print("")
print("4. what it does when it cannot tell")

note("no screen size, no scaling", scale_for(None) is None)
note("an empty answer is not a scale of zero", scale_for(()) is None)
note("nor is a zero-sized display", scale_for((0, 0)) is None)
note("nor a negative one", scale_for((-1, 900)) is None)

print("")
print("5. Windows is not in this at all")

# The rule this project is built on: a Mac's problem may not move one
# Windows pixel. These are the three doors it could come through.
note("the platform predicate says this is not a Mac", app_main.MACOS is False
     or sys.platform == "darwin", sys.platform)

if sys.platform != "darwin":
    note("asking the window server for a screen returns nothing",
         app_main._macos_screen_points() is None)
    before = os.environ.get("QT_SCALE_FACTOR")
    note("applying the scale does nothing",
         app_main.apply_macos_display_scale() is None)
    note("and sets no environment variable",
         os.environ.get("QT_SCALE_FACTOR") == before,
         repr(os.environ.get("QT_SCALE_FACTOR")))
else:
    print("  ..   skipped the Windows-side checks: running on macOS")

# A factor set by hand wins, on either platform - which is also how the
# whole thing is switched off.
os.environ["QT_SCALE_FACTOR"] = "1.0"
try:
    note("a factor set by hand is not overridden",
         app_main.apply_macos_display_scale() is None
         and os.environ["QT_SCALE_FACTOR"] == "1.0",
         os.environ["QT_SCALE_FACTOR"])
finally:
    del os.environ["QT_SCALE_FACTOR"]

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
