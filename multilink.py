"""
Several links to one aircraft at once - naming them, and showing them.

MAVProxy can hold an RFD on a serial port and an LTE bridge on a socket
together and use whichever is working. MavGCS does the same: the first
link is opened with Connect as always, and while it is open the button
adds more. This module is the part with no Qt and no sockets in it - what
a link is called on screen, and the one line that shows them all - so it
can be tested on its own.
"""

import html
from collections import deque

# The marks in front of each link on the link line. Shapes as well as
# colours, so the line still reads on a screen washed out by sunlight.
MARK_ACTIVE = "●"      # filled circle - the link in use
MARK_STANDBY = "○"     # hollow circle - connected and ready
MARK_DOWN = "×"        # cross - lost, or not usable

COLOUR_ACTIVE = "#7ddc7d"
COLOUR_TEXT_ACTIVE = "#dfe6ee"
COLOUR_STANDBY = "#9aa4ad"
COLOUR_DOWN = "#e6a23c"
COLOUR_WRONG = "#f66"


def link_label(connection_string: str) -> str:
    """What a link is called on screen, from the string that opened it.

    Short, because several sit on one line: the port is what tells two
    links apart on one machine, so a link on this computer is named by
    its port alone and only a remote one carries its address.
    """
    s = (connection_string or "").strip()
    lowered = s.lower()
    for prefix, name in (("tcp:", "TCP"), ("udpin:", "UDP"),
                         ("udpout:", "UDP"), ("udp:", "UDP")):
        if lowered.startswith(prefix):
            host, _, port = s[len(prefix):].rpartition(":")
            if not port:
                return name
            local = host in ("", "0.0.0.0", "127.0.0.1", "localhost")
            return "%s %s" % (name, port if local else "%s:%s" % (host, port))
    # Serial: "COM36:57600" or "/dev/tty.usbserial-A10K5:57600".
    device = s.rsplit(":", 1)[0] if s.rsplit(":", 1)[-1].isdigit() else s
    device = device.rsplit("/", 1)[-1]
    for prefix in ("tty.", "cu."):
        if device.startswith(prefix):
            device = device[len(prefix):]
    return device or "link"


def _one(link: dict, detailed: bool):
    """One link's piece of the line: (rich text, the same as plain text).

    The plain text is what the line's length is judged by - the font is
    monospaced, so characters are the measure.
    """
    label = str(link.get("label", "link"))
    state = link.get("state", "standby")
    if state == "active":
        mark, colour, text = MARK_ACTIVE, COLOUR_ACTIVE, COLOUR_TEXT_ACTIVE
    elif state == "standby":
        mark, colour, text = MARK_STANDBY, COLOUR_STANDBY, COLOUR_STANDBY
    elif state == "other":
        mark, colour, text = MARK_DOWN, COLOUR_WRONG, COLOUR_WRONG
    else:                                   # lost, waiting
        mark, colour, text = MARK_DOWN, COLOUR_DOWN, COLOUR_DOWN
    if not detailed:
        detail = ""
    elif state in ("active", "standby"):
        detail = " %.0f/s %.1f%%" % (float(link.get("rx_mps", 0.0)),
                                     float(link.get("loss_pct", 0.0)))
    else:
        detail = " " + {"lost": "lost", "waiting": "waiting",
                        "other": "other aircraft"}.get(state, state)
    rich = ('<span style="color:%s">%s</span>'
            '<span style="color:%s"> %s%s</span>'
            % (colour, mark, text, html.escape(label), html.escape(detail)))
    return rich, "%s %s%s" % (mark, label, detail)


SEPARATOR = "   "


def link_tooltip(links) -> str:
    """The detail the line has no room for, one link to a row."""
    words = {"active": "in use", "standby": "ready",
             "waiting": "no aircraft heard on it yet", "lost": "lost",
             "other": "a different aircraft - not used",
             "failed": "could not be opened - still trying"}
    rows = []
    for link in links:
        state = link.get("state", "standby")
        row = "%s - %s" % (link.get("label", "link"), words.get(state, state))
        if state in ("active", "standby"):
            row += ", %.0f messages a second, %.1f%% lost" % (
                float(link.get("rx_mps", 0.0)), float(link.get("loss_pct", 0.0)))
            lag = link.get("lag_s")
            if lag is not None and lag >= 0.05:
                row += ", %.1f s behind the fastest" % lag
        elif link.get("error"):
            row += " (%s)" % link["error"]
        if link.get("connection"):
            row += "\n      " + str(link["connection"])
        rows.append(row)
    return ("\n".join(rows) + "\n\nMavGCS uses the best link by itself: it "
            "moves at once when the one in use goes quiet, and otherwise "
            "only when another has been clearly better for a few seconds."
            "\nRight-click the Connection panel to remove a link.")


