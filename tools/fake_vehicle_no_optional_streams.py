"""
Fake vehicle that deliberately does NOT stream SCALED_PRESSURE or
TERRAIN_REPORT by default - only sends them if the GCS explicitly
requests them via MAV_CMD_SET_MESSAGE_INTERVAL, simulating a vehicle
whose default stream config doesn't include these. Used to verify
MavlinkLink's proactive request actually works, rather than assuming.
"""

import time
from pymavlink import mavutil

conn = mavutil.mavlink_connection("udpout:127.0.0.1:14550", source_system=1)

requested_scaled_pressure = False
requested_terrain_report = False

t0 = time.time()
last_heartbeat = 0.0
last_optional = 0.0

print("Fake vehicle (no default SCALED_PRESSURE/TERRAIN_REPORT) streaming...")

while True:
    now = time.time()
    t = now - t0

    if now - last_heartbeat > 1.0:
        conn.mav.heartbeat_send(
            mavutil.mavlink.MAV_TYPE_FIXED_WING,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED, 12,
            mavutil.mavlink.MAV_STATE_ACTIVE,
        )
        last_heartbeat = now

    # Always send GLOBAL_POSITION_INT so QNH has an altitude to work with
    lat = int(47.6062 * 1e7)
    lon = int(-122.3321 * 1e7)
    conn.mav.global_position_int_send(int(t * 1000), lat, lon, 50000, 20000, 0, 0, 0, 0)

    msg = conn.recv_match(blocking=False)
    if msg is not None and msg.get_type() == "COMMAND_LONG":
        if msg.command == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL:
            if msg.param1 == mavutil.mavlink.MAVLINK_MSG_ID_SCALED_PRESSURE:
                requested_scaled_pressure = True
                print("<< SCALED_PRESSURE was explicitly requested - will now stream it")
            elif msg.param1 == mavutil.mavlink.MAVLINK_MSG_ID_TERRAIN_REPORT:
                requested_terrain_report = True
                print("<< TERRAIN_REPORT was explicitly requested - will now stream it")

    if now - last_optional > 0.5:
        if requested_scaled_pressure:
            conn.mav.scaled_pressure_send(int(t * 1000), 1008.3, 0.0, 2500)
        if requested_terrain_report:
            conn.mav.terrain_report_send(lat, lon, 100, 30.0, 20.0, 0, 1)
        last_optional = now

    time.sleep(0.05)
