"""Does the position QR code actually lead to the aircraft?

The point of this feature is that a phone, scanning a laptop screen in a
field, ends up at the right patch of ground. So the test is not "a QR
code was produced" - it is that a real decoder reading the real image
recovers the exact coordinates that went in.

Uses OpenCV's QR detector, which is a genuine scanner rather than segno
reading back its own working.
"""

import os
import re
import sys

# The repo root, wherever this checkout happens to be. Everything below
# imports the real modules, so this has to come before them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import io

import cv2
import numpy as np
import segno

from main import PositionQrDialog

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


def encode(lat, lon):
    """The bytes the dialog would draw, for these coordinates."""
    url = PositionQrDialog.MAPS_URL % (lat, lon)
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=7, border=3)
    return url, buf.getvalue()


def scan(png):
    """Read it back the way a phone would."""
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    decoded, _pts, _ = cv2.QRCodeDetector().detectAndDecode(arr)
    return decoded


def coords_of(url):
    m = re.search(r"query=(-?[\d.]+),(-?[\d.]+)", url or "")
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


print("")
print("the code leads back to the exact spot")
HOME = (39.925386, 32.836523)
url, png = encode(*HOME)
back = scan(png)
note("a real decoder reads it", bool(back), repr(back[:48] if back else None))
note("byte for byte what was encoded", back == url)
lat, lon = coords_of(back)
note("and the coordinates survive exactly",
     lat == HOME[0] and lon == HOME[1], "%s, %s" % (lat, lon))

print("")
print("places that are not the northern hemisphere")
for name, (lat0, lon0) in (("south + west (Patagonia)", (-51.623000, -69.216000)),
                           ("south + east (Tasmania)", (-42.880000, 147.325000)),
                           ("north + west (Nova Scotia)", (44.648000, -63.575000)),
                           ("near zero (Gulf of Guinea)", (0.000100, -0.000100))):
    u, p = encode(lat0, lon0)
    d = scan(p)
    la, lo = coords_of(d)
    note(name, la == lat0 and lo == lon0, "%s, %s" % (la, lo))

print("")
print("the link a phone follows")
note("it is the universal https form, not a geo: URI",
     url.startswith("https://www.google.com/maps/"), url[:40])
note("and it carries a query the maps app understands",
     "?api=1&query=" in url)

print("")
print("precision is enough to walk to")
# Six decimal places of latitude is about 0.11 m; the aircraft is not
# going to be found more precisely than that anyway.
u, p = encode(39.9253864, 32.8365231)
la, lo = coords_of(scan(p))
err_m = max(abs(la - 39.9253864), abs(lo - 32.8365231)) * 111320.0
note("rounding loses less than a metre", err_m < 1.0, "%.3f m" % err_m)

print("")
print("the dialog can draw it")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
pix = PositionQrDialog._render(url)
note("a pixmap comes back", pix is not None and not pix.isNull(),
     "%dx%d" % (pix.width(), pix.height()) if pix else "None")
note("and it is big enough to scan off a screen",
     pix is not None and pix.width() >= 200, )

print("")
print("a missing library must not stop the program")
import builtins
real_import = builtins.__import__


def no_segno(name, *a, **k):
    if name == "segno":
        raise ImportError("pretend it is not installed")
    return real_import(name, *a, **k)


builtins.__import__ = no_segno
try:
    note("it returns None rather than raising",
         PositionQrDialog._render(url) is None)
finally:
    builtins.__import__ = real_import

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