def link_line_html(links, max_chars=None) -> str:
    """Every link on one line: which is in use, and how each is doing.

    Only used with two or more links. A single link keeps the line it has
    always had, so a ground station with one radio looks exactly as it
    did before any of this existed.

    The line must never be wider than the panel: a label asks its layout
    for room, and in the left column that room comes out of the map. So
    when everything does not fit in max_chars, the links not in use drop
    their figures - their state still shows in the mark and its colour,
    and the tooltip has the rest - and if even that is too long, the link
    in use drops its figures too.
    """
    gap = "&nbsp;" * len(SEPARATOR)
    plans = ([True] * len(links),
             [link.get("state") == "active" for link in links],
             [False] * len(links))
    for detailed in plans:
        parts = [_one(link, d) for link, d in zip(links, detailed)]
        plain = SEPARATOR.join(p for _, p in parts)
        if max_chars is None or len(plain) <= max_chars:
            return gap.join(rich for rich, _ in parts)
    # Even the names alone are too long: the link in use and a count of
    # the rest, which the tooltip lists in full - then the link in use
    # alone - then its name cut to fit. The first of these that fits.
    active = [link for link in links if link.get("state") == "active"]
    first = (active or links)[0]
    more = "+%d more" % (len(links) - 1)
    more_rich = '<span style="color:%s">%s</span>' % (COLOUR_STANDBY, more)
    for detailed, with_more in ((True, True), (False, True), (False, False)):
        rich, plain = _one(first, detailed)
        if with_more:
            rich, plain = rich + gap + more_rich, plain + SEPARATOR + more
        if len(plain) <= max_chars:
            return rich
    room = max(1, max_chars - 2)
    return _one(dict(first, label=str(first.get("label", ""))[:room]),
                False)[0]


# ----------------------------------------------------------------------
# Health: is a link carrying the aircraft, and how well.

class SequenceCounter:
    """Frames missing from one link, counted per sender.

    The single link counts with one of these (MavlinkLink._count_sequence)
    and every one of several links has its own: the aircraft numbers its
    frames per output port, so the RFD and the LTE bridge each carry a
    sequence of their own, and mixing them would count every frame on one
    as a gap in the other. A frame that arrives late takes back the loss
    it was counted as, and the radio's own status frames are not the
    aircraft's.
    """

    # A backwards step larger than this is read as a frame arriving out
    # of order, not as that many losses. Losing more than half the
    # sequence space between two received frames is far less likely than
    # two frames having swapped places, which UDP does routinely.
    REORDER_LIMIT = 128
    # A telemetry radio injects RADIO_STATUS under a fixed identity -
    # ord('3'), ord('D') - and both ends of the link can emit under it
    # with sequence counters of their own. Counting those interleaved
    # streams as one sender reports losses that never happened, which is
    # why pymavlink excludes the same tuple. Nothing is learned from the
    # sequence of a radio's own status messages anyway.
    RADIO = (ord("3"), ord("D"))
    # A sender heard again after this long has not sent a late frame -
    # it has resumed, and its sequence has moved on by an unknowable
    # amount. Found in the rehearsal against real ArduPlane firmware:
    # after an 8 s outage the sequence had come more than half way round,
    # every frame that followed looked late, and each one took back a
    # loss counted before the outage - until the link's loss read -164%.
    # A late frame follows its successor within moments; a second of
    # silence is never reordering.
    RESYNC_S = 1.0

    def __init__(self):
        self.last = {}
        self.heard_at = {}
        self.lost = 0

    def count(self, key, seq, now=None):
        if key == self.RADIO:
            return
        # When this sender was last heard at all - late frames and
        # duplicates included. Timed from the last frame that moved the
        # count forward instead, a run of late frames looked like a
        # silence, and normal traffic stopped being counted as V2.3.3
        # counted it - 109 of 200 random histories, in the test.
        heard, self.heard_at[key] = self.heard_at.get(key), now
        last = self.last.get(key)
        if last is None:
            self.last[key] = seq
            return
        gap = (seq - last - 1) % 256
        if gap > self.REORDER_LIMIT:
            if (now is not None and heard is not None
                    and now - heard > self.RESYNC_S):
                self.last[key] = seq        # resumed after a silence
                return
            if self.lost > 0:
                self.lost -= 1
            return
        self.last[key] = seq
        self.lost += gap


