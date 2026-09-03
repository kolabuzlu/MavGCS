"""
A hidden 8-bit river-flying view, driven by the real aircraft.

Reached by pressing and holding on bare sky or ground in the HUD, and
left the same way. It is a toy, but it is an honest one: the scenery is
invented, while everything that moves comes from telemetry. Groundspeed
scrolls the river; the aeroplane's place across it is the crosstrack
error, so the river is the track it is meant to be on; throttle fills
the fuel gauge; altitude sizes the shadow. Nothing here commands the
aircraft, and nothing here moves unless the aircraft did.

The look comes from drawing into a small image - a grid about 150 rows
tall, as many columns wide as the panel's shape calls for - and blitting
it up with nearest-neighbour scaling. Drawing "big pixels" directly at
the widget's own size never looks right: the shapes end up crisp where
the era's hardware would have been blocky, and diagonals give the game
away. The grid follows the panel rather than being a fixed shape inside
it, so the picture fills the space with no black down the sides.

It is pure QPainter. That matters on this project beyond taste: the 3D
view drives Chromium's GPU path, which is what crashes on the Intel
driver here, and this deliberately stays out of it.
"""

import math
import random
import time

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QImage, QColor, QFont, QPen, QBrush
from PySide6.QtCore import Qt, QTimer, QRect, Signal


# The palette, kept small on purpose - an eight bit machine had no more.
WATER = QColor(48, 80, 216)
WATER_DK = QColor(29, 95, 168)
LAND = QColor(56, 160, 56)
LAND_DK = QColor(30, 110, 30)
LAND_EDGE = QColor(20, 60, 130)
ROAD = QColor(160, 160, 160)
ROAD_LINE = QColor(240, 224, 32)
HOUSE = QColor(240, 240, 240)
ROOF = QColor(208, 32, 32)
ROOF_ALT = QColor(40, 60, 160)
TREE = QColor(24, 104, 24)
TREE_DK = QColor(16, 72, 16)
PLANE = QColor(248, 248, 248)
PLANE_DK = QColor(32, 40, 64)
GAUGE_BG = QColor(240, 224, 32)
INK = QColor(16, 16, 16)


