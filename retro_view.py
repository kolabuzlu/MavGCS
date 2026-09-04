"""
A hidden 8-bit chase view, flown by the real aircraft.

Reached by pressing and holding on bare sky or ground in the HUD, and
left the same way.

The camera sits behind the aeroplane and above it, looking down at
forty-five degrees, so the ground runs away into the distance and things
standing on it have sides as well as tops. It is a proper projection,
not a tilted picture: every corner of every field, house and boat is a
point in metres that gets carried into the camera frame and divided by
its distance. That is what makes the far bank narrow towards the top of
the screen while the near one sprawls.

It is a view of a place, not a game. The countryside is invented - there
is no river where this draws one - but it is pinned to real latitude and
longitude and it stays where it is put. Nothing scrolls on a timer. The
picture moves only because the aircraft moved, by as much as the
aircraft moved, and turns only because the aircraft turned. Fly a
circuit and the same houses come round again; sit on the ground and
nothing happens at all.

Height is the one place the drawing bends. The camera height above the
ground is a squashed version of the real altitude - it follows a climb
closely near the ground and less and less as the aircraft gets high, so
that at two thousand feet the scenery is still something you can make
out rather than a green haze. Altitude still reads, in how far the
shadow has slid back from the aeroplane and how far the ground has
fallen away; it just stops running off the end of the scale.

The look comes from drawing into a small image - about 112 rows tall, as
many columns as the panel shape calls for - and blitting it up with
smoothing off. Drawing at the widget own size never looks right: the
shapes come out crisp where the era hardware was blocky.

It is pure QPainter, with no textures, no shaders and no z-buffer: solid
faces, painted far to near. That matters here beyond taste. The 3D view
drives Chromium GPU path, which is what crashes on the Intel driver on
this machine, and this deliberately stays out of it.
"""

import math

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import (QPainter, QImage, QColor, QFont, QPen, QBrush,
                           QPolygonF)
from PySide6.QtCore import Qt, QTimer, QRect, QPointF, Signal


# The palette, kept small on purpose - an eight bit machine had no more.
# Everything that stands up gets a top colour and a darker side, which is
# the whole of the shading model and quite enough for this.
WATER = QColor(48, 80, 216)
WATER_DK = QColor(29, 62, 168)
LAND = QColor(56, 160, 56)
LAND_ALT = QColor(74, 176, 66)
LAND_DRY = QColor(150, 156, 72)
BANK = QColor(38, 118, 44)              # the lip of ground at the water
HOUSE = QColor(240, 240, 240)
HOUSE_SIDE = QColor(176, 176, 184)
ROOF = QColor(208, 32, 32)
ROOF_SIDE = QColor(140, 20, 20)
ROOF_ALT = QColor(40, 60, 160)
ROOF_ALT_SIDE = QColor(24, 36, 112)
TREE = QColor(40, 136, 40)
TREE_SIDE = QColor(24, 96, 28)
TRUNK = QColor(96, 64, 32)
BOAT = QColor(232, 232, 232)
BOAT_SIDE = QColor(150, 150, 158)
DECK = QColor(216, 48, 40)
DECK_ALT = QColor(240, 200, 48)
PLANE = QColor(248, 248, 248)
PLANE_MID = QColor(196, 200, 212)
PLANE_DK = QColor(32, 40, 64)
SHADOW = QColor(0, 0, 0, 90)
HAZE = QColor(126, 172, 204)            # what distance fades towards
CAPTION = QColor(240, 224, 32)          # the NO FIX notice

# Metres per degree. Good enough for scenery; this is not a survey.
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0

# The camera, relative to the aeroplane, in metres and degrees. Back and
# up are further out than they look like they need to be, and that is
# deliberate: the shadow falls below the aeroplane on screen by more the
# higher it flies, and a closer camera puts it off the bottom edge at
# any useful altitude.
CAM_PITCH_DEG = 45.0    # how far below the horizontal it looks
CAM_BACK_M = 80.0       # how far behind
CAM_UP_M = 96.0         # how far above
V_FOV_DEG = 58.0        # vertical field of view

# The aeroplane drawn larger than life, at a camera eighty metres back.
# Drawn to scale it is nine pixels of wing and reads as a speck; this is
# the one liberty taken with the geometry and it is taken knowingly.
PLANE_SCALE = 1.8

# Nothing nearer than this is drawn; polygons are cut off at it. Without
# a near plane anything level with the camera divides by nothing and
# smears across the screen.
NEAR_M = 4.0

