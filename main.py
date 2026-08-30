"""
MavGCS - main entry point.

Usage - SITL (default, for testing without hardware):
    python main.py                          # udpin:0.0.0.0:14550
                                             # matches ArduPilot sim_vehicle.py's
                                             # default telemetry output port

Usage - real ELRS module later (just swap the connection string):
    python main.py udpout:192.168.4.1:14550 # your ELRS module's WiFi AP + mavlink port
    python main.py udpin:0.0.0.0:14550      # if your module instead connects TO you
    python main.py tcp:192.168.1.50:5760
    python main.py com3:57600               # Windows serial telemetry radio
    python main.py /dev/ttyUSB0:57600       # Linux/Mac serial telemetry radio

Nothing else in this app changes when you switch from SITL to the real
vehicle - same parsing, same widgets. Only this one string differs.
"""

# This is MavGCS V1.14.0 - per-waypoint altitudes you can edit on the map
# and send with Update, and a Fly To Lat/Lon button.
# See CHANGELOG.md.
APP_VERSION = "V1.14.0"

import sys
import os
import math
import html
import time
from datetime import datetime
from PySide6.QtCore import Signal, QTimer, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap, QPen, QColor
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, QSize
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QGridLayout, QFrame, QInputDialog,
    QPushButton, QGroupBox, QCheckBox, QMessageBox, QProgressBar,
    QScrollArea, QPlainTextEdit, QComboBox, QLineEdit, QStackedWidget,
    QDialog, QFormLayout, QDoubleSpinBox, QDialogButtonBox,
)

from mavlink_link import MavlinkLink, PLANE_MODES
from artificial_horizon import ArtificialHorizon
from map_view import MapView
import terrain_provider
from terrain_provider import TerrainRadarWorker
from adsb_provider import AdsbWorker
from tile_cache import TileCacheServer
from fpv_view import FpvView
from app_paths import data_dir, load_settings, resource_path, save_setting