class RetroView(QWidget):
    """The hidden view. Give it telemetry; it flies itself."""

    # Pressing and holding on it again asks to go back.
    exit_requested = Signal()

    # How many pixel rows the scene is drawn in. The number of columns
    # follows from the panel's shape, so the picture fills it exactly
    # rather than being letterboxed inside a fixed grid.
    #
    # This is what decides how chunky it looks, and it has to be read
    # against the space it lands in: the view is about 236px tall here,
    # so 150 rows would make each pixel 1.6 screen pixels - a small
    # drawing rather than a blocky one. 72 rows gives a bit over 3, which
    # is where the aeroplane and the river read the way the original did.
    ROWS = 72

    # World units per second at 1 m/s of groundspeed. Chosen so a typical
    # 22 m/s cruise scrolls at a speed that reads as flying rather than
    # as a slideshow.
    SCROLL_PER_MPS = 2.6
    # With no aircraft attached it still drifts, so the thing is worth
    # looking at on the bench.
    IDLE_SCROLL = 26.0

    # Crosstrack error that puts the aeroplane at the river bank. Beyond
    # this it simply stays there rather than flying over the grass.
    XTRACK_FULL_M = 60.0
    # Below this the autopilot is not really navigating - nothing is
    # steering to a track - and the number is not worth reading.
    XTRACK_LIVE_M = 0.5
    # How quickly the drawn position catches up with the real one. A lag,
    # not an integrator: it always converges on the measurement, so it
    # cannot wander off on its own the way the old roll integration did.
    FOLLOW_PER_S = 3.0

    HOLD_MS = 3000
    FPS = 20                    # deliberately modest: this is scenery

    def __init__(self, parent=None):
        super().__init__(parent)
        # Deliberately no minimum size. This shares a QStackedWidget with
        # the HUD inside a scroll area, and a stack is as tall as its
        # tallest page demands: asking for 220 here raised the column's
        # minimum from 175 and put a scrollbar down the left of the whole
        # program. A hidden view must cost the layout nothing.
        self.setMouseTracking(True)

        self.groundspeed = None     # m/s
        self.roll = 0.0             # radians
        self.throttle = None        # percent
        self.altitude = None        # m
        self.airspeed = None
        self.xtrack = None          # m, signed: positive is right of track

        self._world = 0.0           # how far the river has scrolled
        self._x = 0.0               # the aeroplane's place across the river
        self._last = time.monotonic()
        self._seed = random.randrange(1 << 30)

        self._gw, self._gh = 120, self.ROWS      # refreshed to fit the panel
        self._image = QImage(self._gw, self._gh, QImage.Format_RGB32)

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self.exit_requested.emit)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._advance)

    # ---------------------------------------------------------- telemetry

    def set_state(self, groundspeed=None, roll=None, throttle=None,
                  altitude=None, airspeed=None, xtrack=None):
        """Whatever is known right now. Any of it may be None."""
        if xtrack is not None:
            self.xtrack = xtrack
        if groundspeed is not None:
            self.groundspeed = groundspeed
        if roll is not None:
            self.roll = roll
        if throttle is not None:
            self.throttle = throttle
        if altitude is not None:
            self.altitude = altitude
        if airspeed is not None:
            self.airspeed = airspeed

    # ------------------------------------------------------------ running

    def start(self):
        self._last = time.monotonic()
        self._tick.start(int(1000 / self.FPS))

    def stop(self):
        self._tick.stop()
        self._hold.stop()

    def _advance(self):
        now = time.monotonic()
        dt = min(0.25, now - self._last)     # a stall must not teleport it
        self._last = now

        speed = (self.groundspeed * self.SCROLL_PER_MPS
                 if self.groundspeed else self.IDLE_SCROLL)
        self._world += speed * dt

        cx, half = self._river(self._world + self._gh)
        target = self._lateral_target(half)
        # Ease towards where the aircraft actually is. Because the target
        # is recomputed from telemetry every frame this converges and
        # stays; integrating roll, as this did before, meant any small
        # standing bank slid the aeroplane across the screen by itself.
        self._x += (target - self._x) * min(1.0, dt * self.FOLLOW_PER_S)
        self.update()

    def _lateral_target(self, half):
        """Where across the river the aeroplane belongs, from telemetry.

        The river is the track it is meant to be flying, so its place in
        the river is its crosstrack error - genuinely where it is, not a
        game. Nothing is navigating in MANUAL or a hand-flown cruise
        though, and then crosstrack reads zero and means nothing, so bank
        stands in: a wing down puts it to that side, wings level centres
        it. Neither can drift, because both are read fresh each frame.
        """
        travel = max(2.0, half - 6.0)
        if self.xtrack is not None and abs(self.xtrack) >= self.XTRACK_LIVE_M:
            frac = max(-1.0, min(1.0, self.xtrack / self.XTRACK_FULL_M))
            return frac * travel
        return max(-1.0, min(1.0, math.sin(self.roll) * 1.6)) * travel * 0.85

    # ------------------------------------------------------------- world

    def _river(self, y):
        """Centre and half-width of the river at this distance downstream.

        A closed form rather than stored terrain: the view can be entered
        at any moment and must draw the same river every time, without
        keeping anything.
        """
        # Everything is a fraction of the grid width, so a wide panel gets
        # a wide river rather than the same river with more grass at the
        # sides.
        cx = (self._gw / 2
              + self._gw * 0.172 * math.sin(y / 71.0)
              + self._gw * 0.070 * math.sin(y / 23.0 + 1.7))
        half = self._gw * (0.203 + 0.055 * math.sin(y / 47.0 + 0.6))
        return cx, half

    def _things_near(self, top, bottom):
        """Houses and trees whose slot falls in this stretch of river.

        Each slot's contents come from its own number, so scenery is
        stable as it scrolls rather than flickering into existence.
        """
        out = []
        step = 14
        first = int(top // step) - 1
        last = int(bottom // step) + 1
        for slot in range(first, last + 1):
            h = (slot * 2654435761 + self._seed) & 0xFFFFFFFF
            if (h >> 3) % 5 == 0:
                continue                     # a gap, so it is not a parade
            y = slot * step + (h % step)
            cx, half = self._river(y)
            side = -1 if (h >> 7) & 1 else 1
            off = half + 6 + ((h >> 9) % 16)
            x = cx + side * off
            kind = "house" if ((h >> 5) & 3) == 0 else "tree"
            out.append((x, y, kind, h))
        return out

    # ------------------------------------------------------------ drawing

    def _fit_grid(self):
        """Size the pixel grid to the panel, keeping the pixels square."""
        w, h = max(1, self.width()), max(1, self.height())
        cell = h / float(self.ROWS)
        gw = max(40, int(round(w / cell)))
        if (gw, self.ROWS) != (self._gw, self._gh):
            self._gw, self._gh = gw, self.ROWS
            self._image = QImage(gw, self.ROWS, QImage.Format_RGB32)

    def paintEvent(self, event):
        self._fit_grid()
        img = self._image
        p = QPainter(img)
        self._draw_scene(p)
        p.end()

        out = QPainter(self)
        # No smoothing: the whole point is that the pixels stay square.
        out.setRenderHint(QPainter.SmoothPixmapTransform, False)
        # Filled edge to edge - the grid was shaped to this rectangle, so
        # there is nothing to letterbox.
        out.drawImage(self.rect(), img)
        out.end()

    def _draw_scene(self, p):
        W, H = self._gw, self._gh
        p.fillRect(0, 0, W, H, WATER)
        top = self._world
        p.setPen(Qt.NoPen)

        # Banks, a row at a time. Cheap at this size, and it lets the
        # river edge be genuinely ragged rather than a smooth curve.
        for row in range(H):
            y = top + (H - row)
            cx, half = self._river(y)
            left = int(cx - half)
            right = int(cx + half)
            jag = ((int(y) * 2246822519) >> 13) % 3
            p.fillRect(0, row, max(0, left - jag), 1, LAND)
            p.fillRect(right + jag, row, max(0, W - right - jag), 1, LAND)
            p.fillRect(max(0, left - jag), row, 2, 1, LAND_EDGE)
            p.fillRect(right + jag - 1, row, 2, 1, LAND_EDGE)
            if int(y) % 23 == 0:
                p.fillRect(max(0, left - jag - 3), row, 3, 1, LAND_DK)

        for x, y, kind, h in self._things_near(top, top + H):
            row = int(H - (y - top))
            if not (-8 <= row <= H + 8):
                continue
            if kind == "house":
                self._house(p, int(x), row, h)
            else:
                self._tree(p, int(x), row, h)

        self._aircraft(p)
        self._panel(p)

    def _house(self, p, x, y, h):
        w = 7 + (h % 3)
        p.fillRect(x - w // 2, y - 4, w, 5, HOUSE)
        p.fillRect(x - w // 2, y - 6, w, 2, ROOF if (h >> 11) & 1 else ROOF_ALT)
        p.fillRect(x - 1, y - 2, 2, 3, INK)          # a door

    def _tree(self, p, x, y, h):
        p.fillRect(x - 1, y - 1, 2, 3, TREE_DK)      # trunk
        r = 2 + (h % 2)
        p.fillRect(x - r, y - 4 - r, r * 2, r * 2, TREE)
        p.fillRect(x - r + 1, y - 5 - r, r * 2 - 2, 1, TREE)

    def _aircraft(self, p):
        cx, _ = self._river(self._world + self._gh)
        x = int(cx + self._x)
        y = self._gh - 34

        # A shadow that grows as you descend, which is the only altitude
        # cue the original had and still the clearest one.
        alt = self.altitude if self.altitude is not None else 120.0
        near = max(0.0, min(1.0, 1.0 - alt / 260.0))
        if near > 0.05:
            off = int(2 + near * 5)
            p.fillRect(x - 4 + off, y + 6 + off, 9, 3, WATER_DK)

        bank = math.sin(self.roll)
        tilt = int(round(bank * 2))
        p.fillRect(x - 1, y - 6, 3, 14, PLANE)                 # fuselage
        p.fillRect(x - 8, y + tilt, 17, 3, PLANE)              # main wing
        p.fillRect(x - 4, y + 8 - abs(tilt), 9, 2, PLANE)      # tailplane
        p.fillRect(x - 1, y - 7, 3, 2, PLANE_DK)               # nose
        p.fillRect(x - 1, y + 1, 3, 2, PLANE_DK)               # canopy

    def _panel(self, p):
        """The bottom strip: fuel from throttle, and distance as a score.

        Everything here is a fraction of the grid, not a fixed number of
        rows. Fixed at 16 rows it was a sixth of the picture at a fine
        grid and a third of it at a coarse one.
        """
        W, H = self._gw, self._gh
        strip = max(8, int(H * 0.115))
        top = H - strip
        p.fillRect(0, top, W, strip, INK)

        thr = self.throttle if self.throttle is not None else 0.0
        gw = max(28, int(W * 0.30))
        gh = max(5, strip - 3)
        gx, gy = (W - gw) // 2, top + (strip - gh) // 2
        p.fillRect(gx, gy, gw, gh, GAUGE_BG)
        p.fillRect(gx + 1, gy + 1, gw - 2, gh - 2, INK)
        fill = int((gw - 4) * max(0.0, min(100.0, thr)) / 100.0)
        p.fillRect(gx + 2, gy + 2, fill, gh - 4, GAUGE_BG)

        f = QFont("Courier New")
        f.setPointSizeF(max(3.5, strip * 0.62))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(GAUGE_BG))
        pad = max(6, int(W * 0.02))
        p.drawText(QRect(gx - pad - 8, top, 8, strip),
                   Qt.AlignRight | Qt.AlignVCenter, "E")
        p.drawText(QRect(gx + gw + pad, top, 10, strip),
                   Qt.AlignLeft | Qt.AlignVCenter, "F")
        p.drawText(QRect(0, top, gx - pad - 10, strip),
                   Qt.AlignRight | Qt.AlignVCenter,
                   f"{int(self._world / 40) % 100000:5d} ")
        spd = self.airspeed if self.airspeed is not None else 0.0
        p.drawText(QRect(gx + gw + pad + 12, top,
                         W - (gx + gw + pad + 12), strip),
                   Qt.AlignLeft | Qt.AlignVCenter, f"{spd:0.0f}")

    # ------------------------------------------------------------- input

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
            self._hold.start()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._hold.isActive():
            start = getattr(self, "_press", None)
            if start is None or (event.position().toPoint()
                                 - start).manhattanLength() > 12:
                self._hold.stop()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._hold.stop()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._hold.stop()
        super().leaveEvent(event)
