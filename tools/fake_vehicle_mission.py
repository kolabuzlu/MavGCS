"""
Not part of the shipped app - a fake vehicle that properly implements the
RECEIVING side of the MAVLink mission upload protocol (MISSION_CLEAR_ALL
-> MISSION_ACK, MISSION_COUNT -> per-item MISSION_REQUEST_INT loop ->
final MISSION_ACK), used to test MavlinkLink.upload_and_start_mission()
end-to-end without needing SITL or real hardware.

Run this, then run tools/test_mission_upload.py in another terminal.
"""

import time
from pymavlink import mavutil

conn = mavutil.mavlink_connection("udpout:127.0.0.1:14550", source_system=1)

received_items = {}
expected_count = None
state = "idle"

print("Fake mission-receiving vehicle listening, streaming to 127.0.0.1:14550")

t0 = time.time()
last_heartbeat = 0.0

while True:
    now = time.time()
    if now - last_heartbeat > 1.0:
        conn.mav.heartbeat_send(
            mavutil.mavlink.MAV_TYPE_FIXED_WING,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
            12,  # LOITER
            mavutil.mavlink.MAV_STATE_ACTIVE,
        )
        last_heartbeat = now

    msg = conn.recv_match(blocking=False)
    if msg is None:
        time.sleep(0.02)
        continue

    mtype = msg.get_type()
    if mtype == "BAD_DATA":
        continue

    if mtype == "COMMAND_LONG" and msg.command == mavutil.mavlink.MAV_CMD_GET_HOME_POSITION:
        conn.mav.home_position_send(
            int(47.6062 * 1e7), int(-122.3321 * 1e7), 50000,
            0, 0, 0, [1, 0, 0, 0], 0, 0, 0,
        )

    if mtype == "MISSION_CLEAR_ALL":
        print("<< MISSION_CLEAR_ALL received -> ACKing")
        received_items = {}
        expected_count = None
        state = "idle"
        conn.mav.mission_ack_send(255, 190, mavutil.mavlink.MAV_MISSION_ACCEPTED)

    elif mtype == "MISSION_COUNT":
        expected_count = msg.count
        received_items = {}
        state = "uploading"
        print(f"<< MISSION_COUNT received: {expected_count} items -> requesting seq 0")
        conn.mav.mission_request_int_send(255, 190, 0)

    elif mtype == "MISSION_ITEM_INT":
        # The frame is printed because it is what decides where the
        # altitude is measured from: 6 relative to home, 5 above sea
        # level, 11 above the terrain. Without it the same alt=120 looks
        # identical for three different places.
        try:
            frame_name = mavutil.mavlink.enums["MAV_FRAME"][msg.frame].name
        except (KeyError, AttributeError):
            frame_name = str(msg.frame)
        # The command decides what the aircraft DOES at the point: fly
        # through it, land on it, or orbit it until told otherwise. Two
        # missions that differ only in their last command look identical
        # without it, which is the case worth watching.
        try:
            cmd_name = mavutil.mavlink.enums["MAV_CMD"][msg.command].name
        except (KeyError, AttributeError):
            cmd_name = str(msg.command)
        print(f"<< MISSION_ITEM_INT seq={msg.seq}: lat={msg.x/1e7:.6f} "
              f"lon={msg.y/1e7:.6f} alt={msg.z} frame={msg.frame} "
              f"({frame_name}) command={msg.command} ({cmd_name})")
        received_items[msg.seq] = msg
        if expected_count is not None and len(received_items) < expected_count:
            conn.mav.mission_request_int_send(255, 190, len(received_items))
        elif expected_count is not None:
            print(f"<< All {expected_count} items received -> final ACK")
            conn.mav.mission_ack_send(255, 190, mavutil.mavlink.MAV_MISSION_ACCEPTED)
            state = "done"

    elif mtype == "MISSION_SET_CURRENT":
        print(f"<< MISSION_SET_CURRENT seq={msg.seq}")

    elif mtype == "COMMAND_LONG":
        print(f"<< COMMAND_LONG command={msg.command} param2={msg.param2}")
        conn.mav.command_ack_send(msg.command, mavutil.mavlink.MAV_RESULT_ACCEPTED)
