"""
A hidden 8-bit map view, flown by the real aircraft.

Reached by pressing and holding on bare sky or ground in the HUD, and
left the same way.

It is a map, not a game. The countryside is invented - there is no river
where this draws one - but it is pinned to real latitude and longitude
and it stays where it is put. Nothing scrolls on a timer. The picture
moves only because the aircraft moved, by as much as the aircraft moved,
and turns only because the aircraft turned. Fly a circuit and the same
houses come round again; sit on the ground and nothing happens at all.

The aeroplane is drawn fixed, pointing up, with the world turned under
it so its heading is towards the top of the screen - the same track-up
convention as the moving map. Its place on screen never changes, because
on a map the aircraft is the fixed thing and the ground is what moves.

The look comes from drawing into a small image - about 72 rows tall, as
many columns as the panel's shape calls for - and blitting it up with
smoothing off. Drawing "big pixels" at the widget's own size never looks
right: the shapes come out crisp where the era's hardware was blocky.

It is pure QPainter. That matters here beyond taste: the 3D view drives
Chromium's GPU path, which is what crashes on the Intel driver on this
machine, and this deliberately stays out of it.
"""

import math

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import (QPainter, QImage, QColor, QFont, QPen, QBrush,
                           QPolygonF, QTransform)
from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPointF, Signal


# The palette, kept small on purpose - an eight bit machine had no more.
WATER = QColor(48, 80, 216)
WATER_DK = QColor(29, 95, 168)
LAND = QColor(56, 160, 56)
LAND_ALT = QColor(74, 176, 66)
LAND_DRY = QColor(150, 156, 72)
HOUSE = QColor(240, 240, 240)
ROOF = QColor(208, 32, 32)
ROOF_ALT = QColor(40, 60, 160)
TREE = QColor(24, 104, 24)
PLANE = QColor(248, 248, 248)
PLANE_DK = QColor(32, 40, 64)
CAPTION = QColor(240, 224, 32)          # the NO FIX notice

# Metres per degree. Good enough for scenery; this is not a survey.
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0


def _hash(a, b):
    """A stable number for a pair of world cells."""
    h = (int(a) * 73856093) ^ (int(b) * 19349663)
    h = (h ^ (h >> 13)) * 1274126177
    return (h ^ (h >> 16)) & 0xFFFFFFFF


def river_centre(y):
    """Where the invented river runs, at this northing. Metres.

    A closed form, so the same stretch is drawn every time it comes into
    view, with nothing stored between frames or between flights.
    """
    # Wound tighter than a real river would be. With a long meander the
    # water sat a kilometre off to one side and was almost never in view,
    # and this is meant to be a river flight.
    return (620.0 * math.sin(y / 2100.0)
            + 260.0 * math.sin(y / 780.0 + 1.3)
            + 90.0 * math.sin(y / 310.0 + 0.7))


def river_half_width(y):
    return 95.0 + 30.0 * math.sin(y / 900.0 + 0.4)


