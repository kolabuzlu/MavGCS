"""Has the macOS layout moved from the approved one?

The counterpart to tests/test_windows_layout_frozen.py, for the rule
Derin stated about his 16-inch: the app as it runs on that screen shall
not change by one pixel. Until this file existed that rule was enforced
by a render run by hand when somebody remembered to ask for it, which is
a habit rather than a gate. This fails on its own when something moves.

It is deliberately the same idea as the Windows one - baseline commit at
the top, both sides rendered in this run, widget by widget - so that
whoever reads one can read the other. Where it differs from Windows it
differs because something here was measured and found to behave
differently, and each of those places says so.

How it works, and why there is no golden file
---------------------------------------------
The approved version is checked out into a worktree and rendered by the
same interpreter on the same machine as the working tree, and the two
are compared. Widget sizes come from font metrics, so a recorded
baseline would give a different answer on a different Qt, a different
macOS or a different DPI, and nobody could then tell a real shift from a
difference in the runner. Rendering both sides removes the question.

The cost is a worktree and two subprocess renders. The subprocesses are
not laziness: Qt keeps process-wide state, and two MainWindows from two
checkouts cannot both be built in one interpreter.

What this does NOT do, and why
------------------------------
It does not compare the whole window's pixels, where the Windows check
does. The right-hand side of this window is a live map: tiles arrive
over the network asynchronously and the cache readout counts them, so
two runs minutes apart legitimately differ - 555 tiles against 524 was
measured on this machine within the hour. A whole-window pixel
comparison would go red constantly for reasons that are not regressions,
and a gate that cries wolf is worse than no gate. The pixel comparison
here is of the LEFT COLUMN, which is Qt all the way down and settles to
the same bytes every run.

This needs history, so a shallow clone has to fetch it:
fetch-depth: 0 in the workflow, the same as the Windows check.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The last macOS appearance somebody approved. A tag at release time, or
# any commit between releases.
#
# Moving it is the deliberate act that signs a change off. An accidental
# change fails against it, which is the point; a change that was asked
# for updates it in the commit AFTER the one that lands the change,
# since a SHA does not exist until it is written. Make both, push both,
# and the gate is green at the tip rather than staying red until the
# next release.
APPROVED_BASELINE = "ba35108e5a"   # the merge that landed this gate

# The client area the window actually gets when maximised on the 16-inch
# this rule is about: a 1792x1120 point screen, less the menu bar and
# the Dock, less the 32 points of title bar that macOS constrains the
# FRAME to rather than the client area. Measured, not assumed - and the
# frame-versus-client distinction is worth stating because getting it
# wrong is what made an earlier hand measurement of this window 32
# points too generous.
WIDTH, HEIGHT = 1792, 968

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % detail) if detail else ""))
    if not ok:
        fails.append(name)


if sys.platform != "darwin":
    # Loudly, and twice. A skip reads exactly like a pass in a CI log,
    # and a check nobody notices was skipped is a check nobody has.
    print("  SKIPPED - NOT RUN - this is a macOS rule and the platform "
          "is %r" % (sys.platform,))
    print("")
    print("  SKIPPED: nothing above was measured. The macOS layout is "
          "unverified by this run.")
    sys.exit(0)


DUMPER = r'''
import hashlib, json, os, re, sys
ROOT = os.environ["TREE"]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main as app_main
from PySide6.QtWidgets import QApplication, QScrollArea
from PySide6.QtGui import QImage
app = QApplication.instance() or QApplication([])
win = app_main.MainWindow("udp:127.0.0.1:14999")
win.resize(int(os.environ["W"]), int(os.environ["H"]))
win.show()
for _ in range(15):
    app.processEvents()


def steady_text(w):
    """What this widget says, with the parts that are not about the app
    normalised away.

    The tile proxy binds an ephemeral port, so FpvView's address differs
    between any two runs of the same code - measured at 64902 against
    64910 for two renders of identical checkouts. That is the one field
    known to move for a reason that is not a change. It is normalised
    rather than the widget being ignored, so that a DIFFERENT change to
    the same label is still caught.

    A web view has no text worth comparing at all, and reading it makes
    this flaky: whether its URL has been set by the time the dump runs
    depends on how far the page has got, not on the code. Measured -
    two runs of identical trees reported FpvView relabelled from "" to
    an address, which is a false failure of exactly the kind a gate may
    not produce.
    """
    cls = type(w)
    if "WebEngine" in cls.__name__ or any(
            "WebEngine" in b.__name__ for b in cls.__mro__):
        return ""
    for getter in ("text", "title", "currentText"):
        if not hasattr(w, getter):
            continue
        try:
            v = getattr(w, getter)()
        except Exception:
            continue
        if isinstance(v, str) and v:
            v = re.sub(r"(127\.0\.0\.1|localhost):\d+", r"\1:<port>", v)
            return " ".join(v.split())[:200]
    return ""


def own_paint(w):
    """A hash of what this widget draws, for the widgets that draw
    themselves.

    Web views are excluded, and on this platform NOT for the reason the
    Windows check gives. Measured here: grabbing a QWebEngineView does
    not crash anything - one was held for 35 seconds and produced 38373
    distinct colours of real tile imagery. They are excluded because
    what they draw is not reproducible: the map's tiles arrive over the
    network and its own cache readout counts them, so hashing those
    pixels would fail runs that changed nothing.

    Whether a per-widget hash adds anything over a window grab is not
    settled, and this does not pretend otherwise. Live in one process,
    painting the artificial horizon magenta moves the window hash, the
    column hash and the widget hash together - measured here, and
    reproduced on Windows, so there is no platform difference. Across
    two fresh subprocess renders, which is what this suite actually
    does, the Windows check measured the opposite: a change to that
    widget moved 3660 of its own pixels and 0 of the window's. Nobody
    has explained that yet.

    So this stays, on the only ground a gate needs: it is deterministic,
    and it is the narrower question. A hash of one widget cannot be
    quietly satisfied by a window grab that answered from somewhere
    else.
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


