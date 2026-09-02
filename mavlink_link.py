"""
Background thread that opens a MAVLink connection and turns incoming
messages into Qt signals the GUI can listen to.

Running this on a QThread (instead of the main thread) is important:
recv_match(blocking=True) would otherwise freeze your whole UI.
"""

import time
import math
import threading
from PySide6.QtCore import QThread, Signal
from pymavlink import mavutil

# Recent pymavlink versions dropped the "device:baud with no known prefix
# means serial" fallback - mavlink_connection() now treats ANY string
# containing a colon (that isn't a known tcp:/udp:/etc. prefix) as a UDP
# host:port, which sends "COM3" through socket.getaddrinfo() and fails
# with "[Errno 11001] getaddrinfo failed". So for serial connection
# strings like "com3:57600" we split the baud out ourselves and pass it
# as mavlink_connection()'s separate baud= argument instead.
_NETWORK_PREFIXES = (
    "tcp:", "tcpin:", "udp:", "udpin:", "udpout:", "udpbcast:",
    "mcast:", "ws:", "wss:", "wsserver:",
)


def _open_mavlink_connection(connection_string):
    if connection_string.startswith(_NETWORK_PREFIXES):
        return mavutil.mavlink_connection(connection_string)
    if ":" in connection_string:
        device, baud_str = connection_string.rsplit(":", 1)
        try:
            return mavutil.mavlink_connection(device, baud=int(baud_str))
        except ValueError:
            pass
    return mavutil.mavlink_connection(connection_string)

# ArduPlane flight modes this app exposes as buttons, with their
# custom_mode numbers confirmed against pymavlink's mode_mapping_apm.
# (Different vehicle types - copter, rover - use different numbers for
# the same names, so this mapping is ArduPlane-specific.)
PLANE_MODES = {
    "MANUAL": 0,
    "FBWA": 5,
    "CRUISE": 7,
    "LOITER": 12,
    "AUTO": 10,
    "RTL": 11,
    "TAKEOFF": 13,
    "AUTOLAND": 26,
    "AUTOTUNE": 8,
    "GUIDED": 15,
}


def _is_vehicle_heartbeat(msg):
    """True only for a heartbeat from an actual flight controller.

    Everything else that shares a MAVLink link - telemetry radios,
    companion computers, gimbals, ADS-B receivers, other GCSs, our own
    heartbeat echoed back by SITL/MAVProxy - reports its autopilot field
    as MAV_AUTOPILOT_INVALID. Their custom_mode and armed bit mean nothing
    for this vehicle.

    This is what distinguishes the vehicle, not "whoever heartbeat first".
    SITL has only the one heartbeat so anything works there, but a real
    airframe usually has several components talking, and locking onto the
    wrong one leaves the mode and armed state stuck forever - the GCS then
    reads READY TO ARM through an entire flight, because ready_to_arm
    comes from SYS_STATUS and isn't filtered, while armed comes from the
    heartbeats being thrown away.
    """
    return (msg.type != mavutil.mavlink.MAV_TYPE_GCS
            and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID)


# How long a mission upload may go without progress before it is abandoned.
# The upload is a request/response conversation with the vehicle, and every
# step of it can be lost on a radio link. Without a deadline a single
# dropped packet left the upload half-finished forever, and because a
# half-finished upload blocks the next one, Start Mission stopped working
# for the rest of the session. Refreshed on every step, so a long mission
# over a slow link is never cut short - this is "no progress at all", not
# "total time".
MISSION_STEP_TIMEOUT_S = 10.0


