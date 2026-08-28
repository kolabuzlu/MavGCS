"""
Headless smoke test: runs MavlinkLink against whatever is streaming to
udpin:0.0.0.0:14550 (e.g. tools/fake_vehicle.py, or later a real SITL
instance) and prints what it decodes. No GUI/display needed - useful for
confirming the connection + parsing logic works before wiring it to Qt.
"""

import sys
from PySide6.QtCore import QCoreApplication, QTimer

sys.path.insert(0, "..")
from mavlink_link import MavlinkLink

app = QCoreApplication(sys.argv)

conn_str = sys.argv[1] if len(sys.argv) > 1 else "udpin:0.0.0.0:14550"
link = MavlinkLink(conn_str)
link.attitude_update.connect(
    lambda r, p, y: print(f"ATTITUDE   roll={r:.3f} pitch={p:.3f} yaw={y:.3f}")
)
link.position_update.connect(
    lambda lat, lon, alt, hdg: print(
        f"POSITION   lat={lat:.6f} lon={lon:.6f} alt={alt:.1f} hdg={hdg:.0f}"
    )
)
link.vfr_update.connect(
    lambda a, g, c: print(f"VFR_HUD    airspeed={a:.1f} groundspeed={g:.1f} climb={c:.1f}")
)
link.status_update.connect(lambda d: print(f"STATUS     {d}"))
link.connection_status.connect(lambda ok, msg: print(f"CONNECTION ok={ok} msg={msg}"))

link.start()

print(f"Connecting via {conn_str} for 8 seconds...")


def _finish():
    link.stop()
    print("Done.")
    app.quit()


QTimer.singleShot(8000, _finish)
app.exec()
