# MavGCS

A minimal ground control station: map + artificial horizon + telemetry panel,
talking MAVLink over UDP.

## Setup

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

### If you're using an ELRS module's WiFi + MAVLink bridge

1. Bind the ELRS module to the vehicle, power it up, and let it start its
   WiFi access point.
2. Join that WiFi network from your computer.
3. Find the module's IP address — while connected to its network, it's
   almost always your machine's **default gateway**:
   - Windows: `ipconfig` → look for "Default Gateway"
   - macOS/Linux: `route -n get default` or `ip route`
   - Common values are `192.168.4.1` (typical ESP32 SoftAP default) or `10.0.0.1`
4. Run:
   ```bash
   python main.py udpout:192.168.4.1:14550
   ```
   (swap in the real IP you found, and 14550 unless your module's docs say
   otherwise)

**Why `udpout` and not `udpin`:** most of these ESP32/ESP8266-based WiFi
telemetry bridges sit there listening on a UDP port, and only start sending
telemetry *back* once they've received a packet from your machine — a
"who do I talk to" handshake. `main.py` sends a heartbeat once a second for
exactly this reason (it's also just correct GCS behavior — real GCS software
identifies itself to the vehicle the same way). If your particular module
instead pushes data unprompted, switch to `udpin:0.0.0.0:14550`.

### Other connection types

```bash
python main.py                          # default: udpout:192.168.4.1:14550
python main.py udpin:0.0.0.0:14550      # listen instead of connecting out
```

Other connection string formats (same syntax pymavlink/MAVProxy use):

```bash
python main.py tcp:192.168.1.50:5760
python main.py com3:57600              # Windows serial telemetry radio
python main.py /dev/ttyUSB0:57600      # Linux/Mac serial telemetry radio
```

## What's here

- `mavlink_link.py` — background QThread that opens the MAVLink connection
  and turns `ATTITUDE`, `GLOBAL_POSITION_INT`, `VFR_HUD`, `SYS_STATUS`, and
  `HEARTBEAT` messages into Qt signals.
- `artificial_horizon.py` — hand-drawn attitude indicator (QPainter), no
  image assets.
- `map_view.py` — Leaflet.js map embedded in a QWebEngineView, with a
  Python method to push new drone positions to the JS side.
- `main.py` — wires it all together into one window.

## Try it right now, no SITL install needed

`tools/fake_vehicle.py` streams fake but realistic HEARTBEAT/ATTITUDE/
GLOBAL_POSITION_INT/VFR_HUD/SYS_STATUS messages over UDP to `127.0.0.1:14550`
— the same messages and port ArduPilot SITL uses by default. Good for
seeing the GUI move before installing anything else.

```bash
# terminal 1
python tools/fake_vehicle.py

# terminal 2
python main.py
```

You should see the artificial horizon rock back and forth, the map marker
slowly circle near Seattle, and the telemetry panel fill in.

There's also `tools/fake_vehicle_tcp.py <port>` if you want to test the TCP
path the same way (mimics SITL's multi-instance TCP ports like 5762/5763):

```bash
python tools/fake_vehicle_tcp.py 5762
python main.py tcp:127.0.0.1:5762
```

## Testing against your real SITL instance

Your SITL setup exposes extra GCS connection points on TCP, typically
`127.0.0.1:5762` and `:5763` (in addition to the UDP outputs on 14550/14551).
Point the app straight at one of them - no config changes needed elsewhere:

```bash
python main.py tcp:127.0.0.1:5762
```

If you'd rather use the UDP outputs instead, plain `python main.py` (no
arguments) already defaults to `udpin:0.0.0.0:14550`, which matches
ArduPilot's default telemetry stream.

## Then, switching to your real ELRS setup

Nothing in the app changes except the connection string:

```bash
python main.py udpout:<module-ip>:14550
```

## Telemetry dashboard

The bottom-left panel is a 4x4 dashboard: AirSpeed, GroundSpeed, Vertical
Speed (m/s), Altitude, Rangefinder, Dist to Home, Dist to WP, Sat Count,
Roll, Pitch, Yaw, GPS HDOP, Wind Direction, Wind Velocity, QNH, Terrain Alt (m).
All white text, label above a large value, matching a requested layout.

Two of these are computed here rather than read directly off the wire,
since MAVLink doesn't provide them as single fields:
- **Dist to Home** - haversine distance between the vehicle's current
  position and `HOME_POSITION` (requested explicitly via
  `MAV_CMD_GET_HOME_POSITION` right after connecting, since vehicles
  normally only send it once at boot/arm).
