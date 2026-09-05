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
from PySide6.QtWidgets import QWidget, QComboBox
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF, QFont
from PySide6.QtCore import Qt, QPointF, QRect, QRectF


class ArtificialHorizon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll = 0.0   # radians
        self.pitch = 0.0  # radians
        self.airspeed = None   # m/s, None until first update
        self.throttle = None   # percent, None until first update
        self.altitude = None   # m, None until first update
        self.heading = None    # degrees, None until first update
        self.wind_dir = None   # degrees (direction wind is coming FROM), None until first update
        self.wind_speed = None  # m/s, None until first update
        self.battery_voltage = None  # total pack voltage (V), None until first update
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
    # The altitude box on the right keeps the original 6px margin.
    LEFT_GROUP_MARGIN = 13.0

    BATTERY_BOX_W = 104
    BATTERY_BOX_H = 44
    BATTERY_MARGIN = 6

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
        return QRect(int(rect.right() - 6 - combo_w),
                     int(rect.bottom() - 6 - combo_h), combo_w, combo_h)

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
        font = QFont("Sans")
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
        painter.setFont(QFont("Sans", 8))
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
        painter.setFont(QFont("Sans", 8, QFont.Bold))
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
        painter.setFont(QFont("Sans", 10, QFont.Bold))
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
        painter.setFont(QFont("Sans", 8, QFont.Bold))
        dir_text = f"{int(round(self.wind_dir)):03d}\u00b0" if self.wind_dir is not None else "---\u00b0"
        painter.drawText(
            QRectF(wind_rect.left() + 32, wind_rect.top() + 4, wind_box_w - 36, 16),
            Qt.AlignVCenter | Qt.AlignLeft, dir_text,
        )
        painter.setFont(QFont("Sans", 8))
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
        painter.setFont(QFont("Sans", 10, QFont.Bold))
        total_text = f"{self.battery_voltage:.2f} V" if self.battery_voltage is not None else "-- V"
        painter.drawText(
            QRectF(batt_rect.left() + 6, batt_rect.top() + 3, batt_rect.width() - 12, 18),
            Qt.AlignVCenter | Qt.AlignLeft, total_text,
        )

        painter.setFont(QFont("Sans", 8))
        if self.battery_voltage is not None and self.cell_count:
            cell_text = f"{self.battery_voltage / self.cell_count:.2f} V/c"
        else:
            cell_text = "-- V/c"
        # Leave room on the right for the cell_selector combo box that
        # sits over this same row.
        painter.drawText(
            QRectF(batt_rect.left() + 6, batt_rect.top() + 22, batt_rect.width() - 58, 16),
            Qt.AlignVCenter | Qt.AlignLeft, cell_text,
        )

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

        painter.setFont(QFont("Sans", 11, QFont.Bold))

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
        painter.setFont(QFont("Sans", 7))
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

        # Altitude box - middle right
        painter.setFont(QFont("Sans", 11, QFont.Bold))
        altitude_rect = QRectF(w - margin - box_w, cy - box_h / 2, box_w, box_h)
        painter.setPen(QPen(Qt.white, 1))
        painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
        painter.drawRect(altitude_rect)
        painter.setPen(QPen(Qt.white))
        text = f"{self.altitude:.1f}" if self.altitude is not None else "--"
        painter.drawText(altitude_rect, Qt.AlignCenter, text)
        painter.setFont(QFont("Sans", 7))
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
        painter.setFont(QFont("Sans", 8, QFont.Bold))
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

        painter.setFont(QFont("Sans", 8, QFont.Bold))
        painter.setPen(QPen(status_colors.get(self.ekf_color, Qt.white)))
        painter.drawText(ekf_rect, Qt.AlignCenter, "EKF")
        painter.setPen(QPen(status_colors.get(self.vibe_color, Qt.white)))
        painter.drawText(vibe_rect, Qt.AlignCenter, "VIBE")

        painter.end()