class RetroView(QWidget):
    """The hidden map. Give it position and heading; it draws the rest."""

    exit_requested = Signal()

    # Pixel rows the scene is drawn in; the columns follow the panel's
    # shape. This decides how chunky it looks: the view is about 236px
    # tall here, so 72 rows puts each drawn pixel a bit over 3 real ones.
    ROWS = 72

    # Metres of ground per drawn pixel. About 900m across the panel -
    # close enough to see the aeroplane move, wide enough that a circuit
    # is not a blur.
    M_PER_CELL = 4.0

    # Where the aeroplane sits down the screen. Low, so most of the
    # picture is the ground ahead of it.
    ANCHOR_Y = 0.66

    # The world grid scenery is scattered on, in metres.
    PLOT_M = 70.0

    HOLD_MS = 3000
    FPS = 15                # scenery, and the ground does not race past

    def __init__(self, parent=None):
        super().__init__(parent)
        # Deliberately no minimum size. This shares a QStackedWidget with
        # the HUD inside a scroll area, and a stack is as tall as its
        # tallest page demands: asking for one here raised the column's
        # minimum and put a scrollbar down the side of the whole program.
        self.setMouseTracking(True)

        self.lat = None
        self.lon = None
        self.heading = 0.0          # degrees, where the nose points
        self.roll = 0.0
        self.altitude = None

        self._gw, self._gh = 120, self.ROWS
        self._image = QImage(self._gw, self._gh, QImage.Format_RGB32)

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self.exit_requested.emit)

        # Repaints so the picture keeps up with telemetry. It draws the
        # same thing every time until the aircraft actually moves.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.update)

    # ---------------------------------------------------------- telemetry

    def set_state(self, roll=None, altitude=None, heading=None,
                  lat=None, lon=None):
        """Whatever is known right now. Any of it may be None.

        Only what is actually drawn: where the aircraft is, which way it
        points, how it is banked and how high. The speeds and throttle
        went with the readout strip - carrying telemetry nothing draws
        would only invite someone to wonder where it had gone.
        """
        if roll is not None:
            self.roll = roll
        if altitude is not None:
            self.altitude = altitude
        if heading is not None:
            self.heading = heading
        if lat is not None:
            self.lat = lat
        if lon is not None:
            self.lon = lon

    def start(self):
        self._tick.start(int(1000 / self.FPS))

    def stop(self):
        self._tick.stop()
        self._hold.stop()

    # -------------------------------------------------------------- world

    def world_xy(self):
        """The aircraft in metres east and north, straight from lat/lon.

        Absolute, rather than relative to wherever the view happened to be
        opened, so the countryside is pinned to the ground: fly a circuit
        and the same houses come round again.
        """
        if self.lat is None or self.lon is None:
            return None
        y = self.lat * M_PER_DEG_LAT
        x = self.lon * M_PER_DEG_LON * math.cos(math.radians(self.lat))
        return x, y

    def _view_transform(self):
        """World metres -> drawn pixels: track up, aircraft on the anchor."""
        pos = self.world_xy()
        if pos is None:
            return None
        wx, wy = pos
        t = QTransform()
        t.translate(self._gw / 2.0, self._gh * self.ANCHOR_Y)
        # Turn the world the opposite way to the heading, so the nose
        # points up the screen.
        t.rotate(-self.heading)
        # Metres to pixels, and the y flip that puts north up.
        t.scale(1.0 / self.M_PER_CELL, -1.0 / self.M_PER_CELL)
        t.translate(-wx, -wy)
        return t

    def _visible_world(self, t):
        """An axis-aligned world box covering everything on screen."""
        inv, ok = t.inverted()
        if not ok:
            return None
        pts = [inv.map(QPointF(x, y))
               for x, y in ((0, 0), (self._gw, 0),
                            (0, self._gh), (self._gw, self._gh))]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = self.PLOT_M * 2
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    # ------------------------------------------------------------ drawing

    def _fit_grid(self):
        w, h = max(1, self.width()), max(1, self.height())
        cell = h / float(self.ROWS)
        gw = max(40, int(round(w / cell)))
        if (gw, self.ROWS) != (self._gw, self._gh):
            self._gw, self._gh = gw, self.ROWS
            self._image = QImage(gw, self.ROWS, QImage.Format_RGB32)

    def paintEvent(self, event):
        self._fit_grid()
        p = QPainter(self._image)
        self._draw(p)
        p.end()

        out = QPainter(self)
        out.setRenderHint(QPainter.SmoothPixmapTransform, False)
        out.drawImage(self.rect(), self._image)
        out.end()

    def _draw(self, p):
        p.fillRect(0, 0, self._gw, self._gh, LAND)
        t = self._view_transform()
        if t is None:
            self._no_fix(p)
        else:
            box = self._visible_world(t)
            p.save()
            p.setTransform(t)
            p.setPen(Qt.NoPen)
            self._fields(p, box)
            self._ponds(p, box)
            self._river(p, box)
            self._plots(p, box)
            p.restore()
            self._aircraft(p)

    def _fields(self, p, box):
        """Blocks of slightly different green, so the ground has grain."""
        x0, y0, x1, y1 = box
        step = self.PLOT_M * 3
        for gx in range(int(math.floor(x0 / step)),
                        int(math.floor(x1 / step)) + 1):
            for gy in range(int(math.floor(y0 / step)),
                            int(math.floor(y1 / step)) + 1):
                h = _hash(gx, gy * 3 + 1)
                if h % 3 == 0:
                    continue
                colour = LAND_ALT if h % 3 == 1 else LAND_DRY
                p.fillRect(QRectF(gx * step, gy * step, step, step), colour)

    def _river(self, p, box):
        """The invented river, as a band along its centreline."""
        x0, y0, x1, y1 = box
        step = 60.0
        first = math.floor(y0 / step) * step
        n = int((y1 - first) / step) + 2
        left, right = [], []
        for i in range(n):
            y = first + i * step
            c = river_centre(y)
            hw = river_half_width(y)
            left.append(QPointF(c - hw, y))
            right.append(QPointF(c + hw, y))
        if len(left) < 2:
            return
        p.setBrush(QBrush(WATER))
        p.drawPolygon(QPolygonF(left + list(reversed(right))))

    def _ponds(self, p, box):
        """Standing water, so the ground is not all field between rivers."""
        x0, y0, x1, y1 = box
        step = self.PLOT_M * 4
        p.setBrush(QBrush(WATER))
        for gx in range(int(math.floor(x0 / step)),
                        int(math.floor(x1 / step)) + 1):
            for gy in range(int(math.floor(y0 / step)),
                            int(math.floor(y1 / step)) + 1):
                h = _hash(gx * 7 + 3, gy * 11 + 5)
                if h % 7 != 0:
                    continue
                wx = gx * step + (h % 120)
                wy = gy * step + ((h >> 7) % 120)
                w = 60.0 + (h >> 15) % 90
                d = 40.0 + (h >> 21) % 70
                p.fillRect(QRectF(wx, wy, w, d), WATER)

    def _plots(self, p, box):
        """Houses and trees on a world grid, and never in the water."""
        x0, y0, x1, y1 = box
        step = self.PLOT_M
        for gx in range(int(math.floor(x0 / step)),
                        int(math.floor(x1 / step)) + 1):
            for gy in range(int(math.floor(y0 / step)),
                            int(math.floor(y1 / step)) + 1):
                h = _hash(gx, gy)
                if h % 5 < 2:
                    continue                    # most plots are empty
                wx = gx * step + (h % 40)
                wy = gy * step + ((h >> 6) % 40)
                if abs(wx - river_centre(wy)) < river_half_width(wy) + 12:
                    continue                    # nothing stands in a river
                if (h >> 11) % 4 == 0:
                    self._house(p, wx, wy, h)
                else:
                    self._tree(p, wx, wy, h)

    def _house(self, p, wx, wy, h):
        m = self.M_PER_CELL
        w = m * (6 + h % 3)
        d = m * 5
        p.fillRect(QRectF(wx, wy, w, d), HOUSE)
        p.fillRect(QRectF(wx, wy + d * 0.55, w, d * 0.45),
                   ROOF if (h >> 13) & 1 else ROOF_ALT)

    def _tree(self, p, wx, wy, h):
        m = self.M_PER_CELL
        r = m * (2 + h % 2)
        p.fillRect(QRectF(wx, wy, r * 2, r * 2), TREE)

    def _aircraft(self, p):
        """Fixed, pointing up. On a map the aircraft is the fixed thing."""
        x = int(self._gw / 2)
        y = int(self._gh * self.ANCHOR_Y)

        alt = self.altitude if self.altitude is not None else 120.0
        near = max(0.0, min(1.0, 1.0 - alt / 260.0))
        if near > 0.05:
            off = int(1 + near * 4)
            p.fillRect(x - 4 + off, y - 1 + off, 9, 3, WATER_DK)

        # Bank shows in the wing, which is the only place it can show
        # when the aeroplane itself never turns on screen.
        tilt = int(round(math.sin(self.roll) * 2))
        p.fillRect(x - 1, y - 6, 3, 13, PLANE)              # fuselage
        p.fillRect(x - 8, y + tilt, 17, 3, PLANE)           # main wing
        p.fillRect(x - 4, y + 7 - abs(tilt), 9, 2, PLANE)   # tailplane
        p.fillRect(x - 1, y - 7, 3, 2, PLANE_DK)            # nose
        p.fillRect(x - 1, y + 1, 3, 2, PLANE_DK)            # canopy

    def _no_fix(self, p):
        """No position, nothing to place. Say so rather than invent one."""
        W, H = self._gw, self._gh
        p.fillRect(0, 0, W, H, WATER_DK)
        f = QFont("Courier New")
        f.setPointSizeF(max(4.0, H * 0.10))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(CAPTION))
        p.drawText(QRect(0, 0, W, H), Qt.AlignCenter, "NO FIX")

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
