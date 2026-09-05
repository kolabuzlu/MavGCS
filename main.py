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

# This is MavGCS V1.15.0 - a flight summary when the vehicle disarms:
# time, distance, speeds, altitudes and battery use for the flight just
# flown. See CHANGELOG.md.
APP_VERSION = "V1.18.2"

import sys
import os
import math
import html
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Signal, QTimer, Qt
from PySide6.QtGui import (QIcon, QImage, QPainter, QPixmap, QPen, QColor,
                           QDesktopServices)
from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QPoint, QPointF,
                            QSize, QUrl, QStandardPaths)
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QGridLayout, QFrame, QInputDialog,
    QPushButton, QGroupBox, QCheckBox, QMessageBox, QProgressBar,
    QScrollArea, QPlainTextEdit, QComboBox, QLineEdit, QStackedWidget,
    QSizePolicy,
    QDialog, QFormLayout, QDoubleSpinBox, QDialogButtonBox, QMenu,
    QFileDialog, QRadioButton, QButtonGroup,
)

from pymavlink import mavutil     # for the SYS_STATUS sensor bit names
from mavlink_link import MavlinkLink, PLANE_MODES
from artificial_horizon import ArtificialHorizon
from map_view import MapView
import terrain_provider
from terrain_provider import TerrainRadarWorker
from adsb_provider import AdsbWorker
from tile_cache import TileCacheServer
from fpv_view import FpvView
from app_paths import data_dir, load_settings, resource_path, save_setting
from update_check import UpdateChecker, UpdateDownloader, RELEASES_PAGE
from track_export import write_track


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


def _hms(seconds) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


class ReturnHomeEstimator:
    """Can the aircraft still reach home on what is left in the pack?

    The naive answer - consumed mAh divided by distance flown, scaled to
    the distance home - is wrong in a way that flatters the aircraft. The
    climb out is in that average, loitering contributes consumption but
    no distance, and neither says anything about the leg home.

    Wind is what actually decides it, and it is the part a pilot is worst
    at eyeballing. Five kilometres downwind, an eighteen metre a second
    cruise and a ten metre a second headwind home is a groundspeed of
    eight: the return takes well over twice as long as the outbound leg
    that felt fine. So the time home is worked out through the wind
    triangle from the wind, the bearing home and the current airspeed,
    including the case where the crosswind alone exceeds the airspeed and
    the track cannot be held at all.

    Cost is then the trailing average current over that time. Average
    rather than instantaneous because a single sample swings with every
    gust and throttle nudge; current rather than consumed-so-far because
    what matters is the rate the aircraft is burning at now.

    What it does NOT account for, and no caller should pretend otherwise:
    the climb back to a safe altitude if the aircraft is low, terrain in
    the way, the circuit and landing at the end, and any change of wind
    on the way. It answers "does the straight line home fit in what is
    left", which is a floor, not a promise.
    """

    # How much recent current draw the average covers. Long enough to
    # ride out gusts and throttle nudges, short enough to follow a real
    # change in how hard the aircraft is working.
    CURRENT_WINDOW_S = 30.0

    # Below this airspeed the wind triangle stops meaning anything, and
    # on the ground airspeed reads near zero anyway.
    MIN_AIRSPEED_MPS = 3.0

    # A groundspeed home at or under this is not progress. Guards the
    # division and the case of being blown backwards.
    MIN_GROUNDSPEED_MPS = 1.0

    # Nearer than this, the question is not worth asking and the bearing
    # home is noise.
    MIN_DISTANCE_M = 50.0

    # Headroom over the estimate before it will say a plain yes. The
    # estimate is a floor - it knows nothing of the climb back up, the
    # circuit, or the wind changing on the way - so an answer with only a
    # little margin should not read as comfortable.
    #
    # Half the trip again, rather than the third it started at: MARGINAL
    # is the useful state, the one that says start thinking about home,
    # and it earns its keep by arriving while there is still something to
    # be done about it. OK on a whisker of margin is the answer nobody
    # needs.
    COMFORT_MARGIN = 0.50

    # Everything feeding the ratio moves: the wind estimate wanders, the
    # airspeed breathes, the current average follows the throttle. Sat on
    # a threshold, that is enough to flip the verdict every second or two,
    # and a warning that blinks is one you learn to ignore.
    #
    # So a worse verdict is sticky. Climbing back out of it needs the
    # ratio to clear the threshold by this much, where falling in needed
    # only to touch it.
    VERDICT_HYSTERESIS = 0.15

    # And a change has to persist before it is shown. Asymmetric on
    # purpose: bad news should arrive at once, good news can wait to be
    # sure of itself.
    WORSEN_HOLD_S = 1.0
    IMPROVE_HOLD_S = 5.0

    # Worst to best, for deciding which of the two holds above applies.
    _SEVERITY = {"no": 0, "marginal": 1, "yes": 2}

    def __init__(self):
        self.capacity_mah = None
        self.reserve_mah = 0.0
        self.consumed_mah = None
        self.dist_home_m = None
        self.home_bearing_deg = None
        self.wind_from_deg = None
        self.wind_speed_mps = None
        self.airspeed_mps = None
        self._samples = []          # (monotonic seconds, amps)
        self._state = None          # the verdict currently being shown
        self._pending = None        # one waiting out its hold
        self._pending_since = 0.0

    # ---------------------------------------------------------- inputs

    def set_limits(self, capacity_mah, low_mah):
        self.capacity_mah = capacity_mah if capacity_mah > 0 else None
        self.reserve_mah = max(0.0, low_mah)

    def set_power(self, amps, consumed_mah):
        self.consumed_mah = consumed_mah
        now = time.monotonic()
        self._samples.append((now, amps))
        cutoff = now - self.CURRENT_WINDOW_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.pop(0)

    def set_distance(self, metres):
        self.dist_home_m = metres

    def set_home_bearing(self, deg):
        # -1 is the "too close to say" sentinel the compass arrow uses.
        self.home_bearing_deg = deg if deg >= 0 else None

    def set_wind(self, from_deg, speed_mps):
        self.wind_from_deg = from_deg
        self.wind_speed_mps = speed_mps

    def set_airspeed(self, mps):
        self.airspeed_mps = mps

    def reset(self):
        self.__init__()

    # ------------------------------------------------------------ maths

    def average_amps(self):
        if not self._samples:
            return None
        return sum(a for _, a in self._samples) / len(self._samples)

    def groundspeed_home(self):
        """Groundspeed the aircraft would make on the bearing home.

        None when it could not hold that track at all - when the
        crosswind alone matches the airspeed there is no heading that
        flies the course, and no honest number to report.
        """
        v = self.airspeed_mps
        if v is None or v < self.MIN_AIRSPEED_MPS:
            return None
        if (self.home_bearing_deg is None or self.wind_from_deg is None
                or self.wind_speed_mps is None):
            return v                    # no wind known: still air is the
                                        # best guess available
        delta = math.radians(self.wind_from_deg - self.home_bearing_deg)
        head = self.wind_speed_mps * math.cos(delta)
        cross = self.wind_speed_mps * math.sin(delta)
        if abs(cross) >= v:
            return None
        return math.sqrt(v * v - cross * cross) - head

    def needed_mah(self):
        """What the leg home costs, at the present rate of burn."""
        amps = self.average_amps()
        gs = self.groundspeed_home()
        if amps is None or gs is None or gs < self.MIN_GROUNDSPEED_MPS:
            return None
        if self.dist_home_m is None:
            return None
        hours = (self.dist_home_m / gs) / 3600.0
        return amps * hours * 1000.0

    def available_mah(self):
        """What is left above the reserve the aircraft failsafes on."""
        if self.capacity_mah is None or self.consumed_mah is None:
            return None
        return self.capacity_mah - self.consumed_mah - self.reserve_mah

    def verdict(self):
        """(state, needed_mah, available_mah).

        state is 'off' when there is not enough to say anything, else
        'yes', 'marginal' or 'no'.
        """
        if self.dist_home_m is not None and self.dist_home_m < self.MIN_DISTANCE_M:
            # No cost to quote for a leg that is not worth flying, but
            # what is in the pack is still worth showing - it is the same
            # number the box reports everywhere else, and dropping it
            # only here made sitting on the strip look like knowing less
            # than sitting in the hangar.
            return "home", None, self.available_mah()
        need = self.needed_mah()
        have = self.available_mah()
        if need is None or have is None or need <= 0.0:
            # Nothing to judge, so nothing to hold on to either - the next
            # real reading should be believed rather than argued with.
            self._state = self._pending = None
            return "off", need, have
        return self._settle(have / need), need, have

    def _raw_state(self, ratio):
        """The verdict this ratio calls for, given the one on show.

        The thresholds are not fixed: leaving a worse verdict costs more
        than falling into it, which is what stops the two swapping back
        and forth while the ratio hovers on a line.
        """
        ok = 1.0 + self.COMFORT_MARGIN
        current = self._SEVERITY.get(self._state, -1)
        # Climbing to a better verdict has to clear the line by the
        # hysteresis; staying where you are only has to touch it.
        ok_bar = ok if current >= self._SEVERITY["yes"] else ok + self.VERDICT_HYSTERESIS
        marginal_bar = (1.0 if current >= self._SEVERITY["marginal"]
                        else 1.0 + self.VERDICT_HYSTERESIS)
        if ratio >= ok_bar:
            return "yes"
        if ratio >= marginal_bar:
            return "marginal"
        return "no"

    def _settle(self, ratio):
        """Hold a change briefly before showing it."""
        raw = self._raw_state(ratio)
        if self._state is None:
            # First answer of the flight: say it straight away, there is
            # nothing yet for it to flicker against.
            self._state = raw
            self._pending = None
            return raw
        now = time.monotonic()
        if raw == self._state:
            self._pending = None
            return self._state
        if raw != self._pending:
            self._pending = raw
            self._pending_since = now
            return self._state
        worse = self._SEVERITY[raw] < self._SEVERITY[self._state]
        hold = self.WORSEN_HOLD_S if worse else self.IMPROVE_HOLD_S
        if now - self._pending_since >= hold:
            self._state = raw
            self._pending = None
        return self._state


