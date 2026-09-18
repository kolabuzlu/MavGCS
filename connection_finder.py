"""Find the vehicle, instead of asking the user where it is.

Connecting to a ground station is the one step that asks a pilot to know
something a computer already knows. The port a radio enumerated as, the
TCP port a simulator opened, the address of a laptop running SITL on the
other side of the room - all of it is discoverable, and all of it is
currently typed in by hand from memory.

What gets looked at, cheapest first:

  Serial      every port pyserial can see, named as the driver names it,
              so a SiK radio reads as a SiK radio rather than as COM7.
  Local TCP   the simulator ports on this machine. SITL opens 5760 and
              gives 5762 and 5763 to extra ground stations.
  UDP         both directions. The ports a vehicle or a bridge sends to
              without being asked - 14550 above all, the MAVLink default
              - and the ones where something is instead waiting to be
              spoken to, which only answer if you speak.
  The subnet  the same TCP ports on every other address in this /24.
              This is the case that is genuinely hard to guess: SITL on
              a PC, a ground station on a laptop, and nothing on either
              screen saying what the other one's address is.

Two phases, because they cost different amounts. A TCP connect that
succeeds costs milliseconds and says only that something is listening -
a web server would pass. So every hit is then asked for a MAVLink
heartbeat, and only what answers is offered as a vehicle. The difference
matters: "port 5762 is open" is a fact about a socket, "ArduPlane,
system 1" is a fact about an aircraft, and only the second one is worth
putting in front of a pilot.

One probe sends; the rest only read. Finding something that listens on
UDP and says nothing until spoken to cannot be done by listening, so
udp_connect_candidate() speaks first - a GCS heartbeat, the frame this
program sends a second after Connect is pressed anyway. It carries no
command and asks for nothing. Everything else opens a socket, reads and
closes. A discovery step that armed something by accident would be an
unusually bad bug, and nothing here can: no probe encodes a command, a
mode, a parameter or an arm.
"""

import os
import socket
import threading

os.environ.setdefault("MAVLINK20", "1")

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

# The simulator's own port, then the two it keeps for extra ground
# stations. Mission Planner takes 5760 and leaves the others, which is
# why a second station on the same machine wants 5762.
SITL_TCP_PORTS = (5760, 5762, 5763)

# What a vehicle or a bridge sends to when nobody has told it otherwise.
# 14550 is the MAVLink default; 14551 is the usual second one.
MAVLINK_UDP_PORTS = (14550, 14551)

# Long enough for a machine on the same wire to answer, short enough
# that a /24 finishes while somebody is still looking at the window.
TCP_CONNECT_TIMEOUT_S = 0.35

# A heartbeat is sent at 1Hz, so anything less than a second can miss a
# vehicle that is there. This is only paid for ports that already
# answered, which is a handful at most.
HEARTBEAT_TIMEOUT_S = 2.5

# How long to sit on a UDP port waiting to be spoken to. A vehicle that
# is streaming will say something well inside this.
UDP_LISTEN_S = 1.5

# What a machine on the same wire takes to answer a datagram it was
# going to answer at all: milliseconds. The full UDP_LISTEN_S is for
# waiting on a vehicle that streams on its own schedule, and spending it
# on each of 508 addresses that will never reply added twelve seconds to
# a search for nothing.
UDP_SWEEP_S = 0.6

# The subnet sweep, widest thing here. 254 addresses against three ports
# is 762 connects; at this many at once it lands in a few seconds.
SWEEP_WORKERS = 64


class Candidate:
    """One thing worth connecting to, as the connection bar needs it."""

    def __init__(self, protocol, host, port, label, detail="",
                 confirmed=False):
        self.protocol = protocol      # matches ConnectionPanel.PROTOCOLS
        self.host = host
        self.port = port
        self.label = label            # the line the user reads
        self.detail = detail          # what answered, if anything did
        self.confirmed = confirmed    # a heartbeat was seen

    def __repr__(self):
        return "Candidate(%r, %r, %r, confirmed=%r)" % (
            self.protocol, self.host, self.port, self.confirmed)


