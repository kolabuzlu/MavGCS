"""Settings: the attitude and GPS position rates on offer, the ones a fresh
install starts with, and a saved one kept.

The pilot asked for 4 Hz between 3 and 5 Hz, for both, and for a fresh
install to start at 4 Hz attitude and 2 Hz position (2026-10-13). The
list is shared by the two choosers and by the check that throws out a
saved rate the dialog does not offer - so a rate offered but not in that
check would be shown, saved, and then quietly put back to the default on
the next connection.

The dialog is built for real, reading a made-up settings file: the user's
own is never touched.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import main as app_main

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail != "" else ""))
    if not ok:
        fails.append(name)


app = QApplication.instance() or QApplication(sys.argv)
D = app_main.TelemetryRatesDialog
WANTED = ["1 Hz", "2 Hz", "3 Hz", "4 Hz", "5 Hz"]

print("")
print("a fresh install: nothing saved yet")
saved = {}
app_main.load_settings = lambda: dict(saved)
note("it asks for attitude at 4 Hz and GPS position at 2 Hz",
     D.current()[:2] == (4.0, 2.0), D.current())
fresh = D()
note("and the dialog opens on 4 Hz and 2 Hz",
     (fresh.attitude_combo.currentText(), fresh.position_combo.currentText())
     == ("4 Hz", "2 Hz"),
     (fresh.attitude_combo.currentText(), fresh.position_combo.currentText()))

print("")
print("the rates on offer, and a saved one kept")
saved = {D.SETTING_ATTITUDE: 4.0, D.SETTING_POSITION: 4.0}
app_main.load_settings = lambda: dict(saved)
note("4 Hz saved for both is what the next connection asks for",
     D.current()[:2] == (4.0, 4.0), D.current())
dialog = D()
for label, combo in (("Attitude", dialog.attitude_combo),
                     ("GPS position", dialog.position_combo)):
    items = [combo.itemText(i) for i in range(combo.count())]
    note("%s offers 1 to 5 Hz, 4 Hz among them" % label, items == WANTED,
         items)
    note("%s opens on the saved 4 Hz" % label, combo.currentData() == 4.0,
         combo.currentText())

# A rate saved before stays, whatever the defaults are now - 5 Hz chosen
# under the old default is kept, not moved to the new one.
saved = {D.SETTING_ATTITUDE: 5.0, D.SETTING_POSITION: 2.0}
note("a rate chosen before stays as chosen",
     D.current()[:2] == (5.0, 2.0), D.current())
# A rate the dialog does not offer is still thrown out, as before.
saved = {D.SETTING_ATTITUDE: 7.0, D.SETTING_POSITION: "fast"}
note("a rate not on offer falls back to the defaults, 4 Hz and 2 Hz",
     D.current()[:2] == (4.0, 2.0), D.current())

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
