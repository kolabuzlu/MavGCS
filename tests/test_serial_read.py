"""A serial port on Windows, read as its frames arrive - not in bursts.

pymavlink waits for more bytes with select() on the port's file handle,
and a serial port on Windows has none, so it sleeps instead: half the
read's timeout, every time it has caught up. MavGCS reads a lone link
with a one-second timeout and each of several links with half a second,
so every frame on a COM port reached it a quarter of a second late on
average and up to half a second late, and the HUD moved in half-second
jumps - in every version up to V2.3.3. With several links it made an RFD
look slower than a modem on 2G. mavlink_link._read_serial_promptly looks
at the port for bytes every few milliseconds instead.

No serial port is needed: a TCP link to the fake plane with its file
handle taken away goes down exactly the path pymavlink takes for one, and
a peek at the socket stands in for the port's count of waiting bytes.
"""

import os
import socket
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pymavlink import mavutil

import mavlink_link
from fake_plane import FakePlane, TcpChannel, free_port

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


def delays(fixed, timeout, seconds=4.0):
    """How late each attitude frame is read, in seconds, sorted."""
    plane = FakePlane()
    port = free_port(socket.SOCK_STREAM)
    plane.channels.append(TcpChannel(port))
    plane.start()
    conn = mavutil.mavlink_connection("tcp:127.0.0.1:%d" % port, retries=1)
    conn.fd = None                      # as a serial port on Windows has
    if fixed:
        def waiting():
            try:
                return len(conn.port.recv(4096, socket.MSG_PEEK))
            except BlockingIOError:
                return 0
        mavlink_link._read_serial_promptly(conn, waiting)
    late = []
    end = time.time() + seconds
    while time.time() < end:
        m = conn.recv_match(blocking=True, timeout=timeout)
        if m is not None and m.get_type() == "ATTITUDE":
            sent = plane.t0 + (m.time_boot_ms - 1) / 1000.0
            late.append(time.time() - sent)
    plane.stop()
    conn.close()
    return sorted(late[10:])            # the start-up aside


def ms(late):
    return "typically %.0f ms, at worst %.0f ms" % (
        1000 * statistics.median(late), 1000 * late[-1])


print("")
print("1. a port with no file handle, as pymavlink reads it")
before = delays(False, 1.0)
note("read in bursts: a frame is typically a quarter of a second late",
     statistics.median(before) > 0.1, ms(before))

print("")
print("2. the same port with MavGCS looking for bytes itself")
for timeout, what in ((1.0, "one link"), (0.5, "each of several links")):
    after = delays(True, timeout)
    note("%s: each frame read as it arrives" % what,
         statistics.median(after) < 0.05 and after[-1] < 0.15, ms(after))

print("")
print("3. what it touches, and what it does")


class Conn:
    pass


class Port:
    def __init__(self):
        self.count = 0
        self.gone = False

    @property
    def in_waiting(self):
        if self.gone:
            raise OSError("ClearCommError failed")
        return self.count


c = Conn()
c.fd, c.select = 7, "pymavlink's own"
mavlink_link._read_serial_promptly(c)
note("a link with a file handle is left alone - macOS serial, any network",
     c.select == "pymavlink's own")
c = Conn()
c.fd, c.port, c.select = None, object(), "pymavlink's own"
mavlink_link._read_serial_promptly(c)
note("so is one with no count of waiting bytes", c.select == "pymavlink's own")

c = Conn()
c.fd, c.port = None, Port()
mavlink_link._read_serial_promptly(c)
t = time.time()
got = c.select(0.2)
took = time.time() - t
note("nothing arrives: it waits as long as it was told, then says so",
     got is False and 0.18 <= took < 0.6, "%.2f s" % took)
threading.Timer(0.1, lambda: setattr(c.port, "count", 12)).start()
t = time.time()
got = c.select(1.0)
took = time.time() - t
note("bytes arrive while it waits: it is back within moments",
     got is True and took < 0.3, "%.2f s" % took)
c.port.gone = True
t = time.time()
got = c.select(1.0)
note("unplugged: back at once, for the read to say what happened",
     got is True and time.time() - t < 0.1)

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