def local_ipv4():
    """This machine's address on the network it would route through.

    Connecting a UDP socket sends nothing - it only fixes which
    interface would be used - so this costs no traffic and works on a
    machine with several interfaces, where the hostname often resolves
    to the wrong one.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tcp_open(host, port, timeout=TCP_CONNECT_TIMEOUT_S):
    """Is anything listening there? Says nothing about what."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


# A ground station announces itself with a heartbeat exactly as a
# vehicle does, and on a simulator port both are flowing. Whichever
# arrives first is not the question being asked.
MAV_TYPE_GCS = 6


def describe_heartbeat(buf):
    """Describe the best heartbeat in `buf` as (text, is_vehicle).

    (None, False) when there is nothing to describe.

    "Best" means a vehicle if one is there. A simulator port carries the
    aircraft's heartbeat and the heartbeat of every ground station
    attached to it, so taking the first one seen reports whoever spoke
    most recently - which on one run here was "Gcs, system 255" and on
    the next "Fixed Wing, system 1", from the same port seconds apart.
    A ground station is still worth naming, because finding one means
    the address is right and something is already using it, but it is
    not the answer to "where is the aircraft".

    Parses bytes already read rather than driving a connection, which
    keeps timeouts and end-of-stream in one place - see read_frames().
    """
    if not buf:
        return None, False
    fallback = None
    try:
        from pymavlink import mavutil
        parser = mavutil.mavlink.MAVLink(None)
        parser.robust_parsing = True
        for msg in parser.parse_buffer(buf) or []:
            if msg.get_type() != "HEARTBEAT":
                continue
            try:
                name = mavutil.mavlink.enums["MAV_TYPE"][msg.type].name
                name = name.replace("MAV_TYPE_", "").replace("_", " ").title()
            except Exception:
                name = "Vehicle"
            text = "%s, system %d" % (name, msg.get_srcSystem())
            if msg.type == MAV_TYPE_GCS:
                # Keep it in case nothing better turns up.
                fallback = fallback or ("%s (a ground station, not a "
                                        "vehicle)" % text)
                continue
            return text, True
    except Exception:
        return None, False
    return fallback, False


def read_frames(host, port, timeout=HEARTBEAT_TIMEOUT_S, limit=8192):
    """Read whatever a TCP port sends, and stop the moment it stops.

    Written against a raw socket rather than handed to pymavlink's
    wait_heartbeat, which does not treat end-of-stream as a reason to
    give up: pointed at a port that accepts a connection and immediately
    closes it - a health check, a probe, half the things on a busy
    machine - it spins on the closed socket until its timeout expires,
    printing "EOF on TCP socket" as fast as it can go. Measured here at
    11MB of output from one probe. In a windowed build there is no
    stdout to notice it in; there is only a core doing nothing at full
    speed, once per candidate.

    So: a deadline, a read loop, and b"" means done.
    """
    deadline = None
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TCP_CONNECT_TIMEOUT_S)
    buf = b""
    try:
        if s.connect_ex((host, port)) != 0:
            return buf
        import time as _time
        deadline = _time.monotonic() + timeout
        while len(buf) < limit:
            left = deadline - _time.monotonic()
            if left <= 0:
                break
            s.settimeout(left)
            try:
                chunk = s.recv(2048)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break          # end of stream - nothing more is coming
            buf += chunk
            # Stop on the aircraft, not on the first heartbeat of any
            # kind: a ground station's arrives just as often, and
            # stopping there would report the wrong thing while the
            # vehicle's was still a few milliseconds away. If only a
            # ground station is ever heard, the deadline ends this.
            if describe_heartbeat(buf)[1]:
                break
    finally:
        try:
            s.close()
        except OSError:
            pass
    return buf