class MavlinkLink(QThread):
    # roll, pitch, yaw in radians
    attitude_update = Signal(float, float, float)
    # lat, lon (deg), relative altitude (m), heading (deg)
    position_update = Signal(float, float, float, float)
    # airspeed, groundspeed, climb rate (m/s)
    vfr_update = Signal(float, float, float)
    # wind direction (deg, coming FROM), wind speed (m/s)
    wind_update = Signal(float, float)
    # generic dict for things like mode/armed/battery
    status_update = Signal(dict)
    # connected flag + human-readable message
    connection_status = Signal(bool, str)
    # human-readable feedback after sending a command (success or failure)
    command_feedback = Signal(str)
    # Raised when the vehicle has ACCEPTED a mission, so the map can stop
    # showing edited altitudes as pending. Deliberately driven by the
    # vehicle's acknowledgement rather than by us pressing send.
    mission_uploaded = Signal()
    # STATUSTEXT from the vehicle: message text, MAV_SEVERITY (0-7)
    status_text_update = Signal(str, int)

    # Where the aircraft is actually travelling, as opposed to where its
    # nose points. Both come off GLOBAL_POSITION_INT, which already carries
    # ground velocity - it was simply being dropped. The two differ by the
    # crab angle, which is what makes the wind's effect visible on the map.
    ground_track_update = Signal(float, float)   # course deg, groundspeed m/s

    # What the navigation controller is steering at. NAV_CONTROLLER_OUTPUT
    # was already being parsed for the Dist to WP readout; these are the
    # bearings in the same message.
    nav_target_update = Signal(float, float)     # bearing deg, distance m

    # Yaw rate, for drawing where the current turn leads.
    turn_rate_update = Signal(float)             # deg/s

    # Which way home lies, for the arrow on the compass. -1 when the vehicle
    # is close enough that the bearing is meaningless.
    home_bearing_update = Signal(float)          # deg, or -1

    # Where home actually is, for the map marker.
    home_position_update = Signal(float, float)  # lat, lon

    def __init__(self, connection_string="udpin:0.0.0.0:14550", parent=None):
        """
        connection_string examples:
          "udpin:0.0.0.0:14550"   -> listen for incoming UDP packets on 14550
                                      (typical when the vehicle/companion
                                      computer streams telemetry TO you)
          "udpout:192.168.1.50:14550" -> actively connect out to a UDP endpoint
          "tcp:192.168.1.50:5760"     -> TCP connection
          "com3:57600" / "/dev/ttyUSB0:57600" -> serial telemetry radio
        """
        super().__init__(parent)
        self.connection_string = connection_string
        self._running = True
        self.master = None
        # mav.xxx_send() ends up doing a single socket write, but it's
        # called both from this thread's heartbeat loop and from the GUI
        # thread (fly_to, send_arm) - a lock keeps those from interleaving.
        self._send_lock = threading.Lock()
        # State for values MAVLink doesn't hand us directly - we compute
        # these ourselves from other messages (see run()).
        self._home_lat = None
        # Arming is when ArduPilot sets home, so an arm is the cue to ask
        # again rather than trust that the announcement reached us.
        self._was_armed = False
        self._home_lon = None
        self._last_amsl_alt_m = None  # from GLOBAL_POSITION_INT.alt (AMSL)
        self._gps_fix_type = None  # from GPS_RAW_INT.fix_type, used by the EKF status color
        self._clear_mission_deadline = None  # time.time() deadline for clear_mission()'s deferred send

        # Multi-waypoint mission upload state machine (see
        # upload_and_start_mission() and the MISSION_* handlers in run()).
        self._mission_pending = None   # list of (lat, lon, alt) once upload starts
        self._mission_deadline = None  # give up if the vehicle stops responding
        self._mission_restart = True   # False when updating a mission in flight
        self._mission_state = None     # None | 'awaiting_clear_ack' | 'uploading'

    def run(self):
        try:
            self._run_link()
        finally:
            # The thread owns the connection and closes it on its own way
            # out. Closing it from stop() instead meant the socket could be
            # pulled out from under this thread while it was still reading,
            # since stop() only waited a couple of seconds before giving up.
            master, self.master = self.master, None
            if master is not None:
                try:
                    master.close()
                except Exception:
                    pass

    def _run_link(self):
        try:
            self.master = _open_mavlink_connection(self.connection_string)

            # Announce ourselves BEFORE the first read. On an outgoing UDP
            # connection ("udpout:") pymavlink never binds the socket, and
            # Windows fails a recv on an unbound UDP socket outright with
            # WSAEINVAL 10022 ("an invalid argument was supplied") instead
            # of just blocking as POSIX does - so the very first read threw
            # before any telemetry could arrive. Sending first implicitly
            # binds the socket and clears that. It's also what a WiFi
            # telemetry bridge needs anyway: those only start streaming back
            # once they've heard from us (see the heartbeat loop below).
            try:
                with self._send_lock:
                    self.master.mav.heartbeat_send(
                        mavutil.mavlink.MAV_TYPE_GCS,
                        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                        0, 0, 0,
                    )
            except Exception:
                pass

            self.connection_status.emit(False, "Waiting for heartbeat...")
            # Deliberately NOT wait_heartbeat(timeout=30): that blocks the
            # whole 30s with no way to interrupt it, so a Disconnect (or a
            # reconnect elsewhere) during it left this thread running long
            # after stop() returned - still wired to the GUI, pushing stale
            # status into it. Polling in one-second slices lets stop() take
            # effect almost immediately.
            hb = None
            deadline = time.time() + 30
            while self._running and time.time() < deadline:
                hb = self.master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
                # Only a real flight controller can be the vehicle. Our own
                # heartbeat comes back off SITL/MAVProxy, another GCS may
                # share the port, and a real airframe has companions,
                # gimbals and radios heartbeating too - locking onto any of
                # those aims every command at it and, worse, makes the
                # filter below discard the vehicle's own heartbeats.
                if hb is not None and not _is_vehicle_heartbeat(hb):
                    hb = None
                    continue
                if hb is not None:
                    break
            if not self._running:
                return
            if hb is None:
                self.connection_status.emit(False, "Connection failed: no heartbeat received")
                return
            # wait_heartbeat() in this pymavlink version doesn't populate
            # target_component itself (only target_system gets locked onto
            # the first vehicle heartbeat internally) - set both explicitly
            # from the heartbeat we just got, since the HEARTBEAT filter
            # below and every command_long_send() call rely on them being
            # the real autopilot's ids, not the (0, 0) default.
            self.master.target_system = hb.get_srcSystem()
            self.master.target_component = hb.get_srcComponent()
            self.connection_status.emit(
                True,
                f"Connected (sysid={self.master.target_system}, "
                f"compid={self.master.target_component})",
            )
            # HOME_POSITION is usually only sent once (at boot or arming),
            # so ask for it explicitly in case we connected after that -
            # needed for the "Dist to Home" calculation.
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_GET_HOME_POSITION,
                    0, 0, 0, 0, 0, 0, 0, 0,
                )
                # SCALED_PRESSURE (for QNH), TERRAIN_REPORT (for Terrain
                # Alt), EKF_STATUS_REPORT and VIBRATION (for the HUD's
                # EKF/Vibe indicators) aren't guaranteed to stream by
                # default on every vehicle/firmware config - request them
                # explicitly at 2 Hz rather than silently depending on
                # whatever the vehicle's default stream rates happen to be.
                for msg_id in (
                    mavutil.mavlink.MAVLINK_MSG_ID_SCALED_PRESSURE,
                    mavutil.mavlink.MAVLINK_MSG_ID_TERRAIN_REPORT,
                    mavutil.mavlink.MAVLINK_MSG_ID_EKF_STATUS_REPORT,
                    mavutil.mavlink.MAVLINK_MSG_ID_VIBRATION,
                ):
                    self.master.mav.command_long_send(
                        self.master.target_system,
                        self.master.target_component,
                        mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                        0,
                        msg_id,
                        500000,  # microseconds -> 2 Hz
                        0, 0, 0, 0, 0,
                    )
        except Exception as e:
            self.connection_status.emit(False, f"Connection failed: {e}")
            return

        last_heartbeat_sent = 0.0

        while self._running:
            # Announce ourselves as a GCS once a second. For WiFi bridges
            # (ESP32/ESP8266-based modules, which is what most "ELRS module
            # hosts WiFi + MAVLink" setups use under the hood) this is also
            # what makes the module start unicasting telemetry back to us -
            # it's listening on UDP and won't send anything until it has
            # received a packet from our address.
            now = time.time()
            if now - last_heartbeat_sent > 1.0:
                try:
                    with self._send_lock:
                        self.master.mav.heartbeat_send(
                            mavutil.mavlink.MAV_TYPE_GCS,
                            mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                            0, 0, 0,
                        )
                except Exception:
                    pass
                last_heartbeat_sent = now

            # A mission upload that has stopped making progress is dead:
            # release it so the next Start Mission isn't refused as "already
            # in progress" for the rest of the session.
            if (
                self._mission_state is not None
                and self._mission_deadline is not None
                and now >= self._mission_deadline
            ):
                self._mission_state = None
                self._mission_pending = None
                self._mission_deadline = None
                self.command_feedback.emit(
                    "Mission upload timed out - no reply from the vehicle. "
                    "Try Start Mission again."
                )

            # Deferred half of clear_mission() (see there for why) - fires
            # once the LOITER mode switch it requested has had time to take
            # effect. Checked here in the background thread's own loop
            # rather than blocking the GUI thread that called clear_mission().
            if self._clear_mission_deadline is not None and now >= self._clear_mission_deadline:
                self._clear_mission_deadline = None
                try:
                    with self._send_lock:
                        self.master.mav.mission_clear_all_send(
                            self.master.target_system,
                            self.master.target_component,
                        )
                    self.command_feedback.emit("Cleared onboard mission")
                except Exception as e:
                    self.command_feedback.emit(f"Failed to clear mission: {e}")

            try:
                msg = self.master.recv_match(blocking=True, timeout=1)
            except OSError as e:
                # WSAECONNRESET (10054) is routine on Windows UDP, not a
                # broken link: sending to a peer that isn't listening yet
                # bounces an ICMP "port unreachable" back, and the next read
                # reports it. With "UDP (connect to)" against a bridge that
                # hasn't booted, surfacing it would flood the status line
                # with alarming errors while we wait perfectly normally.
                if getattr(e, "winerror", None) == 10054:
                    continue
                self.connection_status.emit(False, f"Link error: {e}")
                continue
            except Exception as e:
                self.connection_status.emit(False, f"Link error: {e}")
                continue

            if msg is None:
                continue

            mtype = msg.get_type()

            if mtype == "ATTITUDE":
                self.attitude_update.emit(msg.roll, msg.pitch, msg.yaw)
                self.turn_rate_update.emit(math.degrees(msg.yawspeed))

            elif mtype == "GLOBAL_POSITION_INT":
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.relative_alt / 1000.0
                heading = msg.hdg / 100.0 if msg.hdg != 65535 else 0.0
                self.position_update.emit(lat, lon, alt, heading)

                # vx/vy are ground velocity north/east in cm/s. Below a
                # walking pace their direction is GPS noise rather than a
                # heading, so the course is reported as unknown (-1) instead
                # of spinning a track line around a parked aircraft.
                groundspeed = math.hypot(msg.vx, msg.vy) / 100.0
                course = (math.degrees(math.atan2(msg.vy, msg.vx)) % 360.0
                          if groundspeed >= 1.0 else -1.0)
                self.ground_track_update.emit(course, groundspeed)

                self._last_amsl_alt_m = msg.alt / 1000.0
                # AMSL (not relative) altitude - needed to compare against
                # Copernicus terrain elevation for the terrain radar, which
                # is geoid-referenced (~= MSL), not relative-to-home.
                self.status_update.emit({"amsl_alt": f"{self._last_amsl_alt_m:.2f}"})
                if self._home_lat is not None:
                    dist_home = self._haversine_m(lat, lon, self._home_lat, self._home_lon)
                    self.status_update.emit({"dist_home": f"{dist_home:.2f}"})
                    # Standing on the launch point, "which way is home" has no
                    # answer - the bearing would swing wildly on GPS noise
                    # alone, so the arrow is hidden instead.
                    self.home_bearing_update.emit(
                        self._bearing_deg(lat, lon, self._home_lat, self._home_lon)
                        if dist_home > 15.0 else -1.0
                    )

            elif mtype == "VFR_HUD":
                self.vfr_update.emit(msg.airspeed, msg.groundspeed, msg.climb)

            elif mtype == "MISSION_ACK":
                if self._mission_state == "awaiting_clear_ack":
                    # Whatever the clear result, proceed to upload - clearing
                    # an already-empty mission can ACK oddly on some
                    # firmware and shouldn't block a fresh upload.
                    self._mission_state = "uploading"
                    self._mission_deadline = time.time() + MISSION_STEP_TIMEOUT_S
                    with self._send_lock:
                        self.master.mav.mission_count_send(
                            self.master.target_system,
                            self.master.target_component,
                            len(self._mission_pending),
                        )
                elif self._mission_state == "uploading":
                    if msg.type == mavutil.mavlink.MAV_MISSION_ACCEPTED:
                        n_real_waypoints = len(self._mission_pending) - 1  # exclude home placeholder
                        self.mission_uploaded.emit()
                        if self._mission_restart:
                            self.command_feedback.emit(
                                f"Mission uploaded ({n_real_waypoints} waypoints) - starting AUTO"
                            )
                            with self._send_lock:
                                self.master.mav.mission_set_current_send(
                                    self.master.target_system,
                                    self.master.target_component,
                                    1,  # index 1: the first REAL waypoint, index 0 is the home placeholder
                                )
                                self.master.mav.command_long_send(
                                    self.master.target_system,
                                    self.master.target_component,
                                    mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                                    0,
                                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                                    PLANE_MODES["AUTO"],
                                    0, 0, 0, 0, 0,
                                )
                        else:
                            # Deliberately no set_current and no mode change:
                            # the aircraft keeps flying the leg it is on.
                            self.command_feedback.emit(
                                f"Mission updated ({n_real_waypoints} waypoints) - "
                                "continuing on the current leg"
                            )
                    else:
                        try:
                            result_name = mavutil.mavlink.enums["MAV_MISSION_RESULT"][msg.type].name
                        except (KeyError, AttributeError):
                            result_name = str(msg.type)
                        self.command_feedback.emit(f"Mission upload failed: {result_name}")
                    self._mission_state = None
                    self._mission_pending = None
                    self._mission_deadline = None

            elif mtype in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
                # ArduPilot may use either depending on version - handle both.
                if (
                    self._mission_state == "uploading"
                    and self._mission_pending is not None
                    and msg.seq < len(self._mission_pending)
                ):
                    lat, lon, alt = self._mission_pending[msg.seq]
                    # The vehicle is still asking for items, so the upload is
                    # alive however long the whole mission takes.
                    self._mission_deadline = time.time() + MISSION_STEP_TIMEOUT_S
                    with self._send_lock:
                        self.master.mav.mission_item_int_send(
                            self.master.target_system,
                            self.master.target_component,
                            msg.seq,
                            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                            0,  # current
                            1,  # autocontinue
                            0, 0, 0, 0,  # param1-4
                            int(lat * 1e7),
                            int(lon * 1e7),
                            float(alt),
                        )

            elif mtype == "STATUSTEXT":
                text = msg.text
                if isinstance(text, bytes):
                    text = text.decode(errors="replace")
                text = text.rstrip("\x00").strip()
                self.status_text_update.emit(text, msg.severity)

            elif mtype == "COMMAND_ACK":
                try:
                    cmd_name = mavutil.mavlink.enums["MAV_CMD"][msg.command].name
                except (KeyError, AttributeError):
                    cmd_name = f"command {msg.command}"
                try:
                    result_name = mavutil.mavlink.enums["MAV_RESULT"][msg.result].name
                except (KeyError, AttributeError):
                    result_name = f"result {msg.result}"
                self.command_feedback.emit(f"ACK: {cmd_name} -> {result_name}")

            elif mtype == "PARAM_VALUE":
                # Confirmation for change_loiter_radius() - a PARAM_SET is
                # fire-and-forget (no COMMAND_ACK), so the echoed
                # PARAM_VALUE is the only proof the vehicle took the value.
                param_id = msg.param_id
                if isinstance(param_id, bytes):
                    param_id = param_id.decode(errors="replace")
                if param_id.rstrip("\x00") == "WP_LOITER_RAD":
                    self.command_feedback.emit(
                        f"Loiter radius now {msg.param_value:.0f} m"
                    )

            elif mtype == "WIND":
                # ArduPilot-specific message (id 168): direction is where
                # the wind is coming FROM, in degrees; speed in m/s.
                self.wind_update.emit(msg.direction, msg.speed)

            elif mtype == "WIND_COV":
                # The "common"/official wind message - some ArduPilot
                # configs stream this instead of (or as well as) the
                # legacy WIND message above. Gives a NED wind *velocity*
                # vector (the direction air is moving TOWARD), which we
                # convert to the same "coming FROM" convention as WIND.
                #
                # Note the negated wind_y: empirically (confirmed against
                # a real vehicle) using it unnegated mirrored the result
                # (output = 360 - actual direction). That's the signature
                # of a flipped sign on the East component - negating it
                # here corrects it.
                speed = math.hypot(msg.wind_x, msg.wind_y)
                bearing_toward = math.degrees(math.atan2(-msg.wind_y, msg.wind_x))
                direction_from = (bearing_toward + 180) % 360
                self.wind_update.emit(direction_from, speed)

            elif mtype == "HOME_POSITION":
                lat = msg.latitude / 1e7
                lon = msg.longitude / 1e7
                moved = (self._home_lat is None
                         or abs(lat - self._home_lat) > 1e-7
                         or abs(lon - self._home_lon) > 1e-7)
                self._home_lat = lat
                self._home_lon = lon
                # Only on a real change: this arrives again every time it is
                # re-requested, and redrawing an unmoved marker is noise.
                if moved:
                    self.home_position_update.emit(lat, lon)

            elif mtype == "GPS_RAW_INT":
                sat_count = "--" if msg.satellites_visible == 255 else str(msg.satellites_visible)
                hdop = "--" if msg.eph == 65535 else f"{msg.eph / 100.0:.2f}"
                self.status_update.emit({"sat_count": sat_count, "gps_hdop": hdop})
                self._gps_fix_type = msg.fix_type

            elif mtype == "EKF_STATUS_REPORT":
                # Ported from Mission Planner's own CurrentState.cs/HUD.cs
                # (not just the general ArduPilot variance guidance) so the
                # HUD's EKF indicator matches MP's exactly: > 0.5 -> yellow,
                # > 0.8 -> red, taken from the worst of all 5 variances
                # (MP's own code comment: "> 1, between 0-1 typical > 1 =
                # reject measurement - red / 0.5 > amber"), overridden to
                # red if EKF_ATTITUDE is missing, if EKF_VELOCITY_HORIZ is
                # missing while we have a GPS fix, or if EKF_UNINITIALIZED
                # is set. MP does NOT check EKF_GPS_GLITCHING/
                # EKF_CONST_POS_MODE here, despite their names suggesting
                # otherwise.
                ekfstatus = max(
                    msg.velocity_variance,
                    msg.compass_variance,
                    msg.pos_horiz_variance,
                    msg.pos_vert_variance,
                    msg.terrain_alt_variance,
                )
                have_gps_fix = (self._gps_fix_type or 0) > 0
                if not (msg.flags & mavutil.mavlink.EKF_ATTITUDE):
                    ekfstatus = 1.0
                elif not (msg.flags & mavutil.mavlink.EKF_VELOCITY_HORIZ) and have_gps_fix:
                    ekfstatus = 1.0
                elif msg.flags & mavutil.mavlink.EKF_UNINITIALIZED:
                    ekfstatus = 1.0

                if ekfstatus > 0.8:
                    ekf_color = "red"
                elif ekfstatus > 0.5:
                    ekf_color = "yellow"
                else:
                    ekf_color = "white"
                self.status_update.emit({"ekf_color": ekf_color})

            elif mtype == "VIBRATION":
                # Ported from Mission Planner's HUD.cs: > 30 -> yellow,
                # > 60 -> red, on the raw per-axis vibration values. MP's
                # HUD does NOT factor in the clipping counters here (that
                # was our own addition, and was the bug - clipping starts
                # incrementing well before 30-60 on plenty of boards, which
                # was forcing red and skipping the yellow range entirely).
                vibe_max = max(msg.vibration_x, msg.vibration_y, msg.vibration_z)
                if vibe_max > 60:
                    vibe_color = "red"
                elif vibe_max > 30:
                    vibe_color = "yellow"
                else:
                    vibe_color = "white"
                self.status_update.emit({"vibe_color": vibe_color})

            elif mtype == "NAV_CONTROLLER_OUTPUT":
                self.status_update.emit({"wp_dist": f"{msg.wp_dist:.2f}"})
                # The same message says which way the controller is steering
                # and how far it has to go - enough to draw the line to the
                # waypoint without downloading the mission.
                self.nav_target_update.emit(float(msg.target_bearing),
                                            float(msg.wp_dist))

            elif mtype == "RANGEFINDER":
                self.status_update.emit({"rangefinder_m": f"{msg.distance:.2f}"})

            elif mtype == "DISTANCE_SENSOR":
                # More commonly streamed than the legacy RANGEFINDER message
                # above on current ArduPilot builds. current_distance is in
                # cm, so divide by 100 to match RANGEFINDER's meters.
                # orientation 25 = ROTATION_PITCH_270 = downward-facing,
                # the standard altitude rangefinder - what "Rangefinder"
                # means here. Other orientations (obstacle-avoidance
                # sensors etc.) are ignored.
                if msg.orientation == mavutil.mavlink.MAV_SENSOR_ROTATION_PITCH_270:
                    self.status_update.emit({"rangefinder_m": f"{msg.current_distance / 100.0:.2f}"})

            elif mtype == "SCALED_PRESSURE":
                # MAVLink has no native "QNH" field - we estimate it from
                # absolute pressure and AMSL altitude using the standard
                # ISA barometric formula (the same relationship a real
                # altimeter setting is derived from). Needs an AMSL
                # altitude reading first, so this stays "--" until then.
                if self._last_amsl_alt_m is not None:
                    qnh = msg.press_abs / (
                        (1 - 0.0065 * self._last_amsl_alt_m / 288.15) ** 5.255
                    )
                    self.status_update.emit({"qnh": f"{qnh:.2f}"})

            elif mtype == "TERRAIN_REPORT":
                # current_height is the vehicle's height above the terrain
                # right beneath it - true AGL, which the 3D view needs to
                # place its camera without depending on a sea-level datum.
                self.status_update.emit({
                    "terrain_gl": f"{msg.terrain_height:.2f}",
                    "agl": f"{msg.current_height:.2f}",
                })

            elif mtype == "BATTERY_STATUS":
                # Consumed capacity is the number that actually tells you
                # how much of the pack you used - voltage sags under load
                # and recovers, so it is a poor proxy. -1 means the
                # autopilot has no current sensor, so there is nothing to
                # report rather than a misleading zero.
                if getattr(msg, "current_consumed", -1) >= 0:
                    self.status_update.emit(
                        {"battery_mah": f"{msg.current_consumed}"}
                    )

            elif mtype == "SYS_STATUS":
                # MAV_SYS_STATUS_PREARM_CHECK is just another bit in the
                # sensor present/enabled/health bitmasks - "present" gates
                # whether the bit means anything at all (mirrors how every
                # other sensor flag here is interpreted), "healthy" is the
                # actual pass/fail Mission Planner's "Ready to arm" reflects.
                prearm_bit = mavutil.mavlink.MAV_SYS_STATUS_PREARM_CHECK
                ready_to_arm = bool(
                    msg.onboard_control_sensors_present & prearm_bit
                    and msg.onboard_control_sensors_health & prearm_bit
                )
                self.status_update.emit(
                    {
                        # SYS_STATUS uses sentinels for "the autopilot isn't
                        # reporting this": UINT16_MAX for voltage and -1 for
                        # remaining. Taken literally those render as a
                        # plausible-looking 65.54 V / 16.38 V per cell.
                        "battery_voltage": (
                            "--" if msg.voltage_battery == 65535
                            else f"{msg.voltage_battery / 1000.0:.2f}"
                        ),
                        "battery_remaining": (
                            "--" if msg.battery_remaining == -1
                            else f"{msg.battery_remaining}"
                        ),
                        "ready_to_arm": "YES" if ready_to_arm else "NO",
                    }
                )

            elif mtype == "HEARTBEAT":
                # Only the vehicle's own heartbeats carry a mode and armed
                # bit that mean anything. Without this the mode display
                # flickers between the real mode and whatever the other
                # components on the link report (usually 0/no match).
                #
                # Matched on system id plus "is a flight controller" rather
                # than on an exact component id: the component id is only a
                # guess at connect time, and if that guess is wrong every
                # real heartbeat gets dropped and the mode and armed state
                # never update again.
                if not _is_vehicle_heartbeat(msg):
                    continue
                if msg.get_srcSystem() != self.master.target_system:
                    continue

                try:
                    mode = mavutil.mode_string_v10(msg)
                except Exception:
                    mode = str(msg.custom_mode)
                armed = bool(
                    msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                )
                self.status_update.emit({"mode": mode, "armed": "YES" if armed else "no"})

                # ArduPilot sets home at arming and announces it once. Once
                # is not much of a guarantee over a radio link that drops
                # packets, and missing it means no home marker for the whole
                # flight - so ask directly on the transition.
                if armed and not self._was_armed:
                    self._request_home()
                self._was_armed = armed

    def _request_home(self):
        """Ask the vehicle to send HOME_POSITION now."""
        if self.master is None:
            return
        try:
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_GET_HOME_POSITION,
                    0, 0, 0, 0, 0, 0, 0, 0,
                )
        except Exception:
            # Best effort: the marker simply waits for the next arm.
            pass

    # Force-arm/disarm magic value per MAV_CMD_COMPONENT_ARM_DISARM's spec:
    # bypasses ArduPilot's pre-arm safety checks.
    MAV_ARM_DISARM_FORCE_MAGIC = 21196

    def send_arm(self, arm: bool, force: bool = False):
        """
        Arm or disarm via MAV_CMD_COMPONENT_ARM_DISARM. With force=True,
        bypasses ArduPilot's pre-arm safety checks (e.g. arming without
        GPS lock, or disarming mid-flight) - use deliberately.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't arm/disarm")
            return
        try:
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0,
                    1 if arm else 0,
                    self.MAV_ARM_DISARM_FORCE_MAGIC if force else 0,
                    0, 0, 0, 0, 0,
                )
            action = "ARM" if arm else "DISARM"
            if force:
                action += " (forced)"
            self.command_feedback.emit(f"Requested {action}")
        except Exception as e:
            self.command_feedback.emit(f"Failed to arm/disarm: {e}")

    def preflight_calibration(self):
        """
        Trigger ground pressure (baro) + airspeed calibration via
        MAV_CMD_PREFLIGHT_CALIBRATION - confirmed against a real vehicle's
        message log to match exactly what Mission Planner's "Preflight
        Calibration" Do Action sends (baro calibrates first, then
        airspeed - same order ArduPilot logs them in). An earlier version
        of this used gyro instead of airspeed, which was wrong - ArduPilot
        produced no calibration messages at all for that combination.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't run preflight calibration")
            return
        try:
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_PREFLIGHT_CALIBRATION,
                    0,
                    0,  # param1: gyro - off
                    0,  # param2: magnetometer - off
                    1,  # param3: ground pressure (baro) calibration
                    0,  # param4: RC - off
                    0,  # param5: accelerometer - off
                    2,  # param6: airspeed calibration
                    0,  # param7: ESC - off
                )
            self.command_feedback.emit("Requested preflight calibration (baro + airspeed)")
        except Exception as e:
            self.command_feedback.emit(f"Failed to run preflight calibration: {e}")

    def upload_and_start_mission(self, waypoints, alt_relative_m: float,
                                 restart: bool = True):
        """
        Upload a sequence of (lat, lon) points as a real onboard AUTO
        mission, then switch to AUTO to fly it. Unlike fly_to(), this
        doesn't depend on the GCS staying connected once it's uploaded -
        ArduPilot owns the waypoint-to-waypoint progression itself, onboard,
        using its own WP_RADIUS "close enough" logic. This is the same
        underlying mechanism Mission Planner's flight-plan tab uses.

        This kicks off a small state machine (see the MISSION_ACK and
        MISSION_REQUEST_INT/MISSION_REQUEST handlers in run()) - clear any
        existing mission, upload the new one item-by-item as the vehicle
        requests each one, then start it.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't upload mission")
            return
        if not waypoints:
            self.command_feedback.emit("No waypoints to upload")
            return
        if self._mission_state is not None:
            self.command_feedback.emit("A mission upload is already in progress")
            return

        # ArduPilot convention (which Mission Planner also follows): mission
        # item 0 is treated as a home/reference placeholder, not the first
        # real waypoint - AUTO execution starts at item 1. Without this,
        # the vehicle silently starts at the SECOND clicked point instead
        # of the first, since it treats item 0 as home rather than flying
        # to it. Use the known home position if we have it; otherwise fall
        # back to duplicating the first real waypoint (still gives a valid
        # item 0, just without a truly meaningful "home" coordinate).
        if self._home_lat is not None and self._home_lon is not None:
            placeholder = (self._home_lat, self._home_lon, 0.0)
        else:
            placeholder = (waypoints[0][0], waypoints[0][1], 0.0)

        # Each point may carry its own altitude; those that don't take the
        # mission default. Written as (lat, lon) or (lat, lon, alt).
        resolved = []
        for wp in waypoints:
            if len(wp) >= 3 and wp[2] is not None:
                resolved.append((wp[0], wp[1], float(wp[2])))
            else:
                resolved.append((wp[0], wp[1], float(alt_relative_m)))
        self._mission_pending = [placeholder] + resolved
        # An update to a mission already flying must not send the aircraft
        # back to waypoint 1 - it carries on from wherever it is and picks
        # up the new altitudes on the legs it hasn't flown yet.
        self._mission_restart = restart
        self._mission_state = "awaiting_clear_ack"
        self._mission_deadline = time.time() + MISSION_STEP_TIMEOUT_S
        self.command_feedback.emit(
            f"{'Uploading' if restart else 'Updating'} "
            f"{len(waypoints)}-waypoint mission..."
        )
        try:
            with self._send_lock:
                self.master.mav.mission_clear_all_send(
                    self.master.target_system,
                    self.master.target_component,
                )
        except Exception as e:
            self._mission_state = None
            self._mission_pending = None
            self._mission_deadline = None
            self.command_feedback.emit(f"Failed to start mission upload: {e}")

    def clear_mission(self):
        """
        Erase the vehicle's onboard mission via MISSION_CLEAR_ALL, standalone
        from upload_and_start_mission()'s own clear-then-upload state
        machine - used by the waypoint panel's Clear button, which should
        wipe the vehicle's mission even when there's nothing queued up to
        upload in its place. Also aborts any upload that happens to be
        mid-flight, since a stale MISSION_ACK arriving afterward would
        otherwise be misread as this clear's result.

        ArduPilot silently refuses to clear/modify a mission while it's
        actively flying it in AUTO (confirmed by testing: the identical
        clear works instantly in LOITER, but does nothing in AUTO) - so
        this forces LOITER first, then sends the actual clear ~0.5s later
        via the run() loop rather than blocking here with a sleep (this
        method is called directly from the GUI thread), giving the mode
        switch time to take effect onboard before the clear arrives.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't clear mission")
            return
        self._mission_state = None
        self._mission_pending = None
        self._mission_deadline = None
        self.set_mode("LOITER")
        self._clear_mission_deadline = time.time() + 0.5
        self.command_feedback.emit("Switching to LOITER, then clearing mission...")

    def fly_to(self, lat: float, lon: float, alt_relative_m: float):
        """
        Send the vehicle to (lat, lon, alt_relative_m) using
        MAV_CMD_DO_REPOSITION - the standard MAVLink "guided goto" command
        (see mavlink.io common.xml). It's sent via COMMAND_INT so we can
        specify the altitude frame explicitly, and with the
        MAV_DO_REPOSITION_FLAGS_CHANGE_MODE bit set in param2 so the
        vehicle switches into GUIDED mode as part of this same command -
        no separate DO_SET_MODE call needed.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't send fly-to command")
            return

        MAV_DO_REPOSITION_FLAGS_CHANGE_MODE = 1

        try:
            with self._send_lock:
                self.master.mav.command_int_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                    mavutil.mavlink.MAV_CMD_DO_REPOSITION,
                    0,  # current
                    0,  # autocontinue
                    -1,  # param1: ground speed, -1 = leave unchanged
                    MAV_DO_REPOSITION_FLAGS_CHANGE_MODE,  # param2: bitmask
                    0,  # param3: loiter radius (planes only)
                    float("nan"),  # param4: yaw, NaN = autopilot default
                    int(lat * 1e7),
                    int(lon * 1e7),
                    float(alt_relative_m),
                )
            self.command_feedback.emit(
                f"Sent fly-to: {lat:.6f}, {lon:.6f} @ {alt_relative_m:.0f} m (relative)"
            )
        except Exception as e:
            self.command_feedback.emit(f"Failed to send fly-to command: {e}")

    def change_speed(self, speed_mps: float, speed_type: int = 0):
        """
        Change the commanded speed via MAV_CMD_DO_CHANGE_SPEED (id 178).
        Reverted back to this after confirming DO_REPOSITION (used for a
        prior version of this method) is gated in ArduPlane's firmware
        behind "already in a guided-family mode" - it silently does
        nothing outside GUIDED/AUTO/etc unless the CHANGE_MODE flag is
        set, which is exactly why altitude changes were failing in
        LOITER, and switching speed onto that same command would have
        broken it too. DO_CHANGE_SPEED has no such restriction - it just
        updates the target airspeed value the active mode's speed
        controller reads, regardless of mode.
        speed_type: 0=airspeed, 1=groundspeed (SPEED_TYPE enum).
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't change speed")
            return
        try:
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                    0,
                    speed_type,
                    float(speed_mps),
                    -1,  # throttle: -1 = no change
                    0,   # 0 = absolute (not relative)
                    0, 0, 0,
                )
            self.command_feedback.emit(f"Requested speed change: {speed_mps:.1f} m/s")
        except Exception as e:
            self.command_feedback.emit(f"Failed to change speed: {e}")

    def change_altitude(self, alt_relative_m: float, current_alt_relative_m: float = None, rate_mps: float = 0.0):
        """
        Change target altitude via MAV_CMD_NAV_WAYPOINT (id 16) wrapped in
        a MISSION_ITEM message with current=3 - confirmed by directly
        capturing Mission Planner's own traffic (via a .tlog file) while
        its Change Altitude button worked correctly in LOITER on real
        hardware. current=3 is an ArduPilot-specific convention distinct
        from the well-known current=2 "guided mode override" trick: it
        means "update only the altitude of the current navigation
        target," leaving position and flight mode untouched - which is
        exactly why it works in LOITER where GUIDED_CHANGE_ALTITUDE and
        DO_REPOSITION (both tried first) do not. x/y are sent as 0 since
        they're ignored under current=3.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't change altitude")
            return
        try:
            with self._send_lock:
                self.master.mav.mission_item_send(
                    self.master.target_system,
                    self.master.target_component,
                    0,  # seq
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                    3,  # current = 3: ArduPilot's altitude-only update, no mode change
                    1,  # autocontinue
                    0, 0, 0, 0,  # param1-4
                    0.0, 0.0,  # x, y - ignored when current=3
                    float(alt_relative_m),  # z: target altitude
                )
            self.command_feedback.emit(
                f"Requested altitude change: {alt_relative_m:.0f} m relative"
            )
        except Exception as e:
            self.command_feedback.emit(f"Failed to change altitude: {e}")

    def change_loiter_radius(self, radius_m: float):
        """
        Change the loiter radius by setting ArduPlane's WP_LOITER_RAD
        parameter via PARAM_SET - the same mechanism Mission Planner uses
        for this (it's a parameter, not a command: there is no MAVLink
        "set loiter radius" message). Applies to every loiter the vehicle
        flies from then on - LOITER mode, RTL's circle, AUTO loiter
        waypoints - not just the current one.

        A negative radius is meaningful to ArduPlane, not an error: it
        makes the aircraft circle counter-clockwise instead of clockwise.

        The vehicle echoes a PARAM_VALUE back on success, which the run()
        loop turns into confirmation feedback.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't change loiter radius")
            return
        try:
            with self._send_lock:
                self.master.mav.param_set_send(
                    self.master.target_system,
                    self.master.target_component,
                    b"WP_LOITER_RAD",
                    float(radius_m),
                    mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
                )
            self.command_feedback.emit(f"Requested loiter radius: {radius_m:.0f} m")
        except Exception as e:
            self.command_feedback.emit(f"Failed to change loiter radius: {e}")

    def set_mode(self, mode_name: str):
        """
        Request a flight mode change via MAV_CMD_DO_SET_MODE. Deliberately
        no confirmation dialog at this layer - mode buttons (especially
        RTL) need to be instant, single-click actions the way they are in
        Mission Planner/QGC, since a pilot may need one in a hurry.
        """
        if self.master is None:
            self.command_feedback.emit("Not connected - can't change mode")
            return

        custom_mode = PLANE_MODES.get(mode_name)
        if custom_mode is None:
            self.command_feedback.emit(f"Unknown mode: {mode_name}")
            return

        try:
            with self._send_lock:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                    0,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    custom_mode,
                    0, 0, 0, 0, 0,
                )
            self.command_feedback.emit(f"Requested mode change: {mode_name}")
        except Exception as e:
            self.command_feedback.emit(f"Failed to change mode: {e}")

    @staticmethod
    def _haversine_m(lat1, lon1, lat2, lon2):
        """Great-circle distance in meters between two lat/lon points."""
        r = 6371000.0  # Earth radius, m
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    @staticmethod
    def _bearing_deg(lat1, lon1, lat2, lon2):
        """Initial great-circle bearing from the first point to the second,
        in degrees clockwise from north."""
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        y = math.sin(dl) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def stop(self):
        """
        Ask the thread to finish and wait for it. The socket is closed by
        run()'s own finally block, not here - see the note there.

        Every blocking read in this thread uses a one-second timeout, so it
        notices _running within about a second; the generous wait is only
        so a reconnect can't race a still-live thread that is still wired to
        the GUI's slots. terminate() is a last resort for a thread wedged
        somewhere uninterruptible, which is better than letting Qt destroy
        it while it runs (that aborts the process).
        """
        self._running = False
        if not self.wait(5000):
            self.terminate()
            self.wait(1000)
