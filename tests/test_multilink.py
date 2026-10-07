"""Several links to one aircraft: the rules, checked without any sockets.

What each link is called, how they share one line, how each one's
health is measured, and - the part a flight depends on - when the
ground station moves from one link to another. The time is handed in,
so a minute of flying is checked in a moment, and every rule is tested
on the history that would break it.
"""

import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import multilink
from multilink import LinkChooser, LinkHealth, LinkView

# The marks on the line are not in every console's code page - Windows'
# own is cp1252 - and a test must never fail at printing its result.
try:
    sys.stdout.reconfigure(errors="backslashreplace")
except Exception:
    pass

fails = []


def note(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("  (%s)" % (detail,)) if detail else ""))
    if not ok:
        fails.append(name)


def plain(rich):
    """What the line shows, without its colours."""
    return re.sub(r"<[^>]+>", "", rich).replace("&nbsp;", " ") \
        .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


print("")
print("1. what each link is called")
for conn, want in (
        ("COM36:57600", "COM36"),
        ("/dev/tty.usbserial-A10K5:57600", "usbserial-A10K5"),
        ("/dev/cu.usbmodem14101:115200", "usbmodem14101"),
        ("tcp:127.0.0.1:5760", "TCP 5760"),
        ("tcp:10.8.0.1:5760", "TCP 10.8.0.1:5760"),
        ("udpin:0.0.0.0:14550", "UDP 14550"),
        ("udpout:192.168.4.1:14550", "UDP 192.168.4.1:14550")):
    got = multilink.link_label(conn)
    note("%-32s -> %s" % (conn, want), got == want, got)

print("")
print("2. the line never needs more room than it has")
links = [
    {"label": "COM36", "state": "active", "rx_mps": 45, "loss_pct": 0.4},
    {"label": "UDP 14550", "state": "standby", "rx_mps": 46, "loss_pct": 0.0},
    {"label": "TCP 5760", "state": "waiting"},
    {"label": "UDP 14551", "state": "lost"},
]
full = plain(multilink.link_line_html(links[:2]))
note("two links, room to spare: both carry their figures",
     "COM36 45/s 0.4%" in full and "UDP 14550 46/s 0.0%" in full, full)
too_long = []
for room in range(3, 120):
    shown = plain(multilink.link_line_html(links, room))
    if len(shown) > room:
        too_long.append((room, len(shown)))
note("every width from 3 to 119 characters is respected", not too_long,
     "first over: %r" % (too_long[:1],))
tight = plain(multilink.link_line_html(links[:2], 32))
note("short of room, the link in use keeps its figures",
     "COM36 45/s 0.4%" in tight and "46/s" not in tight, tight)
tiny = plain(multilink.link_line_html(links, 20))
note("very short of room, the rest become a count",
     "+3 more" in tiny and "COM36" in tiny, tiny)
manual_line = plain(multilink.link_line_html(links[:2], 80, manual=True))
note("chosen by hand, the line says so first",
     manual_line.startswith(multilink.MANUAL_PREFIX), manual_line)
over = [r for r in range(12, 120)
        if len(plain(multilink.link_line_html(links, r, manual=True))) > r]
note("and still never needs more room than it has", not over, over[:1])
tip_m = multilink.link_tooltip([dict(links[0], chosen=True), links[1]],
                               manual=True)
note("the tooltip names the link chosen by hand, and how to hand it back",
     "Chosen by hand: COM36" in tip_m and "back to automatic" in tip_m)
evil = plain(multilink.link_line_html(
    [{"label": "<b>x</b>", "state": "active"},
     {"label": "y", "state": "standby"}]))
note("a name is shown as text, never as markup", "<b>x</b>" in evil, evil)
tip = multilink.link_tooltip([
    {"label": "COM36", "state": "active", "rx_mps": 45, "loss_pct": 0.4,
     "lag_s": 0.0, "connection": "COM36:57600"},
    {"label": "TCP 5760", "state": "failed", "error": "connection refused",
     "connection": "tcp:127.0.0.1:5760"}])
note("the tooltip names every link, its state and its trouble",
     "COM36 - in use" in tip and "could not be opened" in tip
     and "connection refused" in tip and "tcp:127.0.0.1:5760" in tip)

print("")
print("3. counting lost frames")
from mavlink_link import MavlinkLink


class Frame:
    def __init__(self, sysid, compid, seq):
        self._s, self._c, self._q = sysid, compid, seq

    def get_srcSystem(self):
        return self._s

    def get_srcComponent(self):
        return self._c

    def get_seq(self):
        return self._q


