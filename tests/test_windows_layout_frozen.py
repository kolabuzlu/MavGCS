"""Has the Windows layout moved from the released version?

The rule this enforces is absolute: not one pixel of the Windows UI may
shift from V2.1.7. Not "no regressions" and not "nothing worse" - the
released layout is what gets flown, and a change to it is a defect even
when it looks like an improvement.

It exists because a macOS port retuned the shared layout constants so
the panels would sit correctly on a Mac, which lays them out about 160px
shorter than Windows does. The constants were written as plain numbers
rather than per-platform ones, so Windows got the macOS spacing too:
59 of 159 widgets moved, every button grew from 18px to 33px tall, and
the left column went from 662px to 976px against a 796px viewport -
putting TelemetryPanel, and with it airspeed, altitude, satellite count
and battery, entirely below the fold on a 1536x816 screen.

How it works, and why there is no golden file
---------------------------------------------
The released version is checked out into a worktree and rendered by the
same code that renders the current one, in this same interpreter on this
same machine, and the two geometries are compared. Widget sizes come
from font metrics, so a recorded baseline would mean a different answer
on a machine with different fonts, a different Qt, or a different DPI -
and the first time it disagreed, nobody would be able to tell a real
shift from a difference in the runner. Rendering both sides removes the
question: whatever the fonts are, they are the same fonts for both.

The cost is a worktree and two subprocess renders, about three seconds.
The subprocesses are not laziness - Qt keeps process-wide state, and two
MainWindows from two different checkouts cannot both be built in one
interpreter.

This needs the release tag, so a shallow clone has to fetch it:
fetch-depth: 0 in the workflow, which is why it is set there.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The released version this platform is frozen against. Moving this is a
# deliberate act: it means a new release has been cut and its layout is
# the one to hold from now on.
RELEASE_TAG = "V2.1.7"

# The size the window actually gets maximised on the machine this is
# flown from: a 1920x1080 panel at 125% scaling, less the taskbar.
WIDTH, HEIGHT = 1536, 816

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


if sys.platform != "win32":
    # The rule is about Windows. Saying so beats passing quietly on a
    # platform where nothing was measured.
    print("  skipped: the frozen layout is a Windows rule, and this is %r"
          % (sys.platform,))
    sys.exit(0)


DUMPER = r'''
import json, os, sys
ROOT = os.environ["TREE"]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main as app_main
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
win = app_main.MainWindow("udp:127.0.0.1:14999")
win.resize(int(os.environ["W"]), int(os.environ["H"]))
win.show()
for _ in range(12):
    app.processEvents()
def walk(w, path, out):
    g = w.geometry()
    out[path] = [g.x(), g.y(), g.width(), g.height()]
    seen = {}
    for c in w.children():
        if not (hasattr(c, "isWidgetType") and c.isWidgetType()):
            continue
        cls = type(c).__name__
        seen[cls] = seen.get(cls, -1) + 1
        walk(c, "%s/%s[%d]" % (path, cls, seen[cls]), out)
out = {}
walk(win, type(win).__name__, out)
sys.stdout.write(json.dumps(out, sort_keys=True))
sys.stdout.flush()
# Leave before Python tears the process down. A MainWindow that was
# never closed takes its background threads with it, and destroying a
# running QThread is a qFatal - 0xC0000409, after the answer has already
# been printed. Closing it properly would run the real shutdown path,
# which writes settings and is not this script's business.
os._exit(0)
'''


def geometry_of(tree, dumper):
    """Every widget's rectangle in that checkout, keyed by tree path."""
    env = dict(os.environ)
    env.update(TREE=tree, W=str(WIDTH), H=str(HEIGHT),
               MAVLINK20="1", QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, dumper], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300)
    # The answer, not the exit code, is what this needs - and the two do
    # not always agree. Building a MainWindow puts a QtWebEngine map on
    # the screen, and tearing that down crashes on roughly one run in
    # three with 0xC0000005, an access violation inside the compositor.
    # It is a known fault of the app on Windows, it happens after the
    # geometry has been written to stdout, and it is not this suite's to
    # fix or to fail over - failing over it would make the check flaky,
    # which is the one thing a gate may not be.
    #
    # Truncated output is a different matter: json.loads refuses it, and
    # then the exit code is worth having.
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except ValueError:
        raise RuntimeError(
            "render produced no usable geometry in %s\n"
            "  exit code: %d (0x%08X)\n  stdout: %d bytes\n  stderr: %s"
            % (tree, r.returncode, r.returncode & 0xFFFFFFFF,
               len(r.stdout),
               r.stderr.decode("utf-8", "replace")[-1500:] or "(empty)"))


