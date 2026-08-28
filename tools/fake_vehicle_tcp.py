"""
Same fake telemetry as fake_vehicle.py, but listens as a TCP server instead
of pushing UDP - mimicking how ArduPilot SITL exposes extra MAVLink client
ports like tcp:127.0.0.1:5762 / :5763.
"""

import sys
import time
import math
from pymavlink import mavutil

port = int(sys.argv[1]) if len(sys.argv) > 1 else 5762

conn = mavutil.mavlink_connection(f"tcpin:0.0.0.0:{port}", source_system=1)
print(f"Fake TCP vehicle listening on 127.0.0.1:{port} ... waiting for a client")

t0 = time.time()
while True:
    t = time.time() - t0

    # pymavlink's TCP server variant only calls socket.accept() from inside
    # recv() - if you only ever send(), it never accepts the connection.
    conn.recv_match(blocking=False)

    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
        4,
        mavutil.mavlink.MAV_STATE_ACTIVE,
    )

    roll = 0.3 * math.sin(t * 0.5)
    pitch = 0.15 * math.sin(t * 0.3)
    yaw = (t * 10) % 360
    conn.mav.attitude_send(int(t * 1000), roll, pitch, math.radians(yaw), 0, 0, 0)

    lat = int((47.6062 + 0.0005 * math.sin(t * 0.1)) * 1e7)
    lon = int((-122.3321 + 0.0005 * math.cos(t * 0.1)) * 1e7)
    conn.mav.global_position_int_send(
        int(t * 1000), lat, lon, 50000, 20000, 0, 0, 0, int(yaw * 100)
    )

    conn.mav.vfr_hud_send(12.0, 12.5, int(yaw), 30, 50.0, 0.5)
    conn.mav.sys_status_send(0, 0, 0, 0, 12600, 5000, 87, 0, 0, 0, 0, 0, 0)

    time.sleep(0.2)