def v233_rule(state, key, seq):
    """The counting rule exactly as V2.3.3 shipped it, written out."""
    if key == (ord("3"), ord("D")):
        return
    last = state["last"].get(key)
    if last is None:
        state["last"][key] = seq
        return
    gap = (seq - last - 1) % 256
    if gap > 128:
        if state["lost"] > 0:
            state["lost"] -= 1
        return
    state["last"][key] = seq
    state["lost"] += gap


# Normal traffic - loss, reordering, duplicates, the radio's own frames,
# twenty a second with no silences - must be counted exactly as V2.3.3
# counted it. The fix below is for what follows an outage, and nothing
# else may move.
rng = random.Random(7)
disagree = 0
for trial in range(200):
    old, new = {"last": {}, "lost": 0}, multilink.SequenceCounter()
    seqs = {(1, 1): rng.randrange(256), (1, 100): rng.randrange(256)}
    t = 0.0
    keys = list(seqs) + [MavlinkLink.SEQ_RADIO_TUPLE]
    for i in range(400):
        # Each sender in turn, so none goes a second unheard: that is an
        # outage, which is what the rule further down changes, and real
        # traffic does not fall silent and then send late frames.
        t += 0.05
        key = keys[i % len(keys)]
        if key == MavlinkLink.SEQ_RADIO_TUPLE:
            seq = rng.randrange(256)
        else:
            step = rng.choice([1, 1, 1, 1, 2, 3, 0, -1, 9])
            seqs[key] = (seqs[key] + step) % 256
            seq = seqs[key]
        v233_rule(old, key, seq)
        new.count(key, seq, t)
    if old["lost"] != new.lost:
        disagree += 1
note("200 random histories counted exactly as V2.3.3 counted them",
     disagree == 0, "%d disagree" % disagree)

link = MavlinkLink("udpin:0.0.0.0:14999")
ref = multilink.SequenceCounter()
for seq in (1, 2, 4, 3, 9, 10):
    link._count_sequence(Frame(1, 1, seq))
    ref.count((1, 1), seq)
note("the single link counts with the very same counter",
     link._seq.lost == ref.lost == 4, (link._seq.lost, ref.lost))

# After an outage the sequence has moved on by an unknowable amount - by
# more than half its range after 8 s at 20 frames a second - and every
# frame that followed looked late. Found in the rehearsal against real
# ArduPlane firmware, where a link's loss read -164%.
h = LinkHealth()
t, seq = 0.0, 0
for i in range(100):                      # 5 s, a third of frames missing
    t += 0.05
    seq = (seq + (2 if i % 3 == 0 else 1)) % 256
    h.on_frame(t, (1, 1), seq, True)
counted = h.seq.lost
h.tick(t)
t += 8.0
seq = (seq + 160) % 256                   # 8 s of silence
for i in range(60):                       # 3 s back, nothing missing
    t += 0.05
    seq = (seq + 1) % 256
    h.on_frame(t, (1, 1), seq, True)
h.tick(t)
note("an outage does not take back the losses counted before it",
     h.seq.lost == counted, "%d before, %d after" % (counted, h.seq.lost))
note("and loss is never shown below zero", h.loss_pct >= 0.0,
     "%.1f%%" % h.loss_pct)
late = multilink.SequenceCounter()
for seq, at in ((10, 0.0), (12, 0.05), (11, 0.06)):
    late.count((1, 1), seq, at)
note("a frame merely arriving late still takes back its loss",
     late.lost == 0, late.lost)

print("")
print("4. a link's health")
h = LinkHealth()
h.on_frame(100.0, (51, 68), 1, from_vehicle=False)
note("the ground radio talking is not the aircraft", not h.alive(100.0))
h.on_frame(100.0, (1, 1), 1, from_vehicle=True)
note("a frame from the aircraft: alive", h.alive(100.5))
note("two seconds of nothing: not", not h.alive(102.1))
h.on_frame(105.0, (1, 1), 2, from_vehicle=True)
note("heard again after a silence: it counts from now",
     h.alive_since == 105.0, h.alive_since)

h = LinkHealth()
t, seq = 0.0, 0
for i in range(100):                 # 5 s, 20 frames a second, 1 in 5 lost
    t = i * 0.05
    seq = (seq + (2 if i % 4 == 3 else 1)) % 256
    h.on_frame(t, (1, 1), seq, from_vehicle=True)
    if abs(t - round(t)) < 1e-9:
        h.tick(t)
h.tick(t)
note("loss measured over the last few seconds",
     18.0 <= h.loss_pct <= 22.0, "%.1f%%" % h.loss_pct)

