"""
Run MavGCS with everything recorded, so a crash can be explained afterwards.

    watch.bat        (in PowerShell:  .\\watch.bat)

Takes nothing. MavGCS does not connect on its own anyway - start it,
then pick the port and press Connect in the Connection panel as usual.

Writes one file per run into logs\\, holding:

  - Python's fault handler output. On a segmentation fault it dumps the
    stack of every thread as it dies. The crash we are chasing starts
    inside Qt's own code, but the Python stack still says what this
    program was doing at that instant, which is the part we can change.
  - Chromium's own log, turned up. The fault is in its compositor, so
    what it was complaining about beforehand is worth having.
  - A sample every five seconds: memory, handles, and how many web engine
    processes are alive, so a leak or a restarting renderer would show as
    a trend rather than a guess.
  - Afterwards, the exit code, Windows' own crash record for the run, and
    where the crash dump landed.

Nothing here changes how MavGCS behaves. It only writes things down.
"""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOGS = ROOT / "logs"
SAMPLE_EVERY_S = 5.0


def sample_line(pid):
    """Memory, handles and web engine processes, in one line."""
    ps = (
        "$p = Get-Process -Id %d -ErrorAction SilentlyContinue;"
        "if (-not $p) { 'gone'; exit };"
        "$we = Get-Process -Name QtWebEngineProcess -ErrorAction SilentlyContinue;"
        "$weMB = if ($we) { [Math]::Round((($we | Measure-Object WorkingSet64 -Sum).Sum)/1MB,1) } else { 0 };"
        "$weN = if ($we) { @($we).Count } else { 0 };"
        # Invariant culture on purpose: on a Turkish locale PowerShell
        # writes 13.8 as "13,8", which turned a four-field line into five
        # and made the numbers unreadable.
        "$inv = [Globalization.CultureInfo]::InvariantCulture;"
        "$mem = ([Math]::Round($p.WorkingSet64/1MB,1)).ToString($inv);"
        "$we2 = ([double]$weMB).ToString($inv);"
        "'{0},{1},{2},{3}' -f $mem, $p.HandleCount, $we2, $weN"
    ) % pid
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip()
    except Exception as exc:
        return "sample failed: %r" % exc


def sampler(pid, log, stop):
    started = time.time()
    while not stop.is_set():
        got = sample_line(pid)
        if got == "gone":
            return
        log.write("[watch %7.1fs] mem_MB,handles,webengine_MB,webengine_procs = %s\n"
                  % (time.time() - started, got))
        log.flush()
        stop.wait(SAMPLE_EVERY_S)


def windows_crash_record(since, log):
    """Whatever Windows recorded about a crash after `since`."""
    ps = (
        "$s = Get-Date '%s';"
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=$s} "
        "-ErrorAction SilentlyContinue | ForEach-Object { $_.TimeCreated; $_.Message }"
    ) % since.strftime("%Y-%m-%d %H:%M:%S")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        text = out.stdout.strip()
    except Exception as exc:
        text = "could not read the event log: %r" % exc
    log.write("\n=== what Windows recorded ===\n")
    log.write(text + "\n" if text else "  nothing - it was not an unhandled crash\n")

    dumps = Path(os.environ.get("LOCALAPPDATA", "")) / "CrashDumps"
    if dumps.is_dir():
        recent = [p for p in dumps.glob("*.dmp")
                  if p.stat().st_mtime >= since.timestamp()]
        if recent:
            log.write("\n=== crash dumps from this run ===\n")
            for p in sorted(recent, key=lambda x: x.stat().st_mtime):
                log.write("  %s  (%.0f MB)\n" % (p, p.stat().st_size / 1e6))


def main():
    LOGS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS / ("mavgcs_%s.log" % stamp)

    env = dict(os.environ)
    # Dump every thread's stack if the process dies on a fault. This is
    # the one that matters: the fault begins in Qt, but the stack says
    # what we were asking it to do.
    env["PYTHONFAULTHANDLER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Chromium's own log, turned up, on stderr where it can be captured.
    existing = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        existing + " --enable-logging=stderr --log-level=0").strip()
    env["QT_LOGGING_RULES"] = "qt.webenginecontext.info=true"

    started = datetime.now()
    with open(path, "w", encoding="utf-8", errors="replace") as log:
        log.write("MavGCS watched run\n")
        log.write("started   %s\n" % started.strftime("%Y-%m-%d %H:%M:%S"))
        log.write("connect from the Connection panel once it is up\n")
        log.write("flags     %s\n" % env["QTWEBENGINE_CHROMIUM_FLAGS"])
        log.write("=" * 70 + "\n")
        log.flush()

        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)

        print("Watching MavGCS. Log: %s" % path)
        print("Fly as normal. When it dies, tell Claude that path.")

        stop = threading.Event()
        threading.Thread(target=sampler, args=(proc.pid, log, stop),
                         daemon=True).start()

        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
        except KeyboardInterrupt:
            pass
        code = proc.wait()
        stop.set()

        log.write("\n" + "=" * 70 + "\n")
        log.write("exit code %d (0x%08X)\n" % (code, code & 0xFFFFFFFF))
        log.write("ran for  %s\n" % (datetime.now() - started))
        if code == 0:
            log.write("closed cleanly - no crash to explain\n")
        else:
            windows_crash_record(started, log)

    print("")
    print("Exit code %d. Log written to:" % code)
    print("  %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
