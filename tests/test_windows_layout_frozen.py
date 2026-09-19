"""Has the Windows layout moved from the released version?

Windows does not change by accident. Not one pixel of it may shift from
the last approved one - APPROVED_BASELINE below - unless the change is
one somebody asked for, in which case that moves in the same commit
that lands it. "No regressions" is not the bar: the released layout is
what gets flown, and an unasked-for change to it is a defect even when
it looks like an improvement.

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

This needs history, so a shallow clone has to fetch it:
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

# The last Windows appearance somebody approved. Anything - a tag at
# release time, a commit between releases.
#
# Moving it is the deliberate act. An accidental change fails against
# it, which is the point; a change that was asked for updates it in the
# commit that lands the change - which has to be the commit after it,
# since a SHA does not exist until it is written. Make both, push both,
# and the gate is green at the tip instead of staying red until the next
# release. A tag alone could not do that: between releases there is
# no tag to move to, and a check that cannot go green is a check people
# start ignoring.
APPROVED_BASELINE = "f0ce86c29f"   # vertical speed indicator on the HUD

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
import hashlib, json, os, re, sys
ROOT = os.environ["TREE"]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main as app_main
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
app = QApplication.instance() or QApplication([])
win = app_main.MainWindow("udp:127.0.0.1:14999")
win.resize(int(os.environ["W"]), int(os.environ["H"]))
win.show()
for _ in range(12):
    app.processEvents()
def own_paint(w):
    """A hash of what this widget draws, for widgets that draw themselves.

    win.grab() does NOT reach the artificial horizon. Measured: painting
    the HUD's every pixel differently changes 0 of the window grab's
    1253376, while grabbing that same widget directly differs in 3660.
    The window holds two QWebEngineViews - the map and the FPV view -
    and an ancestor of a native window does not re-render its children
    into a grab; it hands back what the compositor last put there.

    So the HUD, the one instrument on this screen, sat outside the
    guarantee from the day this check was written until the day a
    vertical speed indicator was added to it and the gate stayed green.

    Only widgets whose class this project defines, and never a web view:
    grabbing one of those is what crashes the compositor.
    """
    cls = type(w)
    if cls.__module__.startswith("PySide6"):
        return ""
    if "WebEngine" in cls.__name__ or any(
            "WebEngine" in b.__name__ for b in cls.__mro__):
        return ""
    if w.width() <= 0 or w.height() <= 0:
        return ""
    try:
        img = w.grab().toImage().convertToFormat(QImage.Format_RGB32)
        h = hashlib.sha256()
        for y in range(img.height()):
            h.update(bytes(img.constScanLine(y)))
        return h.hexdigest()[:16]
    except Exception as exc:
        return "ungrabbable: %r" % (exc,)


def walk(w, path, out):
    g = w.geometry()
    try:
        ss = w.styleSheet().strip()
    except Exception:
        ss = ""
    # A stylesheet can name a file, and resource_path() answers an
    # absolute one - which differs between two checkouts for a reason
    # that has nothing to do with style. The directory is dropped and
    # the filename kept, so pointing at a DIFFERENT image is still a
    # change while sitting in a different folder is not.
    ss = re.sub(r"url\([^)]*?([^/\)]+)\)", r"url(\1)", ss)
    # Whitespace is not style. A constant that interpolates to "" on
    # this platform leaves a blank line where the release had nothing,
    # and Qt's parser does not care - so neither does this. Content
    # changes are still caught, and anything whitespace could hide
    # would show in the pixel comparison anyway.
    ss = " ".join(ss.split())
    out[path] = [[g.x(), g.y(), g.width(), g.height()], ss, own_paint(w)]
    seen = {}
    for c in w.children():
        if not (hasattr(c, "isWidgetType") and c.isWidgetType()):
            continue
        cls = type(c).__name__
        seen[cls] = seen.get(cls, -1) + 1
        walk(c, "%s/%s[%d]" % (path, cls, seen[cls]), out)
out = {}
walk(win, type(win).__name__, out)
win.grab().save(os.environ["PNG"])
sys.stdout.write(json.dumps(out, sort_keys=True))
sys.stdout.flush()
# Leave before Python tears the process down. A MainWindow that was
# never closed takes its background threads with it, and destroying a
# running QThread is a qFatal - 0xC0000409, after the answer has already
# been printed. Closing it properly would run the real shutdown path,
# which writes settings and is not this script's business.
os._exit(0)
'''


def geometry_of(tree, dumper, png):
    """Every widget's rectangle in that checkout, keyed by tree path."""
    env = dict(os.environ)
    env.update(TREE=tree, W=str(WIDTH), H=str(HEIGHT), PNG=png,
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
        ["git", "worktree", "add", "--detach", released, APPROVED_BASELINE],
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
          % (APPROVED_BASELINE, WIDTH, HEIGHT,
             "" if os.path.exists(settings) else " (no saved settings)"))
    shot_before = os.path.join(work, "released.png")
    shot_after = os.path.join(work, "working.png")
    before = geometry_of(released, dumper, shot_before)
    after = geometry_of(ROOT, dumper, shot_after)

    gone = sorted(set(before) - set(after))
    new = sorted(set(after) - set(before))
    shared = sorted(set(before) & set(after))
    moved = [(k, before[k][0], after[k][0])
             for k in shared if before[k][0] != after[k][0]]
    restyled = [(k, before[k][1], after[k][1])
                for k in shared if before[k][1] != after[k][1]]
    # Only for records that carry one: an older baseline predates the
    # third field, and comparing a hash against nothing would fail every
    # widget rather than none.
    repainted = [(k, before[k][2], after[k][2])
                 for k in shared
                 if len(before[k]) > 2 and len(after[k]) > 2
                 and before[k][2] and after[k][2]
                 and before[k][2] != after[k][2]]

    note("no widget has disappeared", not gone,
         "%d gone, first: %s" % (len(gone), gone[0] if gone else ""))
    note("no widget has appeared", not new,
         "%d new, first: %s" % (len(new), new[0] if new else ""))
    note("not one widget has moved or resized", not moved,
         "%d of %d moved" % (len(moved), len(shared)))
    # Geometry alone cannot see this. A colour, a border or a font
    # changes what the program looks like without moving anything, and
    # a check that only compares rectangles reports such a change as
    # "not one widget has moved" - which is true, and useless.
    note("not one widget has been restyled", not restyled,
         "%d of %d restyled" % (len(restyled), len(shared)))

    # And what each widget this project draws itself actually paints.
    # The window grab below cannot see those - see own_paint - so
    # without this the artificial horizon could be redrawn entirely and
    # every other check here would still pass. It did, and they did.
    drawn = sum(1 for k in shared
                if len(before[k]) > 2 and before[k][2]
                and not before[k][2].startswith("ungrabbable"))
    note("not one widget has repainted itself", not repainted,
         "%d of %d self-drawn widgets repainted"
         % (len(repainted), drawn))

    def show(rows, label):
        for key, was, now in rows[:15]:
            short = key.replace("MainWindow/", "")
            if len(short) > 56:
                short = "..." + short[-53:]
            print("       %-56s %s" % (short, label))
            print("           released: %s" % (was,))
            print("           working : %s" % (now,))
        if len(rows) > 15:
            print("       ... and %d more" % (len(rows) - 15))

    show(moved, "moved")
    show(restyled, "restyled")
    show(repainted, "repainted")

    # And the whole window, painted. The two records above are what the
    # widgets say about themselves; this is what the user would see. It
    # catches anything neither of them names - a stylesheet inherited
    # rather than set, a palette, a font that resolved differently.
    #
    # Both sides are painted by the same Qt on the same machine in the
    # same run, so there is no golden image to drift and no tolerance to
    # tune: the answer is zero or it is a change.
    from PySide6.QtGui import QImage
    img_before = QImage(shot_before)
    img_after = QImage(shot_after)
    if img_before.isNull() or img_after.isNull():
        note("both windows were painted", False, "a grab did not load")
    elif img_before.size() != img_after.size():
        note("the window is the same size", False,
             "%dx%d vs %dx%d" % (img_before.width(), img_before.height(),
                                 img_after.width(), img_after.height()))
    else:
        img_before = img_before.convertToFormat(QImage.Format_RGB32)
        img_after = img_after.convertToFormat(QImage.Format_RGB32)
        differing = 0
        for y in range(img_before.height()):
            row_b = img_before.constScanLine(y)
            row_a = img_after.constScanLine(y)
            if bytes(row_b) != bytes(row_a):
                wb, wa = memoryview(bytes(row_b)), memoryview(bytes(row_a))
                differing += sum(1 for x in range(0, len(wb), 4)
                                 if wb[x:x + 4] != wa[x:x + 4])
        note("not one pixel of the window has changed", differing == 0,
             "%d of %d pixels" % (differing,
                                  img_before.width() * img_before.height()))
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
          % APPROVED_BASELINE)
    print("platform has to be scoped to that platform, the way the")
    print("stylesheet in main.py already is.")
sys.exit(1 if fails else 0)