fast, slow = LinkHealth(), LinkHealth()
for i in range(40):
    boot = 10_000 + i * 50
    fast.on_frame(1000.0 + i * 0.05 + 0.03, (1, 1), i, True, boot)
    slow.on_frame(1000.0 + i * 0.05 + 0.43, (1, 1), i, True, boot)
lag = multilink.lags(1002.5, {"rfd": fast, "lte": slow})
note("the slower link is 0.4 s behind, the faster one not at all",
     abs(lag["lte"] - 0.4) < 0.01 and abs(lag["rfd"]) < 1e-9, lag)
note("a link with nothing to judge by is not called slow",
     multilink.lags(1002.5, {"x": LinkHealth()})["x"] == 0.0)

print("")
print("4b. a link that keeps dropping out")
d = multilink.DropOuts()
first, second = d.dropped(100.0), d.dropped(110.0)
note("a drop, and a second: each one said as it happens",
     not first and not second and not d.unsteady)
note("the third within a minute: said once, as dropping out",
     d.dropped(120.0) and d.unsteady)
note("and the fourth not said again", not d.dropped(130.0) and d.unsteady)
note("not steady after a few seconds working", not d.steady(140.0, 131.0))
note("steady once it has worked 30 s without a break",
     d.steady(161.0, 131.0) and not d.unsteady)
note("and said so only once", not d.steady(170.0, 131.0))
note("after that, a drop is a single drop again",
     not d.dropped(175.0) and not d.unsteady)
d = multilink.DropOuts()
spread = [d.dropped(t) for t in (0.0, 35.0, 70.0, 105.0, 140.0)]
note("drops more than half a minute apart never add up to dropping out",
     not any(spread) and not d.unsteady, spread)

print("")
print("5. when to move")


def views(active_alive=True, other_alive=True, a_loss=0.0, b_loss=0.0,
          a_lag=0.0, b_lag=0.0, since=0.0):
    return [LinkView("a", active_alive, since, a_loss, a_lag, 0),
            LinkView("b", other_alive, since, b_loss, b_lag, 1)]


c = LinkChooser()
key, why = c.decide(100.0, views(active_alive=False), "a", busy=True)
note("the link in use goes quiet: move at once, even mid-transfer",
     (key, why) == ("b", "silent"), (key, why))

c = LinkChooser()
note("nothing alive anywhere: stay",
     c.decide(100.0, views(False, False), "a") == (None, None))

c = LinkChooser()
moves = [c.decide(t / 4.0, views(), "a") for t in range(0, 240)]
note("two links both fine for a minute: never moves",
     all(m == (None, None) for m in moves))

c = LinkChooser()
moved_at = None
for step in range(0, 80):
    now = 100.0 + step * 0.25
    key, why = c.decide(now, views(a_loss=25.0, b_loss=1.0, since=95.0), "a")
    if key is not None:
        moved_at = (now, key, why)
        break
note("the other link clearly better: moves only after holding for 5 s",
     moved_at is not None and moved_at[1:] == ("b", "loss")
     and abs(moved_at[0] - 105.0) < 0.3, moved_at)

c = LinkChooser()
first = None
for step in range(0, 80):
    now = 200.0 + step * 0.25
    key, _ = c.decide(now, views(a_loss=25.0, since=now - 1.0), "a")
    if key is not None:
        first = now
        break
note("a link that has only just come up is not moved to (settling)",
     first is None)

c = LinkChooser()
c.decide(300.0, views(active_alive=False), "a")          # forced move to b
back = None
for step in range(0, 200):
    now = 300.0 + step * 0.25
    key, _ = c.decide(now, views(a_loss=0.0, b_loss=30.0, since=280.0), "b")
    if key is not None:
        back = now
        break
note("no voluntary move within 30 s of the last one",
     back is not None and back >= 330.0, back)

c = LinkChooser()
held = [c.decide(400.0 + s * 0.25, views(a_loss=40.0, since=390.0), "a",
                 busy=True)
        for s in range(0, 60)]
after = c.decide(415.0, views(a_loss=40.0, since=390.0), "a", busy=False)
note("a mission or parameter transfer holds a voluntary move",
     all(h == (None, None) for h in held) and after[0] == "b", after)

c = LinkChooser()
flips = 0
for step in range(0, 400):
    now = 500.0 + step * 0.25
    bad_a = int(now) % 2 == 0           # which is worse changes every second
    key, _ = c.decide(now, views(a_loss=30.0 if bad_a else 0.0,
                                 b_loss=0.0 if bad_a else 30.0,
                                 since=490.0), "a")
    if key is not None:
        flips += 1