work = tempfile.mkdtemp(prefix="mavgcs-frozen-")
released = os.path.join(work, "released")
dumper = os.path.join(work, "dump.py")
with open(dumper, "w", encoding="utf-8") as fh:
    fh.write(DUMPER)

try:
    # A worktree whose directory went away stays registered, and the next
    # add then fails for a reason that has nothing to do with the layout.
    # Cheap, and it makes a run that was killed halfway not poison the
    # next one.
    subprocess.run(["git", "worktree", "prune"], cwd=ROOT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", released, RELEASE_TAG],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if add.returncode != 0:
        # A shallow clone without the tag cannot answer the question, and
        # pretending otherwise would be the worst outcome here.
        note("the released version is available to compare against", False,
             add.stdout.decode("utf-8", "replace").strip()[-300:])
        print("")
        print("FAILED: %s" % ", ".join(fails))
        sys.exit(1)

    # Both sides must read the same settings, or the comparison is not
    # of two code versions but of two configurations.
    #
    # app_paths.data_dir() answers the checkout directory when running
    # from source, so each of these two trees has its own settings.json
    # - and the release worktree, freshly checked out, has none at all
    # while a working tree usually does. Settings reach the layout:
    # MainWindow builds FpvView with load_settings()["cesium_ion_token"],
    # and on macOS the presence of a token has been seen to change panel
    # heights, measured as Preflight coming out 25px instead of 57px.
    # Nothing here has ever moved on Windows over it, but a check that
    # can report movement nobody made is worth exactly nothing on the day
    # it does.
    settings = os.path.join(ROOT, "settings.json")
    released_settings = os.path.join(released, "settings.json")
    if os.path.exists(settings):
        shutil.copy2(settings, released_settings)
    elif os.path.exists(released_settings):
        os.remove(released_settings)

    print("comparing against %s at %dx%d%s"
          % (RELEASE_TAG, WIDTH, HEIGHT,
             "" if os.path.exists(settings) else " (no saved settings)"))
    before = geometry_of(released, dumper)
    after = geometry_of(ROOT, dumper)

    gone = sorted(set(before) - set(after))
    new = sorted(set(after) - set(before))
    shared = sorted(set(before) & set(after))
    moved = [(k, before[k], after[k]) for k in shared if before[k] != after[k]]

    note("no widget has disappeared", not gone,
         "%d gone, first: %s" % (len(gone), gone[0] if gone else ""))
    note("no widget has appeared", not new,
         "%d new, first: %s" % (len(new), new[0] if new else ""))
    note("not one widget has moved or resized", not moved,
         "%d of %d moved" % (len(moved), len(shared)))

    for key, was, now in moved[:25]:
        short = key.replace("MainWindow/", "")
        if len(short) > 58:
            short = "..." + short[-55:]
        print("       %-58s %s -> %s" % (short, was, now))
    if len(moved) > 25:
        print("       ... and %d more" % (len(moved) - 25))
finally:
    subprocess.run(["git", "worktree", "remove", "--force", released],
                   cwd=ROOT, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    shutil.rmtree(work, ignore_errors=True)

print("")
if fails:
    print("FAILED: %s" % ", ".join(fails))
    print("")
    print("The Windows layout is frozen at %s. Anything tuned for another"
          % RELEASE_TAG)
    print("platform has to be scoped to that platform, the way the")
    print("stylesheet in main.py already is.")
sys.exit(1 if fails else 0)