# How far out the ground is drawn, and how much nearer the small things
# stop. A house at a kilometre is under a pixel, so there is no sense
# paying for it.
GROUND_FAR_M = 1500.0
DETAIL_FAR_M = 820.0

# Altitude squashing: how high the camera thinks it is when the aircraft
# is really at h. Rises with h at first, then flattens towards
# ALT_SOFT_M so the scenery never shrinks away to nothing.
ALT_FLOOR_M = 12.0
ALT_SOFT_M = 140.0

# How far apart the rivers run, east to west. The meander below is a
# function of northing alone, which put the water at a fixed easting of
# a few hundred metres - somewhere off the west coast of Africa. Flying
# anywhere else on earth there was no river within two thousand miles,
# which is why this view had never shown any water. Repeating it across
# the map puts a river within a mile or so of wherever the aircraft is,
# and keeps it a closed form pinned to the ground: the same stretch is
# still in the same place every time it comes round.
RIVER_SPACING_M = 2200.0

# How much of a bank the camera takes up. Rigidly mounted it would take
# all of it, and the ground would swing about far too hard to read; none
# of it and turns look oddly flat. A third is about right.
ROLL_SHARE = 0.34


def _hash(a, b):
    """A stable number for a pair of world cells."""
    h = (int(a) * 73856093) ^ (int(b) * 19349663)
    h = (h ^ (h >> 13)) * 1274126177
    return (h ^ (h >> 16)) & 0xFFFFFFFF


def meander(y):
    """How far the river wanders from its own line, at this northing.

    A closed form, so the same stretch is drawn every time it comes into
    view, with nothing stored between frames or between flights.
    """
    # Wound tighter than a real river would be. With a long meander the
    # water sat a kilometre off to one side and was almost never in view,
    # and this is meant to be a river flight.
    return (620.0 * math.sin(y / 2100.0)
            + 260.0 * math.sin(y / 780.0 + 1.3)
            + 90.0 * math.sin(y / 310.0 + 0.7))


def river_centre(y, near_x=0.0, branch=None):
    """The easting of a river at this northing, in metres.

    The rivers repeat every RIVER_SPACING_M eastwards, all following the
    same meander, so they run parallel and never meet. By default this
    gives the one nearest near_x - which is what a "is this spot in the
    water" question wants; pass a branch number to ask for a particular
    one.
    """
    m = meander(y)
    if branch is None:
        branch = round((near_x - m) / RIVER_SPACING_M)
    return m + branch * RIVER_SPACING_M


def river_half_width(y):
    return 95.0 + 30.0 * math.sin(y / 900.0 + 0.4)


def drawn_altitude(alt):
    """The height the picture is drawn at, for a real altitude of alt.

    See the note at the top: this follows the first hundred metres of a
    climb fairly honestly and then gives way, so the ground stays worth
    looking at however high the aircraft goes.
    """
    if alt is None or alt < 0.0:
        alt = 0.0
    return ALT_FLOOR_M + ALT_SOFT_M * (1.0 - math.exp(-alt / ALT_SOFT_M))