note("two links trading places every second: no hopping", flips == 0,
     "%d moves" % flips)

# The compare strategy, as the user asked for it: a link that comes
# back and is better takes over again - not only when the one in use
# fails. The rehearsal's case: the RFD back, a quarter second quicker
# than the LTE.
c = LinkChooser()
back = [c.decide(600.0 + s * 0.25, views(a_lag=0.25, since=590.0), "a")
        for s in range(0, 40)]
note("a link that comes back a quarter second quicker takes over again",
     any(k == "b" for k, _ in back))
c2 = LinkChooser()
near = [c2.decide(700.0 + s * 0.25, views(a_lag=0.05, since=690.0), "a")
        for s in range(0, 40)]
note("50 ms quicker is not worth a move", all(k is None for k, _ in near))
c3 = LinkChooser()
lossy = [c3.decide(800.0 + s * 0.25, views(a_loss=7.0, b_loss=1.0,
                                           since=790.0), "a")
         for s in range(0, 40)]
note("6 points less loss is worth a move", any(k == "b" for k, _ in lossy))
c4 = LinkChooser()
close = [c4.decide(900.0 + s * 0.25, views(a_loss=4.0, b_loss=1.0,
                                           since=890.0), "a")
         for s in range(0, 40)]
note("3 points is not", all(k is None for k, _ in close))
c5 = LinkChooser()
mixed = [c5.decide(1000.0 + s * 0.25, views(a_lag=0.3, b_loss=15.0,
                                            since=990.0), "a")
         for s in range(0, 40)]
note("quicker but losing far more is not better",
     all(k is None for k, _ in mixed))