def mavlink_identity(host, port, timeout=HEARTBEAT_TIMEOUT_S):
    """Describe what is behind a TCP port as (text, is_vehicle).

    Only ever reads. (None, False) when nothing identified itself.
    """
    return describe_heartbeat(read_frames(host, port, timeout))


def serial_candidates():
    """Every serial port, named the way its driver names it."""
    out = []
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
    except Exception:
        return out
    for p in ports:
        # The description is what identifies it - "Silicon Labs CP210x"
        # or "FT232R USB UART" - where the device name is just a slot.
        detail = (p.description or "").strip()
        if detail in ("", "n/a"):
            detail = (p.manufacturer or "").strip()
        out.append(Candidate("Serial", p.device, "57600",
                             p.device, detail))
    return out


def udp_listening_candidate(port, seconds=UDP_LISTEN_S):
    """Bind a UDP port and see whether anything is already sending to it.

    Binds rather than sends: a vehicle streaming to this machine is
    already addressing this port, so the only question is whether
    anything arrives. If the port is taken, something else is listening
    - which usually means another ground station has it, and that is
    worth saying rather than hiding.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(seconds)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        s.close()
        return Candidate("UDP (listen)", "0.0.0.0", str(port),
                         "UDP %d" % port,
                         "in use by another program", False)
    # Keep reading until a heartbeat turns up rather than judging the
    # first datagram. One datagram is one frame, and on a vehicle
    # streaming twenty message types the odds of the first one being the
    # heartbeat are poor - which is why this said "MAVLink from
    # 127.0.0.1" when it could have said which aircraft.
    import time as _time
    deadline = _time.monotonic() + seconds
    data, addr, seen = b"", None, b""
    try:
        while _time.monotonic() < deadline:
            s.settimeout(max(0.05, deadline - _time.monotonic()))
            try:
                data, addr = s.recvfrom(2048)
            except (socket.timeout, OSError):
                break
            seen += data
            if describe_heartbeat(seen)[1]:
                break
    finally:
        s.close()
    if addr is None:
        return None
    data = seen
    # MAVLink v1 frames start 0xFE, v2 frames 0xFD. Anything else on a
    # MAVLink port is somebody else's traffic and not worth offering.
    if not data or data[0] not in (0xFE, 0xFD):
        return None
    # One datagram is one frame, and it is often not the heartbeat, so
    # not naming the vehicle here is ordinary rather than a failure.
    who, _is_vehicle = describe_heartbeat(data)
    return Candidate("UDP (listen)", "0.0.0.0", str(port),
                     "UDP %d" % port,
                     "%s, sending from %s" % (who, addr[0]) if who
                     else "MAVLink from %s" % (addr[0],), True)


class FinderWorker(QThread):
    """Runs the search off the UI thread and reports as it goes."""

    found = Signal(object)      # Candidate
    progress = Signal(str)
    done = Signal(int)          # how many were found

    def __init__(self, sweep_subnet=True, parent=None):
        super().__init__(parent)
        self._sweep_subnet = sweep_subnet
        self._stop = threading.Event()
        self._count = 0

    def stop(self):
        self._stop.set()

    def _emit(self, cand):
        if cand is not None and not self._stop.is_set():
            self._count += 1
            self.found.emit(cand)

    def run(self):
        # 1. Serial. Instant, and the most likely answer in the field.
        if not self._stop.is_set():
            self.progress.emit("Looking for serial devices...")
            for c in serial_candidates():
                self._emit(c)

        # 2. This machine's TCP ports. A simulator on the same box is
        #    the commonest development case and costs nothing to check.
        if not self._stop.is_set():
            self.progress.emit("Checking this computer...")
            for port in SITL_TCP_PORTS:
                if self._stop.is_set():
                    break
                if not tcp_open("127.0.0.1", port):
                    continue
                who, is_vehicle = mavlink_identity("127.0.0.1", port)
                self._emit(Candidate(
                    "TCP", "127.0.0.1", str(port),
                    "This computer, port %d" % port,
                    who or "something is listening, but did not identify "
                           "itself as MAVLink",
                    is_vehicle))

        # 3. UDP. Binding tells us whether anything is already streaming
        #    here without anyone having to ask it to.
        if not self._stop.is_set():
            self.progress.emit("Listening for incoming telemetry...")
            for port in MAVLINK_UDP_PORTS:
                if self._stop.is_set():
                    break
                self._emit(udp_listening_candidate(port))
            # And the other direction on this machine: something holding
            # the port open and waiting to be addressed.
            for port in MAVLINK_UDP_PORTS:
                if self._stop.is_set():
                    break
                self._emit(udp_connect_candidate("127.0.0.1", port))

        # 4. The rest of the network. Last because it is the slowest and
        #    because the three above answer most cases.
        if self._sweep_subnet and not self._stop.is_set():
            self._sweep()

        self.done.emit(self._count)

    def _sweep(self):
        from concurrent.futures import ThreadPoolExecutor

        mine = local_ipv4()
        if not mine:
            return
        base = mine.rsplit(".", 1)[0]
        self.progress.emit("Searching %s.0/24..." % base)

        hosts = ["%s.%d" % (base, h) for h in range(1, 255)
                 if "%s.%d" % (base, h) != mine]
        targets = [(h, p) for h in hosts for p in SITL_TCP_PORTS]

        hits = []
        with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
            def probe(t):
                if self._stop.is_set():
                    return None
                return t if tcp_open(t[0], t[1]) else None
            for r in pool.map(probe, targets):
                if r is not None:
                    hits.append(r)

        # The UDP side of the same sweep. One small datagram per address
        # and port, and only the two ports MAVLink actually uses - this
        # is a question asked of a handful of ports, not a port scan.
        if not self._stop.is_set():
            self.progress.emit("Asking %s.0/24 on UDP..." % base)
            udp_targets = [(h, p) for h in hosts for p in MAVLINK_UDP_PORTS]
            with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
                def ask(t):
                    if self._stop.is_set():
                        return None
                    return udp_connect_candidate(t[0], t[1], UDP_SWEEP_S)
                for cand in pool.map(ask, udp_targets):
                    self._emit(cand)

        # Only the hits pay for a heartbeat, and they are few.
        for host, port in hits:
            if self._stop.is_set():
                break
            self.progress.emit("Asking %s:%d who it is..." % (host, port))
            who, is_vehicle = mavlink_identity(host, port)
            self._emit(Candidate(
                "TCP", host, str(port),
                "%s, port %d" % (host, port),
                who or "something is listening, but did not identify "
                       "itself as MAVLink",
                is_vehicle))


class ConnectionFinderDialog(QDialog):
    """Shows what the search finds, and hands back what was picked.

    Results appear as they arrive rather than all at once at the end: the
    serial ports are there immediately, the local simulator a moment
    later, and the subnet sweep some seconds after that. A window that
    stayed empty for five seconds and then filled would look broken for
    four of them.

    A vehicle that answered a heartbeat is marked; something merely
    listening is still offered, because a port that does not answer in
    two seconds is sometimes a vehicle that was busy, and refusing to
    show it would leave the user with no way to reach it from here.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find a connection")
        self.setMinimumWidth(460)
        self._worker = None
        self._chosen = None

        layout = QVBoxLayout(self)

        self.status = QLabel("Starting...")
        self.status.setStyleSheet("color: #9aa4ad; font-size: 11px;")
        layout.addWidget(self.status)

        self.list = QListWidget()
        self.list.setStyleSheet("font-size: 11px;")
        # Picking a line and connecting are the same intent, so a
        # double-click does both rather than only selecting.
        self.list.itemDoubleClicked.connect(lambda _i: self._use())
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.again_btn = QPushButton("Search again")
        self.again_btn.clicked.connect(self._start)
        row.addWidget(self.again_btn)
        row.addStretch(1)
        self.use_btn = QPushButton("Use this")
        self.use_btn.setEnabled(False)
        self.use_btn.setStyleSheet("font-weight: bold;")
        self.use_btn.clicked.connect(self._use)
        row.addWidget(self.use_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)

        self.list.currentRowChanged.connect(
            lambda r: self.use_btn.setEnabled(r >= 0))

        self._start()

    def _start(self):
        self._stop_worker()
        self.list.clear()
        self.use_btn.setEnabled(False)
        self.again_btn.setEnabled(False)
        self._worker = FinderWorker(parent=self)
        self._worker.found.connect(self._on_found)
        self._worker.progress.connect(self.status.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_found(self, cand):
        # Marked rather than sorted: the order things are found in is
        # cheapest-first, which is also roughly likeliest-first, and
        # re-ordering under the cursor while a search runs is unkind.
        mark = "\u2713 " if cand.confirmed else "   "
        text = "%s%s" % (mark, cand.label)
        if cand.detail:
            text += "   -   %s" % cand.detail
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, cand)
        if cand.confirmed:
            item.setForeground(QColor("#8fd18f"))
        self.list.addItem(item)
        if self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def _on_done(self, count):
        self.again_btn.setEnabled(True)
        if count:
            self.status.setText(
                "%d found. A tick means it answered as MAVLink." % count)
        else:
            self.status.setText(
                "Nothing found. Check the vehicle is powered and, for a "
                "network link, that both machines are on the same one.")

    def _use(self):
        item = self.list.currentItem()
        if item is None:
            return
        self._chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def chosen(self):
        """The Candidate the user picked, or None."""
        return self._chosen

    def _stop_worker(self):
        if self._worker is None:
            return
        self._worker.stop()
        # The sweep can be mid-connect on a socket with a timeout still
        # to run, so it is asked to stop and then given long enough to
        # notice. Destroying a running QThread is a qFatal.
        self._worker.wait(4000)
        self._worker = None

    def done(self, result):
        self._stop_worker()
        super().done(result)


