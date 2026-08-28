"""
Reads a Mission Planner .tlog file and prints out every message the GCS
side (Mission Planner) sent to the vehicle - not just COMMAND_LONG/
COMMAND_INT/COMMAND_ACK, since testing showed Mission Planner's Change
Altitude doesn't actually use those for LOITER mode. This casts a much
wider net: any message NOT sent by the vehicle itself.

Usage:
    python inspect_tlog.py "C:\\path\\to\\your\\file.tlog"

Find your .tlog file:
    Mission Planner saves these automatically every session, normally in:
        Documents\\Mission Planner\\logs\\<vehicle type>\\<date>\\
    Look for the one matching when you tested Change Speed/Change
    Altitude in LOITER - filenames look like "2026-08-27 14-42-42.tlog".
"""

import sys
from pymavlink import mavutil

# Message types that are routine two-way chatter and not useful here -
# filtered from the detailed listing (but still counted in the summary).
NOISY_TYPES = {
    "HEARTBEAT",
    "TIMESYNC",
    "SYSTEM_TIME",
    "PARAM_REQUEST_LIST",
    "PARAM_VALUE",
    "PARAM_REQUEST_READ",
    "REQUEST_DATA_STREAM",
    "BAD_DATA",
}

if len(sys.argv) < 2:
    print("Usage: python inspect_tlog.py \"path\\to\\your\\file.tlog\"")
    sys.exit(1)

tlog_path = sys.argv[1]
out_path = tlog_path + ".inspected.txt"

print(f"Reading {tlog_path} ...")

mlog = mavutil.mavlink_connection(tlog_path)

all_lines = []
kept_lines = []
type_counts = {}
gcs_system_ids = set()
vehicle_system_ids = set()

# First pass: figure out which system ID is the vehicle (it's the one
# sending HEARTBEAT messages announcing an autopilot type) vs the GCS.
mlog.rewind()
while True:
    msg = mlog.recv_match(type=["HEARTBEAT"], blocking=False)
    if msg is None:
        break
    if getattr(msg, "autopilot", None) is not None and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID:
        vehicle_system_ids.add(msg.get_srcSystem())
    else:
        gcs_system_ids.add(msg.get_srcSystem())

mlog.rewind()
while True:
    msg = mlog.recv_match(blocking=False)
    if msg is None:
        break
    mtype = msg.get_type()
    if mtype == "BAD_DATA":
        continue

    src = msg.get_srcSystem()
    type_counts[mtype] = type_counts.get(mtype, 0) + 1

    ts = getattr(msg, "_timestamp", None)
    ts_str = f"[{ts:.2f}] " if ts else ""
    line = f"{ts_str}(from sysid {src}) {msg}"
    all_lines.append(line)

    # Keep anything NOT from the vehicle (i.e. sent by the GCS) and not
    # routine noise.
    if src not in vehicle_system_ids and mtype not in NOISY_TYPES:
        kept_lines.append(line)

with open(out_path, "w") as f:
    f.write("\n".join(all_lines))

print()
print(f"Detected vehicle system ID(s): {vehicle_system_ids or 'unknown'}")
print(f"Detected GCS system ID(s): {gcs_system_ids or 'unknown'}")
print()
print("--- Summary: message types seen and how many times ---")
for name, count in sorted(type_counts.items(), key=lambda x: -x[1]):
    tag = " (routine, filtered out below)" if name in NOISY_TYPES else ""
    print(f"  {name}: {count}{tag}")

print()
print(f"--- Messages sent by the GCS, excluding routine noise ({len(kept_lines)} of {len(all_lines)} total) ---")
for line in kept_lines:
    print(line)

print()
print(f"Full unfiltered output also saved to: {out_path}")
if not kept_lines:
    print(
        "No relevant messages found. Double check this is the right "
        ".tlog file and that Mission Planner was disconnected right "
        "after the test so the file finished writing."
    )