class _Camera:
    """Turns world metres into camera metres, and those into pixels.

    Camera space is (X, V, D): X across, V up, D straight out along the
    line of sight. Keeping the two steps apart is what lets polygons be
    cut against the near plane before anything is divided by D.
    """

    def __init__(self, ax, ay, agl, heading, gw, gh):
        h = math.radians(heading)
        self.fx, self.fy = math.sin(h), math.cos(h)     # forward, east/north
        self.rx, self.ry = math.cos(h), -math.sin(h)    # right
        self.cxw = ax - self.fx * CAM_BACK_M
        self.cyw = ay - self.fy * CAM_BACK_M
        self.cz = agl + CAM_UP_M
        t = math.radians(CAM_PITCH_DEG)
        self.st, self.ct = math.sin(t), math.cos(t)
        self.focal = (gh / 2.0) / math.tan(math.radians(V_FOV_DEG / 2.0))
        self.sx0 = gw / 2.0
        self.sy0 = gh / 2.0
        self.gw, self.gh = gw, gh
        # Distance is only ever as far as this camera can see, and that
        # depends on how high it is. Fading against a fixed thousand
        # metres left the low view with no haze in it at all.
        self.haze_far = max(300.0, self.cz * 3.6)

    def cam(self, x, y, z):
        dx, dy, dz = x - self.cxw, y - self.cyw, z - self.cz
        f = dx * self.fx + dy * self.fy
        return (dx * self.rx + dy * self.ry,
                f * self.st + dz * self.ct,
                f * self.ct - dz * self.st)

    def depth(self, x, y, z=0.0):
        dx, dy, dz = x - self.cxw, y - self.cyw, z - self.cz
        return (dx * self.fx + dy * self.fy) * self.ct - dz * self.st

    def screen(self, c):
        s = self.focal / c[2]
        return QPointF(self.sx0 + c[0] * s, self.sy0 - c[1] * s)

    def ground_box(self, far):
        """A world box round everything the ground plane shows.

        The four screen corners are turned back into rays and dropped on
        to the ground. A ray that never reaches it - anything above the
        horizon - is simply cut off at the far limit instead.
        """
        xs, ys = [self.cxw], [self.cyw]
        for sx, sy in ((0, 0), (self.gw, 0), (0, self.gh), (self.gw, self.gh)):
            X = (sx - self.sx0) / self.focal
            V = -(sy - self.sy0) / self.focal
            f = self.ct + V * self.st
            dz = -self.st + V * self.ct
            ex = self.fx * f + self.rx * X
            ny = self.fy * f + self.ry * X
            length = math.sqrt(ex * ex + ny * ny + dz * dz) or 1.0
            t = far / length
            if dz < -1e-6:
                t = min(t, -self.cz / dz)
            xs.append(self.cxw + ex * t)
            ys.append(self.cyw + ny * t)
        return min(xs), min(ys), max(xs), max(ys)


def _clip_near(poly):
    """Cut a camera-space polygon back to the near plane.

    Sutherland and Hodgman, against the one plane that matters. Whole
    fields straddle it whenever the aircraft is low, and a corner left
    behind the camera projects to the wrong side of the screen.
    """
    out = []
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        a_in = a[2] >= NEAR_M
        b_in = b[2] >= NEAR_M
        if a_in:
            out.append(a)
        if a_in != b_in:
            t = (NEAR_M - a[2]) / (b[2] - a[2])
            out.append((a[0] + (b[0] - a[0]) * t,
                        a[1] + (b[1] - a[1]) * t,
                        NEAR_M))
    return out


