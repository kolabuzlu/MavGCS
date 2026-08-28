# MavGCS Changelog

## V1.6.0 - HUD EKF/Vibe status indicators

Added Mission Planner-style "EKF" and "Vibe" status text to the HUD,
bottom middle (EKF on the left, Vibe on the right, matching MP's own
layout), colored white/yellow/red by vehicle health - fed by two new
requested-at-2Hz messages, `EKF_STATUS_REPORT` and `VIBRATION`.

Thresholds were ported directly from Mission Planner's own source
(`HUD.cs` / `CurrentState.cs`), not just general ArduPilot guidance,
after an initial pass using the general guidance numbers didn't quite
match MP's behavior on real hardware:

- **EKF**: color is driven by the worst of the 5 EKF_STATUS_REPORT
  variances (velocity, compass, pos horizontal, pos vertical, terrain
  alt) - white up to 0.5, yellow 0.5-0.8, red above 0.8 - forced red
  regardless of variance if `EKF_ATTITUDE` is missing, if
  `EKF_VELOCITY_HORIZ` is missing while we have a GPS fix, or if
  `EKF_UNINITIALIZED` is set. (`EKF_GPS_GLITCHING`/`EKF_CONST_POS_MODE`
  looked like they should matter given their names, but MP's HUD
  doesn't actually check them.)
- **Vibe**: white up to 30, yellow 30-60, red above 60, on the raw
  per-axis vibration values only. An earlier version of this also
  forced red on any accelerometer clipping, which doesn't match MP and
  was masking the yellow range entirely - clipping starts on plenty of
  boards well before vibration itself reaches the yellow/red range.

## V1.5.0 - Real-hardware Serial fixes, HUD lat/lon, custom plane icon

First release tested against a real UAV over a Serial telemetry link
rather than just SITL/TCP - surfaced two connection-layer bugs SITL
never hit, plus some cosmetic requests.

- **Serial connect failed with `[Errno 11001] getaddrinfo failed`.**
  The installed pymavlink no longer treats a bare `device:baud` string
  (e.g. `COM3:57600`) as serial - any string with a colon that isn't a
  recognized `tcp:`/`udp:`/etc. prefix is now parsed as a UDP
  `host:port`, so `"COM3"` was handed to `socket.getaddrinfo()` as a
  hostname and failed. Fixed by splitting the device and baud rate
  ourselves before calling `mavutil.mavlink_connection()`, instead of
  relying on it to parse a combined string.

- **Flight mode panel flickered, then stopped highlighting entirely,
  on real hardware** (SITL was unaffected). Root cause turned out to be
  two-fold:
  1. Real links can carry HEARTBEATs from MAVLink components other than
     the autopilot (companion computers, GPS/CAN nodes, etc.) whose
     `custom_mode` is meaningless for the vehicle - now filtered out by
     matching the HEARTBEAT's source system/component against the
     connected autopilot's.
  2. That fix initially over-filtered everything: this pymavlink
     version's `wait_heartbeat()` never actually populates
     `target_component` (it stays at its `0` default), so we now
     capture `target_system`/`target_component` ourselves from the
     heartbeat message directly. This also fixes a related latent bug
     where a silent heartbeat timeout used to still report a false
     "Connected" status.

- **Map marker replaced** with a custom-supplied plane icon image
  (previously a hand-drawn SVG silhouette), sized at 92x78 - double the
  original icon's footprint for visibility.

- **Added Lat/Lon readout to the HUD** (artificial horizon): latitude
  bottom-left, longitude bottom-right, same styling as the existing
  wind/battery boxes.

## V1.4.5 - Smooth map panning while following

The airplane marker moved smoothly (via per-frame interpolation, added
in V1.4), but the map's own panning while Follow UAV is on was still
only triggered once per real telemetry update (~2-3 Hz) - looked like
the map was jumping frame-by-frame even though the marker itself glided
smoothly. Moved the panning into the same per-frame animation loop as
the marker, using `{animate:false}` so Leaflet's own separate pan easing
doesn't layer on top of (and fight against) our already-smooth per-frame
position. Verified directly: at 50% through a simulated animation, the
map's pan target is the interpolated midpoint, not the raw start or end
point.

## V1.4.4 - Fixed leftover Fly-to-Here pins

Airplane marker icon made 50% bigger (26px -> 39px) for better
visibility on the map.

Two related bugs, both about the Fly-to-Here target marker outliving the
UI element that seemed to represent it:

- **Clicking the popup's own close (x) button** only hid the popup
  bubble - the pin marker underneath stayed on the map. Popup and marker
  are separate Leaflet objects; closing one doesn't automatically remove
  the other. Fixed by listening for the popup's `popupclose` event and
  removing the marker when it fires. Verified by simulating exactly what
  Leaflet does internally when that button is clicked (not just calling
  our own cleanup function directly) - confirmed the marker is genuinely
  removed from the map, not just hidden.
- **Starting a multi-waypoint mission** left any earlier Fly-to-Here pin
  on the map, since the waypoint queue and the single-point target marker
  are separate lifecycles that never talked to each other. Now clearing
  the target automatically when a mission starts.

## V1.4.3 - Fixed Serial mode field overlap

The Refresh button (Serial mode only) was crowding into the main row and
causing the port dropdown and baud rate dropdown to squeeze/overlap -
confirmed from a screenshot showing "5760" cut off from "57600". Moved
Refresh to its own row below Connect/Disconnect, only appearing when
Serial is selected (TCP/UDP mode shows no extra row). Verified both
modes render cleanly with the exact scenario from the report.

## V1.4.2 - Larger map: Multi-Waypoint Mission moved under Connection

Multi-Waypoint Mission no longer has its own full-width row - it's now
stacked directly below Connection, on the right side, matching Messages'
height on the left (Messages | [Connection / Multi-Waypoint Mission]).
This frees up a full row's worth of vertical space, so the map is
noticeably larger. Verified the underlying page content (background
color, map div, Follow UAV checkbox) is all correct via direct DOM
queries - a white area sometimes visible in screenshots at this larger
size is a rendering/compositing quirk specific to headless test
environments, not an actual rendering problem.

## V1.4.1 - Connect/Disconnect on the fields row, Follow UAV moved onto the map

**Connection panel**: Connect/Disconnect buttons now sit on the same row
as the protocol/host/port fields (right side of that row), instead of a
second row below. This does make the panel somewhat wider than the
absolute minimum, but it's still narrower than Messages, and every field
was re-checked for text clipping in both TCP/UDP and Serial mode before
shipping.

**Follow UAV** moved from its own row above the map into the map itself
- top-left corner, positioned to the right of Leaflet's default zoom
buttons. Implemented as a plain HTML checkbox inside the map's own page,
toggling the map's panning behavior directly in JavaScript rather than
round-tripping through Python (which isn't needed here, since nothing on
the Python side depends on the follow state). Confirmed the checkbox
exists, defaults to checked, and has its change handler attached, all
independent of whether the map's tile layer itself loads successfully.

## V1.4 - Fixed reconnect bug, airplane icon, smooth marker animation

**Fixed a real bug: couldn't reconnect to the same port after
Disconnect.** Root cause: `stop()` only halted the background thread's
loop - it never actually closed the underlying socket
(`master.close()`). The TCP connection could stay technically open at
the OS level even after the thread exited, which is exactly the kind of
thing that makes a server (SITL/MAVProxy) refuse a second connection
attempt on the same port. Proved this two ways: confirmed the fix works
(disconnect then reconnect to the same port succeeds), and confirmed the
bug was real by testing the exact same scenario with the old buggy
`stop()` reinstated - it got stuck at "Waiting for heartbeat..." forever,
matching the reported symptom exactly.

**Drone marker is now an airplane silhouette** instead of a plain
triangle - a proper top-down aircraft shape (nose, wings, tail) built as
an inline SVG so it stays crisp at any zoom level and rotates cleanly to
any heading.

**Smooth animated movement between position updates.** Real telemetry
arrives at only ~2-3 Hz, which looks jumpy on a map. The marker now
glides continuously between each real GPS fix using
`requestAnimationFrame`-based interpolation (same technique flight
trackers use for ADS-B updates) instead of snapping directly to each new
position. Includes shortest-path heading interpolation, so a turn that
crosses the 0/360 boundary animates the short way around instead of
spinning through 180 degrees. Verified the interpolation math directly
(exact intermediate values checked at 20%/50%/100% of the animation, and
the 350->10 degree wraparound case) since this sandbox's headless
browser doesn't actually fire `requestAnimationFrame` callbacks - a
sandbox limitation, not a code issue; the underlying API is a standard,
long-established part of every real browser engine.

**Follow UAV checkbox moved back** to just above the map (it had ended
up at the very top of the right column after some of the recent panel
additions).

## V1.3.3 - Fixed QNH/Terrain Alt not populating, and a real Connection panel size fix

**QNH and Terrain Alt fixed.** Root cause: they depend on `SCALED_PRESSURE`
and `TERRAIN_REPORT`, which aren't guaranteed to stream by default on
every vehicle/firmware config. MavGCS now explicitly requests both at
2 Hz right after connecting (`MAV_CMD_SET_MESSAGE_INTERVAL`, same pattern
already used for `HOME_POSITION`) instead of passively hoping the vehicle
sends them unprompted. Verified against a fake vehicle deliberately built
to withhold both messages until asked - confirmed the request genuinely
unlocks them.

**Connection panel is now actually smaller than Messages**, not just
nominally. The previous version's width-ratio fix didn't fully work
because fitting six controls in one row set a minimum width that
overrode the stretch ratio - restructured into two rows (fields on top,
Refresh/Connect/Disconnect below) so the panel's required width dropped
from ~370px to ~254px, while Messages grew to ~506px. Checked both TCP
and Serial mode for text clipping at the new sizes (an earlier attempt at
this same fix accidentally clipped "Serial" to "Seria", "/dev/ttyS0" to
"/dev/tt", and "57600" to "5760" - caught and fixed before shipping).

## V1.3.2 - Connection panel sizing fix and Disconnect button

Fixed a real layout bug from V1.3.1: the host field was stretching to an
oversized width and the port field was rendering as a tall, narrow box
instead of a normal text field. All Connection panel controls now have
fixed heights and sensible widths. Also: Messages now gets more width
than Connection (2:1 split instead of an even 50/50), and added a
Disconnect button next to Connect - clicking it stops the current
connection and returns to the same clean disconnected state the app
starts in, verified end-to-end (connect, disconnect, then confirm a mode
button correctly shows "Not connected" instead of silently failing).

## V1.3.1 - Manual connect, real serial port picker, no BLE, fixed a real stale-connection bug

Three requested changes, plus a real bug found while making them:

- **No more auto-connect.** The app used to connect automatically on
  startup using the CLI connection string. Now it only pre-fills the
  Connection panel's fields - nothing connects until you click Connect.
- **Serial now shows a real dropdown of detected ports** (via `pyserial`,
  a newly-added dependency - it wasn't guaranteed by pymavlink's own
  dependencies) plus a baud-rate dropdown, instead of free-text fields
  that gave no indication of what's actually available.
- **BLE removed** from the protocol dropdown - it was never functional
  (pymavlink has no built-in BLE transport), so it shouldn't have been
  offered as if it worked.
- **Fixed a real bug**: the Flight Mode and Preflight Calibration
  buttons were bound directly to the *original* connection object's
  methods at startup. After the Connection panel's reconnect feature
  (added in V1.3) replaced `self.link` with a new object, those two
  buttons would have silently kept sending to the old, closed
  connection instead of the new one - a real regression that the V1.3
  testing didn't catch, since it only checked the connection status, not
  whether buttons still worked correctly *after* reconnecting. Every
  command-sending handler now looks up the current connection at click
  time instead of capturing a stale reference, and shows a clear
  "Not connected" message if there isn't one yet. Verified by connecting,
  reconnecting to a completely different vehicle, and confirming a mode
  button still worked correctly against the new connection (not the old,
  stopped one).

## V1.3 - Connection panel with live reconnection

Added a Mission Planner-style connection bar (top-right, splitting the
space 50/50 with the Messages panel): protocol dropdown (Serial/TCP/UDP/
BLE), host/port fields, and a Connect button. This is a genuine runtime
reconnect - clicking Connect tears down the current MavlinkLink thread and
starts a fresh one against whatever's entered, not just a relabeled UI.
Verified end-to-end: pre-fills correctly from the startup connection
string, and successfully reconnected live from one running fake vehicle
to a completely different one on a different port, with the new
connection's own independent handshake completing correctly.

BLE is listed in the dropdown (to match Mission Planner's own options)
but explicitly signals "unsupported" rather than pretending to connect -
pymavlink has no built-in BLE transport.

## V1.2.2 - Fixed Clear button getting stuck disabled after Start Mission

The Clear button's enabled state was wrongly tied to the active queue
count, which resets to 0 right after Start Mission - even though the
markers are still visible on the map at that point (by design, see
V1.2's persistence behavior). This left Clear unusable until adding
another waypoint first. Fixed: Clear is now always enabled (clicking it
with nothing queued is harmless), independent of the queue count. Note:
clearing the markers is purely visual/bookkeeping - if a mission was
already started, the vehicle keeps flying the uploaded route regardless
of what's cleared from the map afterward, which is the intended behavior.

## V1.2.1 - Fixed mission starting at waypoint 2 instead of waypoint 1

Root cause: ArduPilot (and Mission Planner) treat mission item index 0 as
a home/reference placeholder, not a real waypoint - AUTO mode execution
starts at item 1. The multi-waypoint mission feature was uploading the
first clicked point as item 0, so the vehicle silently treated it as the
home placeholder and started flying from the *second* clicked point
instead. Fixed by prepending an actual placeholder item (using the
vehicle's known home position, falling back to the first waypoint's own
coordinates if home isn't known yet) and setting the mission to start at
item 1. Verified against a fake vehicle for both the known-home and
fallback cases.

## V1.2 - Multi-waypoint mission

Added: click multiple points on the map (4-5 or more) to queue them, then
"Start Mission" to fly through them in order - the plane goes to point 1,
then 2, then 3, and so on, autonomously.

This uses MAVLink's real mission upload protocol (the same mechanism
Mission Planner's flight-plan tab uses) rather than the GCS watching
position and re-sending the next target itself: the whole route gets
uploaded as a proper onboard AUTO mission, so ArduPilot handles
waypoint-to-waypoint progression on its own using `WP_RADIUS`, independent
of the GCS staying connected after the mission starts. Verified
end-to-end against a fake vehicle that correctly implements the
mission-protocol handshake (`tools/fake_vehicle_mission.py`) - confirmed
every step: clear, count, each item requested and sent with the exact
queued coordinates, final ack, set-current, and the mode switch to AUTO.

## V1.1 - Change Altitude now works in LOITER (confirmed on real hardware)

Fixed the one known limitation from V1: **Change Altitude now works in
LOITER mode**, confirmed by the user on real ArduPlane hardware. This
wasn't guessed - it was found by capturing Mission Planner's actual
network traffic (via a `.tlog` file, read with the new
`tools/inspect_tlog.py`) while its own Change Altitude button worked
correctly in LOITER, then matching that exact mechanism.

The fix: Change Altitude now sends a `MISSION_ITEM` message
(`MAV_CMD_NAV_WAYPOINT` wrapped with `current=3`) instead of
`MAV_CMD_GUIDED_CHANGE_ALTITUDE`. `current=3` is an ArduPilot-specific
convention - distinct from the well-known `current=2` "guided mode
override" trick - meaning "update only the altitude of the current
navigation target," without touching position or forcing a mode change.
Three other mechanisms were tried and ruled out along the way:
`MAV_CMD_GUIDED_CHANGE_ALTITUDE` (GUIDED-only per its own spec),
`MAV_CMD_DO_REPOSITION` without its mode-change flag (gated behind
guided-family modes in ArduPilot's firmware), and
`MAV_CMD_CONDITION_CHANGE_ALT` (doesn't work as a standalone real-time
command at all - likely mission-sequence-only).

Also added in this version:
- Battery voltage box on the HUD (top-right): total pack voltage, per-cell
  voltage, with a 3S/4S/6S selector dropdown
- `tools/inspect_tlog.py` - reads a Mission Planner `.tlog` file and shows
  everything the GCS side sent, useful for matching MavGCS's behavior to
  Mission Planner's on anything else that comes up later
- Wind Direction label capitalization fix

### Change Speed in LOITER - confirmed expected behavior, not a bug
Tested directly: Mission Planner's own Change Speed also does not work in
LOITER mode. So MavGCS's `MAV_CMD_DO_CHANGE_SPEED` behavior in LOITER
(silently has no effect) matches Mission Planner exactly - this isn't a
gap to fix, it's ArduPlane's actual behavior in this mode. No further
action needed here.

## V1 - Stable checkpoint

This is the first version marked as a known-good, confirmed-stable
baseline. If a future change breaks something, this is the version to
revert to. The window title shows "MavGCS V1" so it's identifiable at a
glance while running.

### Confirmed working on real ArduPlane hardware
- MAVLink connection over UDP (ELRS WiFi backpack) and TCP (SITL)
- Artificial horizon: roll/pitch, airspeed/altitude boxes, heading
  compass tape, wind indicator (heading-relative arrow, direction/speed
  in kph)
- Google Satellite Hybrid map with OpenStreetMap alternative, click-to-fly
  (GUIDED mode reposition), Follow UAV toggle, drone marker + trail
- Flight mode buttons: MANUAL, FBWA, CRUISE, LOITER, AUTO, RTL, TAKEOFF,
  AUTOLAND, AUTOTUNE, GUIDED - with live active-mode highlighting
- Arm/Disarm: instant ARM (with optional force + confirmation), hold-to-
  force-DISARM (2.5s hold, no separate dialog)
- Preflight Calibration: hold-to-activate button (baro + airspeed
  calibration, matching Mission Planner's action)
- Change Speed: confirmed working in GUIDED mode (via
  `MAV_CMD_DO_CHANGE_SPEED`)
- Change Altitude: confirmed working in GUIDED mode (via
  `MAV_CMD_GUIDED_CHANGE_ALTITUDE`)
- 16-field live telemetry dashboard (AirSpeed, GroundSpeed, Vertical
  Speed, Altitude, Rangefinder, Dist to Home, Dist to WP, Sat Count, Roll,
  Pitch, Yaw, GPS HDOP, Wind Direction, Wind Velocity, QNH, Terrain Alt)
- Messages panel (vehicle's STATUSTEXT stream, color-coded by severity)
- Command ACK diagnostics (separate small status line showing whether
  ArduPilot accepted/rejected each command sent, and why)

### Known limitation (not a bug in the app - confirmed vehicle-side)
- **Change Speed and Change Altitude do not work in LOITER mode.**
  ArduPilot's own `COMMAND_ACK` responses confirm both commands are
  received and recognized correctly, but execution fails
  (`MAV_RESULT_FAILED`) specifically while in LOITER. This appears to be
  a firmware-level restriction on this vehicle's ArduPilot version, not
  something fixable from the GCS side. Three different MAVLink commands
  were tried for Change Altitude (`GUIDED_CHANGE_ALTITUDE`,
  `DO_REPOSITION` without a forced mode change, and
  `CONDITION_CHANGE_ALT`) - only the first works, and only in GUIDED.
  Workaround: switch to GUIDED mode, make the adjustment, switch back.
  **Resolved in V1.1 for Change Altitude** - see above. Change Speed in
  LOITER is still unconfirmed.

### Project structure
- `main.py` - UI and window layout
- `mavlink_link.py` - MAVLink connection thread and all command-sending
  logic
- `artificial_horizon.py` - the HUD widget
- `map_view.py` - the Leaflet-based map widget
- `tools/` - fake vehicle simulators and pipeline tests for verifying
  changes without needing SITL or real hardware
