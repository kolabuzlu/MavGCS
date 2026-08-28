"""
Headless test: connects via MavlinkLink exactly like the real app, waits
for the connection, then calls fly_to() and prints the feedback signal.
Check the fake_vehicle.py terminal output to see the actual decoded
COMMAND_INT it received.
"""

import sys
from PySide6.QtCore import QCoreApplication, QTimer

sys.path.insert(0, "..")
from mavlink_link import MavlinkLink

app = QCoreApplication(sys.argv)

link = MavlinkLink("udpin:0.0.0.0:14550")
link.connection_status.connect(lambda ok, msg: print(f"CONNECTION ok={ok} msg={msg}"))
link.command_feedback.connect(lambda msg: print(f"FEEDBACK: {msg}"))

sent = {"done": False}


def try_send():
    if link.master is not None and not sent["done"]:
        sent["done"] = True
        print("Sending fly_to(47.610000, -122.340000, 75)...")
        link.fly_to(47.610000, -122.340000, 75)


def try_modes():
    print("Sending set_mode for each mode...")
    for name in ["MANUAL", "FBWA", "CRUISE", "LOITER", "AUTO", "RTL", "TAKEOFF", "AUTOLAND"]:
        link.set_mode(name)


def try_arm():
    print("Sending send_arm(True, force=False)...")
    link.send_arm(True, force=False)
    print("Sending send_arm(False, force=False)...")
    link.send_arm(False, force=False)
    print("Sending send_arm(True, force=True)...")
    link.send_arm(True, force=True)


def try_guided():
    print("Sending change_speed(18.5)...")
    link.change_speed(18.5)
    print("Sending change_altitude(120)...")
    link.change_altitude(120)


def finish():
    link.stop()
    app.quit()


link.start()
poll = QTimer()
poll.timeout.connect(try_send)
poll.start(200)

QTimer.singleShot(2500, try_modes)
QTimer.singleShot(3500, try_arm)
QTimer.singleShot(4500, try_guided)
QTimer.singleShot(6000, finish)
app.exec()