class RetroView(QWidget):
    """The hidden chase view. Give it position, heading, bank and height."""

    exit_requested = Signal()

    # Pixel rows the scene is drawn in; the columns follow the panel
    # shape. This decides how chunky it looks: the view is about 236px
    # tall here, so 112 rows puts each drawn pixel a bit over two real
    # ones - blocky, but with room for a roof to read as a roof.
    ROWS = 112

    # The world grid scenery is scattered on, in metres.
    PLOT_M = 70.0

    HOLD_MS = 3000
    FPS = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        # Deliberately no minimum size. This shares a QStackedWidget with
        # the HUD inside a scroll area, and a stack is as tall as its
        # tallest page demands: asking for one here raised the column
        # minimum and put a scrollbar down the side of the whole program.
        self.setMouseTracking(True)

        self.lat = None
        self.lon = None
        self.heading = 0.0          # degrees, where the nose points
        self.roll = 0.0             # radians, right wing down positive
        self.altitude = None

        self._gw, self._gh = 160, self.ROWS
        self._image = QImage(self._gw, self._gh, QImage.Format_RGB32)
        self._haze_cache = {}

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

    def _camera(self):
        pos = self.world_xy()
        if pos is None:
            return None
        wx, wy = pos
        return _Camera(wx, wy, drawn_altitude(self.altitude),
                       self.heading, self._gw, self._gh)

    # ------------------------------------------------------------ drawing

    def _fit_grid(self):
        w, h = max(1, self.width()), max(1, self.height())
        cell = h / float(self.ROWS)
        gw = max(48, int(round(w / cell)))
        if (gw, self.ROWS) != (self._gw, self._gh):
            self._gw, self._gh = gw, self.ROWS
            self._image = QImage(gw, self.ROWS, QImage.Format_RGB32)

    def _haze(self, colour, depth, cam):
        """Fade a colour towards the distance.

        Banded into a dozen steps and remembered, both because a period
        machine would have had a dozen shades and not a smooth ramp, and
        because it turns a blend per polygon into a dictionary lookup.
        """
        f = (depth - CAM_BACK_M) / (cam.haze_far - CAM_BACK_M)
        band = int(max(0.0, min(1.0, f)) * 11)
        if band == 0:
            return colour
        key = (colour.rgb(), band)
        got = self._haze_cache.get(key)
        if got is None:
            k = band / 11.0 * 0.55
            got = QColor(
                int(colour.red() + (HAZE.red() - colour.red()) * k),
                int(colour.green() + (HAZE.green() - colour.green()) * k),
                int(colour.blue() + (HAZE.blue() - colour.blue()) * k))
            self._haze_cache[key] = got
        return got

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
        cam = self._camera()
        if cam is None:
            self._no_fix(p)
            return

        # Whatever the far ground does not cover is distance itself.
        p.fillRect(0, 0, self._gw, self._gh, HAZE)
        p.setPen(Qt.NoPen)

        # The share of the bank the camera takes. Turning the scene about
        # the aeroplane rather than the middle of the frame keeps the
        # aeroplane where it belongs while the ground swings.
        anchor = cam.screen(cam.cam(cam.cxw + cam.fx * CAM_BACK_M,
                                    cam.cyw + cam.fy * CAM_BACK_M,
                                    cam.cz - CAM_UP_M))
        share = math.degrees(self.roll) * ROLL_SHARE
        p.save()
        p.translate(anchor)
        p.rotate(-share)
        p.translate(-anchor)

        box = cam.ground_box(GROUND_FAR_M)
        self._fields(p, cam, box)
        self._river(p, cam, box)
        self._ponds(p, cam, box)
        self._props(p, cam)
        # The shadow lies on the ground, so it belongs inside the scene
        # and swings with it. Drawn outside, it slid off its own patch
        # of grass every time the aircraft banked.
        self._plane_shapes(p, cam, shadow=True)
        p.restore()

        self._plane_shapes(p, cam, shadow=False)

    def _quad(self, p, cam, pts, colour):
        """One flat world quad, cut to the near plane and filled."""
        cpts = [cam.cam(x, y, z) for x, y, z in pts]
        if max(c[2] for c in cpts) < NEAR_M:
            return
        if min(c[2] for c in cpts) < NEAR_M:
            cpts = _clip_near(cpts)
            if len(cpts) < 3:
                return
        poly = QPolygonF([cam.screen(c) for c in cpts])
        rect = poly.boundingRect()
        if rect.right() < -2 or rect.left() > self._gw + 2:
            return
        if rect.bottom() < -2 or rect.top() > self._gh + 2:
            return
        depth = sum(c[2] for c in cpts) / len(cpts)
        p.setBrush(QBrush(self._haze(colour, depth, cam)))
        p.drawPolygon(poly)

    def _fields(self, p, cam, box):
        """Blocks of slightly different green, so the ground has grain."""
        x0, y0, x1, y1 = box
        # Smaller than a field really is, but the grain has to survive
        # being pushed into the distance: at three plots to a tile the
        # near ground was one flat slab of colour.
        step = self.PLOT_M * 1.8
        # Far to near, so the near tiles win where they meet on the
        # rounding. There is no depth buffer to sort this out later.
        cells = []
        for gx in range(int(math.floor(x0 / step)),
                        int(math.floor(x1 / step)) + 1):
            for gy in range(int(math.floor(y0 / step)),
                            int(math.floor(y1 / step)) + 1):
                wx, wy = gx * step, gy * step
                cells.append((cam.depth(wx + step / 2, wy + step / 2),
                              wx, wy, _hash(gx, gy * 3 + 1)))
        cells.sort(reverse=True)
        for _, wx, wy, h in cells:
            colour = (LAND if h % 3 == 0
                      else LAND_ALT if h % 3 == 1 else LAND_DRY)
            self._quad(p, cam, ((wx, wy, 0.0), (wx + step, wy, 0.0),
                                (wx + step, wy + step, 0.0),
                                (wx, wy + step, 0.0)), colour)

    def _river(self, p, cam, box):
        """The invented river, as a band along its centreline.

        Drawn a strip at a time rather than as one long polygon. One
        polygon is right in plan but wrong here: the near plane cuts it
        into a shape whose two banks join up across the middle of the
        water, and the fill closes over the wrong side.
        """
        x0, y0, x1, y1 = box
        step = 55.0
        first = math.floor(y0 / step) * step
        n = int((y1 - first) / step) + 2
        strips = []
        for i in range(n):
            ya = first + i * step
            m = meander(ya)
            # Only the branches that could reach the visible box. Away
            # from the coast that is one; sometimes two.
            lo = int(math.floor((x0 - m) / RIVER_SPACING_M))
            hi = int(math.ceil((x1 - m) / RIVER_SPACING_M))
            for k in range(lo, hi + 1):
                strips.append((cam.depth(river_centre(ya, branch=k), ya),
                               ya, ya + step, k))
        strips.sort(reverse=True)
        for _, ya, yb, k in strips:
            ca = river_centre(ya, branch=k)
            cb = river_centre(yb, branch=k)
            ha, hb = river_half_width(ya), river_half_width(yb)
            # A lip of darker ground either side, so the water is cut
            # into the land instead of painted on top of it.
            self._quad(p, cam,
                       ((ca - ha - 9, ya, 0.0), (ca + ha + 9, ya, 0.0),
                        (cb + hb + 9, yb, 0.0), (cb - hb - 9, yb, 0.0)),
                       BANK)
            self._quad(p, cam,
                       ((ca - ha, ya, 0.0), (ca + ha, ya, 0.0),
                        (cb + hb, yb, 0.0), (cb - hb, yb, 0.0)),
                       WATER)

    def _ponds(self, p, cam, box):
        """Standing water, so the ground is not all field between rivers."""
        x0, y0, x1, y1 = box
        step = self.PLOT_M * 4
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
                self._quad(p, cam, ((wx, wy, 0.0), (wx + w, wy, 0.0),
                                    (wx + w, wy + d, 0.0), (wx, wy + d, 0.0)),
                           WATER)

    # ------------------------------------------------------- solid things

    def _box(self, p, cam, wx, wy, base, w, d, h, side, top):
        """An upright box on the world grid: four sides and a lid.

        Every side gets painted, not just the two facing the camera. All
        four are the one colour, so the far pair land under the near pair
        and cost nothing but a fill; sorting them would take more thought
        than it saves.
        """
        x0, y0, x1, y1 = wx, wy, wx + w, wy + d
        zb, zt = base, base + h
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        for i in range(4):
            ax, ay = corners[i]
            bx, by = corners[(i + 1) % 4]
            self._quad(p, cam, ((ax, ay, zb), (bx, by, zb),
                                (bx, by, zt), (ax, ay, zt)), side)
        self._quad(p, cam, ((x0, y0, zt), (x1, y0, zt),
                            (x1, y1, zt), (x0, y1, zt)), top)

    def _props(self, p, cam):
        """Everything that stands up, painted from the back forwards."""
        x0, y0, x1, y1 = cam.ground_box(DETAIL_FAR_M)
        step = self.PLOT_M
        items = []
        for gx in range(int(math.floor(x0 / step)),
                        int(math.floor(x1 / step)) + 1):
            for gy in range(int(math.floor(y0 / step)),
                            int(math.floor(y1 / step)) + 1):
                h = _hash(gx, gy)
                if h % 5 < 2:
                    continue                    # most plots are empty
                wx = gx * step + (h % 40)
                wy = gy * step + ((h >> 6) % 40)
                off = abs(wx - river_centre(wy, near_x=wx))
                if off < river_half_width(wy) - 14:
                    if (h >> 17) % 3:
                        continue                # the river is not a marina
                    kind = "boat"
                elif off < river_half_width(wy) + 14:
                    continue                    # nothing stands on the bank
                else:
                    kind = "house" if (h >> 11) % 4 == 0 else "tree"
                d = cam.depth(wx, wy)
                if d < NEAR_M:
                    continue
                items.append((d, wx, wy, h, kind))
        items.sort(reverse=True)
        for _, wx, wy, h, kind in items:
            if kind == "house":
                self._house(p, cam, wx, wy, h)
            elif kind == "tree":
                self._tree(p, cam, wx, wy, h)
            else:
                self._boat(p, cam, wx, wy, h)

    def _house(self, p, cam, wx, wy, h):
        w = 9.0 + h % 7
        d = 8.0 + (h >> 3) % 5
        wall = 5.0 + (h >> 8) % 4
        self._box(p, cam, wx, wy, 0.0, w, d, wall, HOUSE_SIDE, HOUSE)
        if (h >> 13) & 1:
            top, side = ROOF, ROOF_SIDE
        else:
            top, side = ROOF_ALT, ROOF_ALT_SIDE
        # A roof that oversails a little reads as a roof even when the
        # whole house is nine pixels across.
        self._box(p, cam, wx - 1.0, wy - 1.0, wall, w + 2.0, d + 2.0,
                  3.0, side, top)

    def _tree(self, p, cam, wx, wy, h):
        r = 4.0 + (h >> 4) % 4
        tall = 6.0 + (h >> 9) % 6
        self._box(p, cam, wx - r * 0.2, wy - r * 0.2, 0.0,
                  r * 0.4, r * 0.4, tall * 0.5, TRUNK, TRUNK)
        self._box(p, cam, wx - r / 2, wy - r / 2, tall * 0.4,
                  r, r, tall * 0.6, TREE_SIDE, TREE)

    def _boat(self, p, cam, wx, wy, h):
        """Moored, not motoring. Nothing here moves on a clock."""
        # Barge-sized rather than dinghy-sized. At twelve metres a boat
        # three hundred metres off was four pixels of white and read as
        # a speck of dirt on the screen.
        along = 22.0 + (h >> 5) % 14
        wide = 7.0 + (h >> 12) % 4
        self._box(p, cam, wx, wy, -0.5, wide, along, 2.6, BOAT_SIDE, BOAT)
        deck = DECK if (h >> 19) & 1 else DECK_ALT
        self._box(p, cam, wx + wide * 0.15, wy + along * 0.25, 2.1,
                  wide * 0.7, along * 0.4, 2.2, BOAT_SIDE, deck)

    # ---------------------------------------------------------- aeroplane

    # The aeroplane in its own frame, in metres: x out the right wing,
    # y out the nose, z up through the canopy. Drawn back to front, so
    # the wing lies over the fuselage the way it looks from up here.
    WING = ((-5.6, -1.6, 0.0), (0.0, 1.4, 0.0), (5.6, -1.6, 0.0),
            (5.6, -2.9, 0.0), (0.0, -1.7, 0.0), (-5.6, -2.9, 0.0))
    BODY = ((-0.8, -4.1, 0.0), (0.8, -4.1, 0.0),
            (0.8, 3.6, 0.0), (0.0, 4.7, 0.0), (-0.8, 3.6, 0.0))
    TAILPLANE = ((-2.3, -4.3, 0.0), (2.3, -4.3, 0.0),
                 (2.3, -3.3, 0.0), (-2.3, -3.3, 0.0))
    FIN = ((0.0, -4.2, 0.0), (0.0, -2.6, 0.0),
           (0.0, -3.1, 2.1), (0.0, -4.2, 1.7))
    CANOPY = ((-0.5, 0.9, 0.35), (0.5, 0.9, 0.35),
              (0.5, 2.6, 0.35), (-0.5, 2.6, 0.35))
    NOSE = ((-0.7, 3.4, 0.0), (0.7, 3.4, 0.0), (0.0, 4.7, 0.0))

    def _plane_shapes(self, p, cam, shadow):
        """The aeroplane, or the shadow it casts on the ground below it.

        Both are the same shape put through the same projection, one at
        height and one flattened on to the grass. That is where altitude
        shows: the higher it flies the further back the shadow slides.

        The aeroplane is drawn last of everything and outside the scene
        rotation, so the share of the bank the camera did not take up is
        the bank you see in the wings.
        """
        pos = self.world_xy()
        if pos is None:
            return
        ax, ay = pos
        az = drawn_altitude(self.altitude)
        if shadow and az <= ALT_FLOOR_M * 0.6:
            return
        cr, sr = math.cos(self.roll), math.sin(self.roll)

        def world(pt):
            x, y, z = (v * PLANE_SCALE for v in pt)
            xr = x * cr + z * sr
            zr = -x * sr + z * cr
            return (ax + cam.rx * xr + cam.fx * y,
                    ay + cam.ry * xr + cam.fy * y,
                    0.05 if shadow else az + zr)

        if shadow:
            parts = ((self.WING, SHADOW), (self.BODY, SHADOW),
                     (self.TAILPLANE, SHADOW))
        else:
            parts = ((self.BODY, PLANE), (self.FIN, PLANE_MID),
                     (self.WING, PLANE), (self.TAILPLANE, PLANE),
                     (self.NOSE, PLANE_DK), (self.CANOPY, PLANE_DK))

        p.setPen(Qt.NoPen)
        for shape, colour in parts:
            cpts = [cam.cam(*world(pt)) for pt in shape]
            if min(c[2] for c in cpts) < NEAR_M:
                cpts = _clip_near(cpts)
            if len(cpts) < 3:
                continue
            p.setBrush(QBrush(colour))
            p.drawPolygon(QPolygonF([cam.screen(c) for c in cpts]))

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