- **QNH** - MAVLink has no native QNH message. This estimates it from
  `SCALED_PRESSURE.press_abs` and the vehicle's AMSL altitude using the
  standard ISA barometric formula - the same relationship a real altimeter
  setting comes from, but it's an estimate, not a transmitted value.

A few fields depend on optional sensors/subsystems and will show `--` if
those aren't present on your vehicle: Rangefinder needs a configured
rangefinder, Terrain Alt needs terrain following/database enabled, and
QNH needs both a barometer and an altitude reading before it can compute.

QNH and Terrain Alt specifically also depend on `SCALED_PRESSURE` and
`TERRAIN_REPORT` actually being streamed, which isn't guaranteed by every
vehicle/firmware's default configuration - MavGCS explicitly requests
both at 2 Hz right after connecting (via `MAV_CMD_SET_MESSAGE_INTERVAL`,
same pattern as the `HOME_POSITION` request) rather than passively hoping
they show up unprompted. Verified against a vehicle deliberately built to
withhold both messages until asked - confirmed the request actually
unlocks them.

Rangefinder and Wind Direction/Velocity each accept **two different
MAVLink messages**, since ArduPilot can stream either depending on version
and configuration: Rangefinder reads from the legacy `RANGEFINDER`
message or the more commonly-used `DISTANCE_SENSOR` (filtered to the
downward-facing sensor); wind reads from ArduPilot's own `WIND` message or
the official `WIND_COV` (its NED wind vector is converted to the same
direction/speed convention - the East component needed an empirically-
confirmed sign correction to line up with real vehicle output). Whichever
one your vehicle actually sends will populate the field - no configuration
needed on your end.

## Map imagery

The map defaults to Google Satellite Hybrid (satellite imagery + roads/
labels), with OpenStreetMap available as an alternative via the layer
switcher in the map's top-right corner. The Google tiles are fetched from
the same unauthenticated endpoint Mission Planner itself uses (no API key)
- fine for hobby use, but be aware it's not an officially licensed API
path, so don't build anything commercial on it without going through
Google's actual Maps Platform API.

A small "Created by Derin Hakan Karakurt" credit sits in the bottom-right
corner of the map, on its own dark background. It's `pointer-events: none`
in CSS, so it never blocks map clicks (including the Fly to Here handler).

## Arm / Disarm

Below the mode buttons (with a small gap) is a separate "Arm / Disarm"
panel.

**ARM** is a single instant click, same one-click philosophy as the mode
buttons. Checking "Force ARM" first and then clicking shows a confirmation
dialog, since it bypasses ArduPilot's pre-arm safety checks (GPS lock, RC
failsafe, etc.).

**DISARM works differently on purpose**: it's a press-and-hold button -
hold it down continuously for 2.5 seconds (a thin progress bar fills as you
hold) and it fires a *forced* disarm the moment the bar completes. Release
early and it cancels with no effect. It's always forced because ArduPilot
generally refuses a normal disarm mid-flight anyway, and the hold
itself is the confirmation - no extra dialog on top of it, since holding a
button for 2.5 full seconds is already a deliberate act, not an accidental
click.

Whichever state the vehicle currently reports (armed/disarmed) is
highlighted with a white border, based on live telemetry.