def gcs_heartbeat():
    """One GCS heartbeat, as any ground station announces itself with."""
    from pymavlink import mavutil
    mav = mavutil.mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
    msg = mav.heartbeat_encode(
        MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0,
        mavutil.mavlink.MAV_STATE_ACTIVE)
    return msg.pack(mav)


def udp_connect_candidate(host, port, seconds=UDP_LISTEN_S):
    """Speak first, and see whether anything answers.

    This is the one probe here that sends. It has to: an endpoint
    listening on UDP - a vehicle, a simulator, a telemetry bridge set to
    udpin - says nothing at all until something addresses it, and it
    learns where to reply from the packet that arrives. Listening for it
    would wait forever. There is no passive way to find a thing whose
    whole design is to wait.

    What it sends is a GCS heartbeat: the frame every ground station
    announces itself with, the same one this program sends a second
    after Connect is pressed. It carries no command, sets nothing, and
    asks for nothing. An autopilot that receives it learns that a ground
    station exists at this address, which is true, and which it would
    learn a moment later anyway if the user picked this result.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(seconds)
    try:
        try:
            s.sendto(gcs_heartbeat(), (host, port))
        except OSError:
            return None
        import time as _time
        deadline = _time.monotonic() + seconds
        seen = b""
        while _time.monotonic() < deadline:
            s.settimeout(max(0.05, deadline - _time.monotonic()))
            try:
                data, _addr = s.recvfrom(2048)
            except (socket.timeout, OSError):
                return None
            seen += data
            who, is_vehicle = describe_heartbeat(seen)
            if is_vehicle:
                return Candidate("UDP (connect to)", host, str(port),
                                 "%s, port %d" % (host, port), who, True)
        return None
    finally:
        s.close()
