"""
Not part of the shipped app - a throwaway fake "vehicle" that streams
HEARTBEAT/ATTITUDE/GLOBAL_POSITION_INT/VFR_HUD/SYS_STATUS over UDP to
127.0.0.1:14550, the same port and message types ArduPilot SITL uses.

Used to smoke-test mavlink_link.py's parsing without needing a full SITL
build. Run this, then run main.py in another terminal (or run the
verification script below) to confirm messages are received and decoded
correctly.
"""

import time
import math
from pymavlink import mavutil

conn = mavutil.mavlink_connection("udpout:127.0.0.1:14550", source_system=1)

# Cycled periodically below to test the Messages panel - uses the same
# messages/severities seen in a real Mission Planner session.
STATUS_MESSAGES = [
    (6, "Initialising ArduPilot"),
    (4, "PreArm: GPS 1: not healthy"),
    (6, "Updating barometer calibration"),
    (6, "Barometer calibration complete"),
    (6, "Airspeed 1 calibration started"),
    (6, "Airspeed 1 calibrated"),
    (3, "EKF3 IMU0 forced reset"),
]
_last_status_sent = 0.0
_status_idx = 0

t0 = time.time()
print("Fake vehicle streaming to 127.0.0.1:14550 ... Ctrl+C to stop")

# Send before the first read. This is an outgoing ("udpout") socket, which
# pymavlink leaves unbound, and Windows rejects a read on an unbound UDP
# socket with WSAEINVAL 10022 rather than just returning nothing - so the
# recv_match() at the top of the loop below used to kill this tool on its
# very first iteration. Sending once implicitly binds the socket.
conn.mav.heartbeat_send(
    mavutil.mavlink.MAV_TYPE_QUADROTOR,
    mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
    mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
    4,
    mavutil.mavlink.MAV_STATE_ACTIVE,
)

while True:
    t = time.time() - t0

    # Print anything the GCS sends us (heartbeats, and commands like the
    # app's fly-to COMMAND_INT), so we can verify what the app actually sends.
    # Tolerate WSAECONNRESET (10054): on Windows, sending to a UDP port
    # with nothing listening (the GCS isn't up yet) bounces an ICMP
    # "port unreachable" back, and the NEXT read on that socket raises it.
    # It says nothing about our socket's health - keep streaming.
    try:
        incoming = conn.recv_match(blocking=False)
    except OSError:
        incoming = None
    if incoming is not None and incoming.get_type() != "BAD_DATA":
        print(f"<< received from GCS: {incoming}")
        if (
            incoming.get_type() == "COMMAND_LONG"
            and incoming.command == mavutil.mavlink.MAV_CMD_GET_HOME_POSITION
        ):
            conn.mav.home_position_send(
                int(47.6062 * 1e7), int(-122.3321 * 1e7), 50000,
                0, 0, 0, [1, 0, 0, 0], 0, 0, 0,
            )
        elif incoming.get_type() == "COMMAND_LONG":
            # Generic ACK for any other command, so the app's ACK
            # decoding can be tested end-to-end.
            conn.mav.command_ack_send(incoming.command, mavutil.mavlink.MAV_RESULT_ACCEPTED)

    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
        4,  # custom_mode (arbitrary)
        mavutil.mavlink.MAV_STATE_ACTIVE,
    )
    # Simulate SITL/MAVProxy echoing our own GCS heartbeat back to us -
    # this is the exact scenario that caused the mode flicker bug.
    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
        0, 0, 0,
    )

    roll = 0.3 * math.sin(t * 0.5)
    pitch = 0.15 * math.sin(t * 0.3)
    yaw = (t * 10) % 360
    conn.mav.attitude_send(
        int(t * 1000), roll, pitch, math.radians(yaw), 0, 0, 0
    )

    lat = int((47.6062 + 0.0005 * math.sin(t * 0.1)) * 1e7)
    lon = int((-122.3321 + 0.0005 * math.cos(t * 0.1)) * 1e7)
    conn.mav.global_position_int_send(
        int(t * 1000), lat, lon, 50000, 20000, 0, 0, 0, int(yaw * 100)
    )

    conn.mav.vfr_hud_send(12.0, 12.5, int(yaw), 30, 50.0, 0.5)

    conn.mav.sys_status_send(
        0, 0, 0, 0, 12600, 5000, 87, 0, 0, 0, 0, 0, 0
    )

    wind_dir = (250 + 15 * math.sin(t * 0.05)) % 360
    conn.mav.wind_send(wind_dir, 5.8, 0.1)

    # Sent once near the start, like a real vehicle does at boot/arm
    if t < 1.0:
        conn.mav.home_position_send(
            int(47.6062 * 1e7), int(-122.3321 * 1e7), 50000,
            0, 0, 0, [1, 0, 0, 0], 0, 0, 0,
        )

    conn.mav.gps_raw_int_send(
        int(t * 1e6), 3, lat, lon, 50000,
        150, 65535, 500, 0, 11,
    )

    conn.mav.nav_controller_output_send(
        0.0, 0.0, int(yaw), int(yaw), int(180 - t * 2) % 500, 0.0, 0.0, 0.0,
    )

    conn.mav.rangefinder_send(20.5 + 0.5 * math.sin(t * 0.4), 3.3)

    conn.mav.scaled_pressure_send(int(t * 1000), 1008.3, 0.0, 2500)

    conn.mav.terrain_report_send(
        lat, lon, 100, 30.0 + 2 * math.sin(t * 0.2), 20.0, 0, 1,
    )

    if t - _last_status_sent > 3.0:
        severity, text = STATUS_MESSAGES[_status_idx % len(STATUS_MESSAGES)]
        conn.mav.statustext_send(severity, text.encode("utf-8"))
        _status_idx += 1
        _last_status_sent = t

    time.sleep(0.2)
