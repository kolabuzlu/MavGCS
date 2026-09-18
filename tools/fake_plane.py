"""A synthetic ArduPlane in LOITER, streaming everything the real one does.

Not part of the app. bench.bat drives the map page with position and
heading only, overlays off, and no longer reaches the post-quantisation
crash. The run that did reach it was the real app fed by SITL with every
overlay on. This stands in for SITL: every message type mavlink_link.py
dispatches on, at the rates ArduPilot sends them, for an aircraft
circling the home tile area at 20 m/s with 30 degrees of bank.

Sends to udpout:127.0.0.1:PORT (the app listens on udpin:0.0.0.0:PORT).
Answers parameter reads and a streamed parameter list, NAKs MAVFTP so the
app falls back to streaming, and ACKs every command. Each send is guarded
so one bad field cannot silence the whole stream; failures are logged
once per message type.
"""
import argparse
import math
import os
import struct
import sys
import time

# Before pymavlink is imported: it decides its dialect at import
# time, and the app it is pretending to talk to speaks MAVLink 2.
os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=14550)
ap.add_argument("--listen", action="store_true",
                help="wait to be spoken to instead of sending. Binds the "
                     "port and answers whoever addresses it first, which "
                     "is what an autopilot set to udpin does - and what "
                     "the app's 'UDP (connect to)' reaches. Without this "
                     "the plane sends to the port and the app listens.")
ap.add_argument("--radius", type=float, default=150.0)      # m
ap.add_argument("--speed", type=float, default=20.0)        # m/s ground
ap.add_argument("--alt", type=float, default=100.0)         # m AGL
ap.add_argument("--porpoise", type=float, default=3.0,
                help="metres of gentle climb and descent either side of "
                     "--alt, on a 46-second cycle. The default is small "
                     "enough to be realistic cruise; raise it to exercise "
                     "anything that draws altitude against terrain")
ap.add_argument("--attitude-hz", type=float, default=5.0)
ap.add_argument("--ignore-param-sets", type=int, default=0,
                help="silently drop this many PARAM_SET writes before "
                     "honouring one - a lossy link swallowing a write, "
                     "which has no acknowledgement to miss")
ap.add_argument("--ignore-mode-requests", type=int, default=0,
                help="silently drop this many DO_SET_MODE commands before "
                     "honouring one - what a lossy link does to a button "
                     "press, and what MavGCS now has to ride out")
args = ap.parse_args()

HOME_LAT, HOME_LON = 39.925386148184316, 32.83652351127223
HOME_ALT_MSL = 890.0          # Ankara-ish; only relative alt matters
T0 = time.time()


def log(s):
    print("%7.1f PLANE %s" % (time.time() - T0, s), flush=True)


# udpin binds and waits; pymavlink learns where to reply from the first
# datagram that arrives, exactly as an autopilot does. udpout sends from
# the first moment and needs nobody to speak first.
_link = ("udpin:0.0.0.0:%d" if args.listen else "udpout:127.0.0.1:%d")
conn = mavutil.mavlink_connection(_link % args.port,
                                  source_system=1, source_component=1)
mav = conn.mav
M = mavutil.mavlink

# ---- parameters the app asks for --------------------------------------
values = {}
for ch in range(1, 9):
    values["SERVO%d_FUNCTION" % ch] = {1: 4, 2: 19, 3: 70, 4: 21}.get(ch, 0)
    values["SERVO%d_MIN" % ch] = 1000
    values["SERVO%d_MAX" % ch] = 2000
    values["SERVO%d_TRIM" % ch] = 1500
    values["SERVO%d_REVERSED" % ch] = 0
values.update({"GCS_PID_MASK": 0, "TRIM_THROTTLE": 45, "TRIM_ARSPD_CM": 1800,
               "ARSPD_FBW_MIN": 12, "ARSPD_FBW_MAX": 28, "WP_LOITER_RAD": 150,
               "RTL_RADIUS": 150, "ALT_HOLD_RTL": 10000, "FS_SHORT_ACTN": 0,
               "FS_LONG_ACTN": 1, "THR_MAX": 100, "THR_MIN": 0,
               "LIM_ROLL_CD": 4500, "LIM_PITCH_MAX": 2000, "LIM_PITCH_MIN": -2500,
               "BATT_CAPACITY": 5200, "BATT_LOW_VOLT": 14.0, "SYSID_THISMAV": 1,
               "TERRAIN_ENABLE": 1, "TERRAIN_FOLLOW": 0, "RNGFND1_TYPE": 10,
               "AHRS_EKF_TYPE": 3, "EK3_ENABLE": 1, "COMPASS_USE": 1})
