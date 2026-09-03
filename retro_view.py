"""
A hidden 8-bit river-flying view, driven by the real aircraft.

Reached by pressing and holding on bare sky or ground in the HUD, and
left the same way. It is a toy, but it is an honest one: the scenery is
invented, while everything that moves comes from telemetry. Groundspeed
scrolls the river, roll steers, throttle fills the fuel gauge, altitude
sizes the shadow. Nothing here commands the aircraft.

The look comes from drawing into a small image - 128x160 pixels - and
blitting it up with nearest-neighbour scaling. Drawing "big pixels"
directly at the widget's own size never looks right: the shapes end up
crisp where the era's hardware would have been blocky, and diagonals
give the game away.

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

    # The pixel grid everything is drawn on, then scaled up whole.
    GRID_W = 128
    GRID_H = 160

    # World units per second at 1 m/s of groundspeed. Chosen so a typical
    # 22 m/s cruise scrolls at a speed that reads as flying rather than
    # as a slideshow.
    SCROLL_PER_MPS = 2.6
    # With no aircraft attached it still drifts, so the thing is worth
    # looking at on the bench.
    IDLE_SCROLL = 26.0

    HOLD_MS = 3000
    FPS = 20                    # deliberately modest: this is scenery

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setMouseTracking(True)

        self.groundspeed = None     # m/s
        self.roll = 0.0             # radians
        self.throttle = None        # percent
        self.altitude = None        # m
        self.airspeed = None

        self._world = 0.0           # how far the river has scrolled
        self._x = 0.0               # the aeroplane's place across the river
        self._last = time.monotonic()
        self._seed = random.randrange(1 << 30)

        self._image = QImage(self.GRID_W, self.GRID_H, QImage.Format_RGB32)

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self.exit_requested.emit)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._advance)

    # ---------------------------------------------------------- telemetry

    def set_state(self, groundspeed=None, roll=None, throttle=None,
                  altitude=None, airspeed=None):
        """Whatever is known right now. Any of it may be None."""
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

        # Bank to steer, as the original did. The aeroplane is not really
        # moving across the river, so this is honest only as a feel: the
        # roll is real, where it takes you is not.
        self._x += math.sin(self.roll) * 46.0 * dt
        cx, half = self._river(self._world + self.GRID_H)
        self._x = max(-half + 6, min(half - 6, self._x))
        self.update()

    # ------------------------------------------------------------- world

    def _river(self, y):
        """Centre and half-width of the river at this distance downstream.

        A closed form rather than stored terrain: the view can be entered
        at any moment and must draw the same river every time, without
        keeping anything.
        """
        cx = (self.GRID_W / 2
              + 22.0 * math.sin(y / 71.0)
              + 9.0 * math.sin(y / 23.0 + 1.7))
        half = 26.0 + 7.0 * math.sin(y / 47.0 + 0.6)
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

    def paintEvent(self, event):
        img = self._image
        p = QPainter(img)
        self._draw_scene(p)
        p.end()

        out = QPainter(self)
        # No smoothing: the whole point is that the pixels stay square.
        out.setRenderHint(QPainter.SmoothPixmapTransform, False)
        side = min(self.width() / self.GRID_W, self.height() / self.GRID_H)
        w = int(self.GRID_W * side)
        h = int(self.GRID_H * side)
        out.fillRect(self.rect(), INK)
        out.drawImage(QRect((self.width() - w) // 2, (self.height() - h) // 2,
                            w, h), img)
        out.end()

    def _draw_scene(self, p):
        W, H = self.GRID_W, self.GRID_H
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
        cx, _ = self._river(self._world + self.GRID_H)
        x = int(cx + self._x)
        y = self.GRID_H - 34

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
        """The bottom strip: fuel from throttle, and distance as a score."""
        W, H = self.GRID_W, self.GRID_H
        p.fillRect(0, H - 16, W, 16, INK)

        thr = self.throttle if self.throttle is not None else 0.0
        gw, gh = 62, 9
        gx, gy = (W - gw) // 2, H - 12
        p.fillRect(gx, gy, gw, gh, GAUGE_BG)
        p.fillRect(gx + 1, gy + 1, gw - 2, gh - 2, INK)
        fill = int((gw - 4) * max(0.0, min(100.0, thr)) / 100.0)
        p.fillRect(gx + 2, gy + 2, fill, gh - 4, GAUGE_BG)

        f = QFont("Courier New", 6)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(GAUGE_BG))
        p.drawText(QRect(gx - 12, gy - 1, 12, gh), Qt.AlignRight, "E")
        p.drawText(QRect(gx + gw, gy - 1, 14, gh), Qt.AlignLeft, "F")
        p.setPen(QPen(GAUGE_BG))
        p.drawText(QRect(0, H - 12, gx - 14, gh), Qt.AlignRight,
                   f"{int(self._world / 40) % 100000:5d}")
        spd = self.airspeed if self.airspeed is not None else 0.0
        p.drawText(QRect(gx + gw + 16, H - 12, W - gx - gw - 16, gh),
                   Qt.AlignLeft, f"{spd:0.0f}")

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
