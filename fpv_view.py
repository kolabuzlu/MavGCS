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
var DEFAULT_FOV = 60, MIN_FOV = 8, MAX_FOV = 90;
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
var pose = null;        // where the camera is now
var target = null;      // newest telemetry
var lastFrameMs = 0;

// Time constant of the glide. Long enough to smooth 250ms-apart samples,
// short enough that the view isn't noticeably behind the aircraft.
var SMOOTH_TAU_MS = 120;
// Beyond this the aircraft didn't fly there - it's a reposition, or the
// first fix after connecting - so snap rather than sail across the map.
var SNAP_DEG = 0.01;    // roughly a kilometre

function shortestAngleDelta(from, to) {
    // Via the short way round, so 359 -> 1 turns 2 degrees, not -358.
    return ((to - from + 540) % 360) - 180;
}

// Position and attitude straight from telemetry. Cesium takes
// heading/pitch/roll natively, so no rotation maths is needed here.
function setAircraft(lat, lon, altMsl, yawDeg, pitchDeg, rollDeg) {
    if (!ready || !viewer) return;
    target = {lat: lat, lon: lon, alt: altMsl,
              yaw: yawDeg, pitch: pitchDeg, roll: rollDeg};
    if (pose === null) {
        pose = {lat: lat, lon: lon, alt: altMsl,
                yaw: yawDeg, pitch: pitchDeg, roll: rollDeg};
        applyPose();
    }
}

function applyPose() {
    viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(pose.lon, pose.lat, pose.alt),
        orientation: {
            heading: Cesium.Math.toRadians(pose.yaw),
            pitch: Cesium.Math.toRadians(pose.pitch),
            roll: Cesium.Math.toRadians(pose.roll),
        },
    });
}

// One frame of easing towards the latest telemetry. Exponential, driven by
// elapsed time rather than a fixed step, so the speed of the glide doesn't
// change with the frame rate.
function stepCamera(dtMs) {
    if (pose === null || target === null) return;
    if (Math.abs(target.lat - pose.lat) > SNAP_DEG ||
        Math.abs(target.lon - pose.lon) > SNAP_DEG) {
        pose.lat = target.lat; pose.lon = target.lon; pose.alt = target.alt;
        pose.yaw = target.yaw; pose.pitch = target.pitch; pose.roll = target.roll;
    } else {
        var k = 1 - Math.exp(-dtMs / SMOOTH_TAU_MS);
        pose.lat += (target.lat - pose.lat) * k;
        pose.lon += (target.lon - pose.lon) * k;
        pose.alt += (target.alt - pose.alt) * k;
        pose.pitch += (target.pitch - pose.pitch) * k;
        pose.yaw += shortestAngleDelta(pose.yaw, target.yaw) * k;
        pose.roll += shortestAngleDelta(pose.roll, target.roll) * k;
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

    def set_aircraft(self, lat: float, lon: float, alt_msl: float,
                     yaw_deg: float, pitch_deg: float, roll_deg: float):
        # The no-token page defines none of these; calling into it would
        # just throw in the page console.
        if not self._token:
            return
        self.page().runJavaScript(
            f"setAircraft({lat}, {lon}, {alt_msl}, {yaw_deg}, {pitch_deg}, {roll_deg});"
        )

    def set_hud_image(self, data_uri: str):
        if not self._token:
            return
        self.page().runJavaScript(f"setHud({json.dumps(data_uri)});")

    def set_status(self, text: str):
        if not self._token:
            return
        self.page().runJavaScript(f"setStatus({json.dumps(text)});")