class LinkHealth:
    """What one link is doing, measured from what arrives on it.

    Every figure is about the aircraft, not the link: a ground radio goes
    on reporting its own status with the aircraft long out of range, so
    only frames from the aircraft's system id count towards being alive.

    The time is passed in rather than read, so the rules can be checked
    against any history without waiting for it.
    """

    # The aircraft sends something several times a second on any useful
    # link, so two seconds of nothing is a link that has stopped - not a
    # slow one.
    DEAD_AFTER_S = 2.0
    LOSS_WINDOW_S = 5.0
    # Delay is judged on the best a link has managed lately: queues on a
    # busy link add jitter, and the minimum is what the link itself costs.
    LAG_WINDOW_S = 3.0

    def __init__(self):
        self.seq = SequenceCounter()
        self.received = 0
        self.last_vehicle_at = None
        self.alive_since = None
        self.loss_pct = 0.0
        self.rx_mps = 0.0
        self._window = deque()
        self._offsets = deque()

    def on_frame(self, now, key, seq, from_vehicle, boot_ms=None):
        """One frame arrived on this link."""
        self.seq.count(key, seq, now)
        if not from_vehicle:
            return
        self.received += 1
        if (self.last_vehicle_at is None
                or now - self.last_vehicle_at > self.DEAD_AFTER_S):
            self.alive_since = now          # heard again, after a silence
        self.last_vehicle_at = now
        if boot_ms:
            # The aircraft's clock is the same whichever way a frame came,
            # so arrival time minus send time differs between links only
            # by how long each one took.
            self._offsets.append((now, now - boot_ms / 1000.0))
            while (self._offsets
                   and now - self._offsets[0][0] > self.LAG_WINDOW_S):
                self._offsets.popleft()

    def alive(self, now):
        return (self.last_vehicle_at is not None
                and now - self.last_vehicle_at <= self.DEAD_AFTER_S)

    def offset(self, now):
        """This link's best recent arrival-minus-send time, or None."""
        recent = [o for t, o in self._offsets if now - t <= self.LAG_WINDOW_S]
        return min(recent) if recent else None

    def tick(self, now):
        """Once a second: loss and rate over the last few seconds."""
        self._window.append((now, self.received, self.seq.lost))
        while (len(self._window) > 1
               and now - self._window[0][0] > self.LOSS_WINDOW_S):
            self._window.popleft()
        then, received, lost = self._window[0]
        # A late frame takes back a loss already counted, so this window
        # can see fewer losses than the last one did - never fewer than
        # none.
        got, missed = self.received - received, max(0, self.seq.lost - lost)
        span = now - then
        self.rx_mps = got / span if span > 0 else 0.0
        self.loss_pct = 100.0 * missed / (got + missed) if got + missed else 0.0


def lags(now, healths):
    """How far behind the fastest link each one is, in seconds.

    healths: {key: LinkHealth}. A link with nothing to judge by gets 0 -
    no evidence of delay is not evidence of delay.
    """
    offsets = {k: h.offset(now) for k, h in healths.items()}
    known = [o for o in offsets.values() if o is not None]
    best = min(known) if known else None
    return {k: (o - best if o is not None and best is not None else 0.0)
            for k, o in offsets.items()}


# ----------------------------------------------------------------------
# Choosing: which link carries the commands, and when to move.

class LinkView:
    """A link as the chooser sees it - nothing but the numbers it needs."""

    def __init__(self, key, alive, alive_since=None, loss_pct=0.0,
                 lag_s=0.0, order=0, age=None):
        self.key = key
        self.alive = alive
        self.alive_since = alive_since
        self.loss_pct = loss_pct
        self.lag_s = lag_s
        self.order = order
        self.age = age          # seconds since its last frame from the aircraft


