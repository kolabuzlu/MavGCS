"""
A minimal artificial horizon (attitude indicator), drawn by hand with
QPainter. No image assets needed.

The trick behind every AI (attitude indicator) widget:
  - Draw a big sky/ground rectangle pair, offset vertically by pitch,
    then rotate the whole thing by -roll around the center.
  - Draw the little yellow aircraft symbol and roll pointer un-rotated,
    on top, since those stay fixed relative to the pilot's eyes.
"""

import math
import sys

from PySide6.QtWidgets import QWidget, QComboBox
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF, QFont
from PySide6.QtCore import Qt, QPointF, QRect, QRectF

MACOS = sys.platform == "darwin"

# Ask macOS for tabular figures on this instrument's labels.
#
# Every label here is QFont("Sans", ...). "Sans" is a fontconfig alias.
# Windows resolves it to something and keeps doing so, untouched. macOS
# has no family behind it - it is not in QFontDatabase.families() - and
# falls through to .AppleSystemUIFont, which is proportional: its digits
# are 5, 6 or 7 pixels wide depending on which digit it is.
#
# So every number on this widget changed width as its value changed. At
# the 11pt bold of the airspeed and altitude readouts, "120" measured
# 19px against "888" at 22px - which means those two, the heading and the
# battery figures all shifted sideways while they updated, several times
# a second, on the instrument being watched while flying.
#
# The fix is not a different family. That face is the one that belongs
# here; naming a real one instead changed how the whole instrument looked
# in order to fix how its numbers moved. "tnum" is the OpenType feature
# for tabular figures, which the digits in that font already have -
# asking for it leaves the typeface exactly as it was and gives every
# digit one advance. Measured at all five sizes this widget uses, 7 to 11
# bold: the per-digit widths collapse to a single value and "120" and
# "888" come out identical.
#
# It also buys a little room rather than costing it, which is the
# opposite of what it looks like it should do. A number can no longer be
# made of the widest glyphs, so the widest possible reading gets
# narrower: at 380x200, the tightest this widget is drawn, "8888.8" goes
# from 43px to 41px inside a 48px box. Clearance at the worst case
# improves from 5px to 7px.
#
# Nothing is asked for where the question does not arise: Windows gets
# the QFont it always got, and a Qt too old to know about font features
# keeps today's behaviour rather than failing inside paintEvent.
HUD_TABULAR_DIGITS = MACOS and hasattr(QFont, "Tag")


def _hud_font(*args):
    """The font for a label on this instrument, as QFont takes it."""
    font = QFont(*args)
    if HUD_TABULAR_DIGITS:
        font.setFeature(QFont.Tag("tnum"), 1)
    return font