class FlightStats:
    """Accumulates one flight's numbers, from arming to disarming.

    Totals (time, distance) span the whole armed period. Averages only
    count samples taken while actually airborne - idling on the ground for
    two minutes before takeoff would otherwise drag the average airspeed
    down far enough to be meaningless.
    """

    # What counts as airborne. Deliberately generous: this only decides
    # which samples feed the averages, not what gets reported.
    AIRBORNE_SPEED_MPS = 3.0
    AIRBORNE_ALT_M = 3.0

    # ---- balance, from the pitch integrator -----------------------------
    # ArduPilot's own guidance: a pitch integrator held consistently above
    # centre means the aircraft is carrying up elevator to stay level,
    # which is what nose-heavy looks like; below centre means the reverse.
    #
    # The elevator OUTPUT is read rather than the pitch integrator. Many
    # ArduPilot aircraft fly with SERVO_AUTO_TRIM enabled, which slowly
    # moves the servo trim to absorb whatever the integrator is holding.
    # On those aircraft the integrator settles back to zero while the
    # aircraft is still nose heavy - the offset has simply moved into the
    # trim. The servo output includes the trim, so it stays true either
    # way.
    #
    # That only holds in steady, hands-off, level flight, so a sample is
    # taken nowhere else. Averaging across turns, climbs and manual flying
    # would produce a confident number that means nothing at all - the one
    # outcome worse than saying "not enough evidence".
    BALANCE_MODES = ("CRUISE", "FBWB", "AUTO")
    BALANCE_MAX_ROLL_DEG = 5.0
    BALANCE_MAX_CLIMB_MPS = 0.5
    # The thrust line rarely passes through the centre of gravity, so
    # power changes pitch and the elevator has to answer for it. Only at
    # the cruise power the airframe was trimmed for does the elevator
    # position speak about the balance alone.
    #
    # Measured in SITL cruise: 82% of samples sat within 5 points of
    # TRIM_THROTTLE, and widening the window to 20 only reached 85 - the
    # rest being excursions all the way to the throttle limits, which are
    # exactly the moments worth throwing away. Ten points costs almost
    # nothing here and leaves room for a windier day.
    BALANCE_THROTTLE_TOL = 10.0
    # After a mode change the controller is still settling to its new
    # working point, and says more about the transition than the balance.
    BALANCE_SETTLE_S = 20.0
    # Less steady flight than this is not evidence of anything.
    BALANCE_MIN_SPAN_S = 30.0
    # Only the most recent stretch of level flight is averaged. The
    # centre of gravity is not a constant of the flight - fuel burns off,
    # payload shifts, and on a test rig it is moved deliberately - so
    # averaging everything since takeoff would take longer and longer to
    # notice a change and eventually never notice one at all.
    #
    # Measured from the newest sample rather than from the clock, because
    # sampling only happens in steady cruise: a few circuits in between
    # should not empty the window.
    #
    # 45s rather than 60: the verdict settles a third quicker after the
    # balance shifts, and there is still half again the minimum span to
    # average over. At the measured +-6us of cruise scatter, 45 samples
    # pin the mean to under 1us, so nothing is given away for the speed.
    BALANCE_WINDOW_S = 45.0
    # What matters is an offset held CONSISTENTLY, so consistency is
    # measured rather than just the size of the average. In turbulence the
    # elevator is busy either side of where it is held, and a small offset
    # inside that noise is correctly refused rather than called.
    BALANCE_AGREEMENT = 0.8
    # The integrator is asked first, and answers whenever it is holding
    # elevator. Measured on ArduPlane 4.8 in settled cruise: PIDP.I sat at
    # -11.8 with a spread of 1.3, so its working range is tens of units,
    # not fractions of one. About 45 centidegrees of surface per unit, so
    # a unit is roughly 5us of elevator on a 1000-2000 output.
    # Set from flight on 2026-09-03: 15us of elevator either side of
    # centre, about 1.4 degrees of surface. Both deadbands describe the
    # same physical deflection, using the measured 5us per integrator
    # unit, so neither tier is more eager than the other.
    #
    # This can go lower safely. Noise cannot manufacture a verdict at any
    # deadband, because the agreement test independently requires the
    # offset to sit on one side for 80% of a 30 second window; measured
    # SITL cruise scatter is about +-6us, which over 30 samples leaves
    # the mean good to around 1us.
    BALANCE_I_DEADBAND = 3.0        # ~15us of elevator

    # Second question, asked only when the integrator has gone quiet.
    # SERVO_AUTO_TRIM can shift the elevator centre by about 100us either
    # way and no further, so an imbalance it absorbed COMPLETELY - leaving
    # the integrator at nothing - is bounded by that, and is reported as
    # slight. Anything larger saturates the trim and the remainder goes
    # back into the integrator, where the first question catches it.
    BALANCE_AUTOTRIM_US = 100.0
    BALANCE_ELEV_DEADBAND_US = 15.0

    # The marker is placed by the total elevator being held, in
    # microseconds, WHICHEVER signal chose the words. The words say which
    # signal spoke; the position says how much elevator the aeroplane is
    # actually carrying, so the two cannot contradict each other and the
    # scale runs smoothly from one end to the other.
    #
    # Half deflection falls exactly where SERVO_AUTO_TRIM runs out of
    # authority, so a marker past halfway means the trim is saturated and
    # the controller is making up the rest.
    BALANCE_MARKER_FULL_US = 2 * BALANCE_AUTOTRIM_US
    # Only for placing the marker when the elevator could not be read at
    # all. A unit of PIDP.I is about 45 centidegrees of surface, roughly
    # 5us on a 1000-2000 output.
    BALANCE_US_PER_I = 5.0
    # Below this a report is not worth showing - an arm/disarm on the bench
    # is not a flight.
    MIN_REPORTABLE_S = 30.0

    def __init__(self):
        # Belongs to the aircraft rather than the flight, so it is set
        # here and not in reset(): a new flight must not forget it.
        self.trim_throttle = None
        self.reset()

    def reset(self):
        self.start_time = None
        self.end_time = None
        self.partial = False        # we joined after takeoff
        self.ever_airborne = False
        self._air_samples = 0
        self._ias_sum = 0.0
        self._gs_sum = 0.0
        self.ias_max = 0.0
        self.gs_max = 0.0
        self.alt_max = 0.0          # relative to home
        self.amsl_max = None
        self.dist_home_max = 0.0
        self.climb_max = 0.0
        self.sink_max = 0.0         # most negative climb, stored positive
        self.wind_max = 0.0
        self.volt_start = None
        self.volt_end = None
        self.volt_min = None
        self.mah_start = None
        self.mah_end = None
        self.distance_m = 0.0
        # Seconds of this flight that nobody recorded, because the link was
        # down. Only ever non-zero on a merged view.
        self.unrecorded_s = 0.0
        self.suspended = False
        self._last_pos = None
        # The flown path, for the KMZ export: (unix time, lat, lon, AMSL,
        # altitude above home). Wall-clock rather than monotonic, because
        # Google Earth wants real timestamps to animate against. Bounded
        # only as a guard against something pathological - a two-hour
        # flight at 2Hz is about 14,000 points, a few hundred kilobytes.
        self.track = []
        self._amsl = None
        self.balance_samples = []   # (monotonic time, us off centre, fraction)
        self.integrator_samples = []    # (monotonic time, PIDP.I)
        self._roll_deg = 0.0
        self._climb = 0.0
        self._mode = None
        self._mode_since = None
        self._throttle = None
        self._alt = 0.0
        self._gs = 0.0

    # ---- lifecycle ------------------------------------------------------
    def start(self, partial: bool):
        self.reset()
        self.start_time = time.monotonic()
        self.partial = partial

    def finish(self):
        self.end_time = time.monotonic()

    def suspend(self):
        """The link went while still armed.

        Stop accumulating, but keep everything: the aircraft may well still
        be flying the same sortie, and if it turns out to be the same one we
        do not want to have thrown this away. Whether it IS the same flight
        is not decided here.
        """
        self.end_time = time.monotonic()
        self.suspended = True

    # Beyond this, an earlier segment is not plausibly the same flight and
    # is not offered for merging at all.
    MERGE_WINDOW_S = 30 * 60

    def mergeable_earlier(self, segments):
        """The run of earlier segments that plausibly belong to this flight.

        Walks backwards from this flight: each segment must end before the
        next one starts, separated by a gap short enough to be a dropout
        rather than a separate sortie. A marginal radio link does not fail
        once cleanly - it stutters repeatedly - so this has to chain any
        number of them, not just the most recent.

        Deliberately only a plausibility test, not a verdict. Nothing in the
        protocol distinguishes a dropout from a landing and a relaunch, so
        the software does not decide; it offers, with the numbers on screen.

        Returns oldest first. Empty if this flight never left the ground -
        an arm/disarm on the bench is not the continuation of anything - or
        if this GCS watched the vehicle sitting disarmed before this flight
        began, which is proof that whatever came earlier had already ended.
        That case is an observation rather than a guess, so it is settled
        here rather than being put to the user.
        """
        if not self.ever_airborne or self.start_time is None:
            return []
        if not self.partial:
            # partial is set only when we rejoined an aircraft that was
            # already armed. A flight that began after we saw it disarmed
            # cannot be the continuation of anything.
            return []
        candidates = [
            seg for seg in segments
            if seg.suspended and seg.ever_airborne
            and seg.start_time is not None and seg.end_time is not None
        ]
        chain = []
        next_start = self.start_time
        for seg in sorted(candidates, key=lambda x: x.start_time, reverse=True):
            gap = next_start - seg.end_time
            if not 0 <= gap <= self.MERGE_WINDOW_S:
                break
            chain.append(seg)
            next_start = seg.start_time
        return list(reversed(chain))

    def merged_with(self, earlier_segments):
        """A combined view of earlier segments and this one, oldest first.

        Time spans the first arming to this disarming, gaps included: the
        aircraft really was flying throughout. Distance and the maxima cover
        only what was recorded, so the unrecorded time is reported on its
        own line rather than quietly folded in - and the straight lines
        across the gaps are NOT counted as distance flown, because nobody
        measured those paths.
        """
        parts = list(earlier_segments) + [self]
        first = parts[0]
        m = FlightStats()
        m.start_time = first.start_time
        m.end_time = self.end_time
        m.partial = first.partial
        m.ever_airborne = True
        m.unrecorded_s = max(
            0.0, (m.end_time - m.start_time) - sum(p.duration_s for p in parts)
        )
        for p in parts:
            m._air_samples += p._air_samples
            m._ias_sum += p._ias_sum
            m._gs_sum += p._gs_sum
            m.ias_max = max(m.ias_max, p.ias_max)
            m.gs_max = max(m.gs_max, p.gs_max)
            m.alt_max = max(m.alt_max, p.alt_max)
            m.dist_home_max = max(m.dist_home_max, p.dist_home_max)
            m.climb_max = max(m.climb_max, p.climb_max)
            m.sink_max = max(m.sink_max, p.sink_max)
            m.wind_max = max(m.wind_max, p.wind_max)
            m.distance_m += p.distance_m
            m.track.extend(p.track)
            m.balance_samples.extend(p.balance_samples)
            m.integrator_samples.extend(p.integrator_samples)
            if p.amsl_max is not None:
                m.amsl_max = p.amsl_max if m.amsl_max is None else max(m.amsl_max, p.amsl_max)
            if p.volt_start is not None and m.volt_start is None:
                m.volt_start = p.volt_start
            if p.volt_end is not None:
                m.volt_end = p.volt_end
            if p.volt_min is not None:
                m.volt_min = p.volt_min if m.volt_min is None else min(m.volt_min, p.volt_min)
            if p.mah_start is not None and m.mah_start is None:
                m.mah_start = p.mah_start
            if p.mah_end is not None:
                m.mah_end = p.mah_end
        return m

    @property
    def duration_s(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time is not None else time.monotonic()
        return end - self.start_time

    @property
    def running(self) -> bool:
        return self.start_time is not None and self.end_time is None

    def worth_reporting(self) -> bool:
        return self.ever_airborne and self.duration_s >= self.MIN_REPORTABLE_S

    @property
    def _airborne(self) -> bool:
        return self._gs >= self.AIRBORNE_SPEED_MPS or self._alt >= self.AIRBORNE_ALT_M

    # ---- sample feeds ---------------------------------------------------
    def on_vfr(self, airspeed, groundspeed, climb, throttle=None):
        if not self.running:
            return
        self._gs = groundspeed
        self._climb = climb
        if throttle is not None:
            self._throttle = throttle
        if self._airborne:
            self.ever_airborne = True
            self._air_samples += 1
            self._ias_sum += airspeed
            self._gs_sum += groundspeed
            self.ias_max = max(self.ias_max, airspeed)
            self.gs_max = max(self.gs_max, groundspeed)
            self.climb_max = max(self.climb_max, climb)
            self.sink_max = max(self.sink_max, -climb)

    TRACK_MAX_POINTS = 250_000

    def on_position(self, lat, lon, alt_relative):
        if not self.running:
            return
        self._alt = alt_relative
        if len(self.track) < self.TRACK_MAX_POINTS:
            self.track.append((time.time(), lat, lon, self._amsl, alt_relative))
        self.alt_max = max(self.alt_max, alt_relative)
        # Ground track, summed between fixes. More honest than integrating
        # groundspeed, which drifts whenever telemetry stutters.
        if self._last_pos is not None:
            self.distance_m += self._haversine_m(
                self._last_pos[0], self._last_pos[1], lat, lon
            )
        self._last_pos = (lat, lon)

    def on_attitude(self, roll_deg):
        """Roll only - the balance check needs to know the wings are level."""
        self._roll_deg = roll_deg

    def _steady(self):
        """Whether right now is a moment worth sampling at all.

        Every rejection here is deliberate: a sample taken in a turn, a
        climb, or a mode where the pilot is flying the elevator says
        nothing about where the centre of gravity is.
        """
        if not self.running or not self.ever_airborne:
            return False
        if self._mode not in self.BALANCE_MODES:
            return False
        if self._mode_since is None:
            return False
        if time.monotonic() - self._mode_since < self.BALANCE_SETTLE_S:
            return False
        if abs(self._roll_deg) > self.BALANCE_MAX_ROLL_DEG:
            return False
        if abs(self._climb) > self.BALANCE_MAX_CLIMB_MPS:
            return False
        if (self.trim_throttle is not None and self._throttle is not None
                and abs(self._throttle - self.trim_throttle)
                > self.BALANCE_THROTTLE_TOL):
            return False
        return True

    def on_pitch_integrator(self, value):
        """One PID_TUNING pitch sample, kept only if the flight is steady."""
        if self._steady():
            self.integrator_samples.append((time.monotonic(), float(value)))
            self._trim_window(self.integrator_samples)

    def on_elevator(self, offset_us, fraction):
        """One elevator reading, kept only if the flight is steady."""
        if self._steady():
            self.balance_samples.append(
                (time.monotonic(), float(offset_us), float(fraction)))
            self._trim_window(self.balance_samples)

    def balance_status(self):
        """(state, text, marker position) for the indicator on the map.

        Deliberately shows the collecting as well as the answer: without
        it the readout would sit blank for a whole circuit and look
        broken, when in fact it is waiting for the level flight that makes
        the number mean anything.
        """
        # Progress comes from whichever signal has watched for longest:
        # the integrator arrives ten times faster than the elevator, and
        # on some aircraft one of the two never arrives at all.
        spans = [s[-1][0] - s[0][0]
                 for s in (self.integrator_samples, self.balance_samples) if s]
        if not spans:
            return ("waiting", "Waiting for level cruise", 0.0)
        span = max(spans)
        verdict = self.balance_verdict()
        if verdict is None:
            # Progress towards the minimum span a verdict needs. Clamped
            # below 100 because reaching it is what ends this state: a
            # bar sitting at 100% would look stuck rather than nearly
            # done. It can read over 100 otherwise - one signal can pass
            # the span while still holding too few samples to summarise.
            pct = max(0.0, min(99.0, span / self.BALANCE_MIN_SPAN_S * 100.0))
            return ("sampling", f"Evaluating CG {pct:.0f}%", 0.0)
        headline, _, shift, number = verdict
        # The marker position is decided in balance_verdict, where it is
        # known WHICH signal answered: a unit of integrator and a
        # microsecond of elevator are not the same size.
        text = f"{headline} ({number})" if number else headline
        return (headline, text, shift)

    def balance_verdict(self):
        """(headline, detail, marker, number), or None if nothing to say.

        Two questions in order, because the two signals fail in opposite
        places. The pitch integrator states the balance directly, but goes
        quiet on an aircraft that SERVO_AUTO_TRIM has trimmed level. The
        elevator position keeps the offset either way, but is the coarser
        read. So the integrator is asked first and the elevator only when
        it has nothing to say.

        Returning None is a real answer: a flight with no settled level
        cruise cannot tell you anything about the balance, and should say
        so rather than average whatever it happened to see.
        """
        # Both signals are summarised up front. The words come from
        # whichever answers first, but the marker is placed from the
        # elevator in either case, so it has to be in hand either way.
        # Merged flights can carry more than the window, so trim here
        # too: this is the point where correctness actually matters.
        self._trim_window(self.integrator_samples)
        self._trim_window(self.balance_samples)
        i_span, i_mean, i_agree = self._summarise(
            [(t, v) for t, v in self.integrator_samples])
        e_span, e_mean_us, e_agree = self._summarise(
            [(t, us) for t, us, _ in self.balance_samples])

        # First question: is the integrator holding elevator? While it is,
        # it states the balance directly and nothing else is needed.
        if (i_span is not None and abs(i_mean) >= self.BALANCE_I_DEADBAND
                and i_agree >= self.BALANCE_AGREEMENT):
            detail = (f"integrator {i_mean:+.1f} over {i_span:.0f}s level, "
                      f"{i_agree * 100:.0f}% same side")
            return ("Nose heavy" if i_mean > 0 else "Tail heavy", detail,
                    self._marker(e_mean_us, i_mean, 1 if i_mean > 0 else -1),
                    f"{i_mean:+.1f}")

        # Second question, and only now: the integrator has gone quiet, so
        # either the aircraft is in balance or SERVO_AUTO_TRIM has taken
        # the offset into the elevator centre. The elevator itself tells
        # the two apart.
        if (e_span is not None and abs(e_mean_us) >= self.BALANCE_ELEV_DEADBAND_US
                and e_agree >= self.BALANCE_AGREEMENT):
            detail = (f"elevator {e_mean_us:+.0f}us over {e_span:.0f}s level, "
                      f"{e_agree * 100:.0f}% same side, integrator quiet")
            return ("Slightly nose heavy" if e_mean_us > 0
                    else "Slightly tail heavy", detail,
                    self._marker(e_mean_us, i_mean,
                                 1 if e_mean_us > 0 else -1),
                    f"{e_mean_us:+.0f}us")

        # Neither is saying anything, which is what balance looks like.
        span = i_span if i_span is not None else e_span
        if span is None:
            return None
        return ("Balanced", f"nothing held over {span:.0f}s level", 0.0, "")

    def _marker(self, e_mean_us, i_mean, verdict_sign):
        """Where the marker sits, on one scale whoever answered.

        Held elevator in microseconds is the honest common quantity: the
        servo output already contains the trim AND whatever the
        controller is adding, so it measures the same physical thing in
        both cases. Placing one verdict by integrator units and the other
        by microseconds made a small nose-heavy verdict sit closer to
        centre than a larger slight one, which is nonsense to look at.

        If the elevator contradicts the signal that chose the words - it
        can, briefly, while autotrim is still catching up - the deciding
        signal places the marker instead. A caption saying nose heavy
        beside a marker sitting aft is worse than either alone.
        """
        us = None
        if e_mean_us is not None and (e_mean_us > 0) == (verdict_sign > 0):
            us = e_mean_us
        elif i_mean is not None:
            us = i_mean * self.BALANCE_US_PER_I
        elif e_mean_us is not None:
            us = e_mean_us
        if us is None:
            return 0.0
        # Positive is up elevator, which is nose heavy, and the nose is
        # drawn to the left - so the marker moves the other way.
        return max(-1.0, min(1.0, -us / self.BALANCE_MARKER_FULL_US))

    def _trim_window(self, samples):
        """Drop whatever has aged out of the trailing window."""
        if not samples:
            return
        cutoff = samples[-1][0] - self.BALANCE_WINDOW_S
        i = 0
        while i < len(samples) and samples[i][0] < cutoff:
            i += 1
        if i:
            del samples[:i]

    def _summarise(self, pairs):
        """(span, mean, agreement) for one signal, or Nones if too thin.

        Refusing to summarise is a real answer: a handful of samples over
        a few seconds cannot show that anything is being held steadily.
        """
        if len(pairs) < 5:
            return None, None, None
        span = pairs[-1][0] - pairs[0][0]
        if span < self.BALANCE_MIN_SPAN_S:
            return None, None, None
        values = [v for _, v in pairs]
        mean = sum(values) / len(values)
        # A sample sitting exactly on neutral is on neither side, so it
        # votes for neither. Writing this as (v > 0) == (mean > 0) over
        # every sample silently handed those to the negative side - zero
        # is not greater than zero, and neither is a negative mean - so
        # they counted against a nose-heavy verdict and for a tail-heavy
        # one. PWM is whole microseconds, so an elevator near centre sits
        # exactly on 1500 often, and a nose-heavy trim could read
        # Balanced while its mirror image read tail heavy.
        signed = [v for v in values if v != 0.0]
        agree = (sum(1 for v in signed if (v > 0) == (mean > 0)) / len(signed)
                 if signed else 0.0)
        return span, mean, agree

    def on_wind(self, speed_mps):
        if self.running:
            self.wind_max = max(self.wind_max, speed_mps)

    def on_status(self, status):
        if not self.running:
            return
        if "mode" in status and status["mode"] != self._mode:
            # The settle timer runs from the mode change, not from arming:
            # entering cruise is when the integrator starts winding to its
            # new working point.
            self._mode = status["mode"]
            self._mode_since = time.monotonic()
        if "amsl_alt" in status:
            try:
                amsl = float(status["amsl_alt"])
                # Kept for the track: Google Earth places an absolute
                # altitude correctly over its own terrain, where a
                # height above home would float or sink with the ground.
                self._amsl = amsl
                self.amsl_max = amsl if self.amsl_max is None else max(self.amsl_max, amsl)
            except ValueError:
                pass
        if "dist_home" in status:
            try:
                self.dist_home_max = max(self.dist_home_max, float(status["dist_home"]))
            except ValueError:
                pass
        if "battery_voltage" in status:
            try:
                volts = float(status["battery_voltage"])   # "--" raises
            except ValueError:
                volts = None
            if volts is not None:
                if self.volt_start is None:
                    self.volt_start = volts
                self.volt_end = volts
                self.volt_min = volts if self.volt_min is None else min(self.volt_min, volts)
        if "battery_mah" in status:
            try:
                mah = float(status["battery_mah"])
            except ValueError:
                mah = None
            if mah is not None:
                if self.mah_start is None:
                    self.mah_start = mah
                self.mah_end = mah

    @staticmethod
    def _haversine_m(lat1, lon1, lat2, lon2):
        r = 6371000.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    # ---- output ---------------------------------------------------------
    def rows(self):
        """(label, value) pairs for the report, already formatted."""
        hms = _hms
        avg_ias = self._ias_sum / self._air_samples if self._air_samples else 0.0
        avg_gs = self._gs_sum / self._air_samples if self._air_samples else 0.0
        out = [
            ("Flight time", hms(self.duration_s) + (" +" if self.partial else "")),
        ]
        if self.unrecorded_s > 0:
            out.append(("  of which not recorded", hms(self.unrecorded_s)))
        out += [
            ("Distance flown", f"{self.distance_m / 1000.0:.2f} km"),
            ("Max distance from home", f"{self.dist_home_max:.0f} m"),
            ("Average airspeed", f"{avg_ias:.1f} m/s"),
            ("Max airspeed", f"{self.ias_max:.1f} m/s"),
            ("Average groundspeed", f"{avg_gs:.1f} m/s"),
            ("Max groundspeed", f"{self.gs_max:.1f} m/s"),
            ("Max altitude (above home)", f"{self.alt_max:.0f} m"),
        ]
        if self.amsl_max is not None:
            out.append(("Max altitude (AMSL)", f"{self.amsl_max:.0f} m"))
        out += [
            ("Max climb rate", f"{self.climb_max:.1f} m/s"),
            ("Max descent rate", f"{self.sink_max:.1f} m/s"),
            ("Max wind", f"{self.wind_max * 3.6:.1f} kph"),
        ]
        if self.volt_start is not None:
            out.append(("Battery start / end",
                        f"{self.volt_start:.2f} V / {self.volt_end:.2f} V"))
            out.append(("Battery lowest", f"{self.volt_min:.2f} V"))
        if self.mah_start is not None and self.mah_end is not None:
            out.append(("Consumed", f"{self.mah_end - self.mah_start:.0f} mAh"))
        return out

    def as_text(self) -> str:
        rows = self.rows()
        width = max(len(label) for label, _ in rows)
        lines = [f"{label.ljust(width)}  {value}" for label, value in rows]
        return chr(10).join(lines)


class TrackFormatDialog(QDialog):
    """Which file format to save the flown track as.

    Asked here rather than left to the save dialog's type list because the
    three are not interchangeable - GPX in particular cannot express an
    altitude measured from the ground - and a line of explanation beside
    each is worth more than three extensions in a dropdown.
    """

    # (extension, label, what it is good for)
    FORMATS = [
        (".kmz", "KMZ - Google Earth",
         "One compact file. Replays the flight with a time slider."),
        (".kml", "KML - Google Earth (uncompressed)",
         "The same document as KMZ, but plain text you can open and read."),
        (".gpx", "GPX - GPS exchange format",
         "Read by almost every mapping and GPS tool. Carries altitude only "
         "when the vehicle reported it above sea level."),
    ]

    def __init__(self, current=".kmz", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save Track")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        heading = QLabel("Save the flown track as:")
        heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(heading)

        self._group = QButtonGroup(self)
        for ext, label, blurb in self.FORMATS:
            radio = QRadioButton(label)
            radio.setChecked(ext == current)
            self._group.addButton(radio)
            radio.setProperty("ext", ext)
            layout.addWidget(radio)

            note = QLabel(blurb)
            note.setWordWrap(True)
            note.setStyleSheet("font-size: 11px; color: #aaa;")
            note.setContentsMargins(22, 0, 0, 4)
            layout.addWidget(note)

        # Nothing matched the remembered value - fall back rather than
        # showing a dialog with no selection at all.
        if self._group.checkedButton() is None:
            self._group.buttons()[0].setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Choose File...")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self) -> str:
        button = self._group.checkedButton()
        return button.property("ext") if button else ".kmz"


class FlightSummaryDialog(QDialog):
    """The post-flight report, shown once the vehicle disarms.

    If the link dropped earlier in the same session while the aircraft was
    armed, that segment is offered here rather than merged automatically.
    Nothing can distinguish a dropout from a landing and a relaunch, so the
    decision is left to the person who was there - made at the end, with
    both sets of numbers visible, rather than as a guess at reconnect time.
    """

    def __init__(self, stats, earlier=None, parent=None, home=None):
        super().__init__(parent)
        self.setWindowTitle("Flight Summary")
        self._home = home       # (lat, lon) for the KMZ, if it is known
        self._stats = stats
        self._earlier = list(earlier or [])
        layout = QVBoxLayout(self)

        if stats.partial:
            note = QLabel(
                "Connected after this flight began - these figures cover "
                "only the part seen by this GCS."
            )
            note.setStyleSheet("color: #e6a23c; font-size: 10px;")
            note.setWordWrap(True)
            layout.addWidget(note)

        self._merge_box = None
        if self._earlier:
            preview = stats.merged_with(self._earlier)
            recorded = sum(seg.duration_s for seg in self._earlier)
            if len(self._earlier) == 1:
                text = (f"Include the {_hms(recorded)} recorded before the link "
                        f"dropped ({_hms(preview.unrecorded_s)} unrecorded)")
            else:
                text = (f"Include the {len(self._earlier)} earlier segments "
                        f"({_hms(recorded)} recorded, "
                        f"{_hms(preview.unrecorded_s)} unrecorded)")
            self._merge_box = QCheckBox(text)
            # Ticked by default only when this segment alone would not have
            # been worth reporting - which is the only reason the dialog is
            # on screen at all. Opening on a view we had already judged too
            # short to show would be incoherent. Where both segments stand
            # on their own it stays clear, so merging remains deliberate.
            self._merge_box.setChecked(not stats.worth_reporting())
            self._merge_box.setToolTip(
                "Tick this if the aircraft stayed airborne through the "
                "dropout, so both segments are one flight. Leave it clear if "
                "it landed and took off again."
            )
            self._merge_box.setStyleSheet("font-size: 10px;")
            self._merge_box.toggled.connect(self._rebuild)
            layout.addWidget(self._merge_box)

        self._rows_host = QWidget()
        self._rows_layout = QGridLayout(self._rows_host)
        self._rows_layout.setHorizontalSpacing(18)
        self._rows_layout.setVerticalSpacing(3)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._rows_host)

        buttons = QDialogButtonBox()
        copy_btn = buttons.addButton("Copy", QDialogButtonBox.ActionRole)
        self._track_btn = buttons.addButton("Save Track...",
                                            QDialogButtonBox.ActionRole)
        self._track_btn.setToolTip(
            "Save the flown track as KMZ, KML or GPX - pick the format in "
            "the save dialog. Each point carries a timestamp, so Google "
            "Earth can replay the flight rather than only draw it.")
        buttons.addButton(QDialogButtonBox.Close)
        copy_btn.clicked.connect(self._copy)
        self._track_btn.clicked.connect(self._save_track)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        self._rebuild()

    def current_stats(self):
        """What is on screen: this flight, or both segments together."""
        if self._merge_box is not None and self._merge_box.isChecked():
            return self._stats.merged_with(self._earlier)
        return self._stats

    def _rebuild(self):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for row, (label, value) in enumerate(self.current_stats().rows()):
            name = QLabel(label)
            name.setStyleSheet("color: #b8bcc2;")
            val = QLabel(value)
            val.setStyleSheet("color: white; font-weight: bold;")
            val.setAlignment(Qt.AlignRight)
            self._rows_layout.addWidget(name, row, 0)
            self._rows_layout.addWidget(val, row, 1)
        self.adjustSize()

    def _copy(self):
        QApplication.clipboard().setText(self.current_stats().as_text())

    # Offered in the save dialog's own type list, so choosing a format
    # costs no space in the summary itself.
    EXPORT_FILTERS = [
        ("Google Earth KMZ (*.kmz)", ".kmz"),
        ("Google Earth KML (*.kml)", ".kml"),
        ("GPX track (*.gpx)", ".gpx"),
    ]
    SETTING_EXPORT_FORMAT = "flight_export_format"

    def _save_track(self):
        """Write the flown track where and how the user asks.

        Uses whichever flight the summary is currently showing, so ticking
        the merge box and then saving gives the whole sortie rather than
        the last segment of it.
        """
        stats = self.current_stats()
        if not stats.track:
            QMessageBox.information(
                self, "Nothing to save",
                "This flight has no recorded positions, so there is no "
                "track to export. That happens when the vehicle never had "
                "a GPS fix while armed.")
            return

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())

        # Ask the format here rather than in the file dialog's type list,
        # so each option can say what it is for. Defaults to whatever was
        # chosen last: the format is a habit, and re-picking it after every
        # flight would be a small irritation.
        remembered = load_settings().get(self.SETTING_EXPORT_FORMAT, ".kmz")
        chooser = TrackFormatDialog(remembered, self)
        if chooser.exec() != QDialog.Accepted:
            return
        ext = chooser.chosen()
        save_setting(self.SETTING_EXPORT_FORMAT, ext)

        label = next((f for f, e in self.EXPORT_FILTERS if e == ext),
                     self.EXPORT_FILTERS[0][0])
        suggested = str(Path(folder) / f"MavGCS-flight-{stamp}{ext}")

        path, _ = QFileDialog.getSaveFileName(
            self, f"Save flight track as {ext.lstrip('.').upper()}",
            suggested, label)
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext

        try:
            written = write_track(
                path, stats.track,
                name=f"MavGCS flight {stamp}",
                home=self._home,
                summary=stats.as_text(),
            )
        except Exception as e:
            QMessageBox.warning(self, "Could not save",
                                "Writing the track failed: " + str(e))
            return

        opens_in = ("Google Earth" if ext in (".kmz", ".kml")
                    else "any GPS or mapping tool that reads GPX")
        box = QMessageBox(self)
        box.setWindowTitle("Flight saved")
        box.setText(f"{len(stats.track)} track points written.")
        box.setInformativeText(
            f"{written}"
            f"\n\nOpen it in {opens_in}.")
        open_btn = box.addButton("Open Folder", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(written.parent)))


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
    abort_landing_requested = Signal()

    NORMAL_STYLE = "font-size: 10px; padding: 3px 4px;"
    ACTIVE_STYLE = "background-color: #2a6; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    RTL_STYLE = "background-color: #a33; color: white; font-size: 10px; padding: 3px 4px;"
    # Orange, and not the green every other active mode gets: while this
    # button is lit it does something different from the label it was
    # pressed under, and it should not look like the rest of the row.
    ABORT_STYLE = "background-color: #e07a00; color: white; font-weight: bold; font-size: 10px; padding: 3px 4px;"
    ABORT_TEXT = "ABORT LANDING"
    # Same weight of colour as RTL's red and the active-mode green, so it
    # reads as one of the panel's coloured controls rather than a sore thumb.
    FLY_TO_STYLE = "background-color: #36a; color: white; font-size: 10px; padding: 3px 4px;"

    def __init__(self, parent=None):
        super().__init__("Flight Mode", parent)
        grid = QGridLayout(self)
        grid.setSpacing(4)
        grid.setContentsMargins(6, 10, 6, 6)
        self.buttons = {}
        # True while the aircraft is actually in AUTOLAND, which is when
        # that one button stops being a mode request and becomes the way
        # out of the approach.
        self._landing = False
        for i, name in enumerate(self.MODE_ORDER):
            btn = QPushButton(name)
            btn.setStyleSheet(self.RTL_STYLE if name == "RTL" else self.NORMAL_STYLE)
            if name == "AUTOLAND":
                btn.clicked.connect(self._autoland_clicked)
            else:
                btn.clicked.connect(
                    lambda checked=False, n=name: self.mode_requested.emit(n))
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

    def _autoland_clicked(self):
        """One button, two jobs, decided by what the aircraft is doing.

        Driven by the mode actually reported in telemetry rather than by
        what was last pressed, so a landing begun from the transmitter or
        from another GCS still offers the way out, and a request that the
        aircraft refused never leaves this armed.
        """
        if self._landing:
            self.abort_landing_requested.emit()
        else:
            self.mode_requested.emit("AUTOLAND")

    def set_active_mode(self, mode_name):
        self._landing = mode_name == "AUTOLAND"
        land = self.buttons["AUTOLAND"]
        land.setText(self.ABORT_TEXT if self._landing else "AUTOLAND")
        land.setToolTip(
            "Break off the approach and climb away in "
            f"{MavlinkLink.ABORT_LANDING_MODE}"
            if self._landing else "Fly the automatic landing approach"
        )
        for name, btn in self.buttons.items():
            if name == "AUTOLAND" and self._landing:
                # Deliberately not the active-mode green. It is in
                # AUTOLAND, but pressing it now leaves AUTOLAND.
                btn.setStyleSheet(self.ABORT_STYLE)
            elif name == mode_name:
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