names = list(values)
index_of = {n: i for i, n in enumerate(names)}


def send_param(name, index):
    mav.param_value_send(name.encode(), float(values[name]),
                         M.MAV_PARAM_TYPE_REAL32, len(names), index)


# ---- the flight ---------------------------------------------------------
period = 2 * math.pi * args.radius / args.speed
m_per_deg_lat = 111320.0
m_per_deg_lon = 111320.0 * math.cos(math.radians(HOME_LAT))
bank = math.radians(30.0)


def state(t):
    """Position, velocity and attitude at time t, circling clockwise."""
    ang = (t / period) * 2 * math.pi
    x = args.radius * math.sin(ang)          # east
    y = args.radius * math.cos(ang)          # north
    vx = args.speed * math.cos(ang)          # east velocity
    vy = -args.speed * math.sin(ang)         # north velocity
    heading = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0
    lat = HOME_LAT + y / m_per_deg_lat
    lon = HOME_LON + x / m_per_deg_lon
    alt_rel = args.alt + args.porpoise * math.sin(t / 23.0)          # gentle porpoise
    climb = args.porpoise * math.cos(t / 23.0) / 23.0
    pitch = math.radians(2.0 + 2.0 * math.sin(t / 23.0))
    return dict(lat=lat, lon=lon, alt_rel=alt_rel, vx=vx, vy=vy, climb=climb,
                heading=heading, roll=bank, pitch=pitch,
                yaw=math.radians(heading), ang=ang)


failed = set()


def guarded(name, fn, *a):
    try:
        fn(*a)
    except Exception as e:
        if name not in failed:
            failed.add(name)
            log("SEND FAILED %s: %r" % (name, e))


ms = lambda: int((time.time() - T0) * 1000) & 0xFFFFFFFF
us = lambda: int((time.time() - T0) * 1e6)

armed = True
mode_loiter = 12
mode_drops_left = args.ignore_mode_requests
param_drops_left = args.ignore_param_sets


def tick_heartbeat():
    mav.heartbeat_send(M.MAV_TYPE_FIXED_WING, M.MAV_AUTOPILOT_ARDUPILOTMEGA,
                       M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                       | (M.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0),
                       mode_loiter, M.MAV_STATE_ACTIVE)


def tick_attitude(s, t):
    mav.attitude_send(ms(), s["roll"], s["pitch"], s["yaw"],
                      0.0, 0.0, 2 * math.pi / period)


def tick_position(s, t):
    mav.global_position_int_send(
        ms(), int(s["lat"] * 1e7), int(s["lon"] * 1e7),
        int((HOME_ALT_MSL + s["alt_rel"]) * 1000), int(s["alt_rel"] * 1000),
        int(s["vy"] * 100), int(s["vx"] * 100), int(-s["climb"] * 100),
        int(s["heading"] * 100))


def tick_gps(s, t):
    mav.gps_raw_int_send(us(), 3, int(s["lat"] * 1e7), int(s["lon"] * 1e7),
                         int((HOME_ALT_MSL + s["alt_rel"]) * 1000), 90, 120,
                         int(args.speed * 100), int(s["heading"] * 100), 14)


def tick_vfr(s, t):
    mav.vfr_hud_send(args.speed * 0.95, args.speed, int(s["heading"]), 48,
                     HOME_ALT_MSL + s["alt_rel"], s["climb"])


def tick_sys_status(s, t):
    volts = 16.4 - 0.8 * ((t / 3600.0) % 1.0)
    mav.sys_status_send(0x3FFFFFFF, 0x3FFFFFFF, 0x3FFFFFFF, 350,
                        int(volts * 1000), 1240, int(85 - 20 * ((t / 3600.0) % 1.0)),
                        0, 0, 0, 0, 0, 0)


def tick_battery(s, t):
    volts = 16.4 - 0.8 * ((t / 3600.0) % 1.0)
    cells = [int(volts * 1000 / 4)] * 4 + [65535] * 6
    mav.battery_status_send(0, 0, 1, 2800, cells, 1240, 900, -1,
                            int(85 - 20 * ((t / 3600.0) % 1.0)))


def tick_nav(s, t):
    nav_bearing = int((s["heading"] + 90.0) % 360.0)
    if nav_bearing > 180:
        nav_bearing -= 360
    target = int((math.degrees(math.atan2(-math.sin(s["ang"]), -math.cos(s["ang"]))) + 360.0) % 360.0)
    if target > 180:
        target -= 360
    mav.nav_controller_output_send(math.degrees(s["roll"]), math.degrees(s["pitch"]),
                                   nav_bearing, target, int(args.radius),
                                   0.4 * math.sin(t / 7.0), 0.3, 1.5 * math.sin(t / 11.0))


