"""
3D FPV view: the world ahead, seen from the aircraft.

Built on CesiumJS with Cesium World Terrain and imagery, the same
foundation KiteGCS uses. Cesium handles the parts that are genuinely hard
to do well - streaming level-of-detail terrain, high-resolution imagery,
atmosphere - so the camera is all we have to drive.

Two consequences of that choice, both accepted deliberately:
  * it needs a Cesium Ion token, one free account per user;
  * it only works online, since terrain and imagery are streamed.

The HUD is the real artificial-horizon widget rendered transparently and
laid over the scene, so the 2D and 3D views can't drift apart.

The wheel (or a two-finger slide) zooms, double-click returns to 1x.
"""

import json

from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QSizePolicy

# Shown when no Ion token has been entered yet.
NO_TOKEN_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8" /><style>
  html, body { height:100%; margin:0; background:#0b1016; color:#c8d4de;
               font-family: sans-serif; }
  .wrap { height:100%; display:flex; align-items:center; justify-content:center;
          text-align:center; padding:0 24px; }
  h3 { margin:0 0 8px; font-size:15px; color:#fff; }
  p { margin:4px 0; font-size:12px; line-height:1.5; color:#9fb0be; }
  a { color:#37a8db; }
</style></head>
<body><div class="wrap"><div>
  <h3>Cesium Ion token needed</h3>
  <p>The 3D view streams terrain and imagery from Cesium Ion.</p>
  <p>Create a free account at <a href="#">cesium.com/ion</a>, copy your
     access token, then press the <b>FPV</b> button again to enter it.</p>
</div></div></body></html>
"""

CESIUM_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<link rel="stylesheet" href="%%BASE%%Widgets/widgets.css" />
<style>
  html, body { height: 100%; margin: 0; padding: 0; background: #0b1016; overflow: hidden; }
  #view { width: 100%; height: 100%; }
  /* The real HUD widget, pushed over the scene as a transparent image. */
  #hud { position: absolute; inset: 0; width: 100%; height: 100%;
         pointer-events: none; display: block; z-index: 10; }
  #status { position: absolute; top: 6px; left: 8px; z-index: 20;
            font-family: sans-serif; font-size: 11px; color: #9fb0be;
            text-shadow: 0 0 3px #000; pointer-events: none; }
  /* Zoom factor, shown only while it's being changed. */
  #zoom { position: absolute; top: 22px; left: 8px; z-index: 20;
          font-family: sans-serif; font-size: 12px; font-weight: bold;
          color: #fff; text-shadow: 0 0 3px #000; pointer-events: none;
          opacity: 0; transition: opacity 0.4s; }
  #zoom.show { opacity: 1; transition: none; }
  /* Cesium's own chrome isn't wanted in a cockpit view, but its credit
     line stays - showing attribution is a condition of using the data. */
  .cesium-viewer-toolbar, .cesium-viewer-animationContainer,
  .cesium-viewer-timelineContainer, .cesium-viewer-fullscreenContainer {
      display: none !important;
  }
  /* Left as one run, that credit line lies straight across the HUD's
     bottom row, over LAT and into the EKF/VIBE boxes. It is three separate
     elements though, so it can be split and lifted clear: the Ion logo to
     the left, the "Upgrade for commercial use" text and the data
     attribution link to the right, all sitting above the bottom row
     (6px margin + 18px box = 24px tall). Everything stays on screen -
     this moves the attribution, it doesn't hide it.
     !important throughout: Cesium sets display:inline on these as an
     inline style, which a plain rule can't outrank. */
  .cesium-viewer-bottom { left: 0; right: 0; bottom: 0; padding-right: 0; }
  .cesium-widget-credits { display: block !important; }
  .cesium-credit-logoContainer {
      display: block !important; position: absolute !important;
      /* Hard against the left edge. Ion's logo file carries 1px of
         transparent padding of its own, so the visible mark starts at 1px;
         going below 0 here would crop the logo rather than move it. */
      left: 0; bottom: 28px;
  }
  .cesium-credit-logoContainer img { max-height: 24px; width: auto; }
  .cesium-credit-textContainer, .cesium-credit-expand-link {
      display: block !important; position: absolute !important;
      right: 8px; text-align: right; padding-left: 0;
  }
  .cesium-credit-textContainer { bottom: 28px; max-width: 55%; }
  .cesium-credit-expand-link  { bottom: 42px; }
</style>
<script>
  // Must be set before Cesium.js loads: it resolves its workers, shaders
  // and assets relative to this.
  window.CESIUM_BASE_URL = '%%BASE%%';
</script>
<script src="%%BASE%%Cesium.js"></script>
</head>
<body>
<div id="view"></div>
<img id="hud" />
<div id="status">Starting 3D view...</div>
<div id="zoom"></div>
<script>
var viewer = null, ready = false, failed = false;

// Zoom is done as a lens would do it - by narrowing the field of view -
// rather than by moving the camera. Pulling the camera backwards is what
// Cesium's own wheel handler does, and in a view that is supposed to be
// FROM the aircraft that would just slide it off the nose.
// Opens at the widest the view goes, so there is nothing left to zoom out
// to - you start with the most situational awareness and zoom in on what
// you want a closer look at. That also makes the widest view 1.0x, which
// is what double-click returns to.
var MIN_FOV = 8, MAX_FOV = 90;
var DEFAULT_FOV = MAX_FOV;
var fovDeg = DEFAULT_FOV, zoomTimer = null;

function applyFov() {
    if (!viewer) return;
    var frustum = viewer.camera.frustum;
    // Only a perspective frustum has an fov; an orthographic one would
    // throw. setView() leaves the frustum alone, so this survives every
    // telemetry update without being reapplied.
    if (frustum && typeof frustum.fov === 'number') {
        frustum.fov = Cesium.Math.toRadians(fovDeg);
    }
}

function showZoom() {
    var el = document.getElementById('zoom');
    el.textContent = (DEFAULT_FOV / fovDeg).toFixed(1) + 'x';
    el.classList.add('show');
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(function () { el.classList.remove('show'); }, 900);
}

function onWheel(e) {
    e.preventDefault();
    if (!ready) return;
    var delta = e.deltaY;
    // Wheels report pixels, lines or pages depending on the device;
    // normalise so a touchpad and a mouse wheel feel roughly alike.
    if (e.deltaMode === 1) delta *= 16;
    else if (e.deltaMode === 2) delta *= 400;
    // Exponential, so one notch is the same proportional step whether
    // you're wide open or zoomed right in.
    fovDeg *= Math.exp(delta * 0.0015);
    fovDeg = Math.min(MAX_FOV, Math.max(MIN_FOV, fovDeg));
    applyFov();
    showZoom();
}

function resetZoom() {
    fovDeg = DEFAULT_FOV;
    applyFov();
    showZoom();
}

function init() {
    try {
        Cesium.Ion.defaultAccessToken = '%%TOKEN%%';
        viewer = new Cesium.Viewer('view', {
            terrain: Cesium.Terrain.fromWorldTerrain(),
            // Cesium defaults msaaSamples to 1 - antialiasing off - which
            // is what turns ridge lines and the horizon into a staircase,
            // and it shows all the more in a panel this small. 4 samples
            // is the usual sweet spot for cost against smoothness.
            msaaSamples: 4,
            contextOptions: { webgl: { antialias: true } },
            animation: false, timeline: false, baseLayerPicker: false,
            geocoder: false, homeButton: false, sceneModePicker: false,
            navigationHelpButton: false, fullscreenButton: false,
            infoBox: false, selectionIndicator: false,
        });
        // Draw at the display's real pixel density. Cesium defaults to the
        // "browser recommended" resolution, which ignores devicePixelRatio
        // to save GPU - on a high-DPI screen that alone renders the view at
        // two thirds scale and then stretches it.
        viewer.useBrowserRecommendedResolution = false;
        // Catches the edges MSAA doesn't, notably where terrain meets sky.
        var fxaa = viewer.scene.postProcessStages.fxaa;
        if (fxaa) { fxaa.enabled = true; }
        // Move the camera once per rendered frame, not once per telemetry
        // sample - this is what turns 4Hz of data into smooth motion.
        viewer.scene.preUpdate.addEventListener(onPreUpdate);
        // This is a camera view from the aircraft, not something to drag.
        viewer.scene.screenSpaceCameraController.enableInputs = false;
        viewer.scene.globe.depthTestAgainstTerrain = true;
        applyFov();
        // passive:false so preventDefault works - without it the wheel
        // also scrolls the page under the canvas.
        document.addEventListener('wheel', onWheel, { passive: false });
        document.addEventListener('dblclick', resetZoom);
        ready = true;
        document.getElementById('status').textContent = '';
    } catch (e) {
        // Latched: a later status update must not wipe the reason the
        // view is empty off the screen.
        failed = true;
        document.getElementById('status').textContent = '3D view failed: ' + e;
    }
}

// Telemetry arrives far slower than the scene renders - ATTITUDE comes in
// at about 4Hz on a serial telemetry link, against 60fps of rendering - so
// applying each sample directly makes the view jump in visible steps. The
// samples are kept as a target and the camera glides towards it every
// frame instead, which costs no bandwidth at all.
var pose = null;        // what the camera is showing
var posSamples = [];    // position telemetry, oldest first
var attSamples = [];    // attitude telemetry, oldest first
var lastFrameMs = 0;

// Never sit exactly on the surface: on the ground AGL is 0, and a camera
// level with the terrain clips through it.
var MIN_EYE_M = 1.5;

// Position and attitude arrive as SEPARATE MAVLink messages at different
// rates - typically attitude about twice as often as position. Held in one
// buffer, every other sample repeated the previous position, so playback
// stood still and then jumped a whole step. Each stream therefore gets its
// own timeline and is interpolated over its own intervals.
//
// The view is drawn slightly in the PAST, between two samples that have
// both already arrived, rather than guessing where the aircraft has got to
// since the last one. Guessing means correcting when the truth lands, and
// those corrections show up as sudden movements - the more so on a radio
// link, where packets do not arrive evenly.
//
// The cost is latency equal to the delay. For a view you fly by that would
// matter; for one you watch, smoothness is worth more.
var DELAY_MIN_MS = 150;
var DELAY_MAX_MS = 1600;
var DELAY_FACTOR = 1.6;      // of the average gap between samples
var avgPosIntervalMs = 350;
var avgAttIntervalMs = 250;
// Set from Python, which needs the same figure to draw the HUD overlay at
// the moment this view is showing. One owner, so the two cannot drift.
var overrideDelayMs = null;
var SAMPLE_HISTORY_MS = 6000;

// Rounds the corner where one pair of samples hands over to the next.
// Straight interpolation is continuous in position but not in direction,
// which shows as a slight kink each time; this takes it out.
var SMOOTH_TAU_MS = 60;

// Beyond this the aircraft didn't fly there - it's a reposition, or the
// first fix after connecting - so snap rather than sail across the map.
var SNAP_DEG = 0.01;    // roughly a kilometre

function nowMs() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
}

function shortestAngleDelta(from, to) {
    // Via the short way round, so 359 -> 1 turns 2 degrees, not -358.
    return ((to - from + 540) % 360) - 180;
}

function lerpAngle(from, to, u) {
    return from + shortestAngleDelta(from, to) * u;
}

function pushSample(buffer, sample, t) {
    sample.t = t;
    buffer.push(sample);
    var cutoff = t - SAMPLE_HISTORY_MS;
    while (buffer.length > 2 && buffer[0].t < cutoff) {
        buffer.shift();
    }
}

function updateInterval(buffer, current, t) {
    if (buffer.length === 0) return current;
    var gap = t - buffer[buffer.length - 1].t;
    // Ignore a duplicate arrival and anything after a long silence: neither
    // says what the normal rate is.
    if (gap > 20 && gap < 3000) {
        return current + (gap - current) * 0.2;
    }
    return current;
}

// Straight from GLOBAL_POSITION_INT.
function setAircraftPosition(lat, lon, altMsl, agl) {
    if (!ready || !viewer) return;
    var t = nowMs();
    avgPosIntervalMs = updateInterval(posSamples, avgPosIntervalMs, t);
    pushSample(posSamples, {lat: lat, lon: lon, alt: altMsl, agl: agl}, t);
    initPose();
}

// Straight from ATTITUDE. Cesium takes heading/pitch/roll natively, so no
// rotation maths is needed here.
function setAircraftAttitude(yawDeg, pitchDeg, rollDeg) {
    if (!ready || !viewer) return;
    var t = nowMs();
    avgAttIntervalMs = updateInterval(attSamples, avgAttIntervalMs, t);
    pushSample(attSamples, {yaw: yawDeg, pitch: pitchDeg, roll: rollDeg}, t);
    initPose();
}

function initPose() {
    if (pose !== null || posSamples.length === 0 || attSamples.length === 0) return;
    var p = posSamples[posSamples.length - 1];
    var a = attSamples[attSamples.length - 1];
    pose = {lat: p.lat, lon: p.lon, alt: p.alt, agl: p.agl,
            yaw: a.yaw, pitch: a.pitch, roll: a.roll};
    applyPose();
}

// One delay for both streams, set by the slower of the two. Delaying them
// independently would let the aircraft face a direction that belonged to a
// different moment than its position.
function setPlaybackDelay(ms) {
    overrideDelayMs = (typeof ms === 'number' && ms > 0) ? ms : null;
}

function playbackDelayMs() {
    if (overrideDelayMs !== null) return overrideDelayMs;
    var slowest = Math.max(avgPosIntervalMs, avgAttIntervalMs);
    return Math.max(DELAY_MIN_MS, Math.min(DELAY_MAX_MS, slowest * DELAY_FACTOR));
}

// The two samples either side of `renderTime`, blended. Both are real
// measurements, so nothing here is invented.
function interpolate(buffer, renderTime, blend) {
    if (buffer.length === 0) return null;
    if (buffer.length === 1) return buffer[0];
    if (renderTime <= buffer[0].t) return buffer[0];
    var last = buffer[buffer.length - 1];
    // Telemetry has stalled and playback has caught up with it. Hold on the
    // newest sample rather than carrying on past it into invention.
    if (renderTime >= last.t) return last;
    for (var i = buffer.length - 2; i >= 0; i--) {
        var a = buffer[i], b = buffer[i + 1];
        if (renderTime >= a.t && renderTime <= b.t) {
            var span = b.t - a.t;
            return blend(a, b, span > 0 ? (renderTime - a.t) / span : 1);
        }
    }
    return last;
}

function blendPosition(a, b, u) {
    return {
        lat: a.lat + (b.lat - a.lat) * u,
        lon: a.lon + (b.lon - a.lon) * u,
        alt: a.alt + (b.alt - a.alt) * u,
        agl: a.agl + (b.agl - a.agl) * u
    };
}

function blendAttitude(a, b, u) {
    return {
        yaw: lerpAngle(a.yaw, b.yaw, u),
        pitch: a.pitch + (b.pitch - a.pitch) * u,
        roll: lerpAngle(a.roll, b.roll, u)
    };
}

// The height to hand Cesium, in Cesium's own datum.
//
// The obvious value - the vehicle's altitude AMSL - is the wrong one.
// Cesium measures height from the WGS84 ellipsoid, MAVLink reports it from
// mean sea level, and the two differ by the geoid separation: about 28m at
// Samsun, 37m at Ankara, and something else again elsewhere. Feeding AMSL
// straight in buried the camera that far underground, so sitting on the
// runway you looked up at the underside of the terrain.
//
// Measuring up from Cesium's own terrain keeps everything in one datum and
// is right anywhere in the world, with no geoid model to ship.
function cameraHeight() {
    var ground;
    try {
        ground = viewer.scene.globe.getHeight(
            Cesium.Cartographic.fromDegrees(pose.lon, pose.lat));
    } catch (e) {
        ground = undefined;
    }
    if (typeof ground === 'number' && isFinite(ground)) {
        return ground + Math.max(pose.agl, MIN_EYE_M);
    }
    // Terrain for this spot hasn't streamed in yet. AMSL is off by the
    // geoid separation, but it is the only height we have until it does.
    return pose.alt;
}

function applyPose() {
    viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(pose.lon, pose.lat, cameraHeight()),
        orientation: {
            heading: Cesium.Math.toRadians(pose.yaw),
            pitch: Cesium.Math.toRadians(pose.pitch),
            roll: Cesium.Math.toRadians(pose.roll),
        },
    });
}

// One frame of playback. Both targets are already moving smoothly, so the
// easing here only rounds the corner where one pair of samples hands over
// to the next.
function stepCamera(dtMs) {
    if (pose === null) return;
    var renderTime = nowMs() - playbackDelayMs();
    var wantPos = interpolate(posSamples, renderTime, blendPosition);
    var wantAtt = interpolate(attSamples, renderTime, blendAttitude);
    if (wantPos === null || wantAtt === null) return;

    if (Math.abs(wantPos.lat - pose.lat) > SNAP_DEG ||
        Math.abs(wantPos.lon - pose.lon) > SNAP_DEG) {
        pose.lat = wantPos.lat; pose.lon = wantPos.lon;
        pose.alt = wantPos.alt; pose.agl = wantPos.agl;
        pose.yaw = wantAtt.yaw; pose.pitch = wantAtt.pitch; pose.roll = wantAtt.roll;
    } else {
        var k = 1 - Math.exp(-dtMs / SMOOTH_TAU_MS);
        pose.lat += (wantPos.lat - pose.lat) * k;
        pose.lon += (wantPos.lon - pose.lon) * k;
        pose.alt += (wantPos.alt - pose.alt) * k;
        pose.agl += (wantPos.agl - pose.agl) * k;
        pose.pitch += (wantAtt.pitch - pose.pitch) * k;
        pose.yaw += shortestAngleDelta(pose.yaw, wantAtt.yaw) * k;
        pose.roll += shortestAngleDelta(pose.roll, wantAtt.roll) * k;
    }
    applyPose();
}

function onPreUpdate() {
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    // First frame has no previous timestamp; a long stall (tab hidden,
    // scene paused) shouldn't cash in as one enormous step either.
    var dt = lastFrameMs ? Math.min(now - lastFrameMs, 250) : 16;
    lastFrameMs = now;
    stepCamera(dt);
}

function setHud(dataUri) {
    document.getElementById('hud').src = dataUri;
}

function setStatus(text) {
    if (failed) return;
    document.getElementById('status').textContent = text;
}

init();
</script>
</body>
</html>
"""


class FpvView(QWebEngineView):
    """3D forward view from the aircraft, rendered by Cesium."""

    def __init__(self, tile_proxy_port: int, token: str = "", parent=None):
        super().__init__(parent)
        # A QWebEngineView asks for 640x480 by default. Sharing a layout slot
        # with the HUD, that hint would be the one the slot adopts - which
        # enlarged the HUD and pushed a scrollbar onto the whole left column.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(0, 0)
        self._origin = f"http://127.0.0.1:{tile_proxy_port}"
        self.set_token(token)

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def set_token(self, token: str):
        """(Re)build the page. Cesium is configured with the token at
        startup, so changing it means reloading."""
        self._token = (token or "").strip()
        if not self._token:
            self.setHtml(NO_TOKEN_HTML, QUrl(self._origin + "/"))
            return
        html = (CESIUM_HTML
                .replace("%%BASE%%", self._origin + "/lib/cesium/")
                .replace("%%TOKEN%%", self._token))
        # Served from the proxy's own origin so Cesium's assets, workers and
        # the page itself are same-origin.
        self.setHtml(html, QUrl(self._origin + "/"))

    def set_position(self, lat: float, lon: float, alt_msl: float, agl: float):
        """One GLOBAL_POSITION_INT. Kept separate from attitude because the
        two arrive at different rates, and pairing them would repeat
        whichever was older."""
        if not self._token:
            return
        self.page().runJavaScript(
            f"setAircraftPosition({lat}, {lon}, {alt_msl}, {agl});"
        )

    def set_playback_delay(self, delay_ms: float):
        """How far behind the scene plays. Python owns it so the HUD drawn
        over this view can be rendered at the same moment."""
        if not self._token:
            return
        self.page().runJavaScript(f"setPlaybackDelay({float(delay_ms)});")

    def set_attitude(self, yaw_deg: float, pitch_deg: float, roll_deg: float):
        """One ATTITUDE."""
        if not self._token:
            return
        self.page().runJavaScript(
            f"setAircraftAttitude({yaw_deg}, {pitch_deg}, {roll_deg});"
        )

    def set_hud_image(self, data_uri: str):
        if not self._token:
            return
        self.page().runJavaScript(f"setHud({json.dumps(data_uri)});")

    def set_status(self, text: str):
        if not self._token:
            return
        self.page().runJavaScript(f"setStatus({json.dumps(text)});")