class ElidedLabel(QLabel):
    """One line that shortens its text with an ellipsis instead of wrapping.

    These lines carry command feedback and connection errors, and some of
    those run long - a Windows socket error in Turkish is well over a
    hundred characters. Wrapped, it became two or three lines, which grew
    the left column, which lives in a scroll area, which put a scrollbar
    down the whole panel over one message.

    The full text stays in the tooltip, so nothing is lost by shortening
    it on screen.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setWordWrap(False)
        # Ignored horizontally: a long message must not widen the column
        # either, which would produce a horizontal scrollbar instead.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply_elision()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self):
        width = max(0, self.width())
        super().setText(
            self.fontMetrics().elidedText(self._full, Qt.ElideRight, width)
        )


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


class TelemetryRatesDialog(QDialog):
    """
    How much telemetry to ask the vehicle for.

    A slow RC link carries far less than a flight controller streams by
    default, and the radio drops whatever overflows without regard for
    which messages mattered. Asking for less means what we do ask for
    actually arrives - the 3D view is smoother at 5Hz requested than at
    10Hz sent and mostly discarded.

    Deliberately a dialog rather than controls in the connection panel:
    these are set once and forgotten, and the panel's layout is not worth
    disturbing for them.
    """

    SETTING_ATTITUDE = "telemetry_attitude_hz"
    SETTING_POSITION = "telemetry_position_hz"
    SETTING_FULL = "telemetry_full"
    SETTING_COG = "cog_check"
    # Which side of centre is up elevator. "auto" trusts SERVOn_REVERSED,
    # which is measured to decide the direction on any aircraft that
    # flies; the two explicit settings are for the case where an airframe
    # is wired in a way the parameter does not describe.
    SETTING_ELEV_DIR = "cog_elevator_dir"
    DEFAULT_ELEV_DIR = "auto"
    # On by default: it is a readout people ask for by name, and hiding it
    # behind a setting meant looking for something that was never switched
    # on. It costs about 40 B/s of telemetry and sets no flight parameter -
    # only whether the vehicle reports its pitch controller - and the link
    # puts that back as it found it on the way out.
    DEFAULT_COG = True

    @classmethod
    def elevator_direction(cls) -> str:
        """"auto", "above" or "below" - which side of centre is up."""
        value = load_settings().get(cls.SETTING_ELEV_DIR, cls.DEFAULT_ELEV_DIR)
        return value if value in ("auto", "above", "below") else "auto"

    @classmethod
    def cog_enabled(cls) -> bool:
        return bool(load_settings().get(cls.SETTING_COG, cls.DEFAULT_COG))

    DEFAULT_ATTITUDE = 5.0
    DEFAULT_POSITION = 2.0
    CHOICES = [1.0, 2.0, 3.0, 5.0]

    @classmethod
    def current(cls):
        """(attitude_hz, position_hz, full_telemetry) as configured.

        Falls back to the defaults for anything missing or unreadable. This
        runs on every connection, and a settings file with a null or a
        stray string in it must not be the reason a link fails to open.
        """
        s = load_settings()

        def rate(key, default):
            try:
                value = float(s.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value in cls.CHOICES else default

        return (rate(cls.SETTING_ATTITUDE, cls.DEFAULT_ATTITUDE),
                rate(cls.SETTING_POSITION, cls.DEFAULT_POSITION),
                bool(s.get(cls.SETTING_FULL, False)))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Telemetry Rates")
        att, pos, full = self.current()

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        blurb = QLabel(
            "How often to ask the vehicle for the two messages the 3D view "
            "and map depend on. Lower rates suit a slow radio link, where "
            "asking for more than it can carry means losing packets rather "
            "than gaining data."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        self.attitude_combo = QComboBox()
        self.position_combo = QComboBox()
        for combo, value in ((self.attitude_combo, att),
                             (self.position_combo, pos)):
            for hz in self.CHOICES:
                combo.addItem(f"{hz:g} Hz", hz)
            idx = combo.findData(value)
            combo.setCurrentIndex(idx if idx >= 0 else combo.findData(
                self.DEFAULT_ATTITUDE if combo is self.attitude_combo
                else self.DEFAULT_POSITION))
        form.addRow("Attitude", self.attitude_combo)
        form.addRow("GPS position", self.position_combo)
        layout.addLayout(form)

        self.full_box = QCheckBox("Full MAVLink telemetry")
        self.full_box.setChecked(full)
        self.full_box.setToolTip(
            "Stream everything the flight controller sends at its own rates, "
            "ignoring the settings above. For fast links, and for capturing "
            "everything. Off by default: the reduced set is what fits a slow "
            "RC link."
        )
        self.full_box.toggled.connect(self._sync_enabled)
        layout.addWidget(self.full_box)

        self.cog_box = QCheckBox("Estimate center of gravity")
        self.cog_box.setChecked(self.cog_enabled())
        self.cog_box.setToolTip(
            "Reads where the elevator is held during steady level flight "
            "and shows it on the map: up elevator means nose heavy. Works "
            "with SERVO_AUTO_TRIM on, because the servo output includes "
            "the trim. Costs about 33 B/s. Assumes the elevator is faired "
            "at 1500us; elevon and V-tail aircraft are not supported.")
        layout.addWidget(self.cog_box)

        # What the app worked out about this aircraft, so a wrong reading
        # is caught on the ground rather than after a flight.
        self.cog_detail = QLabel()
        self.cog_detail.setWordWrap(True)
        self.cog_detail.setStyleSheet("font-size: 11px; color: #8fd18f;")
        layout.addWidget(self.cog_detail)

        self.elev_combo = QComboBox()
        self.elev_combo.addItem("Up elevator: detect automatically", "auto")
        self.elev_combo.addItem("Up elevator: below 1500us", "below")
        self.elev_combo.addItem("Up elevator: above 1500us", "above")
        idx = self.elev_combo.findData(self.elevator_direction())
        self.elev_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.elev_combo.setToolTip(
            "Automatic reads SERVOn_REVERSED, which was measured against "
            "ArduPlane to decide this correctly. Override it only if the "
            "reading above disagrees with your aircraft.")
        layout.addWidget(self.elev_combo)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("font-size: 11px; color: #aaa;")
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_enabled(self.full_box.isChecked())

    def _sync_enabled(self, full):
        self.attitude_combo.setEnabled(not full)
        self.position_combo.setEnabled(not full)
        self.note.setText(
            "The vehicle decides everything; these settings are ignored."
            if full else
            "Applied immediately while connected, and on every connection "
            "after this. Only this link is affected - another ground station "
            "talking to the same vehicle keeps its own rates."
        )

    def set_elevator_detail(self, text):
        """Show what the link found, or why it found nothing."""
        self.cog_detail.setText(text or "Elevator: not detected yet")
        self.cog_detail.setStyleSheet(
            "font-size: 11px; color: %s;" % ("#8fd18f" if text else "#aaa"))

    def values(self):
        return (self.attitude_combo.currentData(),
                self.position_combo.currentData(),
                self.full_box.isChecked())

    def save(self):
        att, pos, full = self.values()
        save_setting(self.SETTING_ATTITUDE, att)
        save_setting(self.SETTING_POSITION, pos)
        save_setting(self.SETTING_ELEV_DIR, self.elev_combo.currentData())
        save_setting(self.SETTING_FULL, full)
        save_setting(self.SETTING_COG, self.cog_box.isChecked())
        return att, pos, full


class UpdateDialog(QDialog):
    """
    One dialog for every outcome of an update check - already current, a
    newer release available, or the check itself failed. Keeping the three
    together means there is a single place that explains what happened, and
    a single home for the "check at startup" preference, which otherwise
    has nowhere to live in a window with no menu bar.
    """

    SETTING_AUTO = "check_updates_on_start"

    def __init__(self, result, current_version, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MavGCS Updates")
        self.setMinimumWidth(420)
        self._result = result
        self._downloader = None
        self._saved_path = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.headline = QLabel()
        self.headline.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(self.headline)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(120)
        self.notes.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.notes)
        self.notes.hide()

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        layout.addWidget(self.bar)
        self.bar.hide()

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 11px; color: #aaa;")
        layout.addWidget(self.status)

        self.auto_box = QCheckBox("Check for updates when MavGCS starts")
        self.auto_box.setChecked(bool(load_settings().get(self.SETTING_AUTO, False)))
        self.auto_box.toggled.connect(
            lambda on: save_setting(self.SETTING_AUTO, bool(on)))
        layout.addWidget(self.auto_box)

        buttons = QHBoxLayout()
        self.action_btn = QPushButton()
        self.action_btn.clicked.connect(self._on_action)
        self.page_btn = QPushButton("Release Page")
        self.page_btn.clicked.connect(self._open_page)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.action_btn)
        buttons.addWidget(self.page_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self._render(current_version)

    def _render(self, current_version):
        r = self._result
        if not r["ok"]:
            self.headline.setText("Couldn't check for updates")
            self.detail.setText(r["error"] or "Unknown error.")
            self.action_btn.hide()
            return
        if not r["newer"]:
            self.headline.setText(f"MavGCS {current_version} is up to date")
            self.detail.setText(
                f"The newest release on GitHub is {r['tag']}.")
            self.action_btn.hide()
            return

        self.headline.setText(f"{r['tag']} is available")
        self.detail.setText(
            f"You are running {current_version}. "
            + (f"The download is {r['asset_size'] / 1e6:.0f} MB."
               if r["asset_size"] else "")
        )
        if r["notes"]:
            self.notes.setPlainText(r["notes"])
            self.notes.show()
        if r["asset_url"]:
            self.action_btn.setText("Download")
            self.status.setText(
                "Downloads the zip - it does not install over your current "
                "copy. Unzip it and replace your MavGCS folder; your "
                "settings and map caches are stored elsewhere and are left "
                "alone."
            )
        else:
            self.action_btn.hide()
            self.status.setText("This release has no downloadable zip.")

    def _open_page(self):
        QDesktopServices.openUrl(QUrl(self._result.get("page_url")
                                      or RELEASES_PAGE))

    def _on_action(self):
        if self._saved_path:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(self._saved_path).parent)))
            return
        if self._downloader is not None:
            self._downloader.cancel()
            return
        self._start_download()

    def _start_download(self):
        r = self._result
        folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation) or str(Path.home())
        dest = Path(folder) / (r["asset_name"] or "MavGCS-update.zip")

        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.show()
        self.status.setText(f"Downloading to {dest} ...")
        self.action_btn.setText("Cancel")

        self._downloader = UpdateDownloader(
            r["asset_url"], dest, r["asset_size"], r["asset_sha256"], self)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.finished_ok.connect(self._on_done)
        self._downloader.failed.connect(self._on_failed)
        self._downloader.start()

    def _on_progress(self, received, total):
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(received * 100 / total))
            self.bar.setFormat(
                f"{received / 1e6:.0f} / {total / 1e6:.0f} MB  (%p%)")
        else:
            self.bar.setRange(0, 0)   # unknown length: just show activity
            self.bar.setFormat(f"{received / 1e6:.0f} MB")

    def _on_done(self, path):
        self._downloader = None
        self._saved_path = path
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.bar.setFormat("Done")
        self.status.setText(
            f"Saved and checksum verified:\n{path}\n\n"
            "Close MavGCS, unzip this over a new folder, and run the new "
            "MavGCS.exe. Your settings and caches are kept separately and "
            "carry over."
        )
        self.action_btn.setText("Open Folder")

    def _on_failed(self, message):
        self._downloader = None
        self.bar.hide()
        self.status.setText(message)
        self.action_btn.setText("Download")

    def reject(self):
        if self._downloader is not None:
            self._downloader.cancel()
            self._downloader.wait(3000)
        super().reject()


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
    update_requested = Signal()
    telemetry_settings_requested = Signal()

    FIELD_HEIGHT = 24
    UPDATE_STYLE = "color: #aaa; font-size: 9px; padding: 2px 6px;"
    UPDATE_FOUND_STYLE = ("color: #7ddc7d; font-weight: bold; "
                          "font-size: 9px; padding: 2px 6px;")

    def __init__(self, default_protocol="TCP", default_host="127.0.0.1", default_port="5762", parent=None):
        super().__init__("Connection", parent)
        # No width cap: this sits directly above the mission panel, which has
        # none, so capping this one left the two group frames ending at
        # different places down the right-hand edge.
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
        outer.addStretch(1)  # keep the rows pinned to the top, don't stretch vertically

        # Nothing to do with connecting to a vehicle, but this is the one
        # group that is always on screen and never scrolls, and a window
        # with no menu bar has nowhere else to put it.
        self.telemetry_btn = QPushButton("Telemetry Rates")
        self.telemetry_btn.setFixedHeight(self.FIELD_HEIGHT)
        self.telemetry_btn.setStyleSheet(self.UPDATE_STYLE)
        self.telemetry_btn.setToolTip(
            "How often to ask the vehicle for attitude and position. "
            "Lower rates suit a slow radio link.")
        self.telemetry_btn.clicked.connect(self.telemetry_settings_requested)

        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.setFixedHeight(self.FIELD_HEIGHT)
        self.update_btn.setStyleSheet(self.UPDATE_STYLE)
        self.update_btn.clicked.connect(self.update_requested)


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
        # Shares the button row rather than taking one of its own: a row to
        # itself cost the map 27px of height for a control used once in a
        # while, and there is width to spare here.
        refresh_row.addWidget(self.telemetry_btn)
        refresh_row.addWidget(self.update_btn)
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

    def contextMenuEvent(self, event):
        """Right-click reaches the same settings as the button, for anyone
        who looks for a context menu first."""
        menu = QMenu(self)
        menu.addAction("Telemetry Rates...",
                       self.telemetry_settings_requested.emit)
        menu.exec(event.globalPos())

    def set_update_state(self, text: str, found: bool = False, enabled: bool = True):
        """Reflect a check in the button itself, so a waiting or successful
        check is visible without a dialog being open."""
        self.update_btn.setText(text)
        self.update_btn.setEnabled(enabled)
        self.update_btn.setStyleSheet(
            self.UPDATE_FOUND_STYLE if found else self.UPDATE_STYLE)

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

        # This sits beside the connection and mission panels. Its height is
        # deliberately NOT its own business: an explicit ceiling left it
        # stopping short of them whenever they grew, and letting its own
        # preferred height win made the whole row taller than the panels
        # needed, wasting space the map could have had. Ignoring the
        # vertical hint hands the decision to the stack beside it - this
        # then fills exactly that, no more.
        # Lowered from 130 when the systems strip moved in underneath.
        # The two share this column, and at 130 the log's own floor was
        # pushing the whole top row down and taking the difference off
        # the map. Around six lines of log, which is what it showed
        # before the strip arrived on a smaller window.
        self.setMinimumHeight(112)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Ignored)

    # How close to the bottom still counts as "following". A few pixels of
    # slack, because the scrollbar rarely sits exactly on maximum().
    FOLLOW_SLACK_PX = 4

    def add_message(self, text, severity):
        scrollbar = self.text_edit.verticalScrollBar()
        # Decide BEFORE appending. Afterwards maximum() has already grown by
        # the new line, so "were we at the bottom?" can no longer be asked.
        following = scrollbar.value() >= scrollbar.maximum() - self.FOLLOW_SLACK_PX

        color = self.SEVERITY_COLORS.get(severity, "#cfd2d6")
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_text = html.escape(text)
        self.text_edit.appendHtml(
            f'<span style="color:{color};">{timestamp} : {safe_text}</span>'
        )

        # Follow the tail only if the reader was already at it. Scrolling
        # back to read something and being dragged to the bottom by the next
        # routine message made the log unreadable exactly when it mattered -
        # and made selecting text to copy impossible.
        if following:
            scrollbar.setValue(scrollbar.maximum())


class SensorHealthPanel(QGroupBox):
    """Every subsystem the autopilot reports on, side by side.

    SYS_STATUS carries three bitmasks - present, enabled, healthy - and
    the interesting thing is that they answer different questions. A
    sensor can be fitted and switched off, or fitted and broken, and
    those are not the same trouble. Folding them into one green light
    would throw away the distinction that tells you which.

    So four states rather than two: nothing fitted, fitted but not in
    use, working, failed. The colours are the same ones the EKF and Vibe
    flags on the HUD already use, so a red here means what a red there
    means.
    """

    # Bit, label. Order is roughly the order they matter on a preflight:
    # the IMU, then what corrects it, then what it needs to navigate.
    SENSORS = [
        ("MAV_SYS_STATUS_SENSOR_3D_GYRO", "GYRO"),
        ("MAV_SYS_STATUS_SENSOR_3D_ACCEL", "ACC"),
        ("MAV_SYS_STATUS_SENSOR_3D_MAG", "MAG"),
        ("MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE", "BARO"),
        ("MAV_SYS_STATUS_SENSOR_GPS", "GPS"),
        ("MAV_SYS_STATUS_SENSOR_LASER_POSITION", "RNGFND"),
        ("MAV_SYS_STATUS_SENSOR_DIFFERENTIAL_PRESSURE", "PITOT"),
        ("MAV_SYS_STATUS_AHRS", "EKF"),
    ]

    BASE = ("border: 1px solid #3a3d42; border-radius: 3px; "
            "padding: 3px 0px; font-size: 10px; font-weight: bold;")
    STYLES = {
        # Not reported as present at all: nothing fitted, or this
        # autopilot does not say. Dim, because it is not a fault.
        "absent": BASE + "color: #5c6066; background-color: #1c1e21;",
        # Fitted but switched off. Cool rather than warm on purpose -
        # amber is reserved for something actually going wrong, and a
        # disabled airspeed sensor is a decision, not a warning.
        "off": BASE + "color: #7d8ea0; background-color: #1a1f24;",
        "ok": BASE + "color: #5ccf5c; background-color: #172117;",
        "warn": BASE + "color: #d8a23a; background-color: #241f16;",
        "failed": BASE + "color: #ff5555; background-color: #2a1616;",
    }
    # Mission Planner's own bands for an EKF variance: over 0.5 is
    # worth a look, over 0.8 means the filter is rejecting the
    # measurement.
    VARIANCE_WARN = 0.5
    VARIANCE_BAD = 0.8

    # What ArduPilot itself calls a good enough GPS to arm on:
    # GPS_HDOP_GOOD defaults to 1.4, and the arming check wants six
    # satellites. A fix that would not pass those is a fix, but it is
    # not a healthy GPS.
    HDOP_GOOD = 1.4
    SATS_GOOD = 6

    TIPS = {
        "absent": "not fitted, or not reported by this autopilot",
        "off": "fitted but not enabled",
        "ok": "present, enabled and healthy",
        "failed": "present and enabled, but reporting unhealthy",
    }

    def __init__(self, parent=None):
        super().__init__("Systems", parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 10, 6, 6)
        row.setSpacing(3)
        self.cells = {}
        for attr, label in self.SENSORS:
            cell = QLabel(label)
            cell.setAlignment(Qt.AlignCenter)
            row.addWidget(cell, stretch=1)
            self.cells[label] = (getattr(mavutil.mavlink, attr), cell)
        self._masks = None
        self._gps_fix = None
        self._ekf = None
        self._vibe = None
        self._sats = None
        self._hdop = None
        self._var = {}
        self.clear()
        # Whatever height the labels need and not a pixel more: this sits
        # under the messages log, and every row it takes is one that log
        # does not get.
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------ inputs

    def set_health(self, present, enabled, health):
        self._masks = (present, enabled, health)
        self._apply()

    def set_gps_quality(self, fix_type, sats, hdop):
        self._gps_fix = fix_type
        # -1 is the link's "the receiver did not say", not a bad value.
        self._sats = sats if sats >= 0 else None
        self._hdop = hdop if hdop >= 0 else None
        self._apply()

    def set_variances(self, velocity, compass, pos_horiz, pos_vert, terrain):
        self._var = {
            "MAG": compass, "GPS": pos_horiz,
            "BARO": pos_vert, "RNGFND": terrain,
        }
        self._apply()

    def set_ekf(self, colour):
        self._ekf = colour
        self._apply()

    def set_vibe(self, colour):
        self._vibe = colour
        self._apply()

    def clear(self):
        """Back to unknown, for when there is no aircraft to ask."""
        self._masks = None
        self._gps_fix = self._ekf = self._vibe = None
        self._sats = self._hdop = None
        self._var = {}
        for label, (_bit, cell) in self.cells.items():
            cell.setStyleSheet(self.STYLES["absent"])
            cell.setToolTip(f"{label} - no telemetry")

    # ------------------------------------------------------------ verdict

    def _base_state(self, bit):
        present, enabled, health = self._masks
        if not present & bit:
            return "absent"
        if not enabled & bit:
            return "off"
        return "ok" if health & bit else "failed"

    def _variance(self, label):
        """A cell's own EKF variance, if it has one, as a state."""
        v = self._var.get(label)
        if v is None or v < 0:
            return None
        if v > self.VARIANCE_BAD:
            return "failed", f"{label} variance high"
        if v > self.VARIANCE_WARN:
            return "warn", f"{label} variance raised"
        return None

    def _extra(self, label):
        """A second opinion, for the cells that have one.

        Green is meant to mean nothing is wrong, not merely that the
        autopilot has not declared the part broken. SYS_STATUS answers
        the narrower question - is the hardware working - and it goes on
        answering yes through a GPS with no fix, a compass arguing with
        its neighbours, and an airframe shaking itself apart. So where
        the aircraft sends something sharper, that decides too.

        It can only ever make a cell worse. Good numbers cannot argue an
        unhealthy sensor back to green.
        """
        worst = None

        def worse(candidate):
            nonlocal worst
            if candidate is None:
                return
            order = {"warn": 1, "failed": 2}
            if worst is None or order[candidate[0]] > order[worst[0]]:
                worst = candidate

        if label == "GPS":
            if self._gps_fix is not None:
                if self._gps_fix <= 1:
                    worse(("failed", "no fix"))
                elif self._gps_fix == 2:
                    worse(("warn", "2D fix only"))
            if self._sats is not None and self._sats < self.SATS_GOOD:
                worse(("warn", f"only {self._sats} satellites"))
            if self._hdop is not None and self._hdop > self.HDOP_GOOD:
                worse(("warn", f"HDOP {self._hdop:.1f}"))
            worse(self._variance("GPS"))
        elif label == "MAG":
            worse(self._variance("MAG"))
        elif label == "BARO":
            worse(self._variance("BARO"))
        elif label == "RNGFND":
            worse(self._variance("RNGFND"))
        elif label == "EKF" and self._ekf:
            if self._ekf == "red":
                worse(("failed", "EKF variances high"))
            elif self._ekf == "yellow":
                worse(("warn", "EKF variances raised"))
        elif label == "ACC" and self._vibe:
            # Vibration is measured off the accelerometers, so it belongs
            # to this cell: the sensor is healthy but what it is being
            # asked to measure through is not.
            if self._vibe == "red":
                worse(("failed", "vibration above 60"))
            elif self._vibe == "yellow":
                worse(("warn", "vibration above 30"))
        # GYRO and PITOT reach here with nothing. Neither the EKF nor any
        # other message carries a figure for them, so those two cells are
        # only ever as good as the autopilot's own health bit - worth
        # knowing when reading the strip.
        return worst

    def _apply(self):
        if self._masks is None:
            return
        for label, (bit, cell) in self.cells.items():
            state = self._base_state(bit)
            why = self.TIPS[state]
            if state == "ok":
                extra = self._extra(label)
                if extra:
                    state, why = extra
            cell.setStyleSheet(self.STYLES[state])
            cell.setToolTip(f"{label} - {why}")


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
        # Caching map tiles is what lets a previously flown area work with
        # no internet at the field, so it is on by default rather than
        # something to remember to switch on beforehand. The server itself
        # still defaults to off - that is a sensible default for a cache
        # component; wanting it on is this application's policy.
        self.tile_server.set_size_limit(self.MAP_CACHE_MB_DEFAULT
                                        * 1024 * 1024)
        self.map_view = MapView(_tile_port)
        self.fpv_view = FpvView(_tile_port, load_settings().get("cesium_ion_token", ""))
        self.waypoint_panel = WaypointMissionPanel()
        self.messages_panel = MessagesPanel()
        self.sensor_panel = SensorHealthPanel()
        self.connection_panel = ConnectionPanel(*self._split_connection_string(connection_string))
        self.status_label = QLabel()
        # True while the feedback line is showing a connection problem that
        # we put there, and may therefore remove again.
        self._link_message_shown = False
        self.command_label = ElidedLabel()
        self.command_label.setStyleSheet("color: #ccc; font-size: 9px;")
        self.ack_label = ElidedLabel()
        self.ack_label.setStyleSheet("color: #888; font-size: 9px;")
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
        # Accumulates the numbers reported after landing.
        self._flight_stats = FlightStats()
        # Can it still get home on what is left in the pack?
        self._return_home = ReturnHomeEstimator()
        # SYS_STATUS's own remaining-percent figure, shown beneath the
        # verdict. Not an input to it: the estimate works in mAh, which
        # is the honest unit, while this is the autopilot's own reading.
        self._battery_pct = "--"
        # The verdict is pushed on a timer rather than on every input.
        # Its ingredients arrive at four different rates, and recomputing
        # on each would cross into the map page several times a second to
        # say the same thing.
        self._rh_timer = QTimer(self)
        self._rh_timer.setInterval(1000)
        self._rh_timer.timeout.connect(self._push_return_home)
        self._rh_timer.start()
        # Set by the link if this aircraft's elevator cannot be read.
        self._elevator_unavailable = ""
        self._wp_dist = None
        self._mode = None
        self._was_connected = False
        self._trim_throttle = None
        # Recent HUD state, so the overlay drawn over the 3D view can be
        # rendered at the moment that view is actually showing rather than
        # at the newest telemetry. Without this the instrument lines lead
        # the scene by the playback delay - rolling out of a turn, the
        # horizon line levelled a second before the ground did.
        self._home_pos = None       # (lat, lon) once the vehicle reports it
        self._hud_history = deque(maxlen=240)
        self._att_interval_ms = 250.0
        self._pos_interval_ms = 350.0
        self._last_att_time = None
        self._last_pos_time = None
        # Flights whose link died before they could be closed off, oldest
        # first. A marginal radio drops repeatedly rather than once, so this
        # is a list: keeping only the most recent would silently discard the
        # earliest part of a stuttering flight.
        self._suspended_flights = []
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
        # Apply the remembered cache sizes once the map page exists, and
        # point its dropdowns at what was applied, so the controls never
        # disagree with what is actually in force.
        self.map_view.loadFinished.connect(lambda ok: self._apply_cache_limits())

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

        # The messages log with the systems strip beneath it. They share
        # the width the log used to have on its own, and between them the
        # height, so the row is no taller than it was and the map keeps
        # what it had.
        messages_stack = QVBoxLayout()
        messages_stack.setSpacing(4)
        messages_stack.addWidget(self.messages_panel, stretch=1)
        messages_stack.addWidget(self.sensor_panel)

        top_row = QHBoxLayout()
        top_row.addLayout(messages_stack, stretch=3)
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
        self._set_link_status(False, "Not connected")
        self.connection_panel.connect_requested.connect(self.on_connect_requested)
        self.connection_panel.disconnect_requested.connect(self.on_disconnect_requested)
        self.connection_panel.update_requested.connect(self.on_check_updates)
        self.connection_panel.telemetry_settings_requested.connect(
            self.on_telemetry_settings)

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
        self._tile_stats_timer.timeout.connect(self._push_cog_status)
        self._tile_stats_timer.start()

        self._update_checker = None
        self._update_silent = False
        if load_settings().get(UpdateDialog.SETTING_AUTO, False):
            # After the window is up, so a slow or hanging request cannot
            # delay the app appearing.
            QTimer.singleShot(4000, lambda: self.on_check_updates(silent=True))
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
        self.mode_panel.abort_landing_requested.connect(self.on_abort_landing)
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
        self._flight_stats.on_attitude(math.degrees(roll))
        self._last_att_time = self._note_interval("_att_interval_ms",
                                                  self._last_att_time)
        self._record_hud_state()
        if self.view_stack.currentIndex() == 1:
            self._push_fpv_attitude()
            self._push_hud_overlay()

    def on_ground_track(self, course_deg, groundspeed):
        """Direction of travel, which the map draws alongside the heading -
        the gap between the two is the crab angle."""
        self.map_view.set_ground_track(course_deg, groundspeed)

    # Below this the aircraft is not really going anywhere, and
    # distance/speed turns into hours that change every second.
    ETA_MIN_GS_MPS = 1.0
    # ArduPilot reports a distance of zero when there is no waypoint to
    # steer to, which is not the same as having arrived.
    ETA_MIN_DIST_M = 1.0
    # Past this the number is not telling anyone anything useful.
    ETA_MAX_S = 100 * 3600
    # The modes that actually fly to a waypoint, and so have an arrival
    # worth timing. Everything else shows a dash.
    #
    # This was the other way round once - a list of modes to suppress,
    # holding LOITER, CIRCLE and CRUISE - and every mode nobody had
    # thought of fell straight through it. Hand-flying in MANUAL or FBWA,
    # ArduPlane goes on reporting the distance to whatever waypoint it
    # was last steering to, so the box counted down towards a waypoint
    # the aircraft was not flying to and nothing was going to reach.
    # STABILIZE, ACRO, FBWB, TRAINING, AUTOTUNE and the quadplane hover
    # modes all did the same, as would any mode added to ArduPlane after
    # this was written. Naming what does navigate fails the safe way: a
    # mode this does not know about shows a dash rather than a number
    # that means nothing.
    #
    # TAKEOFF is deliberately left out. It climbs along the runway
    # heading to a target altitude rather than to a place, so its
    # distance recedes as you fly at it - the same reason CRUISE is not
    # in the list.
    ETA_NAV_MODES = ("AUTO", "GUIDED", "RTL", "AUTOLAND", "QRTL")
    ETA_LABEL = "ETA to WP : "

    def on_nav_target(self, bearing_deg, distance_m):
        self.map_view.set_nav_target(bearing_deg, distance_m)
        self._wp_dist = distance_m
        self._push_eta()

    def _push_eta(self):
        """Time to the waypoint, from distance and groundspeed.

        Deliberately blank rather than approximate in the cases where the
        arithmetic runs away: standing still divides by nothing, and no
        waypoint reports zero distance, which reads as "arrived" if you
        let it.
        """
        # Not navigating says so rather than vanishing: the box
        # disappearing would look like a fault, where a dash says plainly
        # that there is no arrival to time.
        if getattr(self, "_mode", None) not in self.ETA_NAV_MODES:
            self.map_view.set_eta(self.ETA_LABEL, "--")
            return
        dist = getattr(self, "_wp_dist", None)
        gs = self._last_groundspeed
        if (dist is None or dist < self.ETA_MIN_DIST_M
                or gs is None or gs < self.ETA_MIN_GS_MPS):
            self.map_view.set_eta("")
            return
        seconds = dist / gs
        if seconds > self.ETA_MAX_S:
            self.map_view.set_eta("")
            return
        self.map_view.set_eta(self.ETA_LABEL, self._hms(seconds))

    @staticmethod
    def _hms(seconds: float) -> str:
        """m:ss under an hour, h:mm:ss over it."""
        total = int(round(seconds))
        h, rem = divmod(total, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    def on_turn_rate(self, deg_per_s):
        self.map_view.set_turn_rate(deg_per_s)

    def on_home_bearing(self, bearing_deg):
        self.map_view.set_home_bearing(bearing_deg)
        self._return_home.set_home_bearing(bearing_deg)

    def on_battery_power(self, amps, consumed_mah):
        self._return_home.set_power(amps, consumed_mah)

    def on_battery_limits(self, capacity_mah, low_mah):
        self._return_home.set_limits(capacity_mah, low_mah)

    def on_home_distance(self, metres):
        self._return_home.set_distance(metres)

    def _push_return_home(self):
        """Hand the map the verdict, once a second.

        The box is up for as long as there is an aircraft on the other
        end, not only once it can answer. Sitting on the ground it has no
        verdict to give - no airspeed to fly the wind triangle with, and
        home is under the wheels - but appearing halfway through a flight
        is how a readout gets missed. It shows dashes until it knows,
        which also makes it obvious when something never arrives.
        """
        if not self._was_connected:
            self.map_view.set_return_home("off", "", "", "")
            return
        state, need, have = self._return_home.verdict()
        if state in ("off", "home"):
            # Deliberately not one of the three verdict colours: no
            # judgement has been made, and grey is the honest colour for
            # that.
            state, text = "unknown", "--"
        else:
            text = self.RETURN_HOME_TEXT[state]
        need_s = "--" if need is None else f"{need:.0f}"
        have_s = "--" if have is None else f"{have:.0f}"
        self.map_view.set_return_home(
            state, text, f"{need_s} of {have_s} mAh", self._battery_pct)

    def on_home_position(self, lat, lon):
        self._home_pos = (lat, lon)     # marked in the exported KMZ too
        self.map_view.set_home(lat, lon)

    def on_position(self, lat, lon, alt, heading):
        self.horizon.set_altitude(alt)
        self.horizon.set_heading(heading)
        self.horizon.set_position(lat, lon)
        self._flight_stats.on_position(lat, lon, alt)
        self._last_pos_time = self._note_interval("_pos_interval_ms",
                                                  self._last_pos_time)
        self._record_hud_state()
        if self.view_stack.currentIndex() == 1:
            self._push_fpv_position()
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

    # ---- update checking ------------------------------------------------

    def on_telemetry_settings(self):
        """Edit the requested rates, and push them to a live link at once."""
        dialog = TelemetryRatesDialog(self)
        # Show what the link has worked out about this aircraft's elevator,
        # so a wrong direction is visible before a flight rather than after.
        dialog.set_elevator_detail(
            self._elevator_unavailable
            or (self.link.elevator_description() if self.link else ""))
        if dialog.exec() != QDialog.Accepted:
            return
        att, pos, full = dialog.save()
        if self.link is not None:
            self.link.set_stream_rates(att, pos, full)
            self.link.set_elevator_direction(dialog.elev_combo.currentData())
            self.link.set_cog_enabled(dialog.cog_box.isChecked())
        self._push_cog_status()

    def on_check_updates(self, silent: bool = False):
        """Ask GitHub what the latest release is, off the GUI thread.

        `silent` is used by the optional check at startup: it reports only
        when there is something to report, so a launch with no network - or
        no new version - passes without a dialog in the way.
        """
        if getattr(self, "_update_checker", None) is not None:
            return          # one at a time
        self._update_silent = silent
        if not silent:
            self.connection_panel.set_update_state("Checking...", enabled=False)
        self._update_checker = UpdateChecker(APP_VERSION, self)
        self._update_checker.result_ready.connect(self._on_update_result)
        self._update_checker.finished.connect(self._clear_update_checker)
        self._update_checker.start()

    def _clear_update_checker(self):
        self._update_checker = None

    def _on_update_result(self, result):
        found = bool(result.get("ok") and result.get("newer"))
        if found:
            self.connection_panel.set_update_state(
                f"Update: {result['tag']}", found=True)
        else:
            self.connection_panel.set_update_state("Check for Updates")

        # A startup check stays quiet unless there is genuinely something
        # new - being told "no update" every launch is noise, and being
        # told the network is down is the network's business, not the
        # program's.
        if getattr(self, "_update_silent", False) and not found:
            return
        UpdateDialog(result, APP_VERSION, self).exec()

    def _note_interval(self, attr, previous):
        """Rolling average of how often one message type arrives."""
        now = time.monotonic()
        if previous is not None:
            gap_ms = (now - previous) * 1000.0
            # Ignore a duplicate arrival and anything after a long silence:
            # neither says what the normal rate is.
            if 20.0 < gap_ms < 3000.0:
                current = getattr(self, attr)
                setattr(self, attr, current + (gap_ms - current) * 0.2)
        return now

    # Matches playbackDelayMs() in the 3D page, which takes this value from
    # here rather than working it out again.
    # Cache sizes in MB. Both are remembered between runs; these are
    # only what a fresh install starts with.
    MAP_CACHE_MB_DEFAULT = 500
    TERRAIN_CACHE_MB_DEFAULT = 2048
    SETTING_MAP_CACHE = "map_cache_mb"
    SETTING_TERRAIN_CACHE = "terrain_cache_mb"

    HUD_DELAY_MIN_MS = 150.0
    HUD_DELAY_MAX_MS = 1600.0
    HUD_DELAY_FACTOR = 1.6

    def _playback_delay_ms(self) -> float:
        slowest = max(self._att_interval_ms, self._pos_interval_ms)
        return max(self.HUD_DELAY_MIN_MS,
                   min(self.HUD_DELAY_MAX_MS, slowest * self.HUD_DELAY_FACTOR))

    def _record_hud_state(self):
        """Snapshot what the HUD is showing, with the time it was true."""
        if self._last_att_time is None:
            # No attitude has arrived yet, so the instruments are still at
            # their defaults. Recording that would have the overlay show a
            # level horizon for the length of the playback delay after
            # connecting, while the 3D already showed the real attitude.
            return
        h = self.horizon
        self._hud_history.append({
            "t": time.monotonic(),
            "roll": h.roll, "pitch": h.pitch, "heading": h.heading,
            "airspeed": h.airspeed, "altitude": h.altitude,
            "throttle": h.throttle,
            "lat": h.lat, "lon": h.lon,
        })

    @staticmethod
    def _blend(a, b, u):
        if a is None or b is None:
            return b if b is not None else a
        return a + (b - a) * u

    @staticmethod
    def _blend_angle(a, b, u):
        """Degrees, the short way round, so 359 -> 1 turns 2 not -358."""
        if a is None or b is None:
            return b if b is not None else a
        return a + (((b - a + 540.0) % 360.0) - 180.0) * u

    @staticmethod
    def _blend_rad(a, b, u):
        """The same for radians - roll comes off ATTITUDE over -pi..+pi, so
        rolling through inverted wraps and a plain average would spin the
        horizon the long way round."""
        if a is None or b is None:
            return b if b is not None else a
        return a + (((b - a + 3.0 * math.pi) % (2.0 * math.pi)) - math.pi) * u

    def _hud_state_at(self, when):
        """What the HUD was showing at `when`, from the two snapshots either
        side of it. None if there is nothing to interpolate between."""
        history = self._hud_history
        if len(history) < 2:
            return None
        if when <= history[0]["t"]:
            # Nothing recorded that far back yet - hold the oldest sample,
            # which is what the 3D view does with its own buffer.
            return history[0]
        if when >= history[-1]["t"]:
            return history[-1]
        for i in range(len(history) - 2, -1, -1):
            a, b = history[i], history[i + 1]
            if a["t"] <= when <= b["t"]:
                span = b["t"] - a["t"]
                u = (when - a["t"]) / span if span > 0 else 1.0
                return {
                    "roll": self._blend_rad(a["roll"], b["roll"], u),
                    "pitch": self._blend(a["pitch"], b["pitch"], u),
                    "heading": self._blend_angle(a["heading"], b["heading"], u),
                    "airspeed": self._blend(a["airspeed"], b["airspeed"], u),
                    "altitude": self._blend(a["altitude"], b["altitude"], u),
                    "throttle": self._blend(a["throttle"], b["throttle"], u),
                    "lat": self._blend(a["lat"], b["lat"], u),
                    "lon": self._blend(a["lon"], b["lon"], u),
                }
        return history[-1]

    def _push_fpv_position(self):
        """Hand one position sample to the 3D view.

        Position and attitude are pushed separately, as each arrives, rather
        than being paired up. They come in different MAVLink messages at
        different rates, so pairing them repeated whichever was older and
        made the view step instead of move.
        """
        if self._last_lat is None or self._last_lon is None:
            self.fpv_view.set_status("Waiting for position...")
            return
        self.fpv_view.set_status("")
        # Height above ground, which is what the 3D camera is positioned by.
        # TERRAIN_REPORT's is the real thing; height above home is the
        # fallback, and only differs once the ground itself rises or falls.
        agl = self._last_agl if self._last_agl is not None else self._last_alt
        self.fpv_view.set_position(self._last_lat, self._last_lon,
                                   self._last_amsl_alt, agl)

    def _push_fpv_attitude(self):
        yaw_deg, pitch_deg, roll_deg = self._last_att_deg
        self.fpv_view.set_playback_delay(self._playback_delay_ms())
        self.fpv_view.set_attitude(yaw_deg, pitch_deg, roll_deg)

    def _update_fpv_camera(self):
        """Both channels at once - used when switching into the view, so it
        has something to show before the next telemetry arrives."""
        self._push_fpv_position()
        self._push_fpv_attitude()

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
        # Draw the instruments at the moment the scene below them is
        # showing, not at the newest telemetry. The 3D view plays back
        # slightly in the past so it moves smoothly on a slow link; drawing
        # the HUD live made the lines lead the terrain by that much, which
        # showed up as the horizon levelling before the ground did.
        h = self.horizon
        live = (h.roll, h.pitch, h.heading, h.airspeed, h.altitude, h.lat,
                h.lon, h.throttle)
        delayed = self._hud_state_at(time.monotonic() - self._playback_delay_ms() / 1000.0)
        if delayed is not None:
            h.roll, h.pitch = delayed["roll"], delayed["pitch"]
            h.heading, h.airspeed = delayed["heading"], delayed["airspeed"]
            h.altitude = delayed["altitude"]
            h.throttle = delayed["throttle"]
            h.lat, h.lon = delayed["lat"], delayed["lon"]

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
            # The widget is shared with the 2D HUD, which must stay live.
            (h.roll, h.pitch, h.heading, h.airspeed,
             h.altitude, h.lat, h.lon, h.throttle) = live

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

    def on_vfr(self, airspeed, groundspeed, climb, throttle=None):
        self.telemetry.set_value("airspeed", f"{airspeed:.2f}")
        self.telemetry.set_value("groundspeed", f"{groundspeed:.2f}")
        self.telemetry.set_value("vspeed_mps", f"{climb:.2f}")
        self.horizon.set_airspeed(airspeed)
        self._return_home.set_airspeed(airspeed)
        if throttle is not None:
            self.horizon.set_throttle(throttle)
        self._last_groundspeed = groundspeed
        self._push_eta()
        self._last_climb = climb
        self._flight_stats.on_vfr(airspeed, groundspeed, climb, throttle)

    def on_wind(self, direction, speed):
        self.horizon.set_wind(direction, speed)
        self.map_view.set_wind(direction, speed)
        self._flight_stats.on_wind(speed)
        self._return_home.set_wind(direction, speed)
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

    def on_abort_landing(self):
        """Break off an AUTOLAND approach.

        No confirmation dialog, for the same reason the mode buttons have
        none: this is wanted in a hurry and with one hand.
        """
        link = self._require_link()
        if link:
            link.abort_landing()

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
        # The EKF and vibration colours already exist for the HUD flags;
        # the systems strip takes the same readings so a raised variance
        # or a shaking airframe shows there too.
        if "ekf_color" in status_dict:
            self.sensor_panel.set_ekf(status_dict["ekf_color"])
        if "vibe_color" in status_dict:
            self.sensor_panel.set_vibe(status_dict["vibe_color"])
        if "battery_remaining" in status_dict:
            # Already formatted for the telemetry row, and already '--'
            # where the autopilot is not reporting it, so it is carried
            # through as it stands rather than parsed and reformatted.
            self._battery_pct = status_dict["battery_remaining"]
        self._flight_stats.on_status(status_dict)
        if "mode" in status_dict:
            self.mode_panel.set_active_mode(status_dict["mode"])
            self._mode = status_dict["mode"]
            self._push_eta()
        if "armed" in status_dict:
            armed = status_dict["armed"] == "YES"
            self._armed = armed
            self.arm_panel.set_armed_state(armed)
            self._update_vehicle_state_label()
            self._set_flight_timer_running(armed)
            if armed:
                self.arm_panel.set_prearm_reason("")
                if not self._flight_stats.running:
                    # Same test the flight timer uses: if this link never saw
                    # the vehicle disarmed, we joined a flight already under
                    # way and the figures cover only part of it.
                    self._flight_stats.start(partial=not self._seen_disarmed)
            elif self._flight_stats.running:
                self._finish_flight()
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

    LINK_STATUS_STYLE = "font-weight: bold; font-size: 15px; color: {};"

    def _set_link_status(self, connected: bool, detail: str = ""):
        """The link indicator: just CONNECTED or DISCONNECTED, large enough
        to read at a glance, which across a field is the only part that
        matters.

        The specifics - sysid/compid, "waiting for heartbeat", or why a
        connection failed - go into the tooltip rather than being thrown
        away. That text is exactly what you need when a link will not come
        up at all, so it stays reachable.
        """
        self.status_label.setText("CONNECTED" if connected else "DISCONNECTED")
        self.status_label.setStyleSheet(
            self.LINK_STATUS_STYLE.format("lightgreen" if connected else "orange")
        )
        self.status_label.setToolTip(detail)

    def on_connection_status(self, connected, message):
        # Losing the aircraft while in the hidden view puts you back on the
        # HUD, where the arm state and the messages are. Only on the
        # transition: a disconnected link repeats this, and being thrown
        # out over and over while poking about on the bench would be
        # worse than staying.
        # On the transition only: a link that is down repeats this
        # status, and "waiting for heartbeat" arrives before ever being
        # connected, neither of which should clear anything.
        if self._was_connected and not connected:
            self._on_link_gone()
        self._was_connected = connected
        self._set_link_status(connected, message)
        # A failure reason is worth more than a tooltip - it is the thing
        # you need when nothing will connect, so put it on the line below,
        # where command feedback already appears.
        if not connected and message.lower().startswith("connection failed"):
            self._show_link_message(message)
        elif connected:
            # Once connected, the last attempt's complaint is history, and
            # it reads as a live problem sitting under a green CONNECTED.
            self._clear_link_message()

    def _show_link_message(self, text: str):
        """Put a connection problem on the feedback line, and remember that
        we were the ones who wrote it."""
        self.command_label.setText(text)
        self._link_message_shown = True

    def _clear_link_message(self):
        """Remove a connection problem, but never anything else.

        Tracked with a flag rather than by matching the text: there is more
        than one such message ("Connection failed: ..." from the link,
        "Can't connect: ..." from the panel's own validation), and matching
        one prefix meant the other stayed on screen under a green
        CONNECTED. A flag cannot be forgotten when a third message is added.
        """
        if self._link_message_shown:
            self.command_label.setText("")
            self._link_message_shown = False

    def on_command_feedback(self, message):
        if message.startswith("ACK:"):
            self.ack_label.setText(message)
        else:
            self.command_label.setText(message)
            # Whatever was there is now overwritten by real feedback, so
            # it is no longer ours to clear.
            self._link_message_shown = False

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

    def _finish_flight(self):
        """Close the flight off and show its report.

        Nothing is shown for a brief arm/disarm that never left the ground -
        a bench test is not a flight, and a dialog for one would be noise.
        """
        self._flight_stats.finish()
        earlier = self._flight_stats.mergeable_earlier(self._suspended_flights)
        # Judged on the whole flight, not just the piece we were connected
        # for. Reconnecting on short final and landing twenty seconds later
        # would otherwise throw away the quarter of an hour before it.
        whole = (self._flight_stats.merged_with(earlier) if earlier
                 else self._flight_stats)
        if not whole.worth_reporting():
            # An arm/disarm that never left the ground is not a flight, and
            # is not the continuation of one either. Any suspended segments
            # are left alone: a bench test between sorties should not
            # silently discard them.
            return
        FlightSummaryDialog(self._flight_stats, earlier, self,
                            home=self._home_pos).exec()
        # Offered once. Keeping them would let them resurface against an
        # unrelated flight later in the session.
        self._suspended_flights = []

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
        # A new connection is a new flight: start with a clean trail rather
        # than drawing this one on top of the last one's.
        self.map_view.clear_trail()
        # Home belongs to the vehicle we were talking to, not the next one.
        self.map_view.clear_home()
        att, pos, full = TelemetryRatesDialog.current()
        self.link = MavlinkLink(connection_string, attitude_hz=att,
                                position_hz=pos, full_telemetry=full)
        # Off unless asked for: this one sets a parameter on the aircraft,
        # which every other ground station shares.
        self.link.set_elevator_direction(
            TelemetryRatesDialog.elevator_direction())
        self.link.set_cog_enabled(TelemetryRatesDialog.cog_enabled())
        self.link.attitude_update.connect(self.on_attitude)
        self.link.position_update.connect(self.on_position)
        self.link.ground_track_update.connect(self.on_ground_track)
        self.link.nav_target_update.connect(self.on_nav_target)
        self.link.turn_rate_update.connect(self.on_turn_rate)
        self.link.home_bearing_update.connect(self.on_home_bearing)
        self.link.home_position_update.connect(self.on_home_position)
        self.link.elevator_update.connect(self._flight_stats.on_elevator)
        self.link.pitch_integrator_update.connect(
            self._flight_stats.on_pitch_integrator)
        self.link.trim_throttle_update.connect(self._on_trim_throttle)
        self.link.elevator_status.connect(self._on_elevator_status)
        self.link.vfr_update.connect(self.on_vfr)
        self.link.battery_power_update.connect(self.on_battery_power)
        self.link.battery_limits_update.connect(self.on_battery_limits)
        self.link.home_distance_update.connect(self.on_home_distance)
        self.link.wind_update.connect(self.on_wind)
        self.link.status_update.connect(self.on_status)
        self.link.mission_uploaded.connect(self.on_mission_uploaded)
        self.link.connection_status.connect(self.on_connection_status)
        self.link.command_feedback.connect(self.on_command_feedback)
        self.link.status_text_update.connect(self.messages_panel.add_message)
        self.link.sensor_health_update.connect(self.sensor_panel.set_health)
        self.link.gps_quality_update.connect(self.sensor_panel.set_gps_quality)
        self.link.ekf_variances_update.connect(self.sensor_panel.set_variances)
        self.link.status_text_update.connect(self.on_status_text)
        self.link.start()

    def on_connect_requested(self, connection_string):
        if not connection_string:
            self._show_link_message(
                "Can't connect: missing or invalid connection details"
            )
            return
        old_link = getattr(self, "link", None)
        if old_link is not None:
            old_link.stop()
        self._set_link_status(False, "Connecting...")
        # Clear the previous attempt's complaint as soon as a new one
        # starts, rather than leaving it up through "Connecting...".
        self._clear_link_message()
        self._connect_link(connection_string)

    def on_disconnect_requested(self):
        if self.link is not None:
            self.link.stop()
            self.link = None
        self._set_link_status(False, "Disconnected by the user")
        self.command_label.setText("")
        self._link_message_shown = False
        self.ack_label.setText("")
        self._on_link_gone()

    def on_tile_cache_limit(self, megabytes):
        self.tile_server.set_size_limit(int(megabytes) * 1024 * 1024)
        save_setting(self.SETTING_MAP_CACHE, int(megabytes))
        self._push_tile_cache_stats()

    def _apply_cache_limits(self):
        """Put the saved cache sizes into force and show them in the UI."""
        settings = load_settings()

        def size(key, default):
            try:
                return max(0, int(settings.get(key, default)))
            except (TypeError, ValueError):
                return default

        map_mb = size(self.SETTING_MAP_CACHE, self.MAP_CACHE_MB_DEFAULT)
        terrain_mb = size(self.SETTING_TERRAIN_CACHE,
                          self.TERRAIN_CACHE_MB_DEFAULT)
        self.tile_server.set_size_limit(map_mb * 1024 * 1024)
        terrain_provider.set_cache_limit(terrain_mb * 1024 * 1024)
        self.map_view.show_cache_limits(map_mb, terrain_mb)
        self._push_tile_cache_stats()

    def _on_trim_throttle(self, value):
        """The cruise power this airframe was trimmed for.

        Kept on the window as well as on the flight, because a new flight
        starts a fresh FlightStats and would otherwise lose it.
        """
        self._trim_throttle = value
        self._flight_stats.trim_throttle = value

    def _on_elevator_status(self, why):
        """The link saying whether it can read this aircraft's elevator.

        A property of the aircraft rather than of the flight, so it is
        kept here: FlightStats.reset() runs every flight and would drop
        it at the first takeoff.
        """
        self._elevator_unavailable = why
        self._push_cog_status()

    def _push_cog_status(self):
        """Keep the map's balance readout current, or hide it."""
        if not TelemetryRatesDialog.cog_enabled():
            self.map_view.set_cog_status("off", "", 0.0)
            return
        if self._elevator_unavailable:
            # Say why nothing is coming, rather than sit on "Waiting for
            # level cruise" forever on an aircraft this cannot read.
            self.map_view.set_cog_status(
                "unavailable", self._elevator_unavailable, 0.0)
            return
        state, text, shift = self._flight_stats.balance_status()
        self.map_view.set_cog_status(state, text, shift)

    def on_tile_cache_clear(self):
        self.tile_server.clear()
        self._push_tile_cache_stats()

    def on_terrain_cache_limit(self, megabytes):
        terrain_provider.set_cache_limit(int(megabytes) * 1024 * 1024)
        save_setting(self.SETTING_TERRAIN_CACHE, int(megabytes))
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

    def _on_link_gone(self):
        """Everything that must happen when the aircraft is no longer there.

        Deliberately one body shared by both ways of losing it - a link
        error and the Disconnect button - because they had drifted apart
        twice already. Each addition went to whichever path was in hand
        and the other quietly fell behind: a dropped radio left the panel
        reading ARMED while the button cleared it properly.
        """
        self._reset_vehicle_state()

    RETURN_HOME_TEXT = {
        "yes": "OK",
        "marginal": "MARGINAL",
        "no": "NO",
    }

    def _reset_vehicle_state(self):
        """
        Clear the indicators that would be actively wrong once the link is
        gone - the armed state above all, which left alone still reads
        ARMED in red after the vehicle is out of contact, along with the
        flight mode, the EKF/Vibe flags and the pre-arm reason.

        The last telemetry frame itself is deliberately left on screen:
        attitude, speeds, altitude, battery, and the position on the HUD
        and map. After a link loss - which is when this matters - that
        frame is the last thing known about the aircraft, and the position
        in particular is how you go and find it.
        """
        self._armed = False
        self._ready_to_arm = False
        # Same reason the EKF and Vibe flags go: a strip of green lights
        # for sensors nobody can see any more is worse than blank.
        self.sensor_panel.clear()
        # The estimate is about an aircraft that is still flying. With the
        # link gone the current draw is a stale number that would go on
        # being averaged into a verdict about nothing.
        self._return_home.reset()
        self._battery_pct = "--"
        self.map_view.set_return_home("off", "", "", "")
        self._update_vehicle_state_label()
        self._set_flight_timer_running(False)
        # A flight is closed off by the disarm arriving over telemetry. If
        # the link goes first that never comes, so the accumulator must not
        # simply stay open - the NEXT flight would be added onto this one,
        # counting the gap between them as flight time and the hop between
        # two locations as distance flown.
        #
        # It is not thrown away either. The aircraft may still be flying the
        # same sortie, in which case these figures are half of it. It is set
        # aside, and the next landing offers to include it.
        if self._flight_stats.running:
            self._flight_stats.suspend()
            self._suspended_flights.append(self._flight_stats)
            self._flight_stats = FlightStats()
            self._flight_stats.trim_throttle = self._trim_throttle
            # Anything too old to be part of a current flight is just memory.
            cutoff = time.monotonic() - FlightStats.MERGE_WINDOW_S
            self._suspended_flights = [
                seg for seg in self._suspended_flights if seg.end_time >= cutoff
            ]
        self._seen_disarmed = False   # nothing known about a link not yet up
        # The last telemetry frame stays on screen, but the ETA is not
        # part of it: altitude and position remain true of the moment the
        # link died, where "arriving in 6:40" stops being true the instant
        # it does. Nothing recomputes it either - it is pushed by incoming
        # messages - so left alone it sits there indefinitely.
        self._wp_dist = None
        self.map_view.set_eta("")
        self.arm_panel.set_prearm_reason("")
        self.arm_panel.set_armed_state(None)
        self.mode_panel.set_active_mode(None)
        self.horizon.set_ekf_status("white")
        self.horizon.set_vibe_status("white")
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