def tick_wind(s, t):
    mav.wind_send((210.0 + 15.0 * math.sin(t / 60.0)) % 360.0,
                  4.0 + 1.0 * math.sin(t / 45.0), 0.2)


def tick_terrain(s, t):
    mav.terrain_report_send(int(s["lat"] * 1e7), int(s["lon"] * 1e7), 100,
                            HOME_ALT_MSL - 5.0 + 4.0 * math.sin(s["ang"]),
                            s["alt_rel"] + 5.0 - 4.0 * math.sin(s["ang"]), 0, 168)


def tick_mission_current(s, t):
    mav.mission_current_send(2)


def tick_servo(s, t):
    ail = int(1500 + 250 * math.sin(s["ang"]))
    ele = int(1500 - 60 + 40 * math.sin(t / 5.0))
    mav.servo_output_raw_send(us(), 0, ail, ele, 1480, 1500, 1500, 1500, 1500, 1500)


def tick_rc(s, t):
    ch = [1500, 1440, 1480, 1500, 1800, 1000, 1000, 1500] + [0] * 10
    mav.rc_channels_send(ms(), 8, *ch, 65)


def tick_vibration(s, t):
    mav.vibration_send(us(), 3.2 + math.sin(t), 2.9 + math.cos(t / 1.3),
                       4.1 + math.sin(t / 0.7), 0, 0, 0)


def tick_pressure(s, t):
    mav.scaled_pressure_send(ms(), 907.4 - s["alt_rel"] * 0.118,
                             0.5 * (args.speed * 0.95) ** 2 * 1.1 / 100.0, 2150)


def tick_distance(s, t):
    d = int(min(4000, (s["alt_rel"] + 5.0) * 100))
    mav.distance_sensor_send(ms(), 20, 4000, d, 0, 0, 25, 0)


def tick_rangefinder(s, t):
    mav.rangefinder_send(min(40.0, s["alt_rel"] + 5.0), 2.1)


def tick_ekf(s, t):
    mav.ekf_status_report_send(0x3FF, 0.05, 0.08, 0.06, 0.12, 0.2)