out = {}
unstable = []


def walk(w, path):
    g = w.geometry()
    try:
        ss = w.styleSheet().strip()
    except Exception:
        ss = ""
    # A stylesheet can name a file, and resource_path() answers an
    # absolute one - which differs between two checkouts for a reason
    # that has nothing to do with style. Drop the directory, keep the
    # filename, so pointing at a DIFFERENT image is still a change.
    ss = re.sub(r"url\([^)]*?([^/\)]+)\)", r"url(\1)", ss)
    ss = " ".join(ss.split())
    txt = steady_text(w)
    out[path] = [[g.x(), g.y(), g.width(), g.height()], ss, own_paint(w),
                 txt]
    # The GPU string is filled in after the first paint from whatever
    # the driver reports, so on screen whether it is in a grab depends
    # on timing rather than on the layout - measured at 6884 differing
    # pixels between two renders of the SAME checkout, every one inside
    # that one label.
    #
    # Under the offscreen platform this suite uses it is never
    # populated at all: probed, and the label carries no text. So this
    # has never fired, and the run prints how many regions it masked so
    # that stays visible rather than being assumed. It is kept because
    # it costs one rectangle and it is what makes the pixel comparison
    # safe for anyone who runs this against a real screen.
    if txt.startswith("Graphics:"):
        try:
            area = w.window().findChildren(QScrollArea)[0].viewport()
            tl = w.mapTo(area, w.rect().topLeft())
            unstable.append([tl.x(), tl.y(), w.width(), w.height()])
        except Exception:
            pass
    seen = {}
    for c in w.children():
        if not (hasattr(c, "isWidgetType") and c.isWidgetType()):
            continue
        cls = type(c).__name__
        seen[cls] = seen.get(cls, -1) + 1
        walk(c, "%s/%s[%d]" % (path, cls, seen[cls]))


walk(win, type(win).__name__)

# The left column only. See the module docstring: the right-hand side is
# a live map and its pixels are not reproducible.
#
# The viewport's logical size goes out with it. A QImage read back from
# a PNG reports a device pixel ratio of 1 whatever it was grabbed at, so
# the only honest way to turn a widget rectangle into a pixel rectangle
# later is to divide the image's width by the width it was grabbed from.
viewport = [0, 0]
areas = win.findChildren(QScrollArea)
if areas:
    vp = areas[0].viewport()
    viewport = [vp.width(), vp.height()]
    areas[0].grab().save(os.environ["PNG"])

# The title bar is the one thing a release always changes and nothing
# above could see: a QMainWindow has windowTitle() rather than any of the
# text getters steady_text tries, so its own record carries no text at
# all. Measured on this gate first - it reported "0 relabelled" across a
# title that went from V2.3.1 to V2.3.2, because it had no way to look.
#
# The version is normalised out, so a release is not a failure every
# time and anything ELSE in the title still is. The pattern is the
# Windows gate's, character for character, so the two agree about what a
# title change is.
title = re.sub(r"V\d+\.\d+\.\d+", "V<version>", win.windowTitle())
sys.stdout.write(json.dumps({"widgets": out, "unstable": unstable,
                             "viewport": viewport, "title": title},
                            sort_keys=True))
