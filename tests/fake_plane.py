"""A fake ArduPlane with several telemetry ports, for testing multi-link.

Not a test itself (run_all.py runs test_*.py only): test_multilink_live.py
uses it, and so can anything else that wants a plane on more than one link.

Each port is its own MAVLink channel and numbers its own frames, as an
autopilot's serial ports do - which is the whole point: the ground
station must keep one count per link. A port can be told to fall silent,
to drop every Nth frame or to keep only every Nth, or to deliver late; a
TCP port is the server end, as MavLTE's TCP mode is, and can hang up on
its client and take the next one.
"""

import os
import socket
import threading
import time
from collections import deque

os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil

M = mavutil.mavlink


def free_port(kind=socket.SOCK_DGRAM):
    """A port nothing is using right now."""
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Capture:
    """Where pymavlink writes an encoded frame, so it can be sent - or not."""

    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf = bytes(data)


class Channel:
    """One telemetry port on the plane: its own sequence, its own socket."""

    def __init__(self, sysid):
        self.out = _Capture()
        self.mav = M.MAVLink(self.out, srcSystem=sysid, srcComponent=1)
        self.parser = M.MAVLink(None)
        self.silent = False
        self.drop_every = 0
        # Most frames lost, but evenly: never a second's silence, which a
        # share dropped at random gives now and then on a slow machine.
        self.keep_every = 0
        self.delay = 0.0            # how long each frame takes to get there
        self._held = deque()        # (when it is due, bytes), in order
        self.sent = 0
        self.got = []           # (type, command) of what the GCS sent here
        self.plane = None

    def frame(self, msg):
        """Encode one frame; returns its bytes, or None if it is dropped."""
        self.mav.send(msg)      # numbers it, whether or not it goes out
        self.sent += 1
        if self.silent:
            return None
        if self.drop_every and self.sent % self.drop_every == 0:
            return None
        if self.keep_every and self.sent % self.keep_every:
            return None
        return self.out.buf

    def later(self, data, now):
        """Send it once its delay has passed - in order, as a link
        delivers: a frame never overtakes one sent before it."""
        due = max(now + self.delay, self._held[-1][0] if self._held else now)
        if due <= now:
            self.send(data)
        else:
            self._held.append((due, data))

    def release(self, now):
        """Send whatever has been held for long enough."""
        while self._held and self._held[0][0] <= now:
            self.send(self._held.popleft()[1])

    def heard(self, data):
        try:
            msgs = self.parser.parse_buffer(data) or []
        except Exception:
            return
        for m in msgs:
            self.got.append((m.get_type(), getattr(m, "command", None)))
            # ArduPlane obeys a mode change whichever port it came in on,
            # and its heartbeats on every port then report the new mode.
            if (m.get_type() == "COMMAND_LONG"
                    and m.command == M.MAV_CMD_DO_SET_MODE and self.plane):
                self.plane.custom_mode = int(m.param2)


class UdpChannel(Channel):
    """A port that streams to the ground station's UDP port."""

    def __init__(self, gcs_port, sysid=1):
        super().__init__(sysid)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.dest = ("127.0.0.1", gcs_port)

    def send(self, data):
        try:
            self.sock.sendto(data, self.dest)
        except OSError:
            pass

    def poll(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except OSError:
                return
            self.heard(data)

    def close(self):
        self.sock.close()


class TcpChannel(Channel):
    """The plane's end of a TCP link - a server, as MavLTE's TCP mode is."""

    def __init__(self, port, sysid=1):
        super().__init__(sysid)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen(1)
        self.server.setblocking(False)
        self.client = None

    def hang_up(self):
        """The bridge restarting: drop the client, keep listening."""
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None

    def send(self, data):
        if self.client is None:
            return
        try:
            self.client.sendall(data)
        except OSError:
            self.hang_up()

    def poll(self):
        if self.client is None:
            try:
                self.client, _ = self.server.accept()
                self.client.setblocking(False)
            except OSError:
                return
        while True:
            try:
                data = self.client.recv(4096)
            except OSError:
                return
            if not data:
                self.hang_up()
                return
            self.heard(data)

    def close(self):
        self.hang_up()
        self.server.close()


class FakePlane(threading.Thread):
    """ArduPlane, as far as the ground station can tell: a heartbeat a
    second carrying its mode, attitude twenty times a second carrying
    its clock, and a mode change obeyed whichever port it came in on."""

    def __init__(self):
        super().__init__(daemon=True)
        self.channels = []
        self.custom_mode = 0
        self.running = True
        self.t0 = time.time()

    def run(self):
        last_hb = 0.0
        while self.running:
            now = time.time()
            boot_ms = int((now - self.t0) * 1000) + 1
            for ch in list(self.channels):
                ch.plane = self
                ch.poll()
                ch.release(now)
                frames = [M.MAVLink_attitude_message(boot_ms, 0.1, 0.0, 1.0,
                                                     0.0, 0.0, 0.0)]
                if now - last_hb >= 1.0:
                    frames.insert(0, M.MAVLink_heartbeat_message(
                        M.MAV_TYPE_FIXED_WING, M.MAV_AUTOPILOT_ARDUPILOTMEGA,
                        M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, self.custom_mode,
                        M.MAV_STATE_STANDBY, 3))
                for f in frames:
                    data = ch.frame(f)
                    if data is not None:
                        ch.later(data, now)
            if now - last_hb >= 1.0:
                last_hb = now
            time.sleep(0.05)

    def stop(self):
        self.running = False
        for ch in self.channels:
            try:
                ch.close()
            except OSError:
                pass