def tick_home(s, t):
    mav.home_position_send(int(HOME_LAT * 1e7), int(HOME_LON * 1e7),
                           int(HOME_ALT_MSL * 1000), 0.0, 0.0, 0.0,
                           [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0)


def tick_statustext(s, t):
    mav.statustext_send(M.MAV_SEVERITY_INFO,
                        ("Loiter %d m, bank 30, t=%d s" % (args.radius, int(t))).encode())


def tick_system_time(s, t):
    mav.system_time_send(int(time.time() * 1e6), ms())


def tick_local_ned(s, t):
    mav.local_position_ned_send(ms(), args.radius * math.cos(s["ang"]),
                                args.radius * math.sin(s["ang"]), -s["alt_rel"],
                                s["vy"], s["vx"], -s["climb"])


SCHEDULE = [   # (name, hz, fn)
    ("HEARTBEAT", 1.0, lambda s, t: tick_heartbeat()),
    ("ATTITUDE", args.attitude_hz, tick_attitude),
    ("GLOBAL_POSITION_INT", 2.0, tick_position),
    ("GPS_RAW_INT", 2.0, tick_gps),
    ("VFR_HUD", 2.0, tick_vfr),
    ("SYS_STATUS", 2.0, tick_sys_status),
    ("BATTERY_STATUS", 1.0, tick_battery),
    ("NAV_CONTROLLER_OUTPUT", 2.0, tick_nav),
    ("WIND", 1.0, tick_wind),
    ("TERRAIN_REPORT", 1.0, tick_terrain),
    ("MISSION_CURRENT", 1.0, tick_mission_current),
    ("SERVO_OUTPUT_RAW", 2.0, tick_servo),
    ("RC_CHANNELS", 2.0, tick_rc),
    ("VIBRATION", 1.0, tick_vibration),
    ("SCALED_PRESSURE", 2.0, tick_pressure),
    ("DISTANCE_SENSOR", 2.0, tick_distance),
    ("RANGEFINDER", 2.0, tick_rangefinder),
    ("EKF_STATUS_REPORT", 1.0, tick_ekf),
    ("HOME_POSITION", 1.0 / 30.0, tick_home),
    ("STATUSTEXT", 1.0 / 20.0, tick_statustext),
    ("SYSTEM_TIME", 1.0, tick_system_time),
    ("LOCAL_POSITION_NED", 2.0, tick_local_ned),
]
next_at = {name: 0.0 for name, _, _ in SCHEDULE}
sent = {name: 0 for name, _, _ in SCHEDULE}

# MAVFTP: refuse, so the app falls back to the streamed list promptly.
OP_TerminateSession, OP_ResetSessions, OP_OpenFileRO = 1, 2, 4
OP_Ack, OP_Nack = 128, 129
HDR = "<HBBBBBBI"


def ftp_reply(m, seq, session, opcode, size, req_opcode, payload):
    hdr = struct.pack(HDR, seq & 0xFFFF, session, opcode, size, req_opcode, 0, 0, 0)
    body = (hdr + payload).ljust(251, b"\x00")
    mav.file_transfer_protocol_send(0, m.get_srcSystem(), m.get_srcComponent(), body)


log("loiter r=%.0f m, %.0f m/s, period %.0f s, ATTITUDE %.0f Hz, to udp %d"
    % (args.radius, args.speed, period, args.attitude_hz, args.port)
    + (" (listening)" if args.listen else ""))
tick_home(state(0), 0)
stream_queue = []
next_stream = 0.0
last_report = 0.0

while True:
    now = time.time()
    t = now - T0
    s = state(t)
    for name, hz, fn in SCHEDULE:
        if now >= next_at[name]:
            next_at[name] = now + 1.0 / hz
            guarded(name, fn, s, t)
            sent[name] += 1
    if stream_queue and now >= next_stream:
        idx = stream_queue.pop(0)
        guarded("PARAM_VALUE", send_param, names[idx], idx)
        next_stream = now + 0.02
    if t - last_report >= 60.0:
        last_report = t
        log("alive; sent " + " ".join("%s=%d" % (k[:8], v) for k, v in sent.items()
                                      if k in ("ATTITUDE", "GLOBAL_POSITION_INT", "HEARTBEAT")))
    try:
        m = conn.recv_match(blocking=True, timeout=0.01)
    except OSError:
        m = None
    if m is None:
        continue
    typ = m.get_type()
    if typ in ("HEARTBEAT", "BAD_DATA"):
        continue
    if typ == "PARAM_REQUEST_LIST":
        log("<- PARAM_REQUEST_LIST (streaming %d)" % len(names))
        stream_queue = list(range(len(names)))
    elif typ == "PARAM_REQUEST_READ":
        pid = m.param_id.decode(errors="replace") if isinstance(m.param_id, bytes) else m.param_id
        pid = pid.rstrip("\x00")
        if m.param_index not in (-1, 65535) and 0 <= m.param_index < len(names):
            guarded("PARAM_VALUE", send_param, names[m.param_index], m.param_index)
        elif pid in index_of:
            guarded("PARAM_VALUE", send_param, pid, index_of[pid])
    elif typ == "PARAM_SET":
        pid = m.param_id.decode(errors="replace") if isinstance(m.param_id, bytes) else m.param_id
        pid = pid.rstrip("\x00")
        if param_drops_left > 0:
            # Pretend it never arrived. A PARAM_SET has no acknowledgement,
            # so from the sender's side this is indistinguishable from a
            # link that swallowed it - which is the point.
            param_drops_left -= 1
            log("<- PARAM_SET %s  DROPPED (%d more will be)"
                % (pid, param_drops_left))
            continue
        if pid in index_of:
            values[pid] = float(m.param_value)
            log("<- PARAM_SET %s=%s  accepted" % (pid, m.param_value))
            guarded("PARAM_VALUE", send_param, pid, index_of[pid])
    elif typ == "COMMAND_LONG":
        if m.command == M.MAV_CMD_COMPONENT_ARM_DISARM:
            armed = m.param1 >= 0.5
        if m.command == M.MAV_CMD_DO_SET_MODE:
            if mode_drops_left > 0:
                # Pretend this one never arrived: no mode change, no ACK.
                mode_drops_left -= 1
                log("<- DO_SET_MODE %d  DROPPED (%d more will be)"
                    % (int(m.param2), mode_drops_left))
                continue
            mode_loiter = int(m.param2)
            log("<- DO_SET_MODE %d  accepted" % mode_loiter)
        guarded("COMMAND_ACK", mav.command_ack_send, m.command, M.MAV_RESULT_ACCEPTED)
    elif typ == "FILE_TRANSFER_PROTOCOL":
        seq, session, opcode, size, req_opcode, _b, _p, offset = struct.unpack(HDR, bytes(m.payload[0:12]))
        if opcode in (OP_ResetSessions, OP_TerminateSession):
            ftp_reply(m, seq + 1, session, OP_Ack, 0, opcode, b"")
        elif opcode == OP_OpenFileRO:
            ftp_reply(m, seq + 1, session, OP_Nack, 1, opcode, bytes([1]))
