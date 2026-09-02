"""
Write a flown track as a KMZ for Google Earth.

A KMZ is just a zip with a KML inside it. The track is written as a
<gx:Track> rather than a plain <LineString> because that carries a
timestamp per point: Google Earth then offers a time slider and will fly
the route back at whatever speed you choose, which is the point of looking
at a flight afterwards.

Altitude is written as absolute (above sea level) when the vehicle told us
its AMSL, because that is what puts the trace at the right height over
Google Earth's own terrain. Without it the only honest option is
relative-to-ground, which is a different thing and is labelled as such in
the file.
"""

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

# Google Earth's own line colours are aabbggrr, not rrggbb.
TRACK_COLOUR = "ff00a5ff"       # opaque, orange
TRACK_WIDTH = 3


def _kml_time(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def build_kml(track, name="MavGCS flight", home=None, summary=None) -> str:
    """KML text for one flight.

    `track` is a sequence of (unix_time, lat, lon, amsl_m, rel_m); amsl_m
    may be None on any point. `home` is an optional (lat, lon). `summary`
    is optional text shown when the track is clicked.
    """
    points = [p for p in track
              if p[1] is not None and p[2] is not None]
    if not points:
        raise ValueError("no positions to write")

    # One mode for the whole track: mixing them would put parts of the
    # trace at heights that mean different things.
    use_absolute = sum(1 for p in points if p[3] is not None) >= len(points) * 0.9
    alt_mode = "absolute" if use_absolute else "relativeToGround"

    whens, coords = [], []
    for t, lat, lon, amsl, rel in points:
        alt = amsl if use_absolute else rel
        if alt is None:
            alt = 0.0
        whens.append(f"      <when>{_kml_time(t)}</when>")
        coords.append(f"      <gx:coord>{lon:.7f} {lat:.7f} {alt:.2f}</gx:coord>")

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


def write_kmz(path, track, name="MavGCS flight", home=None, summary=None) -> Path:
    """Write the track to `path` as a KMZ. Returns the path written."""
    kml = build_kml(track, name=name, home=home, summary=summary)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temporary name and moved into place, so an interrupted
    # write cannot leave a half-file that Google Earth would refuse.
    part = path.with_suffix(path.suffix + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", kml)
        path.unlink(missing_ok=True)
        part.rename(path)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    return path
