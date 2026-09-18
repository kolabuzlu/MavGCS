"""Does the connection finder identify what it found, and give up fast?

Two things here were bugs before they were tests, and both were found by
pointing the finder at a real machine rather than by reading it.

A simulator port carries the aircraft's heartbeat and the heartbeat of
every ground station attached to it. Taking the first one seen reported
"Gcs, system 255" on one run and "Fixed Wing, system 1" on the next,
from the same port seconds apart - so the answer depended on timing, and
half the time it named the wrong thing.

And a TCP port that accepts a connection then closes it is common: a
health check, a probe, anything on a busy machine. Handed to pymavlink's
wait_heartbeat, that spins on the closed socket until the timeout,
printing "EOF on TCP socket" as fast as it can - 11MB from one probe
here. A windowed build has no stdout to notice it in, so it would have
been a core at full speed per candidate and nothing on screen.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import connection_finder as cf

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


def heartbeat(system, mav_type):
    """One encoded HEARTBEAT, as it would arrive off the wire."""
    from pymavlink import mavutil
    mav = mavutil.mavlink.MAVLink(None, srcSystem=system, srcComponent=1)
    msg = mav.heartbeat_encode(
        mav_type, mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0,
        mavutil.mavlink.MAV_STATE_ACTIVE)
    return msg.pack(mav)


FIXED_WING = 1
GCS = cf.MAV_TYPE_GCS

print("")
print("1. who it says is there")

text, is_vehicle = cf.describe_heartbeat(heartbeat(1, FIXED_WING))
note("a vehicle is named as one", is_vehicle and "system 1" in (text or ""),
     repr(text))

text, is_vehicle = cf.describe_heartbeat(heartbeat(255, GCS))
note("a ground station is not mistaken for a vehicle", not is_vehicle,
     repr(text))
note("but it is still described", bool(text) and "ground station" in (text or ""),
     repr(text))

# The order that used to decide the answer.
both = heartbeat(255, GCS) + heartbeat(1, FIXED_WING)
text, is_vehicle = cf.describe_heartbeat(both)
note("the vehicle wins when the GCS spoke first", is_vehicle, repr(text))

both = heartbeat(1, FIXED_WING) + heartbeat(255, GCS)
text, is_vehicle = cf.describe_heartbeat(both)
note("and when the vehicle spoke first", is_vehicle, repr(text))

text, is_vehicle = cf.describe_heartbeat(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
note("a web server is not a vehicle", text is None and not is_vehicle,
     repr(text))

text, is_vehicle = cf.describe_heartbeat(b"")
note("nothing is nothing", text is None and not is_vehicle)

print("")
print("2. giving up quickly on a port that is not talking")


def serve_once(behaviour):
    """A throwaway listener. Returns its port; closes itself after one go."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
            behaviour(conn)
        except OSError:
            pass
        finally:
            try:
                srv.close()
            except OSError:
                pass
    threading.Thread(target=run, daemon=True).start()
    return port


# Accept and hang up. This is the shape that span at full speed.
port = serve_once(lambda c: c.close())
started = time.monotonic()
text, is_vehicle = cf.mavlink_identity("127.0.0.1", port, timeout=2.5)
took = time.monotonic() - started
note("end of stream ends the read", took < 1.0, "%.2fs, not 2.5s" % took)
note("and it reports nothing found", text is None and not is_vehicle)

# Nothing listening at all.
closed = socket.socket()
closed.bind(("127.0.0.1", 0))
dead_port = closed.getsockname()[1]
closed.close()
started = time.monotonic()
text, _ = cf.mavlink_identity("127.0.0.1", dead_port, timeout=2.5)
took = time.monotonic() - started
note("a refused connection returns at once", took < 1.0, "%.2fs" % took)

# A real one, answered properly.
port = serve_once(lambda c: (c.sendall(heartbeat(1, FIXED_WING)), c.close()))
text, is_vehicle = cf.mavlink_identity("127.0.0.1", port, timeout=2.5)
note("a heartbeat on a socket is read and named", is_vehicle, repr(text))

print("")
print("3. what the fields are filled with")

import main as app_main
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
panel = app_main.ConnectionPanel()

panel.apply_candidate(cf.Candidate("TCP", "192.168.2.178", "5763", "x"))
note("TCP sets protocol, host and port",
     panel.protocol_combo.currentText() == "TCP"
     and panel.host_edit.text() == "192.168.2.178"
     and panel.port_edit.text() == "5763",
     "%s %s:%s" % (panel.protocol_combo.currentText(),
                   panel.host_edit.text(), panel.port_edit.text()))

panel.apply_candidate(cf.Candidate("UDP (listen)", "0.0.0.0", "14550", "x"))
note("UDP (listen) switches protocol too",
     panel.protocol_combo.currentText() == "UDP (listen)"
     and panel.port_edit.text() == "14550",
     "%s port %s" % (panel.protocol_combo.currentText(),
                     panel.port_edit.text()))


def button_row(widget):
    """The labelled buttons on the bar, in the order they are drawn."""
    out = []

    def walk(layout):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget()
            if w is not None and hasattr(w, "text") and w.text():
                out.append(w.text())
            elif item.layout() is not None:
                walk(item.layout())

    walk(widget.find_btn.parentWidget().layout())
    return out


# Asked for by position, not by existence: "left of the Settings button".
order = button_row(panel)
note("F sits immediately left of Settings",
     "F" in order and "Settings" in order
     and order.index("F") == order.index("Settings") - 1,
     " | ".join(order))

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
