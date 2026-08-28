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

# This is MavGCS V1.8.0 - ESRI World Imagery + Imagery Hybrid map layers
# added alongside Google Hybrid/OpenStreetMap. See CHANGELOG.md.
APP_VERSION = "V1.8.0"

import sys
import math
import html
from datetime import datetime
from PySide6.QtCore import Signal, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QGridLayout, QFrame, QInputDialog,
    QPushButton, QGroupBox, QCheckBox, QMessageBox, QProgressBar,
    QScrollArea, QPlainTextEdit, QComboBox, QLineEdit, QStackedWidget,
)

from mavlink_link import MavlinkLink, PLANE_MODES
from artificial_horizon import ArtificialHorizon
from map_view import MapView


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


class ModePanel(QGroupBox):
    """Row of flight-mode buttons; highlights whichever mode is currently
    active based on telemetry, so it also works as a mode indicator."""

    # Order matches the request: Manual, FBWA, Cruise, Loiter, AUTO, RTL,
    # Takeoff, Autoland, Autotune (lands directly below RTL in the
    # 3-column grid), Guided.
    MODE_ORDER = ["MANUAL", "FBWA", "CRUISE", "LOITER", "AUTO", "RTL", "TAKEOFF", "AUTOLAND", "AUTOTUNE", "GUIDED"]

    mode_requested = Signal(str)

    NORMAL_STYLE = "font-size: 10px; padding: 3px 4px;"
    ACTIVE_STYLE = "background-color: #2a6; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    RTL_STYLE = "background-color: #a33; color: white; font-size: 10px; padding: 3px 4px;"

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

    def set_active_mode(self, mode_name):
        for name, btn in self.buttons.items():
            if name == mode_name:
                btn.setStyleSheet(self.ACTIVE_STYLE)
            elif name == "RTL":
                btn.setStyleSheet(self.RTL_STYLE)
            else:
                btn.setStyleSheet(self.NORMAL_STYLE)


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

        self.force_checkbox = QCheckBox("Force ARM (bypass pre-arm safety checks)")
        layout.addWidget(self.force_checkbox)

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
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip(
            "Click: clear the map.\nHold 3s: also erase the mission stored on the vehicle."
        )
        self.start_btn.clicked.connect(self.start_requested)
        self.clear_btn.clicked.connect(self.clear_requested)
        row.addWidget(self.count_label, stretch=1)
        row.addWidget(self.start_btn)
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

    PROTOCOLS = ["Serial", "TCP", "UDP"]
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
        self.protocol_combo.setFixedWidth(68)
        self.protocol_combo.currentTextChanged.connect(self._on_protocol_changed)

        # Field 1: host (TCP/UDP) vs a real serial-port dropdown (Serial)
        self.field1_stack = QStackedWidget()
        self.field1_stack.setFixedHeight(self.FIELD_HEIGHT)
        self.field1_stack.setFixedWidth(90)
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

        row.addWidget(self.protocol_combo)
        row.addWidget(self.field1_stack)
        row.addWidget(self.field2_stack)
        row.addWidget(self.connect_btn)
        row.addWidget(self.disconnect_btn)
        refresh_row.addWidget(self.refresh_btn)
        refresh_row.addStretch(1)

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
        elif protocol == "UDP":
            host = self.host_edit.text().strip()
            port = self.port_edit.text().strip()
            if not port:
                self.connect_requested.emit("")
                return
            if not host or host == "0.0.0.0":
                # No specific peer given - listen for incoming telemetry,
                # matching this app's default (e.g. ArduPilot SITL streams
                # to a UDP port rather than being connected to directly).
                connection_string = f"udpin:0.0.0.0:{port}"
            else:
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
        self.text_edit.setStyleSheet(
            "background-color: #16171a; color: white; "
            "font-family: Consolas, monospace; font-size: 10px; border: none;"
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

    def __init__(self, parent=None):
        super().__init__("Guided Control", parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 10, 6, 6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.speed_btn = QPushButton("Change Speed")
        self.altitude_btn = QPushButton("Change Altitude")
        btn_row.addWidget(self.speed_btn)
        btn_row.addWidget(self.altitude_btn)
        layout.addLayout(btn_row)

        self._last_speed = 15.0
        self._last_alt = 30.0

        self.speed_btn.clicked.connect(self._prompt_speed)
        self.altitude_btn.clicked.connect(self._prompt_altitude)

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

    def set_last_alt(self, alt):
        """Called from live telemetry so the dialog defaults to something sane."""
        self._last_alt = alt


class MainWindow(QMainWindow):
    def __init__(self, connection_string):
        super().__init__()
        self.setWindowTitle(f"MavGCS {APP_VERSION}")
        self.resize(1300, 920)

        self.horizon = ArtificialHorizon()
        self.horizon.setMinimumHeight(175)
        self.telemetry = TelemetryPanel()
        self.mode_panel = ModePanel()
        self.arm_panel = ArmDisarmPanel()
        self.preflight_cal_panel = PreflightCalPanel()
        self.guided_panel = GuidedControlPanel()
        self.map_view = MapView()
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
        self._last_alt = 30.0  # default guess used to pre-fill the fly-to dialog
        self._last_lat = None
        self._last_lon = None
        self._waypoint_queue = []  # list of (lat, lon) tuples, in click order

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
        left_layout.addWidget(self.status_label)
        left_layout.addWidget(self.command_label)
        left_layout.addWidget(self.ack_label)
        left_layout.addWidget(self.arm_panel)
        left_layout.addWidget(self.preflight_cal_panel)
        left_layout.addWidget(self.mode_panel)
        left_layout.addWidget(self.guided_panel)
        left_layout.addWidget(self.horizon, stretch=1)
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
        self.waypoint_panel.mode_toggled.connect(self.on_waypoint_mode_toggled)
        self.waypoint_panel.start_requested.connect(self.on_start_mission)
        self.waypoint_panel.clear_requested.connect(self.on_clear_waypoints)
        self.waypoint_panel.clear_mission_requested.connect(self.on_clear_vehicle_mission)
        # These two go through wrapper methods rather than binding
        # directly to self.link.set_mode/preflight_calibration - a direct
        # bind captures whatever self.link IS at connect() time, and
        # would silently keep pointing at a stale, stopped link object
        # after any reconnect (self.link gets replaced with a new
        # instance, but the old binding doesn't follow it).
        self.mode_panel.mode_requested.connect(self.on_mode_requested)
        self.arm_panel.arm_requested.connect(self.on_arm_requested)
        self.arm_panel.force_disarm_requested.connect(self.on_force_disarm)
        self.preflight_cal_panel.calibration_requested.connect(self.on_calibration_requested)
        self.guided_panel.speed_requested.connect(self.on_speed_requested)
        self.guided_panel.altitude_requested.connect(self.on_altitude_requested)

    def on_attitude(self, roll, pitch, yaw):
        self.horizon.set_attitude(roll, pitch, yaw)
        self.telemetry.set_value("roll_deg", f"{math.degrees(roll):.2f}")
        self.telemetry.set_value("pitch_deg", f"{math.degrees(pitch):.2f}")
        # ATTITUDE.yaw is in radians over -pi..+pi (MAVLink spec), so a
        # raw degrees() conversion goes negative past 180 instead of
        # continuing to 360 - wrap it, same fix as the wind direction bug.
        self.telemetry.set_value("yaw_deg", f"{math.degrees(yaw) % 360:.2f}")

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

    def on_vfr(self, airspeed, groundspeed, climb):
        self.telemetry.set_value("airspeed", f"{airspeed:.2f}")
        self.telemetry.set_value("groundspeed", f"{groundspeed:.2f}")
        self.telemetry.set_value("vspeed_mps", f"{climb:.2f}")
        self.horizon.set_airspeed(airspeed)

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

    def on_status(self, status_dict):
        for key, value in status_dict.items():
            self.telemetry.set_value(key, value)
        if "mode" in status_dict:
            self.mode_panel.set_active_mode(status_dict["mode"])
        if "armed" in status_dict:
            self.arm_panel.set_armed_state(status_dict["armed"] == "YES")
        if "ekf_color" in status_dict:
            self.horizon.set_ekf_status(status_dict["ekf_color"])
        if "vibe_color" in status_dict:
            self.horizon.set_vibe_status(status_dict["vibe_color"])
        if "battery_voltage" in status_dict:
            self.horizon.set_battery_voltage(float(status_dict["battery_voltage"]))

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

    def on_waypoint_mode_toggled(self, enabled):
        self.map_view.set_waypoint_mode(enabled)

    def on_waypoint_added(self, lat, lon):
        self._waypoint_queue.append((lat, lon))
        self.waypoint_panel.set_count(len(self._waypoint_queue))

    def on_clear_waypoints(self):
        self._waypoint_queue = []
        self.map_view.clear_waypoints()
        self.waypoint_panel.set_count(0)

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
        link.upload_and_start_mission(self._waypoint_queue, alt)
        # A leftover Fly-to-Here target marker is a separate thing from
        # the waypoint queue - clear it too, since starting a mission
        # supersedes any pending single-point target.
        self.map_view.clear_target()
        # Keep the markers on the map as a visible record of what was
        # sent - only remove them when the user explicitly clicks Clear.
        self._waypoint_queue = []
        self.map_view.commit_waypoints()
        self.waypoint_panel.set_count(0)

    @staticmethod
    def _split_connection_string(connection_string):
        """Parse a pymavlink connection string into (protocol, host, port)
        to pre-fill the Connection panel with whatever was passed on the
        command line, so it starts showing the truth instead of a
        hardcoded default."""
        parts = connection_string.split(":")
        if connection_string.startswith("tcp:") and len(parts) == 3:
            return ("TCP", parts[1], parts[2])
        if connection_string.startswith(("udpin:", "udpout:", "udp:")) and len(parts) == 3:
            return ("UDP", parts[1], parts[2])
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
        self.link.connection_status.connect(self.on_connection_status)
        self.link.command_feedback.connect(self.on_command_feedback)
        self.link.status_text_update.connect(self.messages_panel.add_message)
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

    def closeEvent(self, event):
        if self.link is not None:
            self.link.stop()
        event.accept()


def main():
    connection_string = sys.argv[1] if len(sys.argv) > 1 else "udpin:0.0.0.0:14550"
    app = QApplication(sys.argv)
    window = MainWindow(connection_string)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