# Sticky, as the user asked: an RFD at the edge of range, better for
# 25 s and worse for the next 25, over and over for ten minutes. With
# only the fixed wait that is a move at nearly every flip; damping makes
# each voluntary move double the wait for the next, so it settles.
c = LinkChooser()
active, moves = "a", []
for step in range(0, 2400):                 # 600 s, four looks a second
    now = 2000.0 + step * 0.25
    a_better = int((now - 2000.0) // 25) % 2 == 0
    v = [LinkView("a", True, 1990.0, 0.0 if a_better else 12.0, 0.0, 0),
         LinkView("b", True, 1990.0, 12.0 if a_better else 0.0, 0.0, 1)]
    key, _ = c.decide(now, v, active)
    if key is not None:
        moves.append(round(now - 2000.0))
        active = key
gaps = [b - a for a, b in zip(moves, moves[1:])]
note("a link better and worse by turns: it settles instead of hopping",
     2 <= len(moves) <= 5 and gaps == sorted(gaps),
     "%d moves in 10 min, 24 flips; at %s s" % (len(moves), moves))
calm = LinkChooser()
for t in (0.0, 40.0, 100.0):                # three in quick succession
    calm._voluntary_move(t)
note("the wait doubles with each move while they keep coming",
     calm.dwell(101.0) == 120.0, calm.dwell(101.0))
note("and five calm minutes put it back", calm.dwell(401.0) == 30.0,
     calm.dwell(401.0))

# Really bad is nearly silent: left after 5 s whatever the wait, for a
# link that works well. The user agreed to it after asking whether a
# better link could be kept waiting for minutes - with the wait built up
# by a flip-flop, a link losing most of what the aircraft sends could.


def long_wait():
    ch = LinkChooser()
    ch._wait, ch._last_voluntary, ch.last_move_at = 300.0, 1990.0, 1990.0
    return ch


def first_move(ch, start, seconds, **kw):
    for step in range(0, int(seconds * 4)):
        now = start + step * 0.25
        key, why = ch.decide(now, views(since=start - 100.0, **kw), "a")
        if key is not None:
            return round(now - start, 2), key, why
    return None


c = long_wait()
got = first_move(c, 2000.0, 20, a_loss=70.0, b_loss=1.0)
note("a really bad link is left after 5 s, whatever the wait",
     got == (5.0, "b", "lossy"), got)
note("and that does not lengthen the wait for the next move",
     c._last_voluntary == 1990.0 and c._wait == 300.0)
got = first_move(long_wait(), 2000.0, 20, a_lag=1.4)
note("so is one more than a second behind", got == (5.0, "b", "late"), got)
got = first_move(long_wait(), 2000.0, 20, a_loss=70.0, b_loss=60.0)
note("but never for a link that is bad too", got is None, got)
got = first_move(long_wait(), 2000.0, 60, a_loss=30.0, b_loss=1.0)
note("a link only somewhat worse still waits its turn", got is None, got)
c = long_wait()
blip = [c.decide(2000.0 + s * 0.25,
                 views(a_loss=70.0 if s < 12 else 3.0, since=1900.0), "a")
        for s in range(0, 40)]
note("a bad patch shorter than 5 s is not left",
     all(k is None for k, _ in blip))

# Both links failing together: the slower one is still "alive" for a
# moment after the faster has gone. Found in the rehearsal against real
# ArduPlane firmware, which moved onto it a quarter second before it was
# lost too.
c = LinkChooser()
fading = [LinkView("a", False, 0.0, 0.0, 0.0, 0, age=2.3),
          LinkView("b", True, 0.0, 0.0, 0.0, 1, age=1.6)]
note("a forced move never goes to a link that is fading too",
     c.decide(100.0, fading, "a") == (None, None))
working = [LinkView("a", False, 0.0, 0.0, 0.0, 0, age=2.3),
           LinkView("b", True, 0.0, 0.0, 0.0, 1, age=0.2)]
note("but goes at once to one heard from just now",
     c.decide(100.0, working, "a") == ("b", "silent"))

print("")
print("6. a parameter read never reads the link itself")
# pymavlink's MAVFTP waits for two answers - in its constructor, and when
# a transfer ends - by looping on recv_match, which takes frames off the
# link and throws away everything that is not FTP. The constructor ran
# on the window's thread: on V2.3.3, pressing PARAMS froze the window for
# 3.7 s and stopped the attitude stream for 4 s, measured against real
# ArduPlane firmware; with several links it made a healthy link look
# dead. Every FTP frame reaches the transfer through _feed_param_ftp, so
# nothing else may read the link.
from PySide6.QtCore import QCoreApplication
app = QCoreApplication.instance() or QCoreApplication([])


class FakeMav:
    def __init__(self):
        self.sent = 0

    def file_transfer_protocol_send(self, *args, **kwargs):
        self.sent += 1


class FakeConn:
    def __init__(self):
        self.mav = FakeMav()
        self.target_system, self.target_component = 1, 1
        self.reads = 0

    def recv_match(self, *args, **kwargs):
        self.reads += 1
        return None


link = MavlinkLink("udpin:0.0.0.0:14999")
link.master = FakeConn()
import time as _time
t = _time.time()
started = link._start_param_ftp()
held = _time.time() - t
note("the read starts", started is True)
note("without reading the link once", link.master.reads == 0,
     "%d reads" % link.master.reads)
note("and without holding the caller", held < 0.5, "%.2f s" % held)
note("the requests still go out", link.master.mav.sent >= 2,
     "%d FTP frames sent" % link.master.mav.sent)

print("")
print("7. why a link failed, in words")
from mavlink_link import _describe_link_error


class WinErr(Exception):
    """A Windows socket error, on any machine the tests run on."""

    def __init__(self, code, text="Windows' own words, in its own language"):
        super().__init__(text)
        self.winerror = code


for error, want in (
        (ConnectionRefusedError(), "nothing is listening there"),
        (WinErr(10061), "nothing is listening there"),
        (ConnectionResetError(), "closed by the other end"),
        (WinErr(10054), "closed by the other end"),
        (ConnectionError("closed by the other end"), "closed by the other end"),
        (TimeoutError(), "no answer"),
        (OSError("[Errno 98] Address already in use"),
         "that port is already in use by another program"),
        (Exception("could not open port 'COM36': FileNotFoundError(2, "
                   "'Sistem belirtilen dosyayi bulamiyor.', None, 2)"),
         "no such port - is the radio plugged in?"),
        (Exception("could not open port 'COM36': PermissionError(13, "
                   "'Erisim engellendi.', None, 5)"),
         "the port is in use by another program"),
        (Exception("ClearCommError failed (PermissionError(13, 'The device "
                   "does not recognize the command.', None, 22))"),
         "the device went away - unplugged?"),
        (Exception("something nobody has seen before"),
         "something nobody has seen before")):
    got = _describe_link_error(error)
    code = getattr(error, "winerror", None)
    note("%-28s -> %s" % (type(error).__name__ + (
        " %d" % code if isinstance(code, int) else ""), want),
         got == want, got)

from mavlink_link import _sentence
note("a reason ending in a question is not given a second stop",
     _sentence("the device went away - unplugged?")
     == "the device went away - unplugged?"
     and _sentence("nothing is listening there")
     == "nothing is listening there.",
     _sentence("the device went away - unplugged?"))

print("")
print("FAILED: %s" % ", ".join(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
