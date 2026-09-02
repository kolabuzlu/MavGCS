"""
Write a flown track as KMZ, KML or GPX.

KMZ and KML are the same document; a KMZ is only that document zipped, and
is the tidier thing to hand someone. Both are written as a <gx:Track>
rather than a plain <LineString>, because that carries a timestamp per
point: Google Earth then offers a time slider and will fly the route back,
which is the point of looking at a flight afterwards.

GPX is a different animal. It has no notion of an altitude measured from
anywhere but sea level, so where KML can honestly say "relative to
ground", GPX cannot - and rather than write a height above home into a
field every reader will take as elevation, the elevation is left out
entirely. A GPX track with no <ele> is valid and honest; one with the
wrong <ele> is neither.
"""

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

# Google Earth's line colours are aabbggrr, not rrggbb.
TRACK_COLOUR = "ff00a5ff"       # opaque, orange
TRACK_WIDTH = 3

SUPPORTED = (".kmz", ".kml", ".gpx")


def _iso_time(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _usable(track):
    """Points that have a position at all."""
    points = [p for p in track if p[1] is not None and p[2] is not None]
    if not points:
        raise ValueError("no positions to write")
    return points


def _altitudes(points, use_absolute):
    """One altitude per point, with gaps filled from their neighbours.

    A gap must never become zero: in an absolute track that is sea level,
    and a single such point drags the trace down to the sea and back. The
    aircraft was somewhere, and the points either side of it are a far
    better guess than the ocean.
    """
    raw = [(p[3] if use_absolute else p[4]) for p in points]
    filled, carried = [], None
    for value in raw:
        if value is not None:
            carried = value
        filled.append(carried)
    first_known = next((v for v in filled if v is not None), 0.0)
    return [first_known if v is None else v for v in filled]


def _has_amsl(points):
    """Whether nearly every point knows its height above sea level.

    One mode has to serve the whole track: mixing them would put parts of
    the trace at heights that mean different things.
    """
    return sum(1 for p in points if p[3] is not None) >= len(points) * 0.9


# ---------------------------------------------------------------- KML/KMZ

def build_kml(track, name="MavGCS flight", home=None, summary=None) -> str:
    """KML text for one flight.

    `track` is a sequence of (unix_time, lat, lon, amsl_m, rel_m); amsl_m
    may be None on any point. `home` is an optional (lat, lon). `summary`
    is optional text shown when the track is clicked.
    """
    points = _usable(track)
    use_absolute = _has_amsl(points)
    alt_mode = "absolute" if use_absolute else "relativeToGround"
    alts = _altitudes(points, use_absolute)

    whens = [f"      <when>{_iso_time(p[0])}</when>" for p in points]
    coords = [f"      <gx:coord>{p[2]:.7f} {p[1]:.7f} {a:.2f}</gx:coord>"
              for p, a in zip(points, alts)]

    first, last = points[0], points[-1]
    marks = [
        _placemark("Takeoff", first[2], first[1], "#start"),
        _placemark("Landing", last[2], last[1], "#finish"),
    ]
    if home and home[0] is not None and home[1] is not None:
        marks.append(_placemark("Home", home[1], home[0], "#home"))

    described = (f"    <description><![CDATA[<pre>{escape(summary)}</pre>]]>"
                 "</description>\n" if summary else "")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document>
  <name>{escape(name)}</name>
  <Style id="track">
    <LineStyle><color>{TRACK_COLOUR}</color><width>{TRACK_WIDTH}</width></LineStyle>
    <PolyStyle><color>4000a5ff</color></PolyStyle>
    <IconStyle><scale>0</scale></IconStyle>
  </Style>
  <Style id="start">
    <IconStyle><color>ff00ff00</color><Icon><href>http://maps.google.com/mapfiles/kml/paddle/grn-circle.png</href></Icon></IconStyle>
  </Style>
  <Style id="finish">
    <IconStyle><color>ff0000ff</color><Icon><href>http://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon></IconStyle>
  </Style>
  <Style id="home">
    <IconStyle><Icon><href>http://maps.google.com/mapfiles/kml/shapes/home.png</href></Icon></IconStyle>
  </Style>
  <Placemark>
    <name>{escape(name)}</name>
{described}    <styleUrl>#track</styleUrl>
    <gx:Track>
      <altitudeMode>{alt_mode}</altitudeMode>
{chr(10).join(whens)}
{chr(10).join(coords)}
    </gx:Track>
  </Placemark>
{chr(10).join(marks)}
</Document>
</kml>
"""


def _placemark(label, lon, lat, style):
    return (f"  <Placemark>\n"
            f"    <name>{escape(label)}</name>\n"
            f"    <styleUrl>{style}</styleUrl>\n"
            f"    <Point><coordinates>{lon:.7f},{lat:.7f}</coordinates></Point>\n"
            f"  </Placemark>")


# -------------------------------------------------------------------- GPX

def build_gpx(track, name="MavGCS flight", home=None, summary=None) -> str:
    """GPX 1.1 text for one flight.

    Elevation is written only when the vehicle reported its height above
    sea level. GPX has no way to say "this height is measured from the
    ground", so a height above home would be read as elevation by every
    tool that opens it - wrong by however high the launch site is.
    """
    points = _usable(track)
    use_absolute = _has_amsl(points)
    alts = _altitudes(points, use_absolute) if use_absolute else None

    rows = []
    for i, p in enumerate(points):
        rows.append(f'      <trkpt lat="{p[1]:.7f}" lon="{p[2]:.7f}">')
        if alts is not None:
            rows.append(f"        <ele>{alts[i]:.2f}</ele>")
        rows.append(f"        <time>{_iso_time(p[0])}</time>")
        rows.append("      </trkpt>")

    waypoints = [_gpx_waypoint("Takeoff", points[0][1], points[0][2]),
                 _gpx_waypoint("Landing", points[-1][1], points[-1][2])]
    if home and home[0] is not None and home[1] is not None:
        waypoints.append(_gpx_waypoint("Home", home[0], home[1]))

    described = (f"    <desc>{escape(summary)}</desc>\n" if summary else "")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MavGCS"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">
  <metadata>
    <name>{escape(name)}</name>
    <time>{_iso_time(points[0][0])}</time>
  </metadata>
{chr(10).join(waypoints)}
  <trk>
    <name>{escape(name)}</name>
{described}    <trkseg>
{chr(10).join(rows)}
    </trkseg>
  </trk>
</gpx>
"""


def _gpx_waypoint(label, lat, lon):
    return (f'  <wpt lat="{lat:.7f}" lon="{lon:.7f}">\n'
            f"    <name>{escape(label)}</name>\n"
            f"  </wpt>")


# ------------------------------------------------------------------ write

def write_track(path, track, name="MavGCS flight", home=None,
                summary=None) -> Path:
    """Write the track to `path`, in the format its extension asks for."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported format {suffix or '(none)'}; "
                         f"expected one of {', '.join(SUPPORTED)}")

    if suffix == ".gpx":
        payload = build_gpx(track, name=name, home=home, summary=summary)
    else:
        payload = build_kml(track, name=name, home=home, summary=summary)

    path.parent.mkdir(parents=True, exist_ok=True)
    # Written under a temporary name and moved into place, so an
    # interrupted write cannot leave a half-file behind that a reader
    # would choke on.
    part = path.with_suffix(path.suffix + ".part")
    try:
        if suffix == ".kmz":
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("doc.kml", payload)
        else:
            part.write_text(payload, encoding="utf-8")
        path.unlink(missing_ok=True)
        part.rename(path)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return path