class ArtificialHorizon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll = 0.0   # radians
        self.pitch = 0.0  # radians
        self.airspeed = None   # m/s, None until first update
        self.throttle = None   # percent, None until first update
        self.altitude = None   # m, None until first update
        self.climb = None      # m/s, positive up, None until first update
        self.heading = None    # degrees, None until first update
        self.wind_dir = None   # degrees (direction wind is coming FROM), None until first update
        self.wind_speed = None  # m/s, None until first update
        self.battery_voltage = None  # total pack voltage (V), None until first update
        self.battery_amps = None     # current being drawn (A)
        self.battery_mah = None      # taken out of the pack so far
        self.cell_count = 4  # default guess: 4S is a common pack size
        # When True the sky/ground fill is skipped so this widget can be
        # rendered as a transparent overlay on the 3D FPV view.
        self.overlay_mode = False
        self.lat = None  # deg, None until first GLOBAL_POSITION_INT
        self.lon = None  # deg, None until first GLOBAL_POSITION_INT
        self.ekf_color = "white"   # "white" | "yellow" | "red"
        self.vibe_color = "white"  # "white" | "yellow" | "red"
        self.setMinimumSize(220, 220)

        # A combo box has to be a real interactive widget, not something
        # drawn in paintEvent - positioned to sit inside the battery box
        # that IS painted there (see _battery_box_rect / resizeEvent).
        self.cell_selector = QComboBox(self)
        self.cell_selector.addItems(["3S", "4S", "6S"])
        self.cell_selector.setCurrentText("4S")
        self.cell_selector.setStyleSheet(
            "QComboBox { background-color: #0f0f0f; color: white; "
            "font-size: 9px; border: 1px solid white; padding: 1px 2px; }"
            "QComboBox QAbstractItemView { background-color: #0f0f0f; color: white; }"
        )
        self.cell_selector.currentTextChanged.connect(self._on_cell_count_changed)
        self._position_battery_widgets()

    def _on_cell_count_changed(self, text):
        try:
            self.cell_count = int(text.rstrip("Ss"))
        except ValueError:
            self.cell_count = 4
        self.update()

    # Exposed so anything overlaid on the HUD can keep clear of the battery
    # box rather than guessing at where it sits.
    # The wind readout, top left. Named because the throttle bar has to
    # know where it ends.
    WIND_BOX_W = 92
    WIND_BOX_H = 40
    WIND_BOX_MARGIN = 6

    # Over the 3D view the bar is drawn a tenth smaller, and nothing
    # else about it changes: same place, same alignment with the airspeed
    # box, so switching views does not shuffle the instruments about.
    #
    # It can stay put because Cesium's credit is now 14px rather than 24.
    # Its container tops out 47px above the view's lower edge, measured in
    # the running page, and the caption ends clear of that. Enlarge the
    # logo again and that clearance is what gets eaten.
    FPV_CREDIT_H = 50.0          # what the credit occupies, for the check
    FPV_BAR_SCALE = 0.9

    # The throttle caption's plinth is centred on the bar and is wider
    # than it, so at "100%" it reached 2.8px PAST the widget's left edge
    # and was clipped. The whole left group - bar, caption, airspeed box -
    # sits this far in, which leaves the widest caption a 4px gap. Moving
    # the group keeps the caption centred under the bar it belongs to;
    # nudging the caption alone would have left it visibly off-centre.
    LEFT_GROUP_MARGIN = 13.0
    # The mirror of it, and it exists for the same reason: the vertical
    # speed caption is centred on a bar narrower than the caption, so
    # without room beyond the bar it would clip against the right edge
    # exactly as the throttle's did against the left. The altitude box
    # moves inboard with it, the way the airspeed box does on the left.
    # It replaced a plain 6px margin, which was all the altitude box
    # needed when nothing sat outboard of it.
    RIGHT_GROUP_MARGIN = 13.0

    # Full deflection, up or down. Ten covers what an aeroplane does:
    # a brisk climb is 3 to 5, and anything past ten is a dive or a
    # problem, both of which read as "hard over" and neither of which
    # needs a number to act on.
    VSI_FULL_SCALE_MPS = 10.0
    # What the bar leaves between itself and the battery box above it.
    VSI_BATTERY_GAP = 4.0
    # Below this there is no bar worth drawing, only a smear. A HUD this
    # short has bigger problems than its vertical speed readout.
    VSI_MIN_H = 24.0

    BATTERY_BOX_W = 104
    # Three rows now: pack voltage, per-cell with the S selector beside
    # it, and what is being drawn and what has been used.
    BATTERY_BOX_H = 60
    BATTERY_MARGIN = 6
    # Where each row sits inside the box, from its top edge.
    BATTERY_ROW2_Y = 22
    BATTERY_ROW3_Y = 40

    TAPE_TOP = 4
    TAPE_H = 26

    @classmethod
    def heading_tape_rect_for(cls, w, h):
        """Where the heading tape lands in a widget of this size.

        Taken as w/h rather than read off the instance so a sibling widget
        can work out the layout before its own children have been resized.
        """
        tape_w = min(min(w, h) * 0.85, w * 0.50)
        return QRectF(w / 2.0 - tape_w / 2, cls.TAPE_TOP, tape_w, cls.TAPE_H)

    @classmethod
    def top_gap_center_x(cls, w, h):
        """Midpoint of the empty strip along the top of the HUD, between the
        heading tape and the battery box - the one place up there where an
        overlay can sit without covering an instrument."""
        tape_right = cls.heading_tape_rect_for(w, h).right()
        battery_left = w - cls.BATTERY_MARGIN - cls.BATTERY_BOX_W
        return (tape_right + battery_left) / 2.0

    @classmethod
    def battery_box_rect_for(cls, w, h):
        return QRectF(w - cls.BATTERY_MARGIN - cls.BATTERY_BOX_W,
                      cls.BATTERY_MARGIN, cls.BATTERY_BOX_W, cls.BATTERY_BOX_H)

    @classmethod
    def cell_selector_rect_for(cls, w, h):
        """Where the cell-count combo belongs, inside the battery box.

        Public, and taken as a size rather than read off the instance,
        because the FPV view floats the real combo over its scene: there the
        HUD is a flat image, so a combo drawn into it would look right and
        click through to nothing.
        """
        rect = cls.battery_box_rect_for(w, h)
        combo_w, combo_h = 44, 16
        # Measured from the top, not the bottom: it belongs beside the
        # per-cell figure, and anchoring it to the bottom moved it onto
        # the current row the moment the box grew a third one.
        return QRect(int(rect.right() - 6 - combo_w),
                     int(rect.top() + cls.BATTERY_ROW2_Y), combo_w, combo_h)

    def _battery_box_rect(self):
        return self.battery_box_rect_for(self.width(), self.height())

    def _position_battery_widgets(self):
        # While the FPV view has borrowed the combo it owns its geometry;
        # the coordinates happen to be identical, but moving another
        # widget's child from here would be wrong the moment they aren't.
        if self.cell_selector.parent() is self:
            self.cell_selector.setGeometry(
                self.cell_selector_rect_for(self.width(), self.height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_battery_widgets()

    def set_battery_voltage(self, voltage):
        self.battery_voltage = voltage

    def set_battery_power(self, amps, consumed_mah):
        """What the pack is giving right now, and what has gone from it."""
        self.battery_amps = amps
        self.battery_mah = consumed_mah
        self.update()
        self.update()

    def set_attitude(self, roll, pitch, yaw=0.0):
        self.roll = roll
        self.pitch = pitch
        self.update()  # schedules a repaint

    def set_airspeed(self, airspeed):
        self.airspeed = airspeed
        self.update()

    def set_throttle(self, percent):
        """Throttle from VFR_HUD, as a percentage."""
        self.throttle = percent
        self.update()

    def set_altitude(self, altitude):
        self.altitude = altitude
        self.update()

    def set_climb(self, climb_mps):
        """Vertical speed from VFR_HUD, in m/s, positive up."""
        self.climb = climb_mps
        self.update()

    def set_heading(self, heading_deg):
        self.heading = heading_deg % 360
        self.update()

    def set_position(self, lat, lon):
        self.lat = lat
        self.lon = lon
        self.update()

    def set_ekf_status(self, color_name):
        self.ekf_color = color_name
        self.update()

    def set_vibe_status(self, color_name):
        self.vibe_color = color_name
        self.update()

    def set_wind(self, direction_deg, speed_mps):
        self.wind_dir = direction_deg % 360 if direction_deg is not None else None
        self.wind_speed = speed_mps
        self.update()

    def _draw_throttle(self, painter, rect, scale=1.0):
        """A vertical throttle bar, filling from the bottom.

        Drawn even with no reading yet, so the airspeed box does not
        appear to shift sideways when the first telemetry arrives.
        """
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawRect(rect)

        if self.throttle is not None:
            pct = max(0.0, min(100.0, float(self.throttle)))
            filled = rect.height() * pct / 100.0
            if filled > 0:
                # Green through most of the range, amber high up: near the
                # stops the autopilot has little left to give, which is
                # worth seeing without reading the number.
                colour = QColor(120, 200, 120) if pct <= 85 else QColor(255, 167, 38)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(colour))
                painter.drawRect(QRectF(rect.x() + 1,
                                        rect.bottom() - filled + 1,
                                        rect.width() - 2, filled - 2))

        # Quarter marks, so the eye can read a level without a scale.
        painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for frac in (0.25, 0.5, 0.75):
            y = rect.bottom() - rect.height() * frac
            painter.drawLine(QPointF(rect.x(), y),
                             QPointF(rect.x() + rect.width() * 0.45, y))

        # Labelled like the other readouts, which say IAS m/s and ALT m
        # rather than leaving the reader to infer the unit, and on the
        # same black ground they use - over the 3D view the terrain
        # underneath can be any brightness, and white on pale ground was
        # hard to read. Sized to the text so the plinth is no wider than
        # it needs to be.
        font = _hud_font("Sans")
        font.setPointSizeF(7.0 * scale)
        painter.setFont(font)
        text = f"{self.throttle:.0f}%" if self.throttle is not None else "--"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text) + 6.0
        th = fm.height() + 2.0
        # Centred under the bar, but never allowed to touch the frame.
        # How wide the text comes out depends on the font the platform
        # picks and the display's DPI - the same string measured 6px wider
        # under one Qt platform than another - so the group's margin alone
        # cannot guarantee the clearance. This does.
        left = max(rect.center().x() - tw / 2.0, 3.0)
        label = QRectF(left, rect.bottom() + 2, tw, th)
        painter.setPen(Qt.NoPen)
        # The wind readout's black rather than the boxes' lighter one:
        # 170 alpha over snow or pale sand still comes out at luminance
        # 80-odd, which is not enough behind small white text. This is
        # the ground the HUD already uses where text must carry.
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(label)
        painter.setPen(QPen(Qt.white))
        painter.drawText(label, Qt.AlignCenter, text)

    def _draw_vsi(self, painter, rect, scale=1.0):
        """A vertical speed bar, filling from the middle.

        Up for climb, down for descent, hard over at VSI_FULL_SCALE_MPS
        either way. Drawn even with no reading yet, so the altitude box
        does not appear to shift sideways when the first telemetry
        arrives - the same reason the throttle bar is.

        Yellow because this is the one bar on the HUD that reads in two
        directions, and the eye has to find which way before it reads how
        far. Green and amber already mean "how much" on the throttle,
        and red means a hazard elsewhere here; yellow is unclaimed and
        carries against both sky and ground.
        """
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawRect(rect)

        # Snapped to a pixel boundary. The centre of a bar with an odd
        # height falls on a half pixel, and then the two directions round
        # apart - full climb drew 56 rows against full descent's 57 - and
        # the zero line below renders as two half-lit rows instead of one
        # line. Half a pixel of placement buys a symmetrical bar and a
        # crisp zero.
        mid = float(round(rect.center().y()))
        half = rect.height() / 2.0
        if self.climb is not None:
            rate = max(-self.VSI_FULL_SCALE_MPS,
                       min(self.VSI_FULL_SCALE_MPS, float(self.climb)))
            filled = half * abs(rate) / self.VSI_FULL_SCALE_MPS
            # Below a pixel there is nothing to draw and a rectangle of
            # height 0.4 renders as a smear rather than a reading.
            if filled >= 1.0:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(255, 214, 0)))
                top = mid - filled if rate > 0 else mid
                painter.drawRect(QRectF(rect.x() + 1, top,
                                        rect.width() - 2, filled))

        # Half-scale marks, at five up and five down.
        painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for y in (mid - half * 0.5, mid + half * 0.5):
            painter.drawLine(QPointF(rect.x(), y),
                             QPointF(rect.x() + rect.width() * 0.45, y))
        # Zero, drawn after the fill so it is never buried by it. Without
        # it the bar says how fast and not which way, which is the only
        # thing a vertical speed indicator is for.
        painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
        painter.drawLine(QPointF(rect.x(), mid), QPointF(rect.right(), mid))

        # Captioned like the throttle bar, on the same black plinth, and
        # clamped off the right edge for the reason RIGHT_GROUP_MARGIN
        # exists: the text is wider than the bar and how much wider
        # depends on the platform's font.
        font = _hud_font("Sans")
        font.setPointSizeF(7.0 * scale)
        painter.setFont(font)
        text = f"{self.climb:+.1f}" if self.climb is not None else "--"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text) + 6.0
        th = fm.height() + 2.0
        left = min(max(rect.center().x() - tw / 2.0, 3.0),
                   self.width() - tw - 3.0)
        label = QRectF(left, rect.bottom() + 2, tw, th)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(label)
        painter.setPen(QPen(Qt.white))
        painter.drawText(label, Qt.AlignCenter, text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        size = min(w, h)

        painter.save()
        painter.translate(cx, cy)
        painter.rotate(-math.degrees(self.roll))

        pixels_per_deg = size / 90.0
        offset = math.degrees(self.pitch) * pixels_per_deg
        big = size * 2

        # Sky and ground. Skipped in overlay mode, where this same widget is
        # drawn on top of the 3D FPV scene - there the terrain itself is the
        # sky and ground, and filling them would simply hide it. Every other
        # element is painted exactly as usual, so the two views carry an
        # identical HUD rather than two drifting copies of one.
        if not self.overlay_mode:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(70, 130, 220)))
            painter.drawRect(QRectF(-big, -big + offset, 2 * big, big))

            painter.setBrush(QBrush(QColor(120, 80, 40)))
            painter.drawRect(QRectF(-big, offset, 2 * big, big))

        # Horizon line
        painter.setPen(QPen(Qt.white, 2))
        painter.drawLine(QPointF(-big, offset), QPointF(big, offset))

        # Pitch ladder
        painter.setFont(_hud_font("Sans", 8))
        for deg in range(-90, 91, 10):
            if deg == 0:
                continue
            y = offset - deg * pixels_per_deg
            line_w = size * 0.15 if deg % 30 == 0 else size * 0.08
            painter.setPen(QPen(Qt.white, 1))
            painter.drawLine(QPointF(-line_w, y), QPointF(line_w, y))
            painter.drawText(QPointF(line_w + 4, y + 4), str(deg))

        painter.restore()

        # Roll scale arc (fixed) + roll pointer (rotates) - drawn before
        # the heading tape now, so the tape paints on top of it instead of
        # the arc's line showing through the tape.
        radius = size * 0.38
        painter.setPen(QPen(Qt.white, 2))
        painter.drawArc(
            QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius),
            30 * 16, 120 * 16,
        )
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(-math.degrees(self.roll))
        tri = QPolygonF(
            [QPointF(0, -radius + 2), QPointF(-6, -radius + 14), QPointF(6, -radius + 14)]
        )
        painter.setBrush(QBrush(Qt.white))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(tri)
        painter.restore()

        # Heading tape - top center, fixed (doesn't rotate/translate with
        # roll or pitch, like the airspeed/altitude boxes). Drawn with a
        # solidly opaque background so it cleanly layers on top of the
        # roll arc/pitch ladder underneath, same as a real PFD's compass
        # strip sitting above the attitude ball.
        heading = self.heading if self.heading is not None else 0.0
        tape_rect = self.heading_tape_rect_for(w, h)
        tape_w, tape_h = tape_rect.width(), tape_rect.height()

        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 220)))
        painter.drawRect(tape_rect)

        painter.setClipRect(tape_rect)
        pixels_per_deg = tape_w / 60.0  # shows +/-30 deg around current heading
        cardinal = {0: "N", 90: "E", 180: "S", 270: "W"}
        painter.setFont(_hud_font("Sans", 8, QFont.Bold))
        for delta in range(-30, 31):
            deg = round(heading + delta) % 360
            if deg % 10 != 0:
                continue
            x = cx + delta * pixels_per_deg
            major = deg % 30 == 0
            tick_h = 9 if major else 5
            painter.setPen(QPen(Qt.white, 1))
            painter.drawLine(
                QPointF(x, tape_rect.bottom() - tick_h), QPointF(x, tape_rect.bottom())
            )
            if major:
                label = cardinal.get(deg, f"{deg:03d}")
                painter.drawText(QRectF(x - 15, tape_rect.top() + 1, 30, 14), Qt.AlignCenter, label)
        painter.setClipping(False)

        # Digital heading readout embedded IN the tape itself (standard PFD
        # layout) rather than as a separate box below it - a floating box
        # below the tape sits at almost the same height as the roll arc's
        # top / pitch-ladder labels and collided with them.
        readout_w = 42
        readout_rect = QRectF(cx - readout_w / 2, tape_rect.top() + 1, readout_w, tape_h - 2)
        painter.setPen(QPen(Qt.yellow, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 255)))
        painter.drawRect(readout_rect)
        painter.setPen(QPen(Qt.yellow))
        painter.setFont(_hud_font("Sans", 10, QFont.Bold))
        text = f"{int(round(heading)):03d}" if self.heading is not None else "---"
        painter.drawText(readout_rect, Qt.AlignCenter, text)

        # Small pointer triangle just below the tape, pointing up into it -
        # stays clear of the roll arc since it's only ~7px tall.
        pointer = QPolygonF([
            QPointF(cx, tape_rect.bottom() + 7),
            QPointF(cx - 6, tape_rect.bottom()),
            QPointF(cx + 6, tape_rect.bottom()),
        ])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(Qt.yellow))
        painter.drawPolygon(pointer)

        # Wind indicator - top left corner, fixed. Shows direction as a
        # rotating arrow (pointing toward where the wind is coming FROM,
        # matching the WIND message's own convention) plus numeric
        # direction and speed.
        wind_box_w = self.WIND_BOX_W
        wind_box_h = self.WIND_BOX_H
        wind_rect = QRectF(self.WIND_BOX_MARGIN, self.WIND_BOX_MARGIN,
                           wind_box_w, wind_box_h)
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(wind_rect)

        arrow_cx = wind_rect.left() + 18
        arrow_cy = wind_rect.top() + wind_box_h / 2
        painter.save()
        painter.translate(arrow_cx, arrow_cy)
        if self.wind_dir is not None:
            # Arrow shows the direction the wind is blowing TOWARD, relative
            # to the nose (up = straight ahead of the aircraft):
            #   heading-relative "blows toward" bearing = wind source + 180,
            #   then rotated into the aircraft's frame by subtracting heading.
            # A headwind (wind source dead ahead) blows toward the tail ->
            # points down. Wind from the left blows toward the right ->
            # points right.
            heading_ref = self.heading if self.heading is not None else 0.0
            relative_angle = (self.wind_dir - heading_ref + 180) % 360
            painter.rotate(relative_angle)
        painter.setPen(QPen(QColor(120, 220, 255), 2))
        painter.drawLine(QPointF(0, 11), QPointF(0, -11))
        arrow_tip = QPolygonF([QPointF(0, -11), QPointF(-4, -3), QPointF(4, -3)])
        painter.setBrush(QBrush(QColor(120, 220, 255)))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(arrow_tip)
        painter.restore()

        painter.setPen(QPen(Qt.white))
        painter.setFont(_hud_font("Sans", 8, QFont.Bold))
        dir_text = f"{int(round(self.wind_dir)):03d}\u00b0" if self.wind_dir is not None else "---\u00b0"
        painter.drawText(
            QRectF(wind_rect.left() + 32, wind_rect.top() + 4, wind_box_w - 36, 16),
            Qt.AlignVCenter | Qt.AlignLeft, dir_text,
        )
        painter.setFont(_hud_font("Sans", 8))
        speed_text = f"{self.wind_speed * 3.6:.1f} kph" if self.wind_speed is not None else "-- kph"
        painter.drawText(
            QRectF(wind_rect.left() + 32, wind_rect.top() + 20, wind_box_w - 36, 16),
            Qt.AlignVCenter | Qt.AlignLeft, speed_text,
        )

        # Battery indicator - top right corner, fixed. Total pack voltage
        # on top, per-cell voltage below (computed from the S-count
        # selector, since MAVLink only reports total voltage - it has no
        # concept of cell count). The selector itself is a real QComboBox
        # child widget (see _position_battery_widgets), not painted here.
        batt_rect = self._battery_box_rect()
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(batt_rect)

        painter.setPen(QPen(Qt.white))
        painter.setFont(_hud_font("Sans", 10, QFont.Bold))
        total_text = f"{self.battery_voltage:.2f} V" if self.battery_voltage is not None else "-- V"
        painter.drawText(
            QRectF(batt_rect.left() + 6, batt_rect.top() + 3, batt_rect.width() - 12, 18),
            Qt.AlignVCenter | Qt.AlignLeft, total_text,
        )

        painter.setFont(_hud_font("Sans", 8))
        if self.battery_voltage is not None and self.cell_count:
            cell_text = f"{self.battery_voltage / self.cell_count:.2f} V/c"
        else:
            cell_text = "-- V/c"
        # Leave room on the right for the cell_selector combo box that
        # sits over this same row.
        painter.drawText(
            QRectF(batt_rect.left() + 6,
                   batt_rect.top() + self.BATTERY_ROW2_Y,
                   batt_rect.width() - 58, 16),
            Qt.AlignVCenter | Qt.AlignLeft, cell_text,
        )

        # Third row: what is being drawn, and what has been taken out.
        # Current on the left and consumed on the right, so neither has to
        # be read past the other as the figures change width.
        #
        # A point smaller than the row above it, because two labelled
        # figures have to share a box sized for one. At 8pt the widest
        # pair a real aircraft produces - a negative current beside a
        # five-figure consumption - leaves two pixels between them; at
        # 7pt it leaves seventeen.
        painter.setFont(_hud_font("Sans", 7))
        amps_text = ("%.1f A" % self.battery_amps
                     if self.battery_amps is not None else "-- A")
        mah_text = ("%.0f mAh" % self.battery_mah
                    if self.battery_mah is not None else "-- mAh")
        row3 = QRectF(batt_rect.left() + 6,
                      batt_rect.top() + self.BATTERY_ROW3_Y,
                      batt_rect.width() - 12, 16)
        painter.drawText(row3, Qt.AlignVCenter | Qt.AlignLeft, amps_text)
        painter.drawText(row3, Qt.AlignVCenter | Qt.AlignRight, mah_text)

        # Fixed aircraft symbol (always horizontal, always centered)
        painter.setPen(QPen(Qt.yellow, 3))
        painter.drawLine(QPointF(cx - size * 0.2, cy), QPointF(cx - size * 0.05, cy))
        painter.drawLine(QPointF(cx + size * 0.05, cy), QPointF(cx + size * 0.2, cy))
        painter.setBrush(QBrush(Qt.yellow))
        painter.drawEllipse(QPointF(cx, cy), 3, 3)

        # Airspeed (left) and altitude (right) readout boxes, PFD-style.
        # These are drawn un-rotated/un-translated by roll or pitch - they
        # stay fixed relative to the viewer, like a real HUD tape. Anchored
        # to the widget's actual edges (not the horizon circle radius) so
        # they stay on-screen regardless of the widget's aspect ratio.
        margin = 6
        box_w = min(size * 0.24, w * 0.32)
        box_h = size * 0.16

        # Throttle bar, outboard of the airspeed box. Narrow on purpose:
        # the number matters less than seeing at a glance how much power
        # is in, and how near the stops it is.
        bar_w = max(7.0, min(12.0, w * 0.02))
        bar_gap = 4.0
        bar_h = min(box_h * 2.4, h - 2 * margin - 14)
        # Same centreline in both views; only the size differs.
        scale = self.FPV_BAR_SCALE if self.overlay_mode else 1.0
        bar_rect = QRectF(self.LEFT_GROUP_MARGIN, cy - bar_h * scale / 2.0,
                          bar_w * scale, bar_h * scale)
        self._draw_throttle(painter, bar_rect, scale)

        painter.setFont(_hud_font("Sans", 11, QFont.Bold))

        # Airspeed box - middle left, moved inboard to clear the bar.
        # Offset by the bar's UNSCALED width, so shrinking the bar in the
        # 3D overlay does not drag the airspeed box sideways with it.
        airspeed_rect = QRectF(self.LEFT_GROUP_MARGIN + bar_w + bar_gap,
                               cy - box_h / 2, box_w, box_h)
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawRect(airspeed_rect)
        painter.setPen(QPen(Qt.white))
        text = f"{self.airspeed:.1f}" if self.airspeed is not None else "--"
        painter.drawText(airspeed_rect, Qt.AlignCenter, text)
        painter.setFont(_hud_font("Sans", 7))
        painter.drawText(
            QRectF(airspeed_rect.x(), airspeed_rect.bottom() + 2, box_w, 14),
            Qt.AlignHCenter, "IAS m/s",
        )
        # Pointer tab connecting the box to the horizon centerline
        tab = QPolygonF([
            QPointF(airspeed_rect.right(), cy - 8),
            QPointF(airspeed_rect.right() + 8, cy),
            QPointF(airspeed_rect.right(), cy + 8),
        ])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawPolygon(tab)
        painter.setPen(QPen(Qt.white, 1))
        painter.drawLine(tab.at(0), tab.at(1))
        painter.drawLine(tab.at(1), tab.at(2))

        # Vertical speed, outboard of the altitude box: the mirror of the
        # throttle bar on the left, same width and height, so the two
        # sides of the HUD weigh the same.
        #
        # Centred on the horizon like the throttle bar is, then pushed
        # down if that would put it inside the battery box. It has to be
        # pushed and the throttle does not, because the two are not
        # mirror images at the top: the battery box is 60px tall where
        # the wind readout opposite it is 40, so this bar runs into its
        # neighbour on a HUD that leaves the other one clear. Measured,
        # it starts overlapping below about 230px of height and is 40px
        # into the box by the macOS floor of 84.
        #
        # Moved rather than shortened, so the bar reads the same length
        # at every size it can; it is only shortened when moving it down
        # would push its foot through the caption at the bottom.
        # Full size in both views, where the throttle bar shrinks by a
        # tenth over the 3D scene. That is not an oversight in the
        # mirror: the throttle gives up those pixels to clear Cesium's
        # logo in the bottom left corner, and there is no logo under
        # this one - the credit text opposite it was moved inboard of
        # the bar's group instead. Vertical speed is also the reading
        # that matters most in the view where the pilot is looking out
        # rather than at the numbers, so it is the last thing to shrink.
        vsi_h = bar_h
        vsi_top = cy - vsi_h / 2.0
        clear_of_battery = (self.battery_box_rect_for(w, h).bottom()
                            + self.VSI_BATTERY_GAP)
        if vsi_top < clear_of_battery:
            vsi_top = clear_of_battery
        # What is left between the battery box and the caption's line.
        # On a HUD short enough - the macOS floor of 84px is one - there
        # is no room for a bar at all once the box is cleared, and a stub
        # clipped off by the bottom edge would read as a reading rather
        # than as no room. Nothing is drawn instead. The altitude box
        # stays inboard either way, so it does not jump sideways as the
        # column is resized past the threshold.
        vsi_h = min(vsi_h, h - margin - 14.0 - vsi_top)
        # Whole pixels, and an even number of them. This is the only bar
        # on the HUD that fills from its middle, so its middle has to be
        # a pixel: at an odd height the two directions round apart and
        # full climb drew 56 rows where full descent drew 57 - the same
        # reading, a different length, depending on its sign.
        vsi_top = float(round(vsi_top))
        vsi_h = float(int(vsi_h) // 2 * 2)
        if vsi_h >= self.VSI_MIN_H:
            vsi_rect = QRectF(w - self.RIGHT_GROUP_MARGIN - bar_w,
                              vsi_top, bar_w, vsi_h)
            # 1.0, not scale: the caption is sized with the bar it
            # belongs to, and that bar does not shrink here.
            self._draw_vsi(painter, vsi_rect, 1.0)

        # Altitude box - middle right, moved inboard to clear the bar.
        # Offset by the bar's UNSCALED width, so shrinking the bar in the
        # 3D overlay does not drag the altitude box sideways with it -
        # the same rule the airspeed box follows on the left.
        painter.setFont(_hud_font("Sans", 11, QFont.Bold))
        altitude_rect = QRectF(
            w - self.RIGHT_GROUP_MARGIN - bar_w - bar_gap - box_w,
            cy - box_h / 2, box_w, box_h)
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawRect(altitude_rect)
        painter.setPen(QPen(Qt.white))
        text = f"{self.altitude:.1f}" if self.altitude is not None else "--"
        painter.drawText(altitude_rect, Qt.AlignCenter, text)
        painter.setFont(_hud_font("Sans", 7))
        painter.drawText(
            QRectF(altitude_rect.x(), altitude_rect.bottom() + 2, box_w, 14),
            Qt.AlignHCenter, "ALT m",
        )
        tab = QPolygonF([
            QPointF(altitude_rect.left(), cy - 8),
            QPointF(altitude_rect.left() - 8, cy),
            QPointF(altitude_rect.left(), cy + 8),
        ])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawPolygon(tab)
        painter.setPen(QPen(Qt.white, 1))
        painter.drawLine(tab.at(0), tab.at(1))
        painter.drawLine(tab.at(1), tab.at(2))

        # Lat/lon readout - bottom corners, fixed (same style as the wind/
        # battery boxes). Latitude bottom-left, longitude bottom-right.
        latlon_box_w = 108
        latlon_box_h = 18
        latlon_margin = 6

        lat_rect = QRectF(
            latlon_margin, h - latlon_margin - latlon_box_h, latlon_box_w, latlon_box_h
        )
        lon_rect = QRectF(
            w - latlon_margin - latlon_box_w, h - latlon_margin - latlon_box_h,
            latlon_box_w, latlon_box_h,
        )
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(lat_rect)
        painter.drawRect(lon_rect)

        painter.setPen(QPen(Qt.white))
        painter.setFont(_hud_font("Sans", 8, QFont.Bold))
        lat_text = f"LAT {self.lat:.6f}" if self.lat is not None else "LAT --"
        lon_text = f"LON {self.lon:.6f}" if self.lon is not None else "LON --"
        painter.drawText(lat_rect, Qt.AlignCenter, lat_text)
        painter.drawText(lon_rect, Qt.AlignCenter, lon_text)

        # EKF/Vibe status (Mission Planner HUD convention): bottom middle,
        # EKF on the left, Vibe on the right - just the colored word itself,
        # no value, same as MP's own HUD.
        status_colors = {"white": Qt.white, "yellow": Qt.yellow, "red": QColor(255, 60, 60)}
        status_box_w = 48
        status_box_h = latlon_box_h
        status_gap = 4
        ekf_rect = QRectF(
            cx - status_gap / 2 - status_box_w, h - latlon_margin - status_box_h,
            status_box_w, status_box_h,
        )
        vibe_rect = QRectF(
            cx + status_gap / 2, h - latlon_margin - status_box_h,
            status_box_w, status_box_h,
        )
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(15, 15, 15, 210)))
        painter.drawRect(ekf_rect)
        painter.drawRect(vibe_rect)

        painter.setFont(_hud_font("Sans", 8, QFont.Bold))
        painter.setPen(QPen(status_colors.get(self.ekf_color, Qt.white)))
        painter.drawText(ekf_rect, Qt.AlignCenter, "EKF")
        painter.setPen(QPen(status_colors.get(self.vibe_color, Qt.white)))
        painter.drawText(vibe_rect, Qt.AlignCenter, "VIBE")

        painter.end()