Sends `MAV_CMD_COMPONENT_ARM_DISARM`, with `param2 = 21196` (confirmed
against mavlink.io's spec) for forced actions - that's the magic value
that tells ArduPilot to skip its pre-arm checks.

## Heading indicator

A compass tape sits fixed across the top-center of the HUD (doesn't rotate
or shift with roll/pitch, like the airspeed/altitude boxes). It shows a
+/-30 degree window around the current heading with tick marks every 10
degrees, cardinal letters (N/E/S/W) and three-digit headings at the major
30-degree marks, and a digital heading readout embedded directly in the
tape at center (not a separate floating box - that was overlapping the
roll arc underneath it). Wraps correctly through 360/0.

## Messages

A scrolling log above the map, top-right, showing the vehicle's
`STATUSTEXT` stream - the same message feed Mission Planner's Messages
tab shows (things like "PreArm: GPS 1: not healthy", calibration
progress, EKF warnings, etc). Each line is timestamped and color-coded by
severity: red for emergency/alert/critical, orange for error, yellow for
warning, white for notice, light gray for info, dim gray for debug.
Auto-scrolls to the newest message and caps at 500 lines to avoid
unbounded memory growth over a long session.

## Battery voltage

Top-right corner of the HUD: total pack voltage on top, per-cell voltage
below it, with a small S-count dropdown (3S/4S/6S) next to the cell
voltage. MAVLink only reports total pack voltage (via `SYS_STATUS`) - it
has no concept of cell count, so the per-cell figure is simply total
divided by whatever you select in the dropdown. Defaults to 4S; change it
to match your actual pack and the cell voltage updates immediately.

## Wind indicator

Top-left corner of the HUD: a small cyan arrow showing wind direction
**relative to your current heading** (up = straight ahead), plus the
absolute compass direction and speed (in kph) as numbers underneath. A
headwind shows the arrow pointing straight down; wind from your left
shows it pointing right; a tailwind points up - the arrow always shows
which way the wind is pushing you relative to the nose, not compass North.
Sourced from ArduPilot's `WIND` message (id 168), which reports speed in
m/s - converted to kph for display only, the underlying data stays in the
vehicle's native units. ArduPilot normally streams this by default at a low rate on planes with an airspeed
sensor / wind estimator enabled - if it never populates, check your
vehicle's stream rates (`SRx_EXTRA*` parameters) or request it explicitly
via `MAV_CMD_SET_MESSAGE_INTERVAL`.

## Command acknowledgements (diagnostics)

Every command the app sends (mode changes, arm/disarm, fly-to, speed/
altitude changes, calibration) gets a `COMMAND_ACK` back from ArduPilot
saying whether it was accepted or rejected, and why. These show up in the
status line (just above the mode buttons, on its own row below the friendly action message) as e.g. "ACK:
MAV_CMD_DO_CHANGE_SPEED -> MAV_RESULT_ACCEPTED" or "... ->
MAV_RESULT_DENIED" - deliberately kept separate from the Messages panel
below, which mirrors Mission Planner's Messages tab and only shows the
vehicle's own STATUSTEXT stream, not command bookkeeping. This is the
ground truth for diagnosing "I clicked the button and nothing happened" -
it tells you definitively whether
ArduPilot even received the command and what it decided to do with it,
rather than guessing.

## Guided Control (Change Speed / Change Altitude)

Below Arm/Disarm is a "Guided Control" panel, Mission Planner-style: two
buttons, each opening a small prompt for the new value, then sending it
immediately (no separate confirmation - these are routine in-flight
adjustments, not mode/arming changes).

**Change Speed** sends `MAV_CMD_DO_CHANGE_SPEED` (id 178) - the classic
command, documented as having no mode restriction. Confirmed working in
GUIDED on real hardware. Does not work in LOITER - but this was confirmed
to match Mission Planner's own Change Speed exactly (it doesn't work
there either), so this is expected ArduPlane behavior in that mode, not
a gap in MavGCS.

**Change Altitude** sends a `MISSION_ITEM` message (`MAV_CMD_NAV_WAYPOINT`
wrapped with `current=3`) - confirmed working in **both GUIDED and
LOITER** on real hardware. This was found by capturing Mission Planner's
actual network traffic with `tools/inspect_tlog.py` while its own Change
Altitude button worked correctly in LOITER, rather than guessing from
documentation. `current=3` is an ArduPilot-specific convention, separate
from the well-known `current=2` "guided mode override" trick: it means
"update only the altitude of the current navigation target," leaving
position and flight mode untouched - which is why it succeeds where
`MAV_CMD_GUIDED_CHANGE_ALTITUDE`, `MAV_CMD_DO_REPOSITION`, and
`MAV_CMD_CONDITION_CHANGE_ALT` (all tried first, all ruled out) did not.

