"""Run every suite in this folder and say what broke.

    python tests/run_all.py

Each suite is a plain script that prints its own checks and exits
non-zero if any failed, so they can be run one at a time while working on
something - `python tests/test_mode_retry.py` - and all at once here.
They are run as separate processes deliberately: several of them import
main and monkeypatch it, and a failure in one should not be able to leave
another testing the wrong thing.

Exits non-zero if any suite fails, which is what makes it usable from a
build machine.
"""
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def main():
    suites = sorted(HERE.glob("test_*.py"))
    if not suites:
        print("No suites found in %s" % HERE)
        return 1

    env = dict(os.environ)
    env.setdefault("MAVLINK20", "1")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONIOENCODING"] = "utf-8"

    print("")
    print("Running %d suite(s) from %s" % (len(suites), HERE))
    print("")
    failed, results = [], []
    for suite in suites:
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(suite)], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        took = time.monotonic() - started
        ok = proc.returncode == 0
        results.append((suite.name, ok, took, proc.stdout))
        print("  %-4s %-28s %5.1fs" % ("ok" if ok else "FAIL",
                                       suite.name, took))
        if not ok:
            failed.append(suite.name)

    # Only the failures get their output shown, so a green run stays
    # short enough to read and a red one says everything at once.
    for name, ok, _took, output in results:
        if ok:
            continue
        print("")
        print("=" * 68)
        print("%s" % name)
        print("=" * 68)
        print(output.rstrip())

    print("")
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("All %d suite(s) passed." % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
