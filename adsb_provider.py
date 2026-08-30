"""
ADS-B traffic overlay - background polling of free public ADS-B APIs (no
API key or account needed) for nearby manned air traffic, the same kind of
online source KiteGCS's own Radar tool uses.

This has to run from Python, not the map's own JavaScript: these APIs send
no CORS headers, so a browser-context fetch() from the map page gets
blocked outright ("blocked by CORS policy", confirmed directly) - a plain
server-side request has no such restriction.

Two providers are tried in order, because they are community-run and
individually unreliable: adsb.lol was reachable one minute and timing out
the next during development, which showed up as "no traffic anywhere"
rather than an error. Their JSON differs slightly (`aircraft` vs `ac`),
so each has its own small parser.
"""

import json
import threading
import time
import urllib.request
import urllib.error

from PySide6.QtCore import QThread, Signal

RADIUS_NM = 150
# 5s matches KiteGCS's own default online poll interval - fresh enough that
# contacts visibly track rather than jumping between distant positions.
POLL_INTERVAL_S = 5.0
# Deliberately shorter than POLL_INTERVAL_S: a dead endpoint must not keep
# the worker (and its socket) tied up past the next poll.
REQUEST_TIMEOUT_S = 6
# How long to stop querying a provider after it fails (see _poll).
PROVIDER_COOLDOWN_S = 120


def _parse_adsbfi(data):
    return data.get("aircraft") or []


def _parse_adsblol(data):
    return data.get("ac") or []


# (name, url template, parser). adsb.fi first - measurably the more
# responsive of the two during development.
PROVIDERS = [
    (
        "adsb.fi",
        "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{radius}",
        _parse_adsbfi,
    ),
    (
        "adsb.lol",
        "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius}",
        _parse_adsblol,
    ),
    # Deliberately NOT included: adsb.one
    # (https://api.adsb.one/v2/point/{lat}/{lon}/{dist}). It answers 403
    # Forbidden without an API key for any User-Agent, which is why
    # KiteGCS ships it as an example row that's switched off by default.
]


class AdsbWorker(QThread):
    """
    Background thread that polls public ADS-B feeds for traffic around a
    centre point while enabled. set_enabled()/update_center() are the only
    methods safe to call from the GUI thread.
    """

    # list of {hex, flight, lat, lon, alt_baro, gs, track, vert_rate, type, squawk}
    contacts_ready = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._lock = threading.Lock()
        self._enabled = False
        self._center = None  # (lat, lon) - the map's centre, not the vehicle's
        self._wake = threading.Event()
        self._provider_retry_at = {}  # provider name -> time.time() before which to skip it

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._enabled = enabled
        if enabled:
            self._wake.set()  # poll immediately rather than waiting out the interval
        else:
            self.contacts_ready.emit([])

    def update_center(self, lat: float, lon: float):
        with self._lock:
            self._center = (lat, lon)

    def run(self):
        while self._running:
            with self._lock:
                enabled, center = self._enabled, self._center
            if enabled and center is not None:
                self._poll(*center)
            self._wake.wait(POLL_INTERVAL_S)
            self._wake.clear()

    def _poll(self, lat, lon):
        # Query every provider and merge, de-duplicated by ICAO hex (the
        # aircraft's unique address) - the same merge KiteGCS does across
        # its feeds. Each network only sees what its own volunteers'
        # receivers pick up, so the union is meaningfully larger than any
        # single feed, and it keeps working when one of them is down.
        merged = {}
        for name, template, parser in PROVIDERS:
            # Skip a provider that recently failed. urlopen's timeout does
            # NOT bound DNS/TLS connect retries - a dead adsb.lol was
            # measured taking 18s to fail against a 6s timeout, which
            # stalled every poll well past the 5s interval and made traffic
            # look frozen. Backing off keeps one dead feed from throttling
            # the live one.
            if time.time() < self._provider_retry_at.get(name, 0):
                continue

            url = template.format(lat=lat, lon=lon, radius=RADIUS_NM)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MavGCS"})
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    data = json.loads(resp.read())
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                # These feeds are community-run and individually flaky
                # (adsb.lol was timing out for days during development, and
                # shows as failed in KiteGCS too) - park it briefly and let
                # the others answer.
                self._provider_retry_at[name] = time.time() + PROVIDER_COOLDOWN_S
                continue

            self._provider_retry_at.pop(name, None)
            for ac in parser(data):
                if not isinstance(ac.get("lat"), (int, float)):
                    continue
                if not isinstance(ac.get("lon"), (int, float)):
                    continue
                # "ground" is what these feeds report instead of a number
                # for aircraft sitting on the airport surface - not traffic
                # worth drawing on a flight map.
                if ac.get("alt_baro") == "ground":
                    continue
                key = ac.get("hex") or f"{ac.get('flight')}:{ac['lat']}:{ac['lon']}"
                if key in merged:
                    continue
                # baro_rate/geom_rate are ft/min; gs is knots; alt_baro is
                # feet. Converted to metric on the display side.
                vert = ac.get("baro_rate")
                if not isinstance(vert, (int, float)):
                    vert = ac.get("geom_rate")
                merged[key] = {
                    "hex": ac.get("hex"),
                    "flight": (ac.get("flight") or "").strip(),
                    "lat": ac.get("lat"),
                    "lon": ac.get("lon"),
                    "alt_baro": ac.get("alt_baro"),
                    "gs": ac.get("gs"),
                    "track": ac.get("track"),
                    "vert_rate": vert if isinstance(vert, (int, float)) else None,
                    "type": ac.get("t") or "",
                    "squawk": ac.get("squawk") or "",
                }

            # Emit after each provider rather than only once both are done,
            # so a fast feed paints immediately instead of waiting on a slow
            # one; a later provider just adds whatever it saw that this one
            # didn't.
            #
            # Unless the overlay has been switched off in the meantime. A
            # fetch takes up to six seconds, so switching ADS-B off mid-poll
            # used to let the reply land afterwards and re-draw every
            # contact - with the box unticked, and with no further polls
            # coming to move or remove them, so they sat frozen on the map
            # looking like live traffic until ADS-B was toggled again.
            #
            # Only this emit is guarded: set_enabled() sends an empty list
            # through the same signal to clear the markers, and that one
            # has to get through precisely when we are disabled.
            with self._lock:
                still_wanted = self._enabled
            if not still_wanted:
                return
            self.contacts_ready.emit(list(merged.values()))

    def stop(self):
        # urlopen's timeout doesn't bound DNS/TLS connect retries (a dead
        # feed was measured taking ~18s against a 6s timeout), so allow
        # real time to unwind before the last-resort terminate - a QThread
        # still running when Qt destroys it aborts the process.
        self._running = False
        self._wake.set()
        if not self.wait(5000):
            self.terminate()
            self.wait(1000)