A ninth mode button, AUTOTUNE, sits directly below RTL (custom_mode 8,
confirmed against pymavlink's ArduPlane table).

## Preflight Calibration

Below Arm/Disarm: a press-and-hold button (same hold pattern as
DISARM - no separate confirmation dialog, the hold itself is the
confirmation) that triggers ground pressure (baro) + airspeed calibration via
`MAV_CMD_PREFLIGHT_CALIBRATION` - the same combination Mission Planner's
"Preflight Calibration" action sends. Deliberately limited to these two:
accelerometer and compass calibration need interactive physical
repositioning prompts (rotate the vehicle through several orientations)
and can't be done as a single fire-and-forget button click like this one.

## Flight Mode buttons

A "Flight Mode" panel on the left has one-click buttons for MANUAL, FBWA,
CRUISE, LOITER, AUTO, RTL, TAKEOFF, AUTOLAND, AUTOTUNE, and GUIDED. Unlike the fly-to command,
these are deliberately **instant, no confirmation dialog** - matching how
Mission Planner/QGC do it, since you may need RTL in a hurry. The button for
whichever mode the vehicle currently reports lights up green automatically;
RTL stays visibly red at all times as a quick visual anchor.

Under the hood each button sends `MAV_CMD_DO_SET_MODE` with the vehicle's
custom_mode number for that mode (confirmed against pymavlink's ArduPlane
mode table: MANUAL=0, FBWA=5, CRUISE=7, LOITER=12, AUTO=10, RTL=11,
TAKEOFF=13, AUTOLAND=26). These numbers are ArduPlane-specific - if you ever
fly an ArduCopter vehicle instead, the mapping in `PLANE_MODES` (top of
`mavlink_link.py`) would need different numbers for the same names.

## Layout: top-right area

Messages occupies the left side; Connection (top) and Multi-Waypoint
Mission (bottom) are stacked in a column on the right, matching
Messages' height. This frees up a full row's worth of vertical space
for the map below, compared to when Multi-Waypoint Mission had its own
separate row spanning the full width.

## Connection panel

Top-right, sharing the row with the Messages panel. Protocol/host/port
fields and the Connect/Disconnect buttons sit on one row, with
Connect/Disconnect on the right side of that row. In Serial mode, the
Refresh button sits on its own row below (rather than crowding into the
main row), since fitting it there was causing the port and baud fields
to squeeze into each other.

**Disconnect** stops the current connection and returns to the same
clean disconnected state the app starts in - every button correctly
shows "Not connected" if clicked afterward, rather than doing nothing
silently or (worse) trying to use a dead connection.

**The app does not auto-connect on startup.** Whatever connection string
you launch it with (`python main.py tcp:127.0.0.1:5762`) only pre-fills
the panel's fields - nothing is sent over the wire until you actually
click Connect.

Clicking Connect tears down the current MAVLink connection (if any) and
opens a genuinely new one to whatever you've entered - this is a real
runtime reconnect, not just a UI relabel. Every button that sends a
command (mode changes, calibration, arm/disarm, fly-to, etc.) checks the
*current* connection at the moment you click it, so this stays correct
across any number of reconnects - clicking a button before ever
connecting shows a clear "Not connected" message instead of doing
nothing or crashing.

**Serial** shows a real dropdown of currently-detected serial ports
(via `pyserial`, refreshed automatically when you switch to Serial, or
manually with the Refresh button) plus a baud-rate dropdown, instead of
free-text fields - pick from what's actually connected rather than
guessing a port name. **TCP/UDP** show host/port fields as normal - for
UDP, leaving the host blank or as `0.0.0.0` listens for incoming
telemetry (matching this app's default, since e.g. ArduPilot SITL
streams *to* a UDP port rather than being connected to directly);
entering a specific host connects out to it instead - relevant for the
ELRS WiFi bridge setup described earlier in this README.

BLE was in an earlier version of this panel but has been removed -
pymavlink has no built-in Bluetooth Low Energy transport, so it could
never have actually worked.

## Multi-Waypoint Mission

Click "Queue waypoints on map click" (in the "Multi-Waypoint Mission"
panel, top-right above the map) and each map click drops a numbered
marker instead of the single Fly-to-Here popup. Click as many points as
you want (4-5, or more) - they're connected with a dashed line showing
the order. Click "Start Mission" to send them, or "Clear" to discard the
queue and start over.

Markers stay visible on the map after clicking Start Mission - they're
not cleared automatically, so you can see the route while it flies. They
only go away when you click "Clear" (which removes all markers, including
ones from previously-sent missions, not just the current queue). Starting
a new queue after sending one doesn't touch the old markers - they stay
put, the new points just start a fresh, separate batch.

Unchecking "Queue waypoints on map click" and then using a regular single
Fly-to-Here click clears any leftover waypoint markers automatically, so
switching back to single-click mode doesn't leave old routes cluttering
the map.

This is deliberately **not** the same mechanism as Fly to Here. Instead
of one-shot GUIDED repositioning, it uploads the points as a real onboard
AUTO mission using MAVLink's actual mission protocol
(`MISSION_CLEAR_ALL` -> `MISSION_COUNT` -> `MISSION_ITEM_INT` per point,
requested by the vehicle one at a time -> `MISSION_ACK`), then sets the
mission to start at waypoint 0 and switches to AUTO. The advantage:
ArduPilot owns the waypoint-to-waypoint progression itself, onboard, using
its own `WP_RADIUS` "close enough" logic - it doesn't depend on the GCS
staying connected once the mission starts, unlike a scheme where the GCS
watches position and re-sends the next target itself. This is the same
underlying mechanism Mission Planner's flight-plan tab uses.

Mission item 0 is uploaded as a home/reference placeholder (ArduPilot
convention, also followed by Mission Planner) rather than your first
clicked point - your real waypoints start at item 1, and the mission is
explicitly set to start there. Without this, the vehicle silently treated
item 0 as home and started flying from your *second* click instead of
your first. The placeholder uses the vehicle's actual known home position
when available, falling back to your first waypoint's own coordinates if
home hasn't been received yet.

You'll be asked for one altitude that applies to all queued points when
you click Start Mission (not per-point) - simplest for a quick multi-point
flight; if you need different altitudes per waypoint, that's a genuine
mission-planning use case better served by Mission Planner's full flight
plan editor.

## Fly to Here (GUIDED mode)

Click anywhere on the map to drop a pin with a "Fly to Here" button. Clicking
it prompts for a relative altitude, then asks you to confirm before sending
anything - this switches the vehicle to GUIDED mode and repositions it
immediately, so nothing is sent without an explicit yes.

The pin gets removed when you click the popup's own close (x) button, not
just left behind after the popup bubble disappears - popup and marker are
separate Leaflet objects, so this needed an explicit `popupclose` listener
tying them together. It's also cleared automatically if you start a
multi-waypoint mission afterward, since that's a separate marker lifecycle
that otherwise wouldn't know about a leftover single-point target.

Under the hood this sends a single `MAV_CMD_DO_REPOSITION` command (the
standard MAVLink command for exactly this purpose - see mavlink.io) with
the `CHANGE_MODE` flag set, so the mode switch and the fly-to happen
together in one command.

To test the command itself without clicking anything (useful for checking
your vehicle actually responds), see `tools/test_fly_to.py`, which connects
like the real app and calls `fly_to()` directly:

```bash
python tools/fake_vehicle.py       # terminal 1 - also prints any command it receives
python tools/test_fly_to.py        # terminal 2 - from inside tools/
```

## Map marker

The drone position marker is a top-down airplane silhouette (SVG,
rotates to heading), not a plain triangle. It also animates smoothly
between real position updates via `requestAnimationFrame` interpolation
instead of jumping directly to each new fix - real telemetry only
arrives at ~2-3 Hz, which looks jerky without this. Heading interpolates
the shortest way around (a turn crossing 0/360 animates through 0, not
the long way through 180).

Following also pans the map itself on the same per-frame schedule as the
marker's animation (via `{animate:false}`, so Leaflet's own separate pan
easing doesn't fight against our already-smooth per-frame position) -
not just once per real telemetry update, which looked like the map was
jumping in steps even though the marker itself moved smoothly.

"Follow UAV" lives directly on the map now (top-left, to the right of
the +/- zoom buttons) instead of as a separate row above it - toggling
it is handled entirely inside the map's own JavaScript, no round-trip
through Python needed since it only affects the map's own panning
behavior.

## Where to go next

1. **Test against a simulator first.** Run ArduPilot SITL or a PX4 SITL
   build and point this app at its UDP output port (SITL usually already
   streams to `127.0.0.1:14550` by default) before touching a real aircraft.
2. **Add a heading tape / compass strip** next to the artificial horizon —
   same QPainter technique as `artificial_horizon.py`.
3. **Add mission upload/download** using pymavlink's `mission_*` helpers if
   you want waypoint support later.
4. **Add reconnect logic** to `MavlinkLink` (currently it exits its loop if
   the socket errors out repeatedly — fine for a first version, but a real
   GCS should retry).
5. **Split UI from data** further with a proper Qt model if the app grows —
   right now widgets are updated directly from signal handlers, which is
   fine at this scale but gets messy past a certain point.

## A note on the map

The Leaflet map is a self-contained HTML page with OpenStreetMap tiles, so
it needs internet access for tiles the first time (they're cached by
Chromium after that). If you'll be flying somewhere offline, you can swap
the tile URL for a local tile server or pre-downloaded tile set later.
