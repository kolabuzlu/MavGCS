"""
Reproduce the WebEngine compositor crash on the bench, with no aircraft.

    bench.bat                 (in PowerShell:  .\\bench.bat)
    bench.bat straight
    bench.bat notrail

Flies a synthetic circuit at 2Hz - the rate ArduPilot sends position -
with every overlay off, and watches for the two things that mark the
crash: ANGLE reporting its input layout cache overflowed, and the access
violation that follows within seconds.

The point of this is that the fault takes five to eight minutes of real
flying to reach, which made every attempt at it cost a flight. Here it
reproduces in about seven minutes at a desk.

Modes:

  loiter   (default)  the aircraft circles, as it does in LOITER or
                      after RTL. The heading sweeps through 360 degrees
                      and the map pans round. This reproduces the crash.
  straight            the same ground speed on a constant bearing. The
                      map still pans and the trail still grows; the only
                      thing missing is the turning. This is the control
                      for the loiter theory.
  novectors           circling, with the vector overlays off. Those are
                      replaced wholesale on every update, unlike the
                      trail which is only appended to.
  notrail             circling, but the trail capped at 2 points. Ruled
                      the trail out - it crashed anyway, in the air and
                      with the overlays off.
  frozen              the aircraft holds position. Same 2Hz of updates,
                      but nothing moves at all. Survives.

Prints REPRODUCED or SURVIVED at the end, and keeps the full output in
logs\\ either way.
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOGS = ROOT / "logs"
RUN_FOR_S = 660          # 11 minutes - past every crash seen so far
MODES = ("loiter", "straight", "novectors", "notrail", "frozen")


# --------------------------------------------------------------- child --
# Driving the app has to happen in the same process as the window, and a
# crash takes that process with it - so the parent below runs this as a
# child and reads its output. That way the verdict survives the crash.
def drive(mode):
    import math
    import time

    os.environ["MAVLINK20"] = "1"
    os.environ["PYTHONFAULTHANDLER"] = "1"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        + " --enable-logging=stderr --log-level=0").strip()
    sys.path.insert(0, str(ROOT))

    import faulthandler
    faulthandler.enable()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication([])
    import main as M

    win = M.MainWindow("udpin:0.0.0.0:14887")
    win.showMaximized()

    # A circuit the size of a real one, over Anitkabir.
    LAT0, LON0, RADIUS_DEG = 39.9250, 32.8360, 0.004
    ANGLE_STEP = 0.01
    started = time.time()
    n = {"i": 0}

    def step():
        n["i"] += 1
        if mode == "frozen":
            lat, lon, hdg = LAT0, LON0, 0.0
        elif mode == "straight":
            # The same ground speed as the circuit - one arc step is
            # RADIUS_DEG * ANGLE_STEP - but on a constant bearing, so the
            # heading never changes and the map pans one way instead of
            # sweeping round. Everything else is identical.
            lat = LAT0 + n["i"] * RADIUS_DEG * ANGLE_STEP
            lon = LON0
            hdg = 0.0
        else:
            a = n["i"] * ANGLE_STEP
            lat = LAT0 + RADIUS_DEG * math.sin(a)
            lon = LON0 + RADIUS_DEG * math.cos(a)
            hdg = (math.degrees(a) + 90.0) % 360.0
        win.map_view.update_position(lat, lon, hdg)
        if n["i"] % 60 == 0:
            win.map_view.page().runJavaScript(
                "JSON.stringify(mavgcsDrawState());", report)
        QTimer.singleShot(500, step)

    def report(payload):
        print("[%6.1fs] %s" % (time.time() - started, payload), flush=True)

    def arm():
        if mode == "notrail":
            win.map_view.page().runJavaScript(
                "TRAIL_MAX_POINTS = 2; path.setLatLngs([]);")
        elif mode == "novectors":
            # Circling as usual, but with the track, heading, nav and turn
            # arc lines switched off - the overlays that are rebuilt from
            # scratch on every update rather than appended to.
            win.map_view.page().runJavaScript("setVectorsEnabled(false);")
        step()

    def stop():
        print("[%6.1fs] reached the end of the run, still alive"
              % (time.time() - started), flush=True)
        app.quit()

    QTimer.singleShot(9000, arm)
    QTimer.singleShot(RUN_FOR_S * 1000, stop)
    app.exec()
    return 0


# -------------------------------------------------------------- parent --
def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "loiter").lower()
    if mode not in MODES:
        sys.exit("Modes are: %s" % ", ".join(MODES))

    LOGS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS / ("bench_%s_%s.log" % (mode, stamp))

    env = dict(os.environ)
    env["MAVGCS_BENCH_CHILD"] = mode
    env["PYTHONUNBUFFERED"] = "1"

    print("Bench mode '%s'. Up to %d minutes; leave the window alone."
          % (mode, RUN_FOR_S // 60))
    print("Log: %s" % path)
    print("")

    overflows = 0
    faulted = False
    with open(path, "w", encoding="utf-8", errors="replace") as log:
        log.write("bench mode %s, started %s\n" % (mode, datetime.now()))
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "bench_trail.py"), mode],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if "TrimCache" in line:
                overflows += 1
                print("  ANGLE input layout cache overflowed (%d)" % overflows)
            elif "fatal exception" in line:
                faulted = True
                print("  *** access violation ***")
            elif line.startswith("["):
                m = re.search(r'"trail":(\d+)', line)
                if m:
                    print("  %s trail %s points"
                          % (line.split("]")[0] + "]", m.group(1)))
        code = proc.wait()
        log.write("\nexit code %d (0x%08X)\n" % (code, code & 0xFFFFFFFF))

    print("")
    if faulted or overflows:
        print("REPRODUCED - %d cache overflow(s)%s"
              % (overflows, ", and it crashed" if faulted else ""))
    else:
        print("SURVIVED - no cache overflow, no crash")
    print("  %s" % path)
    return 1 if faulted else 0


if __name__ == "__main__":
    child = os.environ.get("MAVGCS_BENCH_CHILD")
    sys.exit(drive(child) if child else main())