sys.stdout.flush()
# Leave before Python tears the process down. A MainWindow that was
# never closed takes its background threads with it, and destroying a
# running QThread is fatal - seen here as an abort inside QThread's
# destructor, after the answer has already been printed. Closing it
# properly would run the real shutdown path, which writes settings and
# is not this script's business.
os._exit(0)
'''


def render(tree, dumper, png):
    """Every widget's rectangle, style, paint and text in that checkout."""
    env = dict(os.environ)
    env.update(TREE=tree, W=str(WIDTH), H=str(HEIGHT), PNG=png,
               MAVLINK20="1", QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, dumper], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300)
    # The answer, not the exit code. Tearing a MainWindow down takes a
    # QtWebEngine process with it and that does not always exit tidily;
    # it happens after the geometry has been written, and failing over
    # it would make this flaky, which is the one thing a gate may not be.
    #
    # Truncated output is a different matter: json.loads refuses it, and
    # then the exit code is worth having.
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except ValueError:
        raise RuntimeError(
            "render produced no usable geometry in %s\n"
            "  exit code: %d\n  stdout: %d bytes\n  stderr: %s"
            % (tree, r.returncode, len(r.stdout),
               r.stderr.decode("utf-8", "replace")[-1500:] or "(empty)"))


work = tempfile.mkdtemp(prefix="mavgcs-macos-frozen-")
approved = os.path.join(work, "approved")
dumper = os.path.join(work, "dump.py")
with open(dumper, "w", encoding="utf-8") as fh:
    fh.write(DUMPER)

try:
    # A worktree whose directory went away stays registered, and the
    # next add then fails for a reason that has nothing to do with the
    # layout. Cheap, and a run that was killed halfway then does not
    # poison the next one - which has happened here, nine times over.
    subprocess.run(["git", "worktree", "prune"], cwd=ROOT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", approved, APPROVED_BASELINE],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if add.returncode != 0:
        note("the approved version is available to compare against", False,
             add.stdout.decode("utf-8", "replace").strip()[-300:])
        print("")
        print("FAILED: %s" % ", ".join(fails))
        sys.exit(1)

    # Both sides must read the same settings, or this compares two
    # configurations rather than two code versions.
    #
    # app_paths.data_dir() answers the checkout directory when running
    # from source, so each tree has its own settings.json - and a fresh
    # worktree has none while a working tree usually does. Settings
    # reach the layout: MainWindow builds FpvView with the cesium token
    # from load_settings(). Measured on this machine at V2.2.3 the token
    # made no difference to the column's height, but that was one
    # version and one measurement, and a check that can report movement
    # nobody made is worth nothing on the day it does.
    settings = os.path.join(ROOT, "settings.json")
    approved_settings = os.path.join(approved, "settings.json")
    if os.path.exists(settings):
        shutil.copy2(settings, approved_settings)
    elif os.path.exists(approved_settings):
        os.remove(approved_settings)

    print("comparing against %s at %dx%d%s"
          % (APPROVED_BASELINE, WIDTH, HEIGHT,
             "" if os.path.exists(settings) else " (no saved settings)"))
    shot_before = os.path.join(work, "approved.png")
    shot_after = os.path.join(work, "working.png")
    dump_before = render(approved, dumper, shot_before)
    dump_after = render(ROOT, dumper, shot_after)
    before = dump_before["widgets"]
    after = dump_after["widgets"]

    gone = sorted(set(before) - set(after))
    new = sorted(set(after) - set(before))
    shared = sorted(set(before) & set(after))
    moved = [(k, before[k][0], after[k][0])
             for k in shared if before[k][0] != after[k][0]]
    restyled = [(k, before[k][1], after[k][1])
                for k in shared if before[k][1] != after[k][1]]
    repainted = [(k, before[k][2], after[k][2])
                 for k in shared
                 if len(before[k]) > 2 and len(after[k]) > 2
                 and before[k][2] and after[k][2]
                 and before[k][2] != after[k][2]]
    relabelled = [(k, before[k][3], after[k][3])
                  for k in shared
                  if len(before[k]) > 3 and len(after[k]) > 3
                  and before[k][3] != after[k][3]]

    note("no widget has disappeared", not gone,
         "%d gone, first: %s" % (len(gone), gone[0] if gone else ""))
    note("no widget has appeared", not new,
         "%d new, first: %s" % (len(new), new[0] if new else ""))
    note("not one widget has moved or resized", not moved,
         "%d of %d moved" % (len(moved), len(shared)))
    # Geometry alone cannot see a colour, a border or a font. Such a
    # change alters what the program looks like without moving anything,
    # and a check that compares only rectangles calls that "nothing
    # moved" - which is true, and useless.
    note("not one widget has been restyled", not restyled,
         "%d of %d restyled" % (len(restyled), len(shared)))
    drawn = sum(1 for k in shared
                if len(before[k]) > 2 and before[k][2]
                and not before[k][2].startswith("ungrabbable"))
    note("not one widget has repainted itself", not repainted,
         "%d of %d self-drawn widgets repainted"
         % (len(repainted), drawn))
    # And what the widgets SAY. A label whose wording changes is a
    # change to the app that moves nothing and repaints nothing this
    # check would otherwise notice.
    note("not one widget has changed its text", not relabelled,
         "%d of %d relabelled" % (len(relabelled), len(shared)))

    # Read at all, before compared. Two empty titles compare equal, and a
    # check that reads nothing and reports a pass is precisely the fault
    # this line was added to close - the title was invisible for a whole
    # release and every count said "0". So an empty title on either side
    # is a failure in its own right, not a match.
    title_before = dump_before.get("title") or ""
    title_after = dump_after.get("title") or ""
    note("the window title was read on both sides",
         bool(title_before) and bool(title_after),
         "%r and %r" % (title_before, title_after))
    note("the window title is unchanged apart from its version",
         title_before == title_after,
         "%r and %r" % (title_before, title_after))

    def show(rows, label):
        for key, was, now in rows[:15]:
            short = key.replace("MainWindow/", "")
            if len(short) > 56:
                short = "..." + short[-53:]
            print("       %-56s %s" % (short, label))
            print("           approved: %s" % (was,))
            print("           working : %s" % (now,))
        if len(rows) > 15:
            print("       ... and %d more" % (len(rows) - 15))

    show(moved, "moved")
    show(restyled, "restyled")
    show(repainted, "repainted")
    show(relabelled, "relabelled")

    # And the left column, painted. The records above are what the
    # widgets say about themselves; this is what the eye would see, and
    # it catches what none of them name - a stylesheet inherited rather
    # than set, a palette, a font that resolved differently.
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import QRect
    img_before = QImage(shot_before)
    img_after = QImage(shot_after)
    if img_before.isNull() or img_after.isNull():
        note("both columns were painted", False, "a grab did not load")
    elif img_before.size() != img_after.size():
        note("the left column is the same size", False,
             "%dx%d vs %dx%d" % (img_before.width(), img_before.height(),
                                 img_after.width(), img_after.height()))
    else:
        img_before = img_before.convertToFormat(QImage.Format_RGB32)
        img_after = img_after.convertToFormat(QImage.Format_RGB32)
        # Mask the GPU string's rectangle on both sides. It is filled in
        # after the first paint from whatever the driver reports, so it
        # is in a grab or not depending on timing - measured as 6884
        # differing pixels between two renders of the SAME checkout,
        # every one of them inside that one label.
        # Widget rectangles are in logical pixels; the grab may be at 2x.
        # Derive the factor from the image against the viewport it was
        # grabbed from rather than trusting devicePixelRatio, which a
        # QImage read back from a PNG reports as 1 however it was made.
        vp_w = (dump_before.get("viewport") or [0])[0]
        scale = (img_before.width() / float(vp_w)) if vp_w else 1.0
        masked = 0
        for rect in (dump_before.get("unstable") or []) + \
                    (dump_after.get("unstable") or []):
            x, y, w, h = rect
            box = QRect(int(x * scale), int(y * scale),
                        int(w * scale) + 1, int(h * scale) + 1)
            for img in (img_before, img_after):
                p = QPainter(img)
                p.fillRect(box, QColor("black"))
                p.end()
            masked += 1
        differing = 0
        for y in range(img_before.height()):
            row_b = bytes(img_before.constScanLine(y))
            row_a = bytes(img_after.constScanLine(y))
            if row_b != row_a:
                differing += sum(1 for x in range(0, len(row_b), 4)
                                 if row_b[x:x + 4] != row_a[x:x + 4])
        note("not one pixel of the left column has changed", differing == 0,
             "%d of %d pixels, %d masked region(s)"
             % (differing, img_before.width() * img_before.height(), masked))
finally:
    subprocess.run(["git", "worktree", "remove", "--force", approved],
                   cwd=ROOT, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    shutil.rmtree(work, ignore_errors=True)

print("")
if fails:
    print("FAILED: %s" % ", ".join(fails))
    print("")
    print("The macOS layout is frozen at %s. If the change above was"
          % APPROVED_BASELINE)
    print("asked for, move APPROVED_BASELINE to the commit that lands it,")
    print("in the commit after it. If it was not, it is a defect.")
sys.exit(1 if fails else 0)