class TelemetryPanel(QFrame):
    """4x4 dashboard grid: small label above a large value, all white
    text. Values are updated by key name, same pattern as before."""

    # Row-major order, 4 columns - matches the requested layout exactly.
    FIELDS = [
        ("airspeed", "AirSpeed (m/s)"),
        ("groundspeed", "GroundSpeed (m/s)"),
        ("vspeed_mps", "Vertical Speed (m/s)"),
        ("altitude", "Altitude (m)"),
        ("rangefinder_m", "Rangefinder (m)"),
        ("dist_home", "Dist to Home (m)"),
        ("wp_dist", "Dist to WP (m)"),
        ("sat_count", "Sat Count"),
        ("roll_deg", "Roll (deg)"),
        ("pitch_deg", "Pitch (deg)"),
        ("yaw_deg", "Yaw (deg)"),
        ("gps_hdop", "Gps HDOP"),
        ("wind_dir_deg", "Wind Direction (deg)"),
        ("wind_speed_mps", "Wind Velocity (kph)"),
        ("qnh", "QNH"),
        ("terrain_gl", "Terrain Alt (m)"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("background-color: #202225;")
        grid = QGridLayout(self)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        grid.setContentsMargins(8, 6, 8, 6)
        self.labels = {}
        for i, (key, text) in enumerate(self.FIELDS):
            row = (i // 4) * 2
            col = i % 4
            name_label = QLabel(text)
            name_label.setAlignment(Qt.AlignCenter)
            name_label.setStyleSheet("color: white; font-size: 9px;")
            value_label = QLabel("--")
            value_label.setAlignment(Qt.AlignCenter)
            value_label.setStyleSheet("color: white; font-size: 14px; font-weight: bold;")
            grid.addWidget(name_label, row, col)
            grid.addWidget(value_label, row + 1, col)
            self.labels[key] = value_label

    def set_value(self, key, value):
        if key in self.labels:
            self.labels[key].setText(str(value))


class FlyToDialog(QDialog):
    """Type a coordinate and send the vehicle there.

    The map already offers this by clicking, but a typed coordinate is what
    you want when someone reads you a position over the radio, or when the
    point is off the visible map.
    """

    def __init__(self, default_alt: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fly to Lat / Lon")
        form = QFormLayout(self)

        self.lat_edit = QLineEdit()
        self.lat_edit.setPlaceholderText("41.269549")
        self.lon_edit = QLineEdit()
        self.lon_edit.setPlaceholderText("36.364060")
        self.alt_spin = QDoubleSpinBox()
        self.alt_spin.setRange(0.0, 1000.0)
        self.alt_spin.setDecimals(0)
        self.alt_spin.setValue(default_alt)
        self.alt_spin.setSuffix(" m")

        form.addRow("Latitude", self.lat_edit)
        form.addRow("Longitude", self.lon_edit)
        form.addRow("Relative altitude", self.alt_spin)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e74c3c; font-size: 10px;")
        self.error_label.setWordWrap(True)
        form.addRow(self.error_label)

        buttons = QDialogButtonBox()
        self.fly_btn = buttons.addButton("Fly", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        # Validate here rather than on the accepted signal: a bad number
        # should keep the dialog open with the reason showing, not close it.
        self.fly_btn.clicked.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._values = None

    def _validate_and_accept(self):
        try:
            lat = float(self.lat_edit.text().strip().replace(",", "."))
            lon = float(self.lon_edit.text().strip().replace(",", "."))
        except ValueError:
            self.error_label.setText("Latitude and longitude must be decimal degrees.")
            return
        if not -90.0 <= lat <= 90.0:
            self.error_label.setText("Latitude must be between -90 and 90.")
            return
        if not -180.0 <= lon <= 180.0:
            self.error_label.setText("Longitude must be between -180 and 180.")
            return
        self._values = (lat, lon, float(self.alt_spin.value()))
        self.accept()

    def values(self):
        """(lat, lon, alt) once accepted, else None."""
        return self._values


class ModePanel(QGroupBox):
    """Row of flight-mode buttons; highlights whichever mode is currently
    active based on telemetry, so it also works as a mode indicator."""

    # Order matches the request: Manual, FBWA, Cruise, Loiter, AUTO, RTL,
    # Takeoff, Autoland, Autotune (lands directly below RTL in the
    # 3-column grid), Guided.
    MODE_ORDER = ["MANUAL", "FBWA", "CRUISE", "LOITER", "AUTO", "RTL", "TAKEOFF", "AUTOLAND", "AUTOTUNE", "GUIDED"]

    mode_requested = Signal(str)
    fly_to_requested = Signal()

    NORMAL_STYLE = "font-size: 10px; padding: 3px 4px;"
    ACTIVE_STYLE = "background-color: #2a6; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    RTL_STYLE = "background-color: #a33; color: white; font-size: 10px; padding: 3px 4px;"
    # Same weight of colour as RTL's red and the active-mode green, so it
    # reads as one of the panel's coloured controls rather than a sore thumb.
    FLY_TO_STYLE = "background-color: #36a; color: white; font-size: 10px; padding: 3px 4px;"

    def __init__(self, parent=None):
        super().__init__("Flight Mode", parent)
        grid = QGridLayout(self)
        grid.setSpacing(4)
        grid.setContentsMargins(6, 10, 6, 6)
        self.buttons = {}
        for i, name in enumerate(self.MODE_ORDER):
            btn = QPushButton(name)
            btn.setStyleSheet(self.RTL_STYLE if name == "RTL" else self.NORMAL_STYLE)
            btn.clicked.connect(lambda checked=False, n=name: self.mode_requested.emit(n))
            grid.addWidget(btn, i // 3, i % 3)
            self.buttons[name] = btn

        # Not a flight mode but the same kind of "go and do this" control,
        # and this is where the eye already is. Kept out of self.buttons so
        # set_active_mode never restyles it as though it were a mode.
        self.fly_to_btn = QPushButton("FLY TO LAT / LON")
        self.fly_to_btn.setStyleSheet(self.FLY_TO_STYLE)
        # White to match the button's own label - the source artwork is
        # black, which on this blue would read as a smudge.
        self.fly_to_btn.setIcon(globe_icon(12, gap=7))
        self.fly_to_btn.setIconSize(QSize(12 + 7, 12))
        self.fly_to_btn.setToolTip(
            "Type a coordinate and send the vehicle there in GUIDED mode."
        )
        self.fly_to_btn.clicked.connect(self.fly_to_requested)
        # Row 3 already holds GUIDED in column 0; this spans the remaining
        # two so it sits directly under AUTOTUNE without an empty gap.
        last_row = (len(self.MODE_ORDER) - 1) // 3
        grid.addWidget(self.fly_to_btn, last_row, 1, 1, 2)

    def set_active_mode(self, mode_name):
        for name, btn in self.buttons.items():
            if name == mode_name:
                btn.setStyleSheet(self.ACTIVE_STYLE)
            elif name == "RTL":
                btn.setStyleSheet(self.RTL_STYLE)
            else:
                btn.setStyleSheet(self.NORMAL_STYLE)


def globe_icon(px: int, color: str = "#ffffff", gap: int = 0) -> QIcon:
    """A wireframe lat/lon globe, drawn rather than loaded from a file.

    Drawn because it has to work at about 14px on a button: a bitmap
    scaled down to that turns the fine grid into grey mush, and it would
    also arrive the wrong colour for a blue background. Painting it means
    it stays crisp at any DPI and takes the colour it is asked for.

    Deliberately fewer grid lines than a full globe illustration - at this
    size any more merge into a solid disc.

    `gap` adds empty space to the right of the globe, inside the icon. Qt
    centres a button's icon and text as one group with no spacing control
    between them, so carrying the gap in the artwork is what pushes the
    globe clear of the label.
    """
    scale = 4                       # supersample, then scale down smoothly
    s = px * scale
    pm = QPixmap(s + gap * scale, s)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(s * 0.055)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    margin = s * 0.06
    r = (s - 2 * margin) / 2.0
    c = QPointF(s / 2.0, s / 2.0)

    painter.drawEllipse(c, r, r)                       # the sphere itself
    painter.drawLine(QPointF(c.x() - r, c.y()),
                     QPointF(c.x() + r, c.y()))        # equator, edge on
    painter.drawLine(QPointF(c.x(), c.y() - r),
                     QPointF(c.x(), c.y() + r))        # meridian, edge on

    # Latitudes: circles of shrinking radius, so they sit inside the sphere
    # rather than being drawn as if flat.
    for frac in (0.5,):
        dy = r * frac
        half_width = r * math.sqrt(max(0.0, 1.0 - frac * frac))
        for sign in (-1, 1):
            painter.drawEllipse(QPointF(c.x(), c.y() + dy * sign),
                                half_width, r * 0.13)
    # Meridians, seen at an angle - narrower ellipses on the same axis.
    for frac in (0.5,):
        painter.drawEllipse(c, r * frac, r)
    painter.end()

    return QIcon(pm.scaled(px + gap, px,
                           Qt.IgnoreAspectRatio, Qt.SmoothTransformation))


class MarqueeLabel(QLabel):
    """One line of text that scrolls itself only when it doesn't fit.

    Pre-arm reasons run long ("PreArm: GPS horiz error 1.85m") and the space
    beside the Force ARM checkbox is narrow. Eliding would hide the specific
    figure, which is the part worth reading, so it scrolls instead - and
    sits still when the text already fits, so a short reason doesn't wander
    about for no reason.
    """

    # ~62 px/s. At the original 1px/40ms (25 px/s) a long pre-arm reason
    # took over half a minute to scroll past once, which is no use at all
    # when the whole point is reading the end of it. A 16ms tick is also
    # smoother than covering the same ground in bigger jumps.
    STEP_PX = 1
    TICK_MS = 16
    PAUSE_MS = 1200          # hold at the start before scrolling begins

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0
        self._pause_ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    def setMessage(self, text: str):
        if text == self._text:
            return
        self._text = text or ""
        self._offset = 0
        self._pause_ticks = self.PAUSE_MS // self.TICK_MS
        self.setToolTip(self._text)     # the full text, always readable
        self._restart()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._restart()

    def _text_width(self):
        return self.fontMetrics().horizontalAdvance(self._text)

    def _restart(self):
        if self._text and self._text_width() > self.width():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0

    def _tick(self):
        if self._pause_ticks > 0:
            self._pause_ticks -= 1
            return
        # Scroll one full text width plus a gap, then wrap and pause again.
        span = self._text_width() + 30
        self._offset += self.STEP_PX
        if self._offset >= span:
            self._offset = 0
            self._pause_ticks = self.PAUSE_MS // self.TICK_MS
        self.update()

    def paintEvent(self, event):
        if not self._text:
            return
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.setFont(self.font())
        y = self.fontMetrics().ascent() + (
            self.height() - self.fontMetrics().height()) // 2
        painter.drawText(-self._offset, y, self._text)
        # Second copy trailing the first, so the line reads continuously
        # rather than blanking between passes.
        if self._timer.isActive():
            painter.drawText(-self._offset + self._text_width() + 30, y, self._text)
        painter.end()


class ArmDisarmPanel(QGroupBox):
    """
    ARM/DISARM controls, kept as their own group below the flight modes.

    ARM is a single instant click (optionally forced via the checkbox,
    matching the mode buttons' philosophy).

    DISARM is different on purpose: it always sends a *forced* disarm
    (ArduPilot generally rejects a normal disarm mid-flight anyway), and
    only fires after the button is held down continuously for
    HOLD_DURATION_MS - a physical safeguard against an accidental click
    instantly cutting power, with the hold itself serving as the
    confirmation (no extra dialog on top of it).
    """

    HOLD_DURATION_MS = 2500
    HOLD_TICK_MS = 50

    # (arm: bool, force: bool) - ARM path only
    arm_requested = Signal(bool, bool)
    # Fired once, only after a completed hold on DISARM (HOLD_DURATION_MS)
    force_disarm_requested = Signal()

    ARM_STYLE = "background-color: #2a6; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    ARM_ACTIVE_STYLE = "background-color: #184; color: white; font-weight: bold; border: 2px solid white; font-size: 10px; padding: 3px 4px;"
    DISARM_STYLE = "background-color: #666; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    DISARM_ACTIVE_STYLE = "background-color: #444; color: white; font-weight: bold; border: 2px solid white; font-size: 10px; padding: 3px 4px;"

    def __init__(self, parent=None):
        super().__init__("Arm / Disarm", parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 10, 6, 6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.arm_btn = QPushButton("ARM")
        self.disarm_btn = QPushButton("Hold to DISARM")
        btn_row.addWidget(self.arm_btn)
        btn_row.addWidget(self.disarm_btn)
        layout.addLayout(btn_row)

        self.disarm_progress = QProgressBar()
        self.disarm_progress.setRange(0, self.HOLD_DURATION_MS)
        self.disarm_progress.setValue(0)
        self.disarm_progress.setTextVisible(False)
        self.disarm_progress.setFixedHeight(5)
        layout.addWidget(self.disarm_progress)

        force_row = QHBoxLayout()
        force_row.setSpacing(8)
        self.force_checkbox = QCheckBox("Force ARM (bypass pre-arm safety checks)")
        force_row.addWidget(self.force_checkbox)
        # The reason the vehicle won't arm, in the space to the right.
        self.prearm_label = MarqueeLabel()
        self.prearm_label.setStyleSheet("color: #e6a23c; font-size: 10px;")
        self.prearm_label.setMinimumWidth(60)
        force_row.addWidget(self.prearm_label, stretch=1)
        layout.addLayout(force_row)

        self.arm_btn.clicked.connect(
            lambda: self.arm_requested.emit(True, self.force_checkbox.isChecked())
        )

        self._hold_elapsed_ms = 0
        self._hold_timer = QTimer(self)
        self._hold_timer.setInterval(self.HOLD_TICK_MS)
        self._hold_timer.timeout.connect(self._tick_hold)
        self.disarm_btn.pressed.connect(self._start_hold)
        self.disarm_btn.released.connect(self._cancel_hold)

        self.set_armed_state(None)

    def _start_hold(self):
        self._hold_elapsed_ms = 0
        self.disarm_progress.setValue(0)
        self._hold_timer.start()

    def _cancel_hold(self):
        self._hold_timer.stop()
        self._hold_elapsed_ms = 0
        self.disarm_progress.setValue(0)

    def _tick_hold(self):
        self._hold_elapsed_ms += self.HOLD_TICK_MS
        self.disarm_progress.setValue(min(self._hold_elapsed_ms, self.HOLD_DURATION_MS))
        if self._hold_elapsed_ms >= self.HOLD_DURATION_MS:
            self._hold_timer.stop()
            self.disarm_progress.setValue(0)
            self.force_disarm_requested.emit()

    def set_prearm_reason(self, text: str):
        self.prearm_label.setMessage(text or "")

    def set_armed_state(self, armed):
        """armed: True, False, or None (unknown yet)."""
        if armed is True:
            self.arm_btn.setStyleSheet(self.ARM_ACTIVE_STYLE)
            self.disarm_btn.setStyleSheet(self.DISARM_STYLE)
        elif armed is False:
            self.arm_btn.setStyleSheet(self.ARM_STYLE)
            self.disarm_btn.setStyleSheet(self.DISARM_ACTIVE_STYLE)
        else:
            self.arm_btn.setStyleSheet(self.ARM_STYLE)
            self.disarm_btn.setStyleSheet(self.DISARM_STYLE)


class PreflightCalPanel(QGroupBox):
    """
    One press-and-hold button that triggers gyro + baro preflight
    calibration. Same hold-to-confirm pattern as the DISARM button - no
    separate dialog, the hold itself is the confirmation.
    """

    HOLD_DURATION_MS = 2500
    HOLD_TICK_MS = 50

    calibration_requested = Signal()

    BTN_STYLE = "font-size: 10px; padding: 3px 4px;"

    def __init__(self, parent=None):
        super().__init__("Preflight Calibration", parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 10, 6, 6)

        self.cal_btn = QPushButton("Hold to Calibrate")
        self.cal_btn.setStyleSheet(self.BTN_STYLE)
        layout.addWidget(self.cal_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, self.HOLD_DURATION_MS)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        layout.addWidget(self.progress)

        self._hold_elapsed_ms = 0
        self._hold_timer = QTimer(self)
        self._hold_timer.setInterval(self.HOLD_TICK_MS)
        self._hold_timer.timeout.connect(self._tick_hold)
        self.cal_btn.pressed.connect(self._start_hold)
        self.cal_btn.released.connect(self._cancel_hold)

    def _start_hold(self):
        self._hold_elapsed_ms = 0
        self.progress.setValue(0)
        self._hold_timer.start()

    def _cancel_hold(self):
        self._hold_timer.stop()
        self._hold_elapsed_ms = 0
        self.progress.setValue(0)

    def _tick_hold(self):
        self._hold_elapsed_ms += self.HOLD_TICK_MS
        self.progress.setValue(min(self._hold_elapsed_ms, self.HOLD_DURATION_MS))
        if self._hold_elapsed_ms >= self.HOLD_DURATION_MS:
            self._hold_timer.stop()
            self.progress.setValue(0)
            self.calibration_requested.emit()


class WaypointMissionPanel(QGroupBox):
    """
    Controls for queueing several map-click points into a real onboard
    AUTO mission (see MavlinkLink.upload_and_start_mission), rather than
    the single-point "Fly to Here" click.
    """

    mode_toggled = Signal(bool)
    start_requested = Signal()
    update_requested = Signal()
    clear_requested = Signal()
    # Fired once, only after a completed hold on Clear (HOLD_DURATION_MS) -
    # separate from clear_requested (a plain click, which still fires
    # normally on release regardless of hold duration) so a quick click
    # only tidies the map, while holding also wipes the vehicle's mission.
    clear_mission_requested = Signal()

    HOLD_DURATION_MS = 3000
    HOLD_TICK_MS = 50

    def __init__(self, parent=None):
        super().__init__("Multi-Waypoint Mission", parent)
        layout = QVBoxLayout(self)

        self.queue_checkbox = QCheckBox("Queue waypoints on map click")
        self.queue_checkbox.toggled.connect(self.mode_toggled)
        layout.addWidget(self.queue_checkbox)

        row = QHBoxLayout()
        self.count_label = QLabel("0 waypoints queued")
        self.start_btn = QPushButton("Start Mission")
        self.start_btn.setEnabled(False)
        self.update_btn = QPushButton("Update")
        self.update_btn.setEnabled(False)
        self.update_btn.setToolTip(
            "Re-send the mission with your edited waypoint altitudes. "
            "The aircraft keeps flying the leg it is on - it does not restart."
        )
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip(
            "Click: clear the map.\nHold 3s: also erase the mission stored on the vehicle."
        )
        self.start_btn.clicked.connect(self.start_requested)
        self.update_btn.clicked.connect(self.update_requested)
        self.clear_btn.clicked.connect(self.clear_requested)
        row.addWidget(self.count_label, stretch=1)
        row.addWidget(self.start_btn)
        row.addWidget(self.update_btn)
        row.addWidget(self.clear_btn)
        layout.addLayout(row)

        self.clear_hold_progress = QProgressBar()
        self.clear_hold_progress.setRange(0, self.HOLD_DURATION_MS)
        self.clear_hold_progress.setValue(0)
        self.clear_hold_progress.setTextVisible(False)
        self.clear_hold_progress.setFixedHeight(4)
        layout.addWidget(self.clear_hold_progress)

        self._clear_hold_elapsed_ms = 0
        self._clear_hold_timer = QTimer(self)
        self._clear_hold_timer.setInterval(self.HOLD_TICK_MS)
        self._clear_hold_timer.timeout.connect(self._tick_clear_hold)
        self.clear_btn.pressed.connect(self._start_clear_hold)
        self.clear_btn.released.connect(self._cancel_clear_hold)

    def _start_clear_hold(self):
        self._clear_hold_elapsed_ms = 0
        self.clear_hold_progress.setValue(0)
        self._clear_hold_timer.start()

    def _cancel_clear_hold(self):
        self._clear_hold_timer.stop()
        self._clear_hold_elapsed_ms = 0
        self.clear_hold_progress.setValue(0)

    def _tick_clear_hold(self):
        self._clear_hold_elapsed_ms += self.HOLD_TICK_MS
        self.clear_hold_progress.setValue(min(self._clear_hold_elapsed_ms, self.HOLD_DURATION_MS))
        if self._clear_hold_elapsed_ms >= self.HOLD_DURATION_MS:
            self._clear_hold_timer.stop()
            self.clear_hold_progress.setValue(0)
            self.clear_mission_requested.emit()

    def set_can_update(self, can_update: bool):
        self.update_btn.setEnabled(can_update)

    def set_count(self, count):
        self.count_label.setText(f"{count} waypoint{'s' if count != 1 else ''} queued")
        self.start_btn.setEnabled(count > 0)
        # Clear stays enabled regardless of the current queue count -
        # markers from a previously-started mission can still be visible
        # on the map even when the active queue is empty, and clicking
        # Clear with nothing to clear is harmless.


class ConnectionPanel(QGroupBox):
    """
    Mission Planner-style connection bar: protocol dropdown (Serial/TCP/
    UDP), fields that change based on protocol, and a Connect button.
    For Serial, the second widget pair becomes a real dropdown of
    detected serial ports (via pyserial) plus a baud-rate dropdown,
    instead of free-text fields - so you pick from what's actually
    available rather than guessing a port name. Emits the constructed
    pymavlink connection string, or an empty string if the fields aren't
    usable (e.g. no serial port selected).

    Deliberately does NOT auto-connect - this only ever emits when the
    user clicks Connect.
    """

    # The same split Mission Planner draws between UDP and UDPCl, but
    # spelled out: "UDP (listen)" binds a port and waits for the vehicle to
    # stream to us, "UDP (connect to)" dials out to a specific host.
    # Previously a single "UDP" entry guessed between the two from whether
    # the host box was filled in, which silently produced an outgoing
    # connection for the common SITL case, where listening is what's needed.
    PROTOCOLS = ["Serial", "TCP", "UDP (listen)", "UDP (connect to)"]
    BAUD_RATES = ["4800", "9600", "19200", "38400", "57600", "115200", "230400"]

    connect_requested = Signal(str)
    disconnect_requested = Signal()

    FIELD_HEIGHT = 24

    def __init__(self, default_protocol="TCP", default_host="127.0.0.1", default_port="5762", parent=None):
        super().__init__("Connection", parent)
        self.setMaximumWidth(380)
        self.setStyleSheet("""
            QGroupBox { font-size: 10px; font-weight: bold; margin-top: 6px; padding-top: 4px; }
            QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 2px; }
            QComboBox, QLineEdit, QPushButton { font-size: 10px; padding: 2px 4px; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 10, 4, 4)
        outer.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(3)
        refresh_row = QHBoxLayout()
        refresh_row.setSpacing(3)
        outer.addLayout(row)
        outer.addLayout(refresh_row)
        outer.addStretch(1)  # keep both rows pinned to the top, don't stretch vertically

        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(self.PROTOCOLS)
        self.protocol_combo.setCurrentText(
            default_protocol if default_protocol in self.PROTOCOLS else "TCP"
        )
        self.protocol_combo.setFixedHeight(self.FIELD_HEIGHT)
        # Deliberately NOT a fixed width: it has to fit the longest entry
        # ("UDP (connect to)") or the label runs underneath the drop-down
        # arrow. Its size hint already accounts for that.
        self.protocol_combo.currentTextChanged.connect(self._on_protocol_changed)

        # Field 1: host (TCP/UDP) vs a real serial-port dropdown (Serial)
        self.field1_stack = QStackedWidget()
        self.field1_stack.setFixedHeight(self.FIELD_HEIGHT)
        # Minimum rather than fixed: this field absorbs the row's spare
        # width so the port box ends flush with the Disconnect button below.
        self.field1_stack.setMinimumWidth(90)
        self.host_edit = QLineEdit(default_host)
        self.serial_port_combo = QComboBox()
        self.field1_stack.addWidget(self.host_edit)          # index 0
        self.field1_stack.addWidget(self.serial_port_combo)  # index 1

        # Field 2: port (TCP/UDP) vs baud rate dropdown (Serial)
        self.field2_stack = QStackedWidget()
        self.field2_stack.setFixedHeight(self.FIELD_HEIGHT)
        self.field2_stack.setFixedWidth(66)
        self.port_edit = QLineEdit(default_port)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(self.BAUD_RATES)
        self.baud_combo.setCurrentText("57600")
        self.field2_stack.addWidget(self.port_edit)   # index 0
        self.field2_stack.addWidget(self.baud_combo)  # index 1

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-scan for connected serial devices")
        self.refresh_btn.setFixedHeight(self.FIELD_HEIGHT)
        self.refresh_btn.setStyleSheet("font-size: 9px; padding: 2px 3px;")
        self.refresh_btn.clicked.connect(self._refresh_serial_ports)
        self.refresh_btn.setVisible(False)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedHeight(self.FIELD_HEIGHT)
        self.connect_btn.setStyleSheet("color: #2af; font-weight: bold; font-size: 9px; padding: 2px 3px;")
        self.connect_btn.clicked.connect(self._on_connect_clicked)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setFixedHeight(self.FIELD_HEIGHT)
        self.disconnect_btn.setStyleSheet("color: #f66; font-weight: bold; font-size: 9px; padding: 2px 3px;")
        self.disconnect_btn.clicked.connect(self.disconnect_requested)

        # Fields on the first row, actions on the second. Keeping all five
        # on one row needed more width than the panel gets, so the
        # fixed-width widgets overflowed into each other.
        row.addWidget(self.protocol_combo)
        row.addWidget(self.field1_stack, 1)   # takes the slack (no trailing
        row.addWidget(self.field2_stack)      # stretch, so the row ends flush)
        refresh_row.addWidget(self.refresh_btn)
        refresh_row.addStretch(1)
        refresh_row.addWidget(self.connect_btn)
        refresh_row.addWidget(self.disconnect_btn)

        self._refresh_serial_ports()
        self._on_protocol_changed(self.protocol_combo.currentText())

    def _refresh_serial_ports(self):
        self.serial_port_combo.clear()
        ports = []
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
        except Exception:
            ports = []

        if not ports:
            self.serial_port_combo.addItem("No serial ports found", None)
            self.serial_port_combo.setEnabled(False)
        else:
            self.serial_port_combo.setEnabled(True)
            for p in ports:
                label = f"{p.device} - {p.description}" if p.description else p.device
                self.serial_port_combo.addItem(label, p.device)

    def _on_protocol_changed(self, protocol):
        is_serial = protocol == "Serial"
        self.field1_stack.setCurrentIndex(1 if is_serial else 0)
        self.field2_stack.setCurrentIndex(1 if is_serial else 0)
        self.refresh_btn.setVisible(is_serial)
        if is_serial:
            self._refresh_serial_ports()

        # Plain "UDP" always binds every interface, so there's no host to
        # enter - grey the box out rather than letting it look meaningful.
        listening_udp = protocol == "UDP (listen)"
        self.host_edit.setEnabled(not listening_udp)
        self.host_edit.setPlaceholderText("(listening)" if listening_udp else "")

    def _on_connect_clicked(self):
        protocol = self.protocol_combo.currentText()

        if protocol == "Serial":
            device = self.serial_port_combo.currentData()
            if not device:
                self.connect_requested.emit("")
                return
            baud = self.baud_combo.currentText()
            connection_string = f"{device}:{baud}"
        elif protocol == "TCP":
            host = self.host_edit.text().strip()
            port = self.port_edit.text().strip()
            if not host or not port:
                self.connect_requested.emit("")
                return
            connection_string = f"tcp:{host}:{port}"
        elif protocol == "UDP (listen)":
            # Listen on every interface. This is what ArduPilot SITL and
            # most telemetry setups need: they stream TO a port rather than
            # accepting a connection, so we have to be bound to that port
            # to receive anything.
            port = self.port_edit.text().strip()
            if not port:
                self.connect_requested.emit("")
                return
            connection_string = f"udpin:0.0.0.0:{port}"
        elif protocol == "UDP (connect to)":
            # Dial out to a specific peer - e.g. an ESP32/ELRS WiFi bridge
            # that waits to hear from us before it starts streaming.
            host = self.host_edit.text().strip()
            port = self.port_edit.text().strip()
            if not host or not port:
                self.connect_requested.emit("")
                return
            connection_string = f"udpout:{host}:{port}"
        else:
            connection_string = ""

        self.connect_requested.emit(connection_string)


class MessagesPanel(QGroupBox):
    """Scrolling log of STATUSTEXT messages from the vehicle - same
    message stream Mission Planner's Messages tab shows, color-coded by
    MAV_SEVERITY."""

    SEVERITY_COLORS = {
        0: "#ff5555",  # EMERGENCY
        1: "#ff5555",  # ALERT
        2: "#ff5555",  # CRITICAL
        3: "#ff8844",  # ERROR
        4: "#ffcc44",  # WARNING
        5: "#ffffff",  # NOTICE
        6: "#cfd2d6",  # INFO
        7: "#888888",  # DEBUG
    }

    def __init__(self, parent=None):
        super().__init__("Messages", parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 6)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setMaximumBlockCount(500)  # cap memory growth
        # Watermark logo baked into the background layer itself (Qt style
        # sheets have no background-size, so mavgcs_logo_watermark.png is
        # pre-scaled/faded to sit right-aligned behind the scrolling text -
        # see the chroma-key + alpha-fade preprocessing that produced it).
        # Its right-hand gap is a transparent margin baked into the image,
        # NOT padding-right: on a scroll area Qt clips the background at the
        # viewport edge, so once enough messages arrive to raise the
        # scrollbar, padding-based positioning left the logo clipped mid-word
        # underneath it.
        logo_path = resource_path("mavgcs_logo_watermark.png").replace("\\", "/")
        self.text_edit.setStyleSheet(
            "background-color: #16171a; color: white; "
            "font-family: Consolas, monospace; font-size: 10px; border: none; "
            f"background-image: url({logo_path}); "
            "background-repeat: no-repeat; background-position: right; "
            # Without this the background belongs to the scrolled content and
            # slides up and down with the messages; "fixed" pins it to the
            # viewport so it stays put while text scrolls past it.
            "background-attachment: fixed;"
        )
        layout.addWidget(self.text_edit)

        self.setMinimumHeight(130)
        self.setMaximumHeight(170)

    def add_message(self, text, severity):
        color = self.SEVERITY_COLORS.get(severity, "#cfd2d6")
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_text = html.escape(text)
        self.text_edit.appendHtml(
            f'<span style="color:{color};">{timestamp} : {safe_text}</span>'
        )
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class GuidedControlPanel(QGroupBox):
    """Change Speed / Change Altitude buttons, Mission Planner-style -
    each opens a small prompt for the new value, then sends it."""

    speed_requested = Signal(float)
    altitude_requested = Signal(float)
    loiter_radius_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__("Guided Control", parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 10, 6, 6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.speed_btn = QPushButton("Change Speed")
        self.altitude_btn = QPushButton("Change Altitude")
        self.loiter_radius_btn = QPushButton("Change Loiter Radius")
        btn_row.addWidget(self.speed_btn)
        btn_row.addWidget(self.altitude_btn)
        btn_row.addWidget(self.loiter_radius_btn)
        layout.addLayout(btn_row)

        self._last_speed = 15.0
        self._last_alt = 30.0
        self._last_loiter_radius = 100.0

        self.speed_btn.clicked.connect(self._prompt_speed)
        self.altitude_btn.clicked.connect(self._prompt_altitude)
        self.loiter_radius_btn.clicked.connect(self._prompt_loiter_radius)

    def _prompt_speed(self):
        speed, ok = QInputDialog.getDouble(
            self, "Change Speed", "New target airspeed (m/s):",
            value=self._last_speed, minValue=0.0, maxValue=100.0, decimals=1,
        )
        if ok:
            self._last_speed = speed
            self.speed_requested.emit(speed)

    def _prompt_altitude(self):
        alt, ok = QInputDialog.getDouble(
            self, "Change Altitude", "New target altitude (m, relative to home):",
            value=self._last_alt, minValue=0.0, maxValue=10000.0, decimals=0,
        )
        if ok:
            self._last_alt = alt
            self.altitude_requested.emit(alt)

    def _prompt_loiter_radius(self):
        radius, ok = QInputDialog.getDouble(
            self, "Change Loiter Radius",
            "New loiter radius (m)\n(negative = counter-clockwise):",
            value=self._last_loiter_radius,
            minValue=-5000.0, maxValue=5000.0, decimals=0,
        )
        if ok:
            self._last_loiter_radius = radius
            self.loiter_radius_requested.emit(radius)

    def set_last_alt(self, alt):
        """Called from live telemetry so the dialog defaults to something sane."""
        self._last_alt = alt


class _OverlayButtonArea(QWidget):
    """
    Holds the HUD/FPV stack with a small button floating along its top edge,
    centred in the gap between the heading tape and the battery box. The
    button is a plain child rather than a layout item, so it costs no height
    in the column it sits in - putting it in its own row was enough to make
    the whole left panel scroll on a shorter window.
    """

    MARGIN = 6

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)

        # A live widget borrowed from the HUD for as long as the FPV view
        # is showing: (widget, callable giving its geometry).
        self._adopted = None

        self.button = QPushButton("FPV", self)
        self.button.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,0.55); color: #fff; "
            "border: 1px solid rgba(255,255,255,0.35); border-radius: 4px; "
            "font-size: 10px; font-weight: bold; padding: 2px 10px; }"
            "QPushButton:hover { background: rgba(55,168,219,0.55); }"
        )
        self.button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_button()
        if self._adopted is not None:
            widget, geometry_fn = self._adopted
            widget.setGeometry(geometry_fn())
            widget.raise_()

    def adopt(self, widget, geometry_fn):
        """Float one of the HUD's real child widgets over the view.

        The FPV scene carries the HUD as a flat image, so anything meant to
        be clicked has to be a live widget sitting on top of it - drawn into
        the image it would look right and do nothing.
        """
        self._adopted = (widget, geometry_fn)
        widget.setParent(self)      # reparenting hides it
        widget.setGeometry(geometry_fn())
        widget.show()
        widget.raise_()

    def release(self, back_to):
        """Hand the borrowed widget back to its owner."""
        if self._adopted is None:
            return
        widget, geometry_fn = self._adopted
        self._adopted = None
        widget.setParent(back_to)
        widget.setGeometry(geometry_fn())
        widget.show()

    def set_label(self, text: str):
        """Change the caption and re-centre: a different word is a different
        width, and off-centre by half a letter is visible."""
        self.button.setText(text)
        self._place_button()

    def _place_button(self):
        b = self.button
        b.adjustSize()
        # The HUD fills this widget, so its geometry is ours.
        center = ArtificialHorizon.top_gap_center_x(self.width(), self.height())
        x = int(round(center - b.width() / 2.0))
        # Narrow window: the gap can close up entirely, so keep the button
        # on screen rather than letting it slide off the edge.
        x = max(self.MARGIN, min(x, self.width() - b.width() - self.MARGIN))
        b.move(x, self.MARGIN)
        b.raise_()


class MainWindow(QMainWindow):
    def __init__(self, connection_string):
        super().__init__()
        self.setWindowTitle(f"MavGCS {APP_VERSION}")
        self.setWindowIcon(QIcon(resource_path("mavgcs_icon.png")))
        self.resize(1300, 920)

        self.horizon = ArtificialHorizon()
        self.horizon.setMinimumHeight(175)
        self.telemetry = TelemetryPanel()
        self.mode_panel = ModePanel()
        self.arm_panel = ArmDisarmPanel()
        self.preflight_cal_panel = PreflightCalPanel()
        self.guided_panel = GuidedControlPanel()
        # Must be listening before the map page loads, since its tile URLs
        # point at this proxy's port. See tile_cache.py.
        self.tile_server = TileCacheServer()
        _tile_port = self.tile_server.start()
        self.map_view = MapView(_tile_port)
        self.fpv_view = FpvView(_tile_port, load_settings().get("cesium_ion_token", ""))
        self.waypoint_panel = WaypointMissionPanel()
        self.messages_panel = MessagesPanel()
        self.connection_panel = ConnectionPanel(*self._split_connection_string(connection_string))
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.command_label = QLabel("")
        self.command_label.setStyleSheet("color: #ccc;")
        self.command_label.setWordWrap(True)
        self.ack_label = QLabel("")
        self.ack_label.setStyleSheet("color: #888; font-size: 9px;")
        self.ack_label.setWordWrap(True)
        self.vehicle_state_label = QLabel()
        self.vehicle_state_label.setAlignment(Qt.AlignCenter)
        self.flight_time_label = QLabel()
        self.flight_time_label.setAlignment(Qt.AlignCenter)
        self.flight_time_label.setStyleSheet(
            "background-color: black; color: #ccc; font-size: 11px; "
            "font-weight: bold; padding: 3px 12px; border-radius: 4px;"
        )
        # Counts up while armed, back to zero on disarm. Monotonic rather
        # than wall-clock so it can't jump if the system clock is corrected.
        self._flight_start = None
        # Whether this link has ever reported the vehicle DISARMED. If it
        # hasn't by the time we first see it armed, we joined an aircraft
        # that was already flying and the clock can only be a lower bound.
        self._seen_disarmed = False
        self._flight_partial = False
        self._flight_timer = QTimer(self)
        # 250ms and a precise timer: the seconds digit is truncated, so a
        # coarse half-second tick can leave the display a whole second
        # behind what the clock actually says.
        self._flight_timer.setInterval(250)
        self._flight_timer.setTimerType(Qt.PreciseTimer)
        self._flight_timer.timeout.connect(self._update_flight_time)
        self._update_flight_time()
        self._armed = False
        self._ready_to_arm = False
        self._update_vehicle_state_label()
        self._last_alt = 30.0  # default guess used to pre-fill the fly-to dialog
        self._last_lat = None
        self._last_lon = None
        # One dict per waypoint: {"id", "lat", "lon", "alt"} with alt None
        # meaning "fly the mission default". _sent_mission holds references
        # to the very same dicts, so editing an altitude reaches both.
        self._waypoint_queue = []
        self._sent_mission = []
        self._mission_default_alt = None
        self._last_amsl_alt = 0.0
        # True height above the terrain below, from TERRAIN_REPORT. None
        # until the vehicle sends one (it needs terrain data loaded), in
        # which case the 3D view falls back to height above home.
        self._last_agl = None
        # Attitude in degrees, kept for the 3D camera: the horizon
        # widget stores roll/pitch in radians and drops yaw entirely.
        self._last_att_deg = (0.0, 0.0, 0.0)   # yaw, pitch, roll
        self._last_groundspeed = 0.0
        self._last_climb = 0.0

        # Runs independently of the mavlink connection lifecycle (it just
        # sits idle with no valid telemetry until on_position/on_vfr start
        # feeding it) - terrain tile downloads take real time, so this
        # can't run on the GUI thread. See terrain_provider.py.
        self.terrain_worker = TerrainRadarWorker(self)
        self.terrain_worker.fan_ready.connect(self.on_terrain_fan_ready)
        self.terrain_worker.start()

        # Same idea for ADS-B - starts disabled, only fetches while the
        # map's "ADS-B" checkbox is on (see map_view.py's adsb_toggled).
        # Anything pushed before the page finishes loading is dropped, so
        # redraw the overlay once it's ready.
        self.fpv_view.loadFinished.connect(lambda ok: self._push_hud_overlay())

        self.adsb_worker = AdsbWorker(self)
        self.adsb_worker.contacts_ready.connect(self.map_view.update_adsb_contacts)
        self.adsb_worker.start()

        left_content = QWidget()
        left_content.setStyleSheet("""
            QPushButton { font-size: 10px; padding: 3px 4px; }
            QGroupBox {
                font-size: 10px; font-weight: bold;
                margin-top: 6px; padding-top: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 6px; padding: 0 2px;
            }
            QLabel { font-size: 10px; }
            QCheckBox { font-size: 10px; }
        """)
        left_layout = QVBoxLayout(left_content)
        left_layout.setSpacing(4)
        left_layout.setContentsMargins(6, 6, 6, 6)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.vehicle_state_label)
        left_layout.addLayout(status_row)
        # Command/ACK text on the left, flight time in the space beside it.
        info_row = QHBoxLayout()
        info_col = QVBoxLayout()
        info_col.setSpacing(4)
        info_col.addWidget(self.command_label)
        info_col.addWidget(self.ack_label)
        info_row.addLayout(info_col, stretch=1)
        info_row.addWidget(self.flight_time_label, alignment=Qt.AlignTop)
        left_layout.addLayout(info_row)
        left_layout.addWidget(self.arm_panel)
        left_layout.addWidget(self.preflight_cal_panel)
        left_layout.addWidget(self.mode_panel)
        left_layout.addWidget(self.guided_panel)
        # HUD and 3D FPV occupy the same slot; the button swaps between them.
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.horizon)   # index 0
        self.view_stack.addWidget(self.fpv_view)  # index 1

        # The toggle floats over the view rather than sitting in its own
        # row: an extra row costs vertical space in this column, which on a
        # shorter window is enough to push the whole panel into scrolling.
        # It centres itself in the gap between the heading tape and the
        # battery box, so it covers neither.
        self.view_area = _OverlayButtonArea(self.view_stack)
        view_area = self.view_area
        self.view_toggle_btn = view_area.button
        self.view_toggle_btn.clicked.connect(self.on_toggle_view)
        left_layout.addWidget(view_area, stretch=1)
        left_layout.addWidget(self.telemetry)

        # Everything above is stacked with its natural size, not squeezed to
        # fit - if the window is too short to show it all, this scrolls
        # instead of compressing/overlapping widgets.
        left = QScrollArea()
        left.setWidget(left_content)
        left.setWidgetResizable(True)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.setMinimumWidth(340)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        top_right_stack = QVBoxLayout()
        top_right_stack.setSpacing(4)
        top_right_stack.addWidget(self.connection_panel)
        top_right_stack.addWidget(self.waypoint_panel)

        top_row = QHBoxLayout()
        top_row.addWidget(self.messages_panel, stretch=3)
        top_row.addLayout(top_right_stack, stretch=1)
        right_layout.addLayout(top_row)

        right_layout.addWidget(self.map_view, stretch=1)

        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.addWidget(left, stretch=2)
        main_layout.addWidget(right, stretch=3)
        self.setCentralWidget(central)

        # Deliberately NOT auto-connecting - self.link stays None until
        # the user clicks Connect in the Connection panel. The CLI
        # connection_string is only used to pre-fill that panel's fields.
        self.link = None
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.connection_panel.connect_requested.connect(self.on_connect_requested)
        self.connection_panel.disconnect_requested.connect(self.on_disconnect_requested)

        self.map_view.fly_to_here.connect(self.on_fly_to_here)
        self.map_view.waypoint_added.connect(self.on_waypoint_added)
        self.map_view.waypoint_alt_changed.connect(self.on_waypoint_alt_changed)
        self.map_view.adsb_toggled.connect(self.adsb_worker.set_enabled)
        self.map_view.adsb_center_changed.connect(self.adsb_worker.update_center)
        self.map_view.tile_cache_limit_changed.connect(self.on_tile_cache_limit)
        self.map_view.tile_cache_clear_requested.connect(self.on_tile_cache_clear)
        self.map_view.terrain_cache_limit_changed.connect(self.on_terrain_cache_limit)
        self.map_view.terrain_cache_clear_requested.connect(self.on_terrain_cache_clear)
        # Keep the map's cache readout current: the size changes as tiles
        # stream in, not just when the user touches the control.
        self._tile_stats_timer = QTimer(self)
        self._tile_stats_timer.setInterval(2000)
        self._tile_stats_timer.timeout.connect(self._push_tile_cache_stats)
        self._tile_stats_timer.start()
        self.waypoint_panel.mode_toggled.connect(self.on_waypoint_mode_toggled)
        self.waypoint_panel.start_requested.connect(self.on_start_mission)
        self.waypoint_panel.update_requested.connect(self.on_update_mission)
        self.waypoint_panel.clear_requested.connect(self.on_clear_waypoints)
        self.waypoint_panel.clear_mission_requested.connect(self.on_clear_vehicle_mission)
        # These two go through wrapper methods rather than binding
        # directly to self.link.set_mode/preflight_calibration - a direct
        # bind captures whatever self.link IS at connect() time, and
        # would silently keep pointing at a stale, stopped link object
        # after any reconnect (self.link gets replaced with a new
        # instance, but the old binding doesn't follow it).
        self.mode_panel.mode_requested.connect(self.on_mode_requested)
        self.mode_panel.fly_to_requested.connect(self.on_fly_to_latlon)
        self.arm_panel.arm_requested.connect(self.on_arm_requested)
        self.arm_panel.force_disarm_requested.connect(self.on_force_disarm)
        self.preflight_cal_panel.calibration_requested.connect(self.on_calibration_requested)
        self.guided_panel.speed_requested.connect(self.on_speed_requested)
        self.guided_panel.altitude_requested.connect(self.on_altitude_requested)
        self.guided_panel.loiter_radius_requested.connect(self.on_loiter_radius_requested)

    def on_attitude(self, roll, pitch, yaw):
        self.horizon.set_attitude(roll, pitch, yaw)
        self.telemetry.set_value("roll_deg", f"{math.degrees(roll):.2f}")
        self.telemetry.set_value("pitch_deg", f"{math.degrees(pitch):.2f}")
        # ATTITUDE.yaw is in radians over -pi..+pi (MAVLink spec), so a
        # raw degrees() conversion goes negative past 180 instead of
        # continuing to 360 - wrap it, same fix as the wind direction bug.
        self.telemetry.set_value("yaw_deg", f"{math.degrees(yaw) % 360:.2f}")
        # Drive the 3D camera from the same attitude. Only while the FPV
        # view is showing - each call crosses into the web page, and there's
        # no point paying that for a hidden widget.
        self._last_att_deg = (math.degrees(yaw) % 360,
                              math.degrees(pitch), math.degrees(roll))
        if self.view_stack.currentIndex() == 1:
            self._update_fpv_camera()
            self._push_hud_overlay()

    def on_position(self, lat, lon, alt, heading):
        self.horizon.set_altitude(alt)
        self.horizon.set_heading(heading)
        self.horizon.set_position(lat, lon)
        self._last_alt = alt
        self._last_lat = lat
        self._last_lon = lon
        self.guided_panel.set_last_alt(alt)
        self.telemetry.set_value("altitude", f"{alt:.2f}")
        self.map_view.update_position(lat, lon, heading)
        self.terrain_worker.update_telemetry(lat, lon, heading, self._last_groundspeed)
        self.map_view.update_terrain_reference(
            self._last_amsl_alt, self._last_groundspeed, self._last_climb
        )

    def _set_flight_timer_running(self, running: bool):
        """Start on arming, back to zero on disarming.

        Starting is guarded on _flight_start, so the 'armed' that arrives
        with every heartbeat restarts nothing - only the transition counts.
        """
        if running:
            if self._flight_start is None:
                # No MAVLink message carries time-since-arming - every
                # timestamp in the protocol is since-boot or epoch - so
                # joining an already-armed aircraft means the real flight
                # is older than anything we can measure. Say so rather
                # than showing a confidently wrong number.
                self._flight_partial = not self._seen_disarmed
                self._flight_start = time.monotonic()
                self._flight_timer.start()
        else:
            self._seen_disarmed = True
            self._flight_start = None
            self._flight_partial = False
            self._flight_timer.stop()
        self._update_flight_time()

    def _update_flight_time(self):
        elapsed = 0 if self._flight_start is None else int(
            time.monotonic() - self._flight_start)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        # A trailing + means "at least this long": we connected mid-flight.
        mark = "+" if self._flight_partial else ""
        self.flight_time_label.setText(
            f"GCS Flight Time : {hours:02d}:{minutes:02d}:{seconds:02d}{mark}")
        self.flight_time_label.setToolTip(
            "The aircraft was already armed when this GCS connected, so it "
            "has been flying at least this long - the autopilot doesn't "
            "report when it armed." if self._flight_partial else "")

    def _update_vehicle_state_label(self):
        """Single box: ARMED (red) while armed; otherwise READY TO ARM
        (green) / NOT READY TO ARM (red) from the prearm-check status -
        "ready to arm" only means anything pre-arm, so armed always wins."""
        if self._armed:
            text, color = "ARMED", "#e74c3c"
        elif self._ready_to_arm:
            text, color = "READY TO ARM", "#2ecc71"
        else:
            text, color = "NOT READY TO ARM", "#e74c3c"
        self.vehicle_state_label.setText(text)
        self.vehicle_state_label.setStyleSheet(
            f"background-color: black; color: {color}; font-weight: bold; "
            "padding: 3px 12px; border-radius: 4px;"
        )

    def _update_fpv_camera(self):
        """Point the 3D camera where the aircraft is, facing where it faces."""
        if self._last_lat is None or self._last_lon is None:
            self.fpv_view.set_status("Waiting for position...")
            return
        self.fpv_view.set_status("")
        yaw_deg, pitch_deg, roll_deg = self._last_att_deg
        # Height above ground, which is what the 3D camera is positioned by.
        # TERRAIN_REPORT's is the real thing; height above home is the
        # fallback, and only differs once the ground itself rises or falls.
        agl = self._last_agl if self._last_agl is not None else self._last_alt
        self.fpv_view.set_aircraft(self._last_lat, self._last_lon,
                                   self._last_amsl_alt, agl,
                                   yaw_deg, pitch_deg, roll_deg)

    def _push_hud_overlay(self):
        """
        Render the HUD widget transparently and hand it to the FPV view.

        Drawing the very same widget - rather than reimplementing its
        instruments in the 3D page - is what keeps the two views identical;
        there's only ever one HUD to change. It's pushed as an image rather
        than layered as a sibling widget because stacking a normal widget
        over a web view isn't reliable.
        """
        if self.view_stack.currentIndex() != 1:
            return
        size = self.view_stack.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        if self.horizon.size() != size:
            # A QStackedWidget doesn't resize the page it isn't showing, so
            # after the window is resized in FPV mode the HUD is still at its
            # old size - and the image gets stretched to fill the 3D view,
            # distorting every instrument on it. Keep the two in step.
            self.horizon.resize(size)
        image = QImage(size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        self.horizon.overlay_mode = True
        try:
            # DrawChildren only: without excluding DrawWindowBackground the
            # widget's own palette background is painted in too, and the
            # whole overlay comes out opaque grey, hiding the 3D entirely.
            self.horizon.render(image, QPoint(), QRegion(),
                                QWidget.RenderFlag.DrawChildren)
        finally:
            self.horizon.overlay_mode = False

        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        image.save(buf, "PNG")
        data = bytes(buf.data().toBase64()).decode("ascii")
        self.fpv_view.set_hud_image("data:image/png;base64," + data)

    def _ensure_ion_token(self) -> bool:
        """
        Ask for a Cesium Ion token the first time the 3D view is opened.

        The token is per-user on purpose: shipping one inside the app would
        bill everybody's streaming to a single account, and it would be
        readable straight out of the download anyway.
        """
        if self.fpv_view.has_token:
            return True
        token, ok = QInputDialog.getText(
            self, "Cesium Ion token",
            "The 3D view streams terrain and imagery from Cesium Ion.\n"
            "Create a free account at cesium.com/ion, then paste your\n"
            "access token here:",
        )
        token = (token or "").strip()
        if not ok or not token:
            return False
        save_setting("cesium_ion_token", token)
        self.fpv_view.set_token(token)
        return True

    def on_toggle_view(self):
        showing_fpv = self.view_stack.currentIndex() == 1
        if not showing_fpv and not self._ensure_ion_token():
            return          # no token: stay on the HUD rather than a blank view
        self.view_stack.setCurrentIndex(0 if showing_fpv else 1)
        if showing_fpv:
            self.view_area.release(self.horizon)
        else:
            area = self.view_area
            self.view_area.adopt(
                self.horizon.cell_selector,
                lambda: ArtificialHorizon.cell_selector_rect_for(
                    area.width(), area.height()))
        # Label names the view you'll get, not the one you're on.
        self.view_area.set_label("FPV" if showing_fpv else "HUD")
        if not showing_fpv:
            # Place the camera and HUD straight away. Otherwise the view sits
            # at Cesium's default whole-globe shot until the next ATTITUDE
            # message arrives - which, disconnected, is never.
            self._update_fpv_camera()
            self._push_hud_overlay()

    def on_terrain_fan_ready(self, elevations, range_m, ang_cells, rad_cells):
        self.map_view.update_terrain_fan(elevations, range_m, ang_cells, rad_cells)

    def on_vfr(self, airspeed, groundspeed, climb):
        self.telemetry.set_value("airspeed", f"{airspeed:.2f}")
        self.telemetry.set_value("groundspeed", f"{groundspeed:.2f}")
        self.telemetry.set_value("vspeed_mps", f"{climb:.2f}")
        self.horizon.set_airspeed(airspeed)
        self._last_groundspeed = groundspeed
        self._last_climb = climb

    def on_wind(self, direction, speed):
        self.horizon.set_wind(direction, speed)
        # direction from atan2-based math (WIND_COV path) can come out
        # negative before wrapping - the HUD widget wraps it internally
        # (self.wind_dir = direction_deg % 360), but this text field was
        # displaying the raw unwrapped value, hence e.g. -21.27 here vs
        # 339 (its wrapped equivalent) on the HUD.
        direction_wrapped = direction % 360
        self.telemetry.set_value("wind_dir_deg", f"{direction_wrapped:.2f}")
        self.telemetry.set_value("wind_speed_mps", f"{speed * 3.6:.2f}")

    def _require_link(self):
        """Returns self.link if connected, else shows feedback and
        returns None. Every handler that sends a command through
        self.link needs this now, since self.link starts as None until
        the user clicks Connect (see 'no auto-connect' above)."""
        if self.link is None:
            self.command_label.setText("Not connected - click Connect first")
            return None
        return self.link

    def on_mode_requested(self, mode_name):
        link = self._require_link()
        if link:
            link.set_mode(mode_name)

    def on_calibration_requested(self):
        link = self._require_link()
        if link:
            link.preflight_calibration()

    def on_speed_requested(self, speed_mps):
        link = self._require_link()
        if link:
            link.change_speed(speed_mps)

    def on_altitude_requested(self, alt_relative_m):
        link = self._require_link()
        if link:
            link.change_altitude(alt_relative_m, current_alt_relative_m=self._last_alt)

    def on_loiter_radius_requested(self, radius_m):
        link = self._require_link()
        if link:
            link.change_loiter_radius(radius_m)

    def on_status(self, status_dict):
        for key, value in status_dict.items():
            self.telemetry.set_value(key, value)
        if "mode" in status_dict:
            self.mode_panel.set_active_mode(status_dict["mode"])
        if "armed" in status_dict:
            armed = status_dict["armed"] == "YES"
            self._armed = armed
            self.arm_panel.set_armed_state(armed)
            self._update_vehicle_state_label()
            self._set_flight_timer_running(armed)
            if armed:
                self.arm_panel.set_prearm_reason("")
        if "ekf_color" in status_dict:
            self.horizon.set_ekf_status(status_dict["ekf_color"])
        if "vibe_color" in status_dict:
            self.horizon.set_vibe_status(status_dict["vibe_color"])
        if "battery_voltage" in status_dict:
            # "--" when the autopilot doesn't report voltage; the HUD already
            # renders None as "-- V".
            raw_voltage = status_dict["battery_voltage"]
            try:
                self.horizon.set_battery_voltage(float(raw_voltage))
            except ValueError:
                self.horizon.set_battery_voltage(None)
        if "amsl_alt" in status_dict:
            self._last_amsl_alt = float(status_dict["amsl_alt"])
        if "agl" in status_dict:
            try:
                self._last_agl = float(status_dict["agl"])
            except (TypeError, ValueError):
                pass
        if "ready_to_arm" in status_dict:
            self._ready_to_arm = status_dict["ready_to_arm"] == "YES"
            if self._ready_to_arm:
                # Whatever it was complaining about has cleared.
                self.arm_panel.set_prearm_reason("")
            self._update_vehicle_state_label()

    def on_connection_status(self, connected, message):
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            "color: lightgreen; font-weight: bold;" if connected
            else "color: orange; font-weight: bold;"
        )

    def on_command_feedback(self, message):
        if message.startswith("ACK:"):
            self.ack_label.setText(message)
        else:
            self.command_label.setText(message)

    def on_arm_requested(self, arm, force):
        link = self._require_link()
        if not link:
            return
        # Only ever called for ARM now - DISARM goes through the
        # press-and-hold mechanism (see on_force_disarm) instead.
        if force:
            reply = QMessageBox.question(
                self,
                "Force ARM",
                "Force ARM bypasses ArduPilot's pre-arm safety checks "
                "(e.g. GPS lock, RC failsafe).\n\n"
                "Are you sure?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        link.send_arm(arm, force)

    def on_force_disarm(self):
        link = self._require_link()
        if not link:
            return
        # No confirmation dialog here on purpose - the hold on
        # the DISARM button already served as the deliberate confirmation.
        link.send_arm(False, force=True)

    def on_fly_to_here(self, lat, lon):
        link = self._require_link()
        if not link:
            self.map_view.clear_target()
            return

        # Switching back to single-click fly-to mode - clear any
        # leftover multi-waypoint markers/queue from before, so the map
        # doesn't stay cluttered with an old route. Harmless no-op if
        # there weren't any.
        self.on_clear_waypoints()

        alt, ok = QInputDialog.getDouble(
            self,
            "Fly to Here",
            f"Target: {lat:.6f}, {lon:.6f}\n\nRelative altitude (m):",
            value=self._last_alt if self._last_alt else 30.0,
            minValue=0.0, maxValue=1000.0, decimals=0,
        )
        if not ok:
            self.map_view.clear_target()
            return

        link.fly_to(lat, lon, alt)

    def on_fly_to_latlon(self):
        """The FLY TO LAT / LON button: same command as clicking the map,
        but for a coordinate you have been given rather than one you can see.
        Deliberately leaves any planned waypoints alone - typing a coordinate
        shouldn't silently wipe a mission you have drawn."""
        link = self._require_link()
        if not link:
            return
        dialog = FlyToDialog(self._last_alt if self._last_alt else 30.0, self)
        if dialog.exec() != QDialog.Accepted:
            return
        lat, lon, alt = dialog.values()
        self.map_view.show_target(lat, lon)
        link.fly_to(lat, lon, alt)

    def on_status_text(self, text, severity):
        """Surface the reason the vehicle won't arm.

        NOT READY TO ARM says only that it is refusing, not why. ArduPilot
        does say why - "PreArm: GPS horiz error 1.85m" - but only as one
        line in a scrolling log that has usually moved on by the time you
        look. It costs nothing to keep the latest one in view, and it turns
        a dead-end indicator into something you can act on.
        """
        if self._armed:
            # Pre-arm advice is meaningless once it is flying, and stale
            # text on a panel you glance at is worse than none.
            return
        stripped = (text or "").strip()
        # ArduPilot uses "PreArm:" for checks that block arming and "Arm:"
        # for a rejected arming attempt. Both answer the same question.
        if stripped.startswith("PreArm:") or stripped.startswith("Arm:"):
            self.arm_panel.set_prearm_reason(stripped)

    def on_waypoint_mode_toggled(self, enabled):
        self.map_view.set_waypoint_mode(enabled)

    def on_waypoint_added(self, lat, lon, wp_id):
        self._waypoint_queue.append(
            {"id": int(wp_id), "lat": lat, "lon": lon, "alt": None}
        )
        self.waypoint_panel.set_count(len(self._waypoint_queue))

    def on_waypoint_alt_changed(self, wp_id, alt):
        """An altitude typed into a waypoint's popup on the map.

        Searches both the pending queue and the mission already sent - a
        point stays editable after it has been flown to the vehicle, which
        is the whole point of the Update button.
        """
        for wp in self._waypoint_queue + self._sent_mission:
            if wp["id"] == int(wp_id):
                wp["alt"] = float(alt)
                which = "queued" if wp in self._waypoint_queue else "sent"
                self.on_command_feedback(
                    f"Waypoint altitude set to {float(alt):.0f} m"
                    + (" - press Update to send it" if which == "sent" else "")
                )
                break

    def on_clear_waypoints(self):
        self._waypoint_queue = []
        self._sent_mission = []
        self.map_view.clear_waypoints()
        self.waypoint_panel.set_count(0)
        self.waypoint_panel.set_can_update(False)

    def on_clear_vehicle_mission(self):
        """Fired only after a completed 3s hold on the Clear button (see
        WaypointMissionPanel.clear_mission_requested) - erases the mission
        actually stored on the vehicle. A plain click of the same button
        still only clears the map (on_clear_waypoints, via clear_requested,
        fires normally on release regardless of hold duration)."""
        link = self._require_link()
        if link:
            link.clear_mission()

    def on_start_mission(self):
        if not self._waypoint_queue:
            return
        link = self._require_link()
        if not link:
            return
        alt, ok = QInputDialog.getDouble(
            self,
            "Start Mission",
            f"Fly through {len(self._waypoint_queue)} waypoints at what "
            f"relative altitude (m)?",
            value=self._last_alt if self._last_alt else 30.0,
            minValue=0.0, maxValue=1000.0, decimals=0,
        )
        if not ok:
            return
        link.upload_and_start_mission(
            [(w["lat"], w["lon"], w["alt"]) for w in self._waypoint_queue], alt
        )
        # Pin down what each point was actually sent with, so the record on
        # the map can't drift when a later mission uses a different default.
        for wp in self._waypoint_queue:
            if wp["alt"] is None:
                wp["alt"] = float(alt)
        # Keep the batch: its altitudes stay editable, and Update re-sends it.
        self._sent_mission = list(self._waypoint_queue)
        self._mission_default_alt = alt
        self.map_view.set_waypoint_default_alt(alt)
        self.waypoint_panel.set_can_update(True)
        # A leftover Fly-to-Here target marker is a separate thing from
        # the waypoint queue - clear it too, since starting a mission
        # supersedes any pending single-point target.
        self.map_view.clear_target()
        # Keep the markers on the map as a visible record of what was
        # sent - only remove them when the user explicitly clicks Clear.
        self._waypoint_queue = []
        self.map_view.commit_waypoints()
        self.waypoint_panel.set_count(0)

    def on_mission_uploaded(self):
        """The vehicle accepted the mission, so the map can stop flagging
        edited altitudes as unsent. Driven by the acknowledgement rather
        than by pressing send, so a failed upload stays flagged."""
        self.map_view.mark_mission_sent()

    def on_update_mission(self):
        """Re-send the mission that is already on the vehicle, with whatever
        altitudes have been edited since. Deliberately does NOT restart it -
        the aircraft carries on from the leg it is flying."""
        if not self._sent_mission:
            return
        link = self._require_link()
        if not link:
            return
        default = self._mission_default_alt if self._mission_default_alt else self._last_alt
        link.upload_and_start_mission(
            [(w["lat"], w["lon"], w["alt"]) for w in self._sent_mission],
            default,
            restart=False,
        )

    @staticmethod
    def _split_connection_string(connection_string):
        """Parse a pymavlink connection string into (protocol, host, port)
        to pre-fill the Connection panel with whatever was passed on the
        command line, so it starts showing the truth instead of a
        hardcoded default."""
        parts = connection_string.split(":")
        if connection_string.startswith("tcp:") and len(parts) == 3:
            return ("TCP", parts[1], parts[2])
        if connection_string.startswith("udpout:") and len(parts) == 3:
            return ("UDP (connect to)", parts[1], parts[2])
        if connection_string.startswith(("udpin:", "udp:")) and len(parts) == 3:
            return ("UDP (listen)", parts[1], parts[2])
        if len(parts) == 2:
            # Anything else with a single colon is treated as Serial
            # (device:baud), e.g. "com3:57600" or "/dev/ttyUSB0:57600".
            return ("Serial", parts[0], parts[1])
        return ("TCP", "127.0.0.1", "5762")

    def _connect_link(self, connection_string):
        """
        Create a MavlinkLink for connection_string and wire up all its
        signals. Used both at startup and by the Connection panel's
        Connect button for runtime reconnection - factored out so both
        paths stay identical instead of duplicating the wiring list.
        """
        self.link = MavlinkLink(connection_string)
        self.link.attitude_update.connect(self.on_attitude)
        self.link.position_update.connect(self.on_position)
        self.link.vfr_update.connect(self.on_vfr)
        self.link.wind_update.connect(self.on_wind)
        self.link.status_update.connect(self.on_status)
        self.link.mission_uploaded.connect(self.on_mission_uploaded)
        self.link.connection_status.connect(self.on_connection_status)
        self.link.command_feedback.connect(self.on_command_feedback)
        self.link.status_text_update.connect(self.messages_panel.add_message)
        self.link.status_text_update.connect(self.on_status_text)
        self.link.start()

    def on_connect_requested(self, connection_string):
        if not connection_string:
            self.command_label.setText(
                "Can't connect: missing or invalid connection details"
            )
            return
        old_link = getattr(self, "link", None)
        if old_link is not None:
            old_link.stop()
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self._connect_link(connection_string)

    def on_disconnect_requested(self):
        if self.link is not None:
            self.link.stop()
            self.link = None
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.command_label.setText("")
        self.ack_label.setText("")
        self._reset_vehicle_state()

    def on_tile_cache_limit(self, megabytes):
        self.tile_server.set_size_limit(int(megabytes) * 1024 * 1024)
        self._push_tile_cache_stats()

    def on_tile_cache_clear(self):
        self.tile_server.clear()
        self._push_tile_cache_stats()

    def on_terrain_cache_limit(self, megabytes):
        terrain_provider.set_cache_limit(int(megabytes) * 1024 * 1024)
        self._push_tile_cache_stats()

    def on_terrain_cache_clear(self):
        terrain_provider.clear_cache()
        self._push_tile_cache_stats()

    def _push_tile_cache_stats(self):
        tiles, used = self.tile_server.stats()
        self.map_view.update_tile_cache_stats(
            tiles, used, self.tile_server.size_limit_bytes
        )
        t_tiles, t_used = terrain_provider.cache_stats()
        self.map_view.update_terrain_cache_stats(
            t_tiles, t_used, terrain_provider.cache_limit_bytes()
        )

    def _reset_vehicle_state(self):
        """
        Drop everything that describes a live vehicle, so a disconnected
        session can't keep presenting the last frame as if it were current.
        The armed indicator matters most here - left alone it still reads
        ARMED in red after the link is gone.
        """
        self._armed = False
        self._ready_to_arm = False
        self._update_vehicle_state_label()
        self._set_flight_timer_running(False)
        self._seen_disarmed = False   # nothing known about a link not yet up
        self.arm_panel.set_prearm_reason("")
        self.arm_panel.set_armed_state(None)
        self.mode_panel.set_active_mode(None)
        self.horizon.set_ekf_status("white")
        self.horizon.set_vibe_status("white")
        self.horizon.set_battery_voltage(None)
        # Stop the terrain radar refreshing off the last known position.
        self.terrain_worker.clear_telemetry()

    def closeEvent(self, event):
        if self.link is not None:
            self.link.stop()
        self.terrain_worker.stop()
        self.adsb_worker.stop()
        self.tile_server.stop()
        event.accept()


def _selftest(connection_string):
    """
    Headless check that this build can actually open a MAVLink connection,
    run as:  MavGCS.exe --selftest [connection-string]

    Exists because a packaged build can start, draw its whole UI, and still
    be unable to connect at all - pymavlink resolves its dialect through a
    runtime import that a bundler can silently drop. Launching the GUI
    proves nothing about that; this does.

    Results go to a file as well as stdout: the shipped executable is built
    windowed, so it has no console to print to.
    """
    lines = [f"MavGCS {APP_VERSION} self-test", f"connection: {connection_string}"]
    ok = False
    try:
        from pymavlink import mavutil
        from mavlink_link import _open_mavlink_connection

        lines.append(f"pymavlink dialect module: {mavutil.mavlink.__name__}")
        master = _open_mavlink_connection(connection_string)
        lines.append("connection opened OK")
        hb = master.recv_match(type="HEARTBEAT", blocking=True, timeout=10)
        if hb is None:
            lines.append("no heartbeat within 10s (link opened, nothing transmitting)")
        else:
            lines.append(f"heartbeat received from sysid={hb.get_srcSystem()}")
        master.close()
        ok = True
    except Exception as e:
        lines.append(f"FAILED: {type(e).__name__}: {e}")

    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    report = "\n".join(lines)

    # Write the log FIRST: it's the part that has to survive, and it's UTF-8
    # so it copes with anything.
    try:
        (data_dir() / "selftest.log").write_text(report, encoding="utf-8")
    except OSError:
        pass

    # Printing is the fragile step. Windows consoles are typically not UTF-8
    # (cp1252 here), and OS error messages arrive in the system language -
    # a Turkish socket error was enough to kill the whole self-test on the
    # print alone, losing the diagnosis it existed to produce.
    try:
        print(report)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(report.encode(enc, errors="replace").decode(enc, errors="replace"))
    except OSError:
        pass  # windowed build with no console attached at all

    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        sys.exit(_selftest(args[1] if len(args) > 1 else "udpin:0.0.0.0:14550"))

    connection_string = args[0] if args else "udpin:0.0.0.0:14550"
    app = QApplication(sys.argv)
    window = MainWindow(connection_string)
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
