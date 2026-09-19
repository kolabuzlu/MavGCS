"""The vertical speed bar: what it reads, and what it keeps clear of.

A bar on the right of the HUD, yellow, filling from the middle - up for
climb, down for descent, hard over at ten metres a second either way.
The mirror of the throttle bar on the left.

Everything here is measured off the rendered pixels rather than off the
geometry that was supposed to produce them, because every fault this
feature actually had was a placement fault that the arithmetic agreed
with. It ran into the battery box on a short HUD; it shrank over the 3D
view when it should not have; and a harness that resized the widget
below its own minimum reported sizes nobody was ever shown.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from artificial_horizon import ArtificialHorizon

app = QApplication.instance() or QApplication([])

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


def hud_image(w, h, climb=None, overlay=False, throttle=80.0):
    hud = ArtificialHorizon()
    # The widget carries setMinimumSize(220, 220) of its own, so a plain
    # resize below that silently hands back a 220-tall widget and the
    # measurement is of a size nobody asked for. main.py lowers the
    # minimum on macOS - HUD_MIN_H is 84 there - so the floor is real in
    # the app and has to be real here. This cost an hour of reading
    # numbers from the wrong widget.
    hud.setMinimumSize(1, 1)
    hud.resize(w, h)
    hud.overlay_mode = overlay
    hud.set_throttle(throttle)
    hud.set_battery_voltage(16.2)
    if climb is not None:
        hud.set_climb(climb)
    hud.show()
    app.processEvents()
    img = hud.grab().toImage()
    size = (hud.width(), hud.height())
    hud.hide()
    return img, size


def is_bar_yellow(c):
    """The bar's fill, (255, 214, 0), and not the HUD's other yellows."""
    return c.red() > 200 and 170 < c.green() < 240 and c.blue() < 70


def bar_rows(img, w):
    """Which rows carry bar fill, in the column band at the right edge."""
    x0 = int(w - ArtificialHorizon.RIGHT_GROUP_MARGIN - 12)
    x1 = int(w - ArtificialHorizon.RIGHT_GROUP_MARGIN)
    rows = []
    for y in range(img.height()):
        for x in range(max(0, x0), min(img.width(), x1)):
            if is_bar_yellow(img.pixelColor(x, y)):
                rows.append(y)
                break
    return rows


print("")
print("1. which way it goes, and how far")

W, H = 328, 300
mid = H / 2.0

rows = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=5.0)))
note("a climb fills above the middle", rows and max(rows) <= mid + 2,
     "rows %d..%d, middle %.0f" % (min(rows), max(rows), mid))

rows = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=-5.0)))
note("a descent fills below it", rows and min(rows) >= mid - 2,
     "rows %d..%d" % (min(rows), max(rows)))

full = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=10.0)))
half = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=5.0)))
note("ten metres a second is roughly twice five",
     abs(len(full) - 2 * len(half)) <= 3,
     "%d rows against %d" % (len(full), len(half)))

# Past full scale the bar has nowhere further to go. A reading that kept
# growing would leave the bar and land on the altitude box.
over = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=40.0)))
note("and beyond ten it is clamped, not overdrawn", len(over) == len(full),
     "%d rows against %d at full scale" % (len(over), len(full)))

under = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=-40.0)))
note("in the descending direction too", len(under) == len(full),
     "%d rows" % len(under))

none_yet = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=None)))
note("with no reading it draws no fill", not none_yet,
     "%d rows" % len(none_yet))

level = bar_rows(*(lambda r: (r[0], r[1][0]))(hud_image(W, H, climb=0.0)))
note("and level draws none either", not level, "%d rows" % len(level))


print("")
print("2. what it keeps clear of")

# The battery box is 60px tall where the wind readout opposite it is 40,
# so this bar runs into its neighbour on a HUD that leaves the throttle
# bar clear. It started overlapping below about 230px of height and was
# 40px inside the box at the macOS floor.
SIZES = [(746, 309), (328, 300), (328, 240), (328, 200), (328, 175),
         (746, 175), (328, 120), (640, 360), (240, 175)]
collisions = []
for w, h in SIZES:
    img, (aw, ah) = hud_image(w, h, climb=10.0)
    box = ArtificialHorizon.battery_box_rect_for(aw, ah)
    inside = [y for y in bar_rows(img, aw)
              if box.top() <= y <= box.bottom()]
    if inside:
        collisions.append("%dx%d" % (w, h))
note("the bar never enters the battery box", not collisions,
     ", ".join(collisions) or "%d sizes checked" % len(SIZES))

# At the macOS floor there is no room for a bar once the box is cleared.
# A stub clipped off by the bottom edge would read as a reading.
img, (aw, ah) = hud_image(328, 84, climb=10.0)
note("and draws nothing at all where there is no room for one",
     not bar_rows(img, aw), "%dx%d" % (aw, ah))

# The caption is centred on a bar narrower than itself, which is the
# fault RIGHT_GROUP_MARGIN exists to prevent - the same one the throttle
# caption had against the left edge before LEFT_GROUP_MARGIN.
touching = []
for w, h in SIZES + [(328, 84)]:
    img, (aw, ah) = hud_image(w, h, climb=-12.5)
    for y in range(img.height()):
        px, bg = img.pixelColor(aw - 1, y), img.pixelColor(0, y)
        if (abs(px.red() - bg.red()) + abs(px.green() - bg.green())
                + abs(px.blue() - bg.blue())) > 24:
            touching.append("%dx%d" % (w, h))
            break
note("nothing it draws reaches the right edge", not touching,
     ", ".join(touching) or "%d sizes checked" % (len(SIZES) + 1))


print("")
print("3. the same instrument in both views")

# overlay_mode shrinks the throttle bar by a tenth to clear Cesium's logo
# in the bottom left. There is no logo under this one, and vertical speed
# is the reading that matters most in the view where the pilot is looking
# out rather than at the numbers - so it does not follow.
for w, h in ((746, 309), (328, 240), (640, 360)):
    flat, (fw, _) = hud_image(w, h, climb=10.0, overlay=False)
    ovl, (ow, _) = hud_image(w, h, climb=10.0, overlay=True)
    a, b = bar_rows(flat, fw), bar_rows(ovl, ow)
    note("at %dx%d it is identical over the 3D view" % (w, h), a == b,
         "%s against %s" % ((min(a), max(a)) if a else None,
                            (min(b), max(b)) if b else None))

# The control for the check above. The throttle SHOULD shrink; if it does
# not, overlay_mode is not reaching the paint and the three checks above
# proved nothing at all.
def throttle_rows(img, w):
    rows = []
    for y in range(img.height()):
        for x in range(0, min(26, img.width())):
            c = img.pixelColor(x, y)
            if 90 < c.red() < 160 and c.green() > 170 and 90 < c.blue() < 160:
                rows.append(y)
                break
    return rows


flat, (fw, _) = hud_image(746, 309, climb=10.0, overlay=False)
ovl, (ow, _) = hud_image(746, 309, climb=10.0, overlay=True)
ta, tb = throttle_rows(flat, fw), throttle_rows(ovl, ow)
note("while the throttle bar still does shrink", ta != tb,
     "%s against %s" % ((min(ta), max(ta)) if ta else None,
                        (min(tb), max(tb)) if tb else None))


print("")
print("4. the numbers behind it")

note("full scale is ten metres a second",
     ArtificialHorizon.VSI_FULL_SCALE_MPS == 10.0)
note("the right group has the same margin as the left",
     ArtificialHorizon.RIGHT_GROUP_MARGIN
     == ArtificialHorizon.LEFT_GROUP_MARGIN,
     "%s and %s" % (ArtificialHorizon.RIGHT_GROUP_MARGIN,
                    ArtificialHorizon.LEFT_GROUP_MARGIN))
note("a reading can be set and read back",
     (lambda hud: (hud.set_climb(-3.5), hud.climb)[1])(ArtificialHorizon())
     == -3.5)

print("")
print("5. what the bar did to the 3D view's credit line")

# Cesium's two credit lines are right-aligned at the bottom of the FPV
# view, which is the edge the bar now occupies. Where they sit is per
# platform: on Windows they move inboard of the bar's group, on macOS
# they stay where they have always been, because moved inboard there
# they left a visibly empty strip beside them.
import fpv_view

note("the inset is chosen per platform",
     fpv_view.CREDIT_RIGHT_PX == (8 if fpv_view.MACOS else 44),
     "%s on %s" % (fpv_view.CREDIT_RIGHT_PX,
                   "macOS" if fpv_view.MACOS else "Windows"))
# On Windows it has to clear the widest the bar's group reaches: the
# margin plus a 12px bar is 25, and the caption is centred on the bar,
# wider than it, and may sit within 3px of the edge.
worst_case = ArtificialHorizon.RIGHT_GROUP_MARGIN + 12
note("and on Windows it clears the bar itself",
     fpv_view.MACOS or fpv_view.CREDIT_RIGHT_PX > worst_case,
     "%s against the bar's %s" % (fpv_view.CREDIT_RIGHT_PX, worst_case))

# The page is built by substitution, and a placeholder that survives it
# leaves "right: %%CREDITRIGHT%%px" in the stylesheet - which the browser
# drops silently, putting the lines wherever Cesium likes. Nothing about
# the running app would say so.
page = (fpv_view.CESIUM_HTML
        .replace("%%BASE%%", "/base/")
        .replace("%%CREDITRIGHT%%", str(fpv_view.CREDIT_RIGHT_PX))
        .replace("%%TOKEN%%", "token"))
import re
leftover = re.findall(r"%%[A-Z]+%%", page)
note("every placeholder in the page is substituted", not leftover,
     ", ".join(leftover) or "none left")
note("and the rule carries the platform's number",
     "right: %dpx !important;" % fpv_view.CREDIT_RIGHT_PX in page)
# The licence asks for legible attribution, not for prominent. Nine is
# the floor; the logo keeps its own 14.
note("the credit is shrunk but not to nothing",
     "font-size: 9px !important;" in page)
note("and the logo keeps its legibility floor",
     "max-height: 14px" in page)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