class LinkChooser:
    """Which link to use, and when to leave the one in use.

    Two kinds of move, deliberately different:

      the link in use has gone quiet - move at once, to the best of the
      rest, whatever else is going on. A command on a dead link is a
      command lost.

      the link in use works but another is clearly better - move only if
      it has stayed clearly better for a few seconds, has itself been up
      for a few, and the last move was a while ago. Two good links that
      trade places by a hair every second would otherwise have the
      ground station hopping between them all flight.

    "Clearly better" is fewer lost frames by five points, or a tenth of a
    second less delay without losing more. Both directions are compared
    all the time, not only after a failure: a link that comes back and is
    better takes over again once it has proved itself for a few seconds.
    The user asked for exactly that - "I want the compare strategy" -
    after the first margins (half a second, ten points) left a returning
    RFD a quarter of a second quicker than the LTE sitting unused.
    Two links within these margins are left as they are: no move is made
    for nothing.

    And it is sticky, which the user asked for in the same breath: "I do
    not want mavgcs to hop back and forth between connections all the
    time." A voluntary move needs the other link better for HOLD_S, that
    link up for SETTLE_S, and a wait since the last move: DWELL_S at
    first, doubled by every voluntary move made within FLAP_WINDOW_S of
    the one before, up to DWELL_MAX_S - 30 s, a minute, two, four, five.
    Only FLAP_WINDOW_S with no voluntary move puts it back to DWELL_S.

    Two cheaper versions were tried against an RFD at the edge of its
    range - better for 25 s, worse for the next 25, for ten minutes -
    and both let it hop in pairs: a count of moves in a sliding window
    forgot old moves in the middle of the flip-flop, the wait shrank,
    and the ground station went there and back within forty seconds
    every couple of minutes. Doubling for as long as the moves keep
    coming does not forget. None of it delays a forced move: a link that
    goes quiet is left at once.
    """

    BETTER_LOSS_POINTS = 5.0
    BETTER_LAG_S = 0.1
    HOLD_S = 5.0
    SETTLE_S = 5.0
    DWELL_S = 30.0
    FLAP_WINDOW_S = 300.0
    DWELL_MAX_S = 300.0
    # Where a forced move may go: only to a link heard from in the last
    # second. "Alive" allows two seconds of quiet, and when both links
    # fail together the slower one is still "alive" for a moment after
    # the faster has gone - the rehearsal against real ArduPlane firmware
    # moved onto exactly such a link, a quarter of a second before it
    # was lost too.
    FRESH_S = 1.0

    def __init__(self):
        self.last_move_at = float("-inf")
        self._better_since = {}
        self._last_voluntary = float("-inf")
        self._wait = self.DWELL_S

    def dwell(self, now):
        """How long since the last move a voluntary one must wait now."""
        if now - self._last_voluntary > self.FLAP_WINDOW_S:
            self._wait = self.DWELL_S        # calm for long enough
        return self._wait

    def _voluntary_move(self, now):
        """A voluntary move made: the next one waits twice as long, for as
        long as they keep coming within FLAP_WINDOW_S of each other."""
        if now - self._last_voluntary <= self.FLAP_WINDOW_S:
            self._wait = min(self.DWELL_MAX_S, self._wait * 2)
        else:
            self._wait = self.DWELL_S
        self._last_voluntary = now

    def clearly_better(self, a, b):
        if b.loss_pct - a.loss_pct >= self.BETTER_LOSS_POINTS:
            return True
        return (b.lag_s - a.lag_s >= self.BETTER_LAG_S
                and a.loss_pct <= b.loss_pct + 2.0)

    def decide(self, now, links, active, busy=False):
        """Returns (key to move to, why) - or (None, None) to stay.

        busy: something is half-way through a conversation with the
        aircraft on the link in use - a mission, a fence, parameters. A
        voluntary move waits for it; a forced one does not.
        """
        current = next((l for l in links if l.key == active), None)
        alive = [l for l in links if l.alive]
        if not alive:
            self._better_since.clear()
            return None, None

        def rank(link):
            return (link.loss_pct, link.lag_s, link.order)

        if current is None or not current.alive:
            fresh = [l for l in alive
                     if l.age is None or l.age <= self.FRESH_S]
            if not fresh:
                return None, None       # fading too: wait for one that works
            best = min(fresh, key=rank)
            self._better_since.clear()
            self.last_move_at = now
            return best.key, "silent"

        ready = []
        for link in alive:
            if link.key == active:
                continue
            if self.clearly_better(link, current):
                since = self._better_since.setdefault(link.key, now)
                settled = (link.alive_since is not None
                           and now - link.alive_since >= self.SETTLE_S)
                if now - since >= self.HOLD_S and settled:
                    ready.append(link)
            else:
                self._better_since.pop(link.key, None)
        if not ready or busy or now - self.last_move_at < self.dwell(now):
            return None, None
        best = min(ready, key=rank)
        why = ("loss" if current.loss_pct - best.loss_pct
               >= self.BETTER_LOSS_POINTS else "delay")
        self._better_since.clear()
        self.last_move_at = now
        self._voluntary_move(now)
        return best.key, why
