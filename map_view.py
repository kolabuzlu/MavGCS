"""
Map display built on Leaflet.js, hosted inside a QWebEngineView, with a
QWebChannel bridge so a map click can call back into Python (used for the
"Fly to Here" guided-mode command).
"""

import json

from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtCore import QUrl, QObject, Signal, Slot



def _clear_browser_cache():
    """
    Drop the browser's HTTP cache at startup.

    Everything this page loads - Leaflet and every map tile - now comes
    from our own local proxy, which answers from disk in about a
    millisecond, so the browser cache buys very little here and can cost
    correctness: one stale entry keeps an area blank long after the tile
    is available again.

    Done unconditionally rather than once behind a marker file. A one-shot
    cleanup can be silently used up - a test run consumed it on this
    machine already - leaving the real users it was meant for uncleaned.
    """
    try:
        QWebEngineProfile.defaultProfile().clearHttpCache()
    except Exception:
        pass

LEAFLET_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<link rel="stylesheet" href="%%TILE_PROXY%%/lib/leaflet/leaflet.css" />
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; padding: 0; background:#222; }
  .fly-to-btn {
    margin-top: 4px; padding: 4px 10px; cursor: pointer;
    background: #2a6; color: white; border: none; border-radius: 4px;
  }
  #compass {
    /* Directly above the terrain radar (which is 200px tall at bottom:26px),
       same size and position so the two read as one stack of instruments.
       Deliberately more transparent than the radar: that one is a data
       display which needs its own ground, while this is a dial you read
       against the map underneath it. */
    position: absolute; bottom: 234px; right: 8px;
    width: 200px; height: 200px;
    background: rgba(30,30,30,0.50);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    z-index: 1000;
    overflow: hidden;
    font-family: sans-serif;
  }
  #compass svg { display: block; width: 100%; height: 100%; }
  #compass .cp-face { fill: rgba(0,0,0,0.22); }
  #compass .cp-rim { fill: none; stroke: rgba(255,255,255,0.18); stroke-width: 2; }
  #compass .cp-tick { stroke: rgba(255,255,255,0.55); stroke-width: 1.5; }
  #compass .cp-tick.major { stroke: #ffffff; stroke-width: 2.5; }
  #compass .cp-card {
    fill: #ffffff; font-size: 20px; font-weight: 700;
    text-anchor: middle; dominant-baseline: middle;
  }
  #compass .cp-card.cp-north { fill: #ff4d4d; }
  /* Two markers at the top of the dial. The white one is fixed: the card
     turns under it, so it always marks the nose. The amber one rides the
     card at the course over ground, so the gap between them IS the drift
     angle - it shows up as a separation you can see, rather than as the
     difference between two numbers you have to subtract. */
  #compass .cp-index { fill: #ffffff; }
  #compass .cp-track { fill: #ffc83d; }
  #compass .cp-course {
    fill: #ffa726; font-size: 13px; font-weight: 600;
    text-anchor: middle; dominant-baseline: middle;
  }
  #compass .cp-heading {
    fill: #ffffff; font-size: 25px; font-weight: 700;
    text-anchor: middle; dominant-baseline: middle;
  }
  #compass .cp-windtext {
    fill: #4fc3f7; font-size: 13px; font-weight: 600;
    text-anchor: middle; dominant-baseline: middle;
  }
  #compass #cp-wind { opacity: 0.78; }
  /* Home. Still ends short of the wind arrow's head, so when the two
     happen to point the same way you see two arrowheads at different radii
     rather than one muddled shape - though the margin is now only a few
     pixels. */
  #compass .cp-home-shaft { stroke: #6ee787; stroke-width: 4; stroke-linecap: round; }
  #compass .cp-home-head { fill: #6ee787; }
  #compass .cp-wind-shaft { stroke: #4fc3f7; stroke-width: 4; stroke-linecap: round; }
  #compass .cp-wind-head { fill: #4fc3f7; }
  #terrain-radar {
    position: absolute; bottom: 26px; right: 8px;
    width: 200px; height: 200px;
    background: rgba(30,30,30,0.75);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    z-index: 1000;
    overflow: hidden;
    font-family: sans-serif;
  }
  #terrain-radar svg { display: block; width: 100%; height: 100%; }
  #terrain-radar .tr-arc { fill: none; stroke: rgba(255,255,255,0.22); stroke-width: 1; }
  #terrain-radar .tr-edge { fill: none; stroke: rgba(255,255,255,0.28); stroke-width: 1; }
  #terrain-radar .tr-hdg { stroke: rgba(255,255,255,0.35); stroke-width: 1; stroke-dasharray: 4 3; }
  #terrain-radar .tr-dist-label { fill: #b8b8b8; }
  #terrain-radar .tr-uav-ring { fill: rgba(55,168,219,0.25); stroke: #fff; stroke-width: 2; }
  #terrain-radar .tr-uav-dot { fill: #fff; stroke: #1a1a1a; stroke-width: 1; }
  #terrain-radar .tr-mode {
    position: absolute; top: 4px; padding: 2px 8px; font-size: 12px; font-weight: 700;
    letter-spacing: 0.05em; color: #37a8db; background: rgba(0,0,0,0.4);
    border: 1px solid rgba(55,168,219,0.4); border-radius: 6px; cursor: pointer;
    font-family: sans-serif;
  }
  #terrain-radar .tr-mode.left { left: 4px; }
  #terrain-radar .tr-mode.right { right: 4px; }
  /* The scale control is a real text input so the value can be typed, but
     it should read as the same chip the mode button on the right is. */
  #terrain-radar input.tr-mode {
    width: 46px; text-align: center; cursor: text; outline: none;
  }
  #terrain-radar input.tr-mode:focus {
    background: rgba(0,0,0,0.75); border-color: #37a8db; color: #ffffff;
  }
  /* Sits to the right of the Follow UAV / ADS-B toggles, spanning the
     same vertical band, rather than stacking underneath them. */
  /* Sits directly under Leaflet's own +/- buttons, matching their width so
     it reads as part of that control. Useful when caching for offline use:
     the cache is per zoom level, so it tells you which one you're filling. */
  #zoom-indicator {
    position: absolute; top: 76px; left: 10px; width: 30px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 3px 0; border-radius: 4px;
    font-family: sans-serif; font-size: 11px; font-weight: 700;
    text-align: center; z-index: 1000; user-select: none;
  }
  /* Map and Terrain side by side rather than stacked. */
  #tilecache-control .tc-cols { display: flex; gap: 10px; }
  #tilecache-control .tc-col { flex: 1; min-width: 0; }
  #tilecache-control .tc-colsep {
    padding-left: 10px;
    border-left: 1px solid rgba(255,255,255,0.15);
  }
  #tilecache-control {
    position: absolute; top: 10px; left: 166px; width: 430px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 5px 8px 6px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px; z-index: 1000;
  }
  #tilecache-control .tc-row {
    display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
  }
  #tilecache-control .tc-title { flex: 1; user-select: none; }
  #tilecache-control select {
    background: #1e1e1e; color: #fff; border: 1px solid rgba(255,255,255,0.25);
    border-radius: 3px; font-size: 11px; padding: 1px 2px; cursor: pointer;
  }
  #tilecache-control .tc-bar {
    flex: 1; height: 6px; background: rgba(255,255,255,0.15);
    border-radius: 3px; overflow: hidden;
  }
  #tilecache-control .tc-bar > div {
    height: 100%; width: 0%; background: #37a8db; border-radius: 3px;
  }
  #tilecache-control button {
    background: rgba(255,255,255,0.12); color: #fff;
    border: 1px solid rgba(255,255,255,0.25); border-radius: 3px;
    font-size: 10px; padding: 1px 7px; cursor: pointer; font-family: sans-serif;
  }
  #tilecache-control button:hover { background: rgba(255,255,255,0.22); }
  #tilecache-control .tc-text {
    font-size: 10px; color: #b8c0c6; white-space: nowrap;
  }
  .adsb-icon { background: none; border: none; }
  /* Altitude above each waypoint. Absolutely positioned so the icon box
     stays 22x22 and the circle keeps sitting on the exact coordinate. */
  .waypoint-icon .wp-alt-label {
    position: absolute; bottom: 24px; left: 50%; transform: translateX(-50%);
    white-space: nowrap; font-family: sans-serif; font-size: 10px;
    font-weight: 700; color: #ffffff;
    text-shadow: 0 0 3px #000, 0 0 3px #000, 0 0 3px #000;
    pointer-events: none;
  }
  .adsb-icon .adsb-label {
    position: absolute; top: 34px; left: 50%; transform: translateX(-50%);
    white-space: nowrap; font-family: sans-serif; font-size: 10px;
    font-weight: 700; color: #ffffff;
    text-shadow: 0 0 3px #000, 0 0 3px #000, 0 0 3px #000;
    pointer-events: none;
  }
  .adsb-tip {
    background: rgba(255,255,255,0.95); color: #111;
    border: none; border-radius: 8px; padding: 4px 9px;
    font-family: sans-serif; font-size: 11px; font-weight: 600;
    box-shadow: 0 2px 6px rgba(0,0,0,0.4);
  }
  .adsb-tip::before { border-top-color: rgba(255,255,255,0.95); }
  #terrain-radar .tr-placeholder {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    color: #888; font-size: 11px; text-align: center; padding: 0 8px;
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="follow-control" style="
    position: absolute; top: 10px; left: 46px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 4px 8px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px;
    z-index: 1000; display: flex; align-items: center; gap: 4px;
">
    <input type="checkbox" id="follow-checkbox" checked
           onchange="followDrone = this.checked;"
           style="margin: 0; cursor: pointer;">
    <label for="follow-checkbox" style="cursor: pointer; user-select: none;">Follow UAV</label>
</div>
<div id="zoom-indicator">Z--</div>
<div id="adsb-control" style="
    position: absolute; top: 42px; left: 46px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 4px 8px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px;
    z-index: 1000; display: flex; align-items: center; gap: 4px;
">
    <input type="checkbox" id="adsb-checkbox"
           onchange="setAdsbEnabled(this.checked);"
           style="margin: 0; cursor: pointer;">
    <label for="adsb-checkbox" style="cursor: pointer; user-select: none;">ADS-B</label>
</div>
<div id="weather-control" style="
    position: absolute; top: 74px; left: 46px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 4px 8px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px;
    z-index: 1000; display: flex; align-items: center; gap: 4px;
">
    <input type="checkbox" id="weather-checkbox"
           onchange="setWeatherEnabled(this.checked);"
           style="margin: 0; cursor: pointer;">
    <label for="weather-checkbox" style="cursor: pointer; user-select: none;">Weather</label>
</div>
<div id="trail-control" style="
    /* Just past the map/terrain cache panel, which runs from 166px to
       612px (430 wide plus its 8px padding either side). The layer
       selector is anchored to the map's right edge, so this sits in the
       gap between the two. */
    position: absolute; top: 10px; left: 622px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 4px 8px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px;
    z-index: 1000; display: flex; align-items: center; gap: 4px;
">
    <button onclick="clearTrail();" title="Erase the red flight trail"
            style="background: rgba(255,255,255,0.12); color: #fff;
                   border: 1px solid rgba(255,255,255,0.25); border-radius: 3px;
                   font-family: sans-serif; font-size: 11px; padding: 1px 6px;
                   cursor: pointer;">Clear Trail</button>
    <!-- Sits in the same flex row as the button rather than at its own
         absolute position, so it stays put if the button's width changes. -->
    <input type="checkbox" id="vectors-checkbox" checked
           onchange="setVectorsEnabled(this.checked);"
           style="margin: 0 0 0 8px; cursor: pointer;">
    <label for="vectors-checkbox" style="cursor: pointer; user-select: none;"
           title="Ground track, nose heading, bearing to waypoint and the current turn"
           >Vectors</label>
</div>
<div id="tilecache-control">
  <div class="tc-cols">
    <div class="tc-col">
        <div class="tc-row">
            <span class="tc-title">Map</span>
            <select id="tc-limit" onchange="setTileCacheLimit(this.value);">
                <!-- 500 MB by default, matching what the app applies at
                     startup. Caching map tiles is what makes a previously
                     flown area work with no internet at the field, and half
                     a gigabyte is a few hundred square kilometres. The app
                     overwrites this selection with the saved preference as
                     soon as the page loads; the value here only has to
                     agree with that default so the two never disagree. -->
                <option value="0">No Cache</option>
                <option value="100">100 MB</option>
                <option value="200">200 MB</option>
                <option value="500" selected>500 MB</option>
                <option value="1024">1 GB</option>
                <option value="2048">2 GB</option>
                <option value="5120">5 GB</option>
            </select>
        </div>
        <div class="tc-row">
            <div class="tc-bar"><div id="tc-fill"></div></div>
            <button onclick="clearTileCache();">Clear</button>
        </div>
        <div id="tc-text" class="tc-text">&nbsp;</div>
    </div>

    <div class="tc-col tc-colsep">
        <div class="tc-row">
            <span class="tc-title">Terrain</span>
            <!-- Bigger steps than the map: one elevation tile is ~40MB, and
                 it defaults to caching because a terrain radar that
                 re-downloaded 40MB per fix would be unusable. -->
            <select id="tr-limit" onchange="setTerrainCacheLimit(this.value);">
                <option value="0">No Cache</option>
                <option value="512">500 MB</option>
                <option value="1024">1 GB</option>
                <option value="2048" selected>2 GB</option>
                <option value="5120">5 GB</option>
                <option value="10240">10 GB</option>
            </select>
        </div>
        <div class="tc-row">
            <div class="tc-bar"><div id="tr-fill"></div></div>
            <button onclick="clearTerrainCache();">Clear</button>
        </div>
        <div id="tr-text" class="tc-text">&nbsp;</div>
    </div>
  </div>
</div>
<div id="cog-readout" style="
    /* Directly above the credit line, same styling, so the two read as one
       corner rather than two competing labels. */
    position: absolute; bottom: 40px; left: 8px;
    background: rgba(0,0,0,0.6);
    padding: 4px 6px 3px 6px; border-radius: 4px;
    font-family: sans-serif;
    /* Fixed to the widest caption the app can produce, measured at this
       font: Slightly nose heavy (+100us) comes to 129px. The gauge must
       not resize as the verdict changes - a box that grows and shrinks
       in the corner of the map is a distraction, and the aeroplane
       inside would shift with it. No quote marks in here: this is a
       double-quoted HTML attribute, and one would end it early and
       silently drop everything after it. */
    width: 134px;
    z-index: 1000; pointer-events: none; display: none;
">
    <!-- Rendered at 90% of the space it is drawn in, and cropped to the
         aeroplane: the coordinates below, and the marker's travel, stay
         as written while the black behind them stops at the wingtips. -->
    <svg id="cog-svg" width="108" height="39.6" viewBox="16 0 120 44"
         style="display: block; margin: 0 auto;">
        <!-- Plan view, nose to the left, so fore and aft run the way the
             marker slides. Drawn once; only the marker moves. -->
        <g id="cog-plane" fill="#8f9aa3">
            <!-- Blunt nose at the left, tapering to a point at the tail.
                 Which end is the nose IS the meaning here, so the two ends
                 must never look alike. -->
            <path d="M17.8,22 Q17.8,15.5 37.6,15.5 L113.2,18.5 L133,22
                     L113.2,25.5 L37.6,28.5 Q17.8,28.5 17.8,22 Z"/>
            <!-- Main wing, swept back, centred on the neutral line. -->
            <path d="M57.4,17 L82.6,17 L91.6,2 L77.2,2 Z"/>
            <path d="M57.4,27 L82.6,27 L91.6,42 L77.2,42 Z"/>
            <!-- Tailplane: clearly the smaller pair, and well aft. -->
            <path d="M107.8,19 L120.4,19 L125.8,10 L116.8,10 Z"/>
            <path d="M107.8,25 L120.4,25 L125.8,34 L116.8,34 Z"/>
            <!-- The fin, edge on from above - a last cue for the tail. -->
            <path d="M102.4,20.4 L131.2,21.5 L131.2,22.5 L102.4,23.6 Z"
                  fill="#c3ced6"/>
        </g>
        <!-- Where a balanced aircraft would sit, so the marker has
             something to be displaced from. -->
        <line id="cog-neutral" x1="70" y1="6" x2="70" y2="38"
              stroke="#3dff85" stroke-width="1.4"
              stroke-dasharray="3 2.5" opacity="0.95"/>
        <!-- The surveyor's centre-of-gravity symbol: a quartered circle.
             Recognisable to anyone who has balanced an aeroplane. -->
        <g id="cog-marker">
            <circle cx="70" cy="22" r="6.5" fill="#ffffff"
                    stroke="#111" stroke-width="1"/>
            <path d="M70,15.5 A6.5,6.5 0 0,1 76.5,22 L70,22 Z" fill="#111"/>
            <path d="M70,28.5 A6.5,6.5 0 0,1 63.5,22 L70,22 Z" fill="#111"/>
        </g>
    </svg>
    <!-- One line, always. nowrap keeps the longest verdict on a single
         row; the ellipsis is a backstop so that even an unforeseen
         caption truncates instead of resizing the gauge. -->
    <div id="cog-label" style="font-size: 10px; color: #cfd8e0;
         text-align: center; margin-top: 1px; line-height: 1.15;
         white-space: nowrap; overflow: hidden;
         text-overflow: ellipsis;"></div>
</div>
<div id="credit" style="
    position: absolute; bottom: 8px; left: 8px;
    background: rgba(0,0,0,0.6); color: white;
    padding: 4px 10px; border-radius: 4px;
    font-family: sans-serif; font-size: 11px;
    z-index: 1000; pointer-events: none;
">Created by Derin Hakan Karakurt</div>
<div id="compass" title="Heading (white), course over ground (orange), wind (blue)">
    <svg id="cp-svg" viewBox="0 0 200 200">
        <circle class="cp-face" cx="100" cy="100" r="94" />
        <circle class="cp-rim" cx="100" cy="100" r="94" />
        <!-- Everything inside this group turns with the aircraft, so the
             card is heading-up: what is at the top is straight ahead. -->
        <g id="cp-rose">
            <g id="cp-ticks"></g>
            <!-- Points the way the wind is blowing TO. Drawn pointing up so
                 a rotation of B aims it at bearing B. Placed before the
                 cardinals so that where it reaches out far enough to cross
                 them, they paint over it and stay the readable thing. -->
            <g id="cp-wind" style="display:none">
                <line class="cp-wind-shaft" x1="100" y1="72" x2="100" y2="42" />
                <path class="cp-wind-head" d="M100,28 L91,46 L100,41 L109,46 Z" />
            </g>
            <g id="cp-home" style="display:none">
                <line class="cp-home-shaft" x1="100" y1="66" x2="100" y2="46" />
                <path class="cp-home-head" d="M100,34 L92,50 L100,45 L108,50 Z" />
            </g>
            <text class="cp-card cp-north" x="100" y="34">N</text>
            <text class="cp-card" x="166" y="100">E</text>
            <text class="cp-card" x="100" y="166">S</text>
            <text class="cp-card" x="34" y="100">W</text>
            <!-- Course over ground, riding the card at its own bearing. -->
            <path class="cp-track" id="cp-trackmark" d="M100,28 L91,6 L109,6 Z"
                  style="display:none" />
        </g>
        <path class="cp-index" d="M100,4 L94,22 L106,22 Z" />
        <text class="cp-course" id="cp-course" x="100" y="79">---</text>
        <text class="cp-heading" id="cp-heading" x="100" y="103">---</text>
        <text class="cp-windtext" id="cp-windtext" x="100" y="134">--</text>
    </svg>
</div>
<div id="terrain-radar">
    <svg id="tr-svg" viewBox="0 0 200 200" style="display:none;">
        <defs>
            <clipPath id="tr-fan-clip"><path id="tr-fan-sector" d="" /></clipPath>
        </defs>
        <g id="tr-cells" clip-path="url(#tr-fan-clip)" opacity="0.85"></g>
        <g id="tr-arcs"></g>
        <path id="tr-edges" class="tr-edge" d="" />
        <line id="tr-hdg-line" class="tr-hdg" x1="100" y1="0" x2="100" y2="0" />
        <g id="tr-labels"></g>
        <circle id="tr-uav-ring" class="tr-uav-ring" cx="100" cy="191" r="6" />
        <circle id="tr-uav-dot" class="tr-uav-dot" cx="100" cy="191" r="3" />
    </svg>
    <div id="tr-placeholder" class="tr-placeholder">Terrain Radar - no data</div>
    <input class="tr-mode left" id="tr-scale-input" type="text" value="120m"
           title="Clearance colour scale - type a value in metres and press Enter">
    <button class="tr-mode right" onclick="toggleTerrainMode()" id="tr-mode-btn">REL</button>
</div>
<script>
// Substituted by MapView with the local tile proxy's address (see
// tile_cache.py) - the map's layers are all served through it.
var TILE_PROXY = '%%TILE_PROXY%%';

// Set up before Leaflet loads and stays independent of it, so a map/tile
// failure can't also take down the fly-to bridge.
var bridge = null;
new QWebChannel(qt.webChannelTransport, function(channel) {
    bridge = channel.objects.bridge;
});

function flyToHere(lat, lon) {
    if (bridge) {
        bridge.flyToHere(lat, lon);
    }
}

// Each waypoint gets a stable id so an altitude edit can name exactly
// which point it applies to, whichever batch it belongs to.
var wpSeq = 0;
// Mission default, echoed back from Python once a mission has been sent,
// so a point with no altitude of its own shows the value it will actually fly.
var wpDefaultAlt = null;

function setWaypointDefaultAlt(alt) {
    wpDefaultAlt = alt;
    // Points with no altitude of their own now have a number to show.
    refreshWaypointIcons();
}

// The altitude a waypoint will actually be flown at: its own if it has
// one, otherwise the mission default. Blank until either is known.
function wpAltText(m) {
    var a = (m._wpAlt !== null && m._wpAlt !== undefined) ? m._wpAlt : wpDefaultAlt;
    if (a === null || a === undefined) return '';
    return Math.round(a) + 'm' + (wpIsDirty(m) ? ' *' : '');
}

// An altitude edited since the vehicle last accepted this mission. Until
// Update is pressed the aircraft will still fly the old figure, so the map
// must not show the new one as though it were live.
function wpIsDirty(m) {
    return !!m._wpSent
        && m._wpSentAlt !== undefined
        && m._wpAlt !== m._wpSentAlt;
}

// Called once the VEHICLE has acknowledged the mission - not when we press
// send, so a failed or lost upload keeps showing as pending.
function markMissionSent() {
    for (var i = 0; i < sentLayers.length; i++) {
        var m = sentLayers[i];
        if (m && m._wpId) { m._wpSentAlt = m._wpAlt; }
    }
    refreshWaypointIcons();
}

function refreshWaypointIcons() {
    for (var i = 0; i < allWaypointLayers.length; i++) {
        var m = allWaypointLayers[i];
        if (m && m._wpId) {
            m.setIcon(waypointIcon(m._wpNum, m._wpSent, wpAltText(m), wpIsDirty(m)));
        }
    }
}

function wpPopupHtml(m) {
    var own = (m._wpAlt !== null && m._wpAlt !== undefined);
    var shown = own ? m._wpAlt : (wpDefaultAlt !== null ? wpDefaultAlt : '');
    var hint = own ? '' :
        '<div style="font-size:10px;color:#aaa">mission default</div>';
    if (wpIsDirty(m)) {
        hint = '<div style="font-size:10px;color:#ffc107">not sent yet - ' +
               'press Update</div>';
    }
    return '<div style="text-align:center;min-width:130px">' +
           '<b>Waypoint ' + m._wpNum + '</b>' +
           '<div style="margin:4px 0">Altitude (m)</div>' +
           '<input id="wp-alt-input" type="text" value="' + shown + '" ' +
           'style="width:70px;text-align:center" ' +
           'onkeydown="if(event.key===&quot;Enter&quot;){applyWaypointAlt(' +
           m._wpId + ');}">' + hint +
           '<div style="margin-top:6px">' +
           '<button class="fly-to-btn" onclick="applyWaypointAlt(' + m._wpId +
           ')">Apply</button></div></div>';
}

// Applied to the marker and reported to Python, which owns the mission.
// Nothing reaches the vehicle until Update Mission is pressed.
function applyWaypointAlt(id) {
    var el = document.getElementById('wp-alt-input');
    if (!el) return;
    var v = parseFloat(String(el.value).replace(/[^0-9.]/g, ''));
    if (!isFinite(v)) return;
    for (var i = 0; i < allWaypointLayers.length; i++) {
        var m = allWaypointLayers[i];
        if (m && m._wpId === id) {
            m._wpAlt = v;
            m.setIcon(waypointIcon(m._wpNum, m._wpSent, wpAltText(m), wpIsDirty(m)));
            if (bridge) { bridge.waypointAltChanged(id, v); }
            map.closePopup();
            break;
        }
    }
}

function waypointAdded(lat, lon, id) {
    if (bridge) {
        bridge.waypointAdded(lat, lon, id);
    }
}
</script>

<script src="%%TILE_PROXY%%/lib/leaflet/leaflet.js"></script>
<script>
// Zoom is deliberately restricted to Z9-Z18. Levels outside that range
// can't be reached, so nothing outside it is ever requested or cached
// either. The proxy enforces the same range independently (see
// tile_cache.py), so a stray request can't slip tiles into the cache.
var MIN_ZOOM = 9, MAX_ZOOM = 18;
var map = L.map('map', {
    minZoom: MIN_ZOOM,
    maxZoom: MAX_ZOOM,
});

// Where the map opens before any telemetry arrives. Once the vehicle
// reports its first fix, updatePosition() recentres on it (see
// haveCentered), so this is purely the starting view.
var HOME_LAT = 39.925386148184316, HOME_LON = 32.83652351127223;
var HOME_ZOOM = 16;
map.setView([HOME_LAT, HOME_LON], HOME_ZOOM);

// Google's hybrid (satellite + roads/labels) tiles, unauthenticated -
// no API key needed, same tile source Mission Planner uses for its
// satellite view. lyrs=y is hybrid; lyrs=s is satellite-only.
// Every layer is fetched through MavGCS's own local tile proxy (see
// tile_cache.py) rather than straight from the provider. The proxy serves
// any tile it has already saved from disk, which is what makes a
// previously-viewed area work with no internet at all; TILE_PROXY is
// substituted with its port when this page is loaded.
var TILE_URL = TILE_PROXY + '/t/';
// Shown in place of a tile that couldn't be fetched. The proxy answers 404
// for those, so Leaflet records the tile as FAILED (unlike a 200 blank,
// which it would keep forever as a successful load) and asks again on the
// next redraw - while this transparent pixel keeps the map looking clean.
// Genuinely transparent (RGBA 0,0,0,0) and verified as such - an earlier
// hand-written blob here decoded to semi-transparent BLUE, which painted
// every failed tile blue instead of leaving the background showing.
var TILE_ERROR_IMG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB' +
    'CAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==';

var googleHybrid = L.tileLayer(
    TILE_URL + 'google/{z}/{x}/{y}',
    {
        maxZoom: MAX_ZOOM,
        errorTileUrl: TILE_ERROR_IMG,
        attribution: 'Imagery &copy; Google',
    }
);

var osmStreets = L.tileLayer(TILE_URL + 'osm/{z}/{x}/{y}', {
    maxZoom: MAX_ZOOM,
    errorTileUrl: TILE_ERROR_IMG,
    attribution: '&copy; OpenStreetMap contributors',
});

// ESRI World Imagery, unauthenticated (no API key) - satellite-only, no
// road/label overlay unlike Google Hybrid above.
var esriWorldImagery = L.tileLayer(
    TILE_URL + 'esri/{z}/{x}/{y}',
    {
        maxZoom: MAX_ZOOM,
        errorTileUrl: TILE_ERROR_IMG,
        attribution: 'Imagery &copy; Esri',
    }
);

// ESRI's own "Imagery Hybrid" basemap is actually two stacked services -
// World_Imagery underneath, with this reference layer (roads/labels/
// boundaries, transparent PNG tiles) drawn on top. Grouped together so
// it behaves as a single selectable layer, like Google Hybrid does.
var esriHybridLabels = L.tileLayer(
    TILE_URL + 'esriref/{z}/{x}/{y}',
    {
        maxZoom: MAX_ZOOM,
        errorTileUrl: TILE_ERROR_IMG,
        attribution: 'Imagery &copy; Esri',
    }
);
var esriWorldImageryHybrid = L.layerGroup([esriWorldImagery, esriHybridLabels]);

// ESRI World Imagery is the layer shown on startup (satellite only - the
// hybrid variant adds the roads/labels overlay).
esriWorldImagery.addTo(map);
L.control.layers(
    {
        'Google Hybrid': googleHybrid,
        'OpenStreetMap': osmStreets,
        'ESRI World Imagery': esriWorldImagery,
        'ESRI World Imagery Hybrid': esriWorldImageryHybrid,
    },
    {},
    { position: 'topright' }
).addTo(map);

var DRONE_ICON_W = 92, DRONE_ICON_H = 78;  // 722x605 source, 2x the original ~39px-tall icon size
var droneIcon = L.divIcon({
    className: 'drone-icon',
    html: '<div style="width:' + DRONE_ICON_W + 'px;height:' + DRONE_ICON_H + 'px;">' +
          '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAtIAAAJdCAYAAAAfjJCnAAAAAXNSR0IB2cksfwAAAARnQU1BAACxjwv8YQUAAAAgY0hSTQAAeiYAAICEAAD6AAAAgOgAAHUwAADqYAAAOpgAABdwnLpRPAAAAAlwSFlzAAASdAAAEnQB3mYfeAAAAAd0SU1FB+oIHAgVM9fC5xgAACAASURBVHja7N15cFzXfSf67zl36UY30N1oNBpbEwQpiaRoarMl2ZFlJc4kzp7YcRyPkziZeYniem/+eFPzXlVm3ktN1bzJZOLEdixPYkemlZmKMxXLihd57CS241WWKImiVooSNxAEsaPR+3LXc94fICVR4gI0AXQ39P1U8Q/LAPr2ueee+72/e+65ABERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERER0ZuXYBMQEXWW0pNPnWmem8kFlaoZ1moAALO3F0aiL+gZG5lJvePtu9hKREQM0kREBCD/rX/+fPHJI+ON05PpoFjMBdVaQnmeVJ4P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" ' +
          'style="width:100%;height:100%;display:block;" /></div>',
    iconSize: [DRONE_ICON_W, DRONE_ICON_H],
    iconAnchor: [DRONE_ICON_W / 2, DRONE_ICON_H / 2]
});

// Tiles that failed (offline, or the provider hiccupped) stay failed until
// something asks for them again. Leaflet tells us via 'tileerror'; we then
// keep retrying periodically until a pass produces no further errors.
//
// Not driven by the browser's 'online' event alone: that depends on
// Chromium noticing the network state change, which it doesn't always do
// promptly - so the timer is the dependable path and 'online' just makes it
// react faster when it does fire.
function updateZoomIndicator() {
    var el = document.getElementById('zoom-indicator');
    if (el) el.textContent = 'Z' + map.getZoom();
}
map.on('zoomend', updateZoomIndicator);
updateZoomIndicator();

var tilesFailed = false;
var TILE_RETRY_MS = 15000;

function retryFailedTiles() {
    if (!tilesFailed) return;
    tilesFailed = false;   // set again by tileerror if they fail once more
    [googleHybrid, osmStreets, esriWorldImagery, esriHybridLabels].forEach(function (l) {
        // redraw() on a layer that isn't currently displayed is a no-op.
        try { l.redraw(); } catch (e) {}
    });
}

[googleHybrid, osmStreets, esriWorldImagery, esriHybridLabels].forEach(function (l) {
    l.on('tileerror', function () { tilesFailed = true; });
});
window.addEventListener('online', retryFailedTiles);
setInterval(retryFailedTiles, TILE_RETRY_MS);

var marker = null;
var path = L.polyline([], {color: 'red', weight: 2}).addTo(map);
// Every position update redraws the whole polyline, so an uncapped trail
// costs more per update the longer you fly - measured at 0.12ms per update
// at 1000 points and 3.27ms at 20000, climbing roughly linearly, all of it
// on the GUI thread.
var TRAIL_MAX_POINTS = 8000;      // about 45 minutes at ArduPilot's 3Hz

function addTrailPoint(latlng) {
    path.addLatLng(latlng);
    var pts = path.getLatLngs();
    if (pts.length <= TRAIL_MAX_POINTS) return;
    // Halve the resolution of the older half rather than discarding it.
    // Dropping the oldest points would erase where you have been, which is
    // what the trail is for; thinning keeps the whole shape of the route
    // and gives up only fine detail on the parts you flew longest ago.
    var half = Math.floor(pts.length / 2);
    var thinned = [];
    for (var i = 0; i < half; i += 2) { thinned.push(pts[i]); }
    path.setLatLngs(thinned.concat(pts.slice(half)));
}
var followDrone = true;
var haveCentered = false;
var targetMarker = null;

// Waypoint queue mode - clicking the map adds numbered points to a
// sequence instead of showing the single Fly to Here popup.
var waypointMode = false;
var waypointMarkers = [];  // markers in the CURRENT (uncommitted) queue
var waypointLine = L.polyline([], {color: '#3af', weight: 2, dashArray: '6,6'}).addTo(map);
// Everything ever added (current queue + previously committed/sent
// batches) - this is what Clear actually removes. Committing a queue
// (see commitWaypoints) keeps items here but out of waypointMarkers, so
// a new queue can start fresh without touching what was already sent.
var allWaypointLayers = [waypointLine];
// The batch currently sitting on the vehicle. Sending a new mission
// replaces it, on the map as well as on the aircraft.
var sentLayers = [];

// Where the vehicle will return to. Drawn beneath the aircraft and the
// waypoints - it is a reference point, not something you interact with, and
// it must never hide the thing you are actually watching.
var homeMarker = null;
var homeIcon = L.divIcon({
    className: 'home-icon',
    html: '<svg width="34" height="34" viewBox="0 0 28 28">' +
          '<circle cx="14" cy="14" r="12" fill="rgba(20,20,20,0.72)" ' +
          'stroke="#4caf50" stroke-width="2"/>' +
          '<path d="M14 6 L22 13.5 L6 13.5 Z" fill="#ffffff"/>' +
          '<rect x="8.5" y="13.5" width="11" height="7" fill="#ffffff"/>' +
          '<rect x="12.2" y="16" width="3.6" height="4.5" fill="#4caf50"/>' +
          '</svg>',
    iconSize: [34, 34],
    iconAnchor: [17, 17]
});

function setHome(lat, lon) {
    var ll = [lat, lon];
    if (homeMarker === null) {
        homeMarker = L.marker(ll, {icon: homeIcon, zIndexOffset: -500,
                                   interactive: true}).addTo(map);
    } else {
        homeMarker.setLatLng(ll);
    }
    homeMarker.bindTooltip('Home ' + lat.toFixed(6) + ', ' + lon.toFixed(6),
                           {direction: 'top', offset: [0, -14]});
}

function clearHome() {
    if (homeMarker !== null) {
        map.removeLayer(homeMarker);
        homeMarker = null;
    }
}

// `sent` draws the muted version used for a mission already uploaded.
// Numbering restarts at 1 for each mission because that is what the
// vehicle receives - so without a visual difference a map holding two
// batches shows two markers labelled "1" and no way to tell them apart.
function waypointIcon(number, sent, altText, dirty) {
    var fill   = sent ? '#5b6b78' : '#3af';
    var text   = sent ? '#cfd8e0' : 'white';
    var border = sent ? 'rgba(255,255,255,0.55)' : 'white';
    var label  = altText
        ? '<div class="wp-alt-label"' +
          (dirty ? ' style="color:#ffc107"' : '') + '>' + altText + '</div>'
        : '';
    return L.divIcon({
        className: 'waypoint-icon',
        html: label +
              '<div style="width:22px;height:22px;border-radius:50%;' +
              'background:' + fill + ';color:' + text + ';font-family:sans-serif;' +
              'font-size:12px;font-weight:bold;display:flex;' +
              'align-items:center;justify-content:center;' +
              'border:2px solid ' + border + ';">' + number + '</div>',
        iconSize: [22, 22],
        iconAnchor: [11, 11],
    });
}


// Real telemetry position updates arrive at only ~2-3 Hz, which looks
// jumpy on a map. Instead of snapping the marker directly to each new
// fix, interpolate smoothly between the last two real fixes over a
// fixed duration using requestAnimationFrame - the same technique flight
// trackers use to animate aircraft between ADS-B updates.
var animFrom = null, animTo = null, animStartTime = 0;
// The marker itself is interpolated every frame - it is the thing being
// watched, and it should glide. The overlays hanging off it are a different
// matter: four polylines (one of up to 49 points) plus the compass rose,
// all rewritten 60 times a second to show quantities that evolve over
// seconds. That is a great deal of continuous compositing for no visible
// gain, and on a machine whose driver is unhappy with sustained GPU work it
// is enough to take the browser engine down. 20Hz is indistinguishable here
// and costs a third as much.
var OVERLAY_INTERVAL_MS = 50;
var lastOverlayMs = 0;
// Set once the overlays have been drawn at the end of an animation leg,
// and cleared by anything that changes what they show. Without it the
// "draw the final frame exactly" rule fired on EVERY frame once a leg had
// finished - so with telemetry stopped, the cap was defeated and the map
// redrew flat out for as long as the app stayed open, which is the very
// load this throttle exists to avoid.
var overlaysSettled = false;

// Every entry point that changes what the overlays display calls this, so
// a change with no accompanying position update still gets drawn.
function overlaysNeedRedraw() { overlaysSettled = false; }
var animDuration = 450;  // ms - tuned to sit comfortably above a 2-3 Hz update interval
var animHeadingFrom = 0, animHeadingTo = 0, currentHeading = 0;

function updatePosition(lat, lon, heading) {
    var latlng = [lat, lon];

    if (marker === null) {
        marker = L.marker(latlng, {icon: droneIcon}).addTo(map);
        animFrom = latlng;
        animHeadingFrom = heading;
    } else {
        // Start the new interpolation leg from wherever the marker
        // actually is right now (which may still be mid-animation
        // toward the previous target) rather than assuming it already
        // reached animTo - keeps motion smooth even if updates arrive
        // at an uneven rate.
        var current = marker.getLatLng();
        animFrom = [current.lat, current.lng];
        animHeadingFrom = currentHeading;
    }
    animTo = latlng;
    animHeadingTo = heading;
    animStartTime = performance.now();
    overlaysNeedRedraw();

    addTrailPoint(latlng);
    if (weatherEnabled) { updateWeatherClip(); }

    if (!haveCentered) {
        // Only take over the view if Follow UAV is on. With it off the user
        // has deliberately chosen where to look, and the first fix is
        // exactly when they are most likely to have arranged it - planning
        // while waiting for GPS lock.
        //
        // The zoom is left alone too. This used to force zoom 17, which
        // threw away a deliberate wider view even for someone who did want
        // to follow the aircraft.
        if (followDrone) {
            map.setView(latlng, map.getZoom());
        }
        // Set either way: _animateMarker gates its panning on this, so
        // ticking Follow UAV later still starts tracking immediately.
        haveCentered = true;
    }
    // Panning while following now happens every animation frame (see
    // _animateMarker below), matching the marker's smooth interpolated
    // motion - not here, which only fired once per real telemetry
    // update (~2-3 Hz) and looked jerky compared to the marker itself.
}

// ---- vectors attached to the aircraft --------------------------------
// Every one of these is drawn from telemetry the link already receives -
// ground velocity out of GLOBAL_POSITION_INT, the bearings out of
// NAV_CONTROLLER_OUTPUT, yaw rate out of ATTITUDE - so none of it costs
// any bandwidth on a link that has little to spare.
//
// They are redrawn from the marker's interpolated position on every
// animation frame rather than on each telemetry update, so they travel
// with the icon instead of stepping along behind it.
var vectorsEnabled = true;
var trackCourse = -1;     // deg over ground, -1 while too slow to know
var trackSpeed = 0;       // m/s
// The course is interpolated on the same clock as the heading. Drawing one
// from smoothed values and the other from the newest telemetry made the
// angle between them - the crab angle, the whole reason for showing both -
// read several degrees out through a turn.
var animCourseFrom = 0, animCourseTo = 0, currentCourse = -1;
var navBearing = null;    // deg, what the controller is steering at
var navDistance = 0;      // m to that point
var turnRate = 0;         // deg/s

// Lengths are given in SECONDS of flight, not metres, so a line always
// means "where this takes you" whatever the speed - and shrinks to nothing
// on the ground instead of covering the map.
var TRACK_SECONDS = 16;
var ARC_SECONDS = 12;
// The nose line states a direction rather than a prediction, so it stays a
// fixed length - set to roughly what the track line reaches at cruise, so
// the two are comparable and the crab angle between them reads easily.
var HEADING_LINE_M = 300;
var MIN_TURN_RATE = 2;      // deg/s; below this the turn arc is just the track
var MIN_ARC_SPEED = 1;      // m/s

var trackLine = L.polyline([], {color: '#00e5ff', weight: 3, opacity: 0.9}).addTo(map);
var headingLine = L.polyline([], {color: '#ffffff', weight: 1.5, opacity: 0.85,
                                  dashArray: '4,4'}).addTo(map);
var navLine = L.polyline([], {color: '#ff2fd0', weight: 2, opacity: 0.9}).addTo(map);
var turnArc = L.polyline([], {color: '#ffd24a', weight: 2, opacity: 0.9}).addTo(map);

function offsetLatLng(lat, lon, bearingDeg, metres) {
    // Equirectangular offset: under a metre of error over the few hundred
    // metres these lines span, and much cheaper than the spherical form
    // when it runs several times per frame.
    var R = 6378137.0;
    var br = bearingDeg * Math.PI / 180;
    var dLat = (metres * Math.cos(br)) / R;
    var dLon = (metres * Math.sin(br)) / (R * Math.cos(lat * Math.PI / 180));
    return [lat + dLat * 180 / Math.PI, lon + dLon * 180 / Math.PI];
}

function setGroundTrack(course, speed) {
    trackSpeed = speed;
    trackCourse = course;
    overlaysNeedRedraw();
    if (course < 0) { currentCourse = -1; return; }
    // Arrives in the same telemetry cycle as the position, so it rides the
    // animation the marker has just started.
    animCourseFrom = (currentCourse >= 0) ? currentCourse : course;
    animCourseTo = course;
}
function setNavTarget(bearing, distance) {
    navBearing = bearing; navDistance = distance; overlaysNeedRedraw();
}
function setTurnRate(rate) { turnRate = rate; overlaysNeedRedraw(); }

function setVectorsEnabled(on) {
    vectorsEnabled = on;
    overlaysNeedRedraw();
    var cb = document.getElementById('vectors-checkbox');
    if (cb) { cb.checked = on; }
    if (!on) {
        trackLine.setLatLngs([]); headingLine.setLatLngs([]);
        navLine.setLatLngs([]); turnArc.setLatLngs([]);
    }
}

function _drawVectors(lat, lon, heading, course) {
    if (!vectorsEnabled) { return; }

    // Where the nose points.
    headingLine.setLatLngs([[lat, lon],
                            offsetLatLng(lat, lon, heading, HEADING_LINE_M)]);

    // Where it is actually going. The angle between this and the nose line
    // is the crab angle, so the wind's effect can be read at a glance
    // rather than worked out from the wind readout.
    if (course >= 0 && trackSpeed > 0) {
        var reach = Math.max(120, trackSpeed * TRACK_SECONDS);
        trackLine.setLatLngs([[lat, lon],
                              offsetLatLng(lat, lon, course, reach)]);
    } else {
        trackLine.setLatLngs([]);
    }

    // Straight at whatever the navigation controller is steering for - the
    // current waypoint in AUTO, the loiter point in RTL, and so on.
    if (navBearing !== null && navDistance > 0) {
        navLine.setLatLngs([[lat, lon],
                            offsetLatLng(lat, lon, navBearing, navDistance)]);
    } else {
        navLine.setLatLngs([]);
    }

    // Where the turn currently leads, integrated forward a few seconds. A
    // straight-line prediction would be wrong exactly when it matters most,
    // which is mid-turn.
    if (Math.abs(turnRate) >= MIN_TURN_RATE && trackSpeed > MIN_ARC_SPEED
        && course >= 0) {
        var h = (course >= 0) ? course : heading;
        var pts = [[lat, lon]], la = lat, lo = lon, step = 0.25, swept = 0;
        for (var t = 0; t < ARC_SECONDS; t += step) {
            var p = offsetLatLng(la, lo, h, trackSpeed * step);
            la = p[0]; lo = p[1];
            h += turnRate * step;
            pts.push([la, lo]);
            // In a hard turn the horizon is long enough to come all the way
            // round. Stopping at one revolution shows the turn circle, which
            // is the useful part, without the line crossing back over itself.
            swept += Math.abs(turnRate) * step;
            if (swept >= 360) { break; }
        }
        turnArc.setLatLngs(pts);
    } else {
        turnArc.setLatLngs([]);
    }
}

// ---- compass rose ----------------------------------------------------
// Heading-up: the card turns under a fixed index, so whatever sits at the
// top of the dial is straight ahead. Everything shown here is telemetry the
// app already had - heading, the course over ground added for the track
// line, and the wind the HUD was already displaying.
var windFrom = null;      // deg the wind is coming FROM (the WIND convention)
var windSpeed = 0;        // m/s

(function buildCompassTicks() {
    var g = document.getElementById('cp-ticks');
    if (!g) { return; }
    for (var deg = 0; deg < 360; deg += 10) {
        var major = (deg % 30) === 0;
        var r1 = 94, r2 = major ? 80 : 87;
        var rad = (deg - 90) * Math.PI / 180;
        var ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        ln.setAttribute('x1', (100 + r1 * Math.cos(rad)).toFixed(2));
        ln.setAttribute('y1', (100 + r1 * Math.sin(rad)).toFixed(2));
        ln.setAttribute('x2', (100 + r2 * Math.cos(rad)).toFixed(2));
        ln.setAttribute('y2', (100 + r2 * Math.sin(rad)).toFixed(2));
        ln.setAttribute('class', major ? 'cp-tick major' : 'cp-tick');
        g.appendChild(ln);
    }
})();

var homeBearing = null;   // deg to home, null until known

// state is 'off', 'waiting', 'sampling' or a verdict; only the verdict
// is coloured, because that is the only one saying anything about the
// aeroplane rather than about the data collection.
var COG_COLOURS = {
    'Nose heavy': '#ffa726',
    'Tail heavy': '#ffa726',
    // Paler, because these come from the elevator trim alone: the pitch
    // integrator had nothing to say, so whatever is out is small enough
    // that SERVO_AUTO_TRIM absorbed all of it.
    'Slightly nose heavy': '#ffd08a',
    'Slightly tail heavy': '#ffd08a',
    'Balanced': '#7ddc7d'
};

// How far the marker may slide from neutral, in SVG units. Bounded on
// purpose: held elevator says which way the balance is out and roughly
// how far, not where the centre of gravity is in millimetres, and a
// marker running off the tail would claim precision that is not there.
var COG_MAX_SHIFT = 26;

function setCogStatus(state, text, deflection) {
    var el = document.getElementById('cog-readout');
    if (!el) { return; }
    if (state === 'off') {
        el.style.display = 'none';
        return;
    }
    el.style.display = '';

    var label = document.getElementById('cog-label');
    if (label) {
        label.textContent = text;
        label.style.color = COG_COLOURS[state] || '#cfd8e0';
    }

    var settled = COG_COLOURS.hasOwnProperty(state);
    var plane = document.getElementById('cog-plane');
    var marker = document.getElementById('cog-marker');
    // While still collecting, the aeroplane and the symbol are dimmed and
    // the marker sits at neutral: it has nothing to report yet, and a
    // confident-looking marker would be a lie.
    if (plane) { plane.setAttribute('opacity', settled ? '1' : '0.45'); }
    if (marker) {
        marker.setAttribute('opacity', settled ? '1' : '0.35');
        var d = settled ? Math.max(-1, Math.min(1, deflection || 0)) : 0;
        marker.setAttribute('transform',
                            'translate(' + (d * COG_MAX_SHIFT).toFixed(1) + ',0)');
    }
}

function setHomeBearing(deg) {
    homeBearing = (deg >= 0) ? (((deg % 360) + 360) % 360) : null;
    overlaysNeedRedraw();
}

function setWind(fromDeg, speedMps) {
    windFrom = ((fromDeg % 360) + 360) % 360;
    windSpeed = speedMps;
    overlaysNeedRedraw();
}

function _updateCompass(heading, course) {
    var rose = document.getElementById('cp-rose');
    if (!rose) { return; }
    rose.setAttribute('transform', 'rotate(' + (-heading).toFixed(2) + ' 100 100)');

    var h = document.getElementById('cp-heading');
    if (h) { h.textContent = Math.round(heading % 360) + '\u00b0'; }

    // Course is blank rather than zero when the aircraft is too slow for
    // the GPS to have a direction - a false 0 would read as due north.
    var c = document.getElementById('cp-course');
    if (c) { c.textContent = (course >= 0) ? (Math.round(course) + '\u00b0') : '---'; }

    // The amber marker sits at the course, inside the card - so on screen it
    // lands (course - heading) from the top, which is the drift angle.
    var tm = document.getElementById('cp-trackmark');
    if (tm) {
        if (course >= 0) {
            tm.style.display = '';
            tm.setAttribute('transform', 'rotate(' + course.toFixed(2) + ' 100 100)');
        } else {
            tm.style.display = 'none';
        }
    }

    var hg = document.getElementById('cp-home');
    if (hg) {
        if (homeBearing === null) {
            hg.style.display = 'none';
        } else {
            hg.style.display = '';
            hg.setAttribute('transform',
                            'rotate(' + homeBearing.toFixed(2) + ' 100 100)');
        }
    }

    var wg = document.getElementById('cp-wind');
    var wt = document.getElementById('cp-windtext');
    if (windFrom === null) {
        if (wg) { wg.style.display = 'none'; }
        if (wt) { wt.textContent = '--'; }
        return;
    }
    if (wg) {
        wg.style.display = '';
        // WIND reports where the wind comes FROM; the arrow shows where it
        // is pushing the aircraft, which is the opposite way.
        var toward = (windFrom + 180) % 360;
        wg.setAttribute('transform', 'rotate(' + toward.toFixed(2) + ' 100 100)');
    }
    // km/h, matching the wind readout in the telemetry panel.
    if (wt) { wt.textContent = (windSpeed * 3.6).toFixed(1) + ' km/h'; }
}

function _animateMarker(now) {
    if (marker && animFrom && animTo) {
        var t = Math.min(1, (now - animStartTime) / animDuration);
        var lat = animFrom[0] + (animTo[0] - animFrom[0]) * t;
        var lon = animFrom[1] + (animTo[1] - animFrom[1]) * t;
        marker.setLatLng([lat, lon]);

        // Shortest-path heading interpolation, so crossing the 0/360
        // boundary doesn't spin the icon the long way around.
        var diff = ((animHeadingTo - animHeadingFrom + 540) % 360) - 180;
        currentHeading = (animHeadingFrom + diff * t + 360) % 360;
        var el = marker.getElement();
        if (el) {
            var inner = el.querySelector('div');
            if (inner) { inner.style.transform = 'rotate(' + currentHeading + 'deg)'; }
        }

        if (trackCourse >= 0) {
            var cdiff = ((animCourseTo - animCourseFrom + 540) % 360) - 180;
            currentCourse = (animCourseFrom + cdiff * t + 360) % 360;
        }
        // Capped mid-leg; drawn once more exactly where the marker
        // settles; then nothing at all until something changes. Idle costs
        // no redraws, which is the state the app sits in whenever the link
        // is quiet.
        if (!overlaysSettled) {
            var settling = t >= 1;
            if (settling || now - lastOverlayMs >= OVERLAY_INTERVAL_MS) {
                lastOverlayMs = now;
                overlaysSettled = settling;
                _drawVectors(lat, lon, currentHeading, currentCourse);
                // Driven from the same interpolated values as the marker,
                // so the card turns as smoothly as the aircraft icon does.
                _updateCompass(currentHeading, currentCourse);
            }
        }

        if (followDrone && haveCentered) {
            // Skip sub-pixel pans. panTo() repositions the whole tile layer
            // and every marker on the map, and its cost scales with the
            // viewport - which got much bigger once the app started opening
            // maximized. At 2-3Hz telemetry interpolated over 450ms most
            // frames move the map by well under a pixel, so those pans cost
            // real work while changing nothing on screen. Drift accumulates
            // until it crosses the threshold, so motion stays smooth.
            var target = map.latLngToContainerPoint([lat, lon]);
            var centre = map.latLngToContainerPoint(map.getCenter());
            if (Math.abs(target.x - centre.x) >= 1 || Math.abs(target.y - centre.y) >= 1) {
                map.panTo([lat, lon], {animate: false});
            }
        }
    }
    requestAnimationFrame(_animateMarker);
}
requestAnimationFrame(_animateMarker);

// Dragging the map is the user asking to look somewhere else, and
// following would spend every frame tugging it back. So following steps
// aside the moment a drag begins, and the checkbox unticks to say so -
// rather than the map appearing to fight, or to ignore, the mouse.
//
// Deliberately 'dragstart' and not 'movestart': the latter also fires for
// the panTo this very feature performs while following, which would
// switch itself off on the first frame.
map.on('dragstart', function () {
    if (followDrone) {
        setFollow(false);
    }
});

function setFollow(v) {
    followDrone = v;
    var cb = document.getElementById('follow-checkbox');
    if (cb) { cb.checked = v; }
}
function clearTrail() { path.setLatLngs([]); }

map.on('click', function(e) {
    var lat = e.latlng.lat;
    var lon = e.latlng.lng;

    if (waypointMode) {
        var m = L.marker([lat, lon], {icon: waypointIcon(waypointMarkers.length + 1)}).addTo(map);
        m._wpId = ++wpSeq;
        m._wpNum = waypointMarkers.length + 1;
        m._wpAlt = null;                 // null = fly the mission default
        m._wpSent = false;
        m.setIcon(waypointIcon(m._wpNum, false, wpAltText(m), false));
        // A function, not a fixed string: the popup is rebuilt each time it
        // opens, so it shows the current altitude and picks up the mission
        // default once one has been set.
        m.bindPopup(function () { return wpPopupHtml(m); });
        waypointMarkers.push(m);
        allWaypointLayers.push(m);
        waypointLine.addLatLng([lat, lon]);
        waypointAdded(lat, lon, m._wpId);
        return;
    }

    if (targetMarker) {
        map.removeLayer(targetMarker);
    }
    targetMarker = L.marker([lat, lon], {opacity: 0.85}).addTo(map);

    var popupHtml =
        '<div style="text-align:center">' +
        lat.toFixed(6) + ', ' + lon.toFixed(6) + '<br>' +
        '<button class="fly-to-btn" onclick="flyToHere(' + lat + ',' + lon + ')">' +
        'Fly to Here</button>' +
        '</div>';
    targetMarker.bindPopup(popupHtml).openPopup();

    // Closing a Leaflet popup (its own built-in x button) only hides the
    // popup bubble - it does NOT remove the marker underneath by itself.
    // Tie them together explicitly so clicking that x actually clears
    // the pin too, not just the popup.
    (function (thisMarker) {
        thisMarker.on('popupclose', function () {
            if (targetMarker === thisMarker) {
                map.removeLayer(thisMarker);
                targetMarker = null;
            }
        });
    })(targetMarker);
});

function setWaypointMode(enabled) {
    waypointMode = enabled;
}

function commitWaypoints() {
    // Called after a mission is sent: keep the just-drawn markers/line
    // visible on the map (don't remove anything), just stop tracking
    // them as the "current queue" so the next batch of clicks starts
    // fresh without extending this route.
    //
    // Grey the batch as it goes, because the next queue starts numbering
    // at 1 again - matching the mission the vehicle actually gets. Left
    // in the same blue, the map would show two "1"s with nothing to say
    // which had been flown and which was still being planned.
    // A new mission REPLACES the old one on the vehicle, so the old one
    // stops being drawn here too. Left up, the map showed two batches that
    // both looked live - two markers numbered "1", only one of which the
    // aircraft actually had.
    for (var i = 0; i < sentLayers.length; i++) {
        map.removeLayer(sentLayers[i]);
        var at = allWaypointLayers.indexOf(sentLayers[i]);
        if (at >= 0) { allWaypointLayers.splice(at, 1); }
    }
    sentLayers = waypointMarkers.slice();
    sentLayers.push(waypointLine);

    for (var i = 0; i < waypointMarkers.length; i++) {
        var m = waypointMarkers[i];
        // Freeze the altitude this point was actually sent with. Left
        // following the shared default, a later mission at a different
        // altitude would silently relabel this batch with a figure the
        // vehicle was never given.
        if (m._wpAlt === null || m._wpAlt === undefined) { m._wpAlt = wpDefaultAlt; }
        m._wpSent = true;
        m.setIcon(waypointIcon(m._wpNum, true, wpAltText(m), wpIsDirty(m)));
    }
    // The next batch has no altitude decided yet, so it shows none rather
    // than borrowing this mission's.
    wpDefaultAlt = null;
    waypointLine.setStyle({color: '#5b6b78', opacity: 0.7});
    waypointMarkers = [];
    waypointLine = L.polyline([], {color: '#3af', weight: 2, dashArray: '6,6'}).addTo(map);
    allWaypointLayers.push(waypointLine);
}

function clearWaypoints() {
    for (var i = 0; i < allWaypointLayers.length; i++) {
        map.removeLayer(allWaypointLayers[i]);
    }
    allWaypointLayers = [];
    waypointMarkers = [];
    sentLayers = [];
    waypointLine = L.polyline([], {color: '#3af', weight: 2, dashArray: '6,6'}).addTo(map);
    allWaypointLayers.push(waypointLine);
}

// How much map to keep for offline use. "No Cache" (0) stops saving new
// tiles; whatever is already saved is still served from disk, so choosing
// it never loses what you've collected - only Clear does that.
// Set both dropdowns to what the app has actually applied. Assigning
// .value does not fire onchange, so this cannot loop back into the app.
function showCacheLimits(mapMb, terrainMb) {
    var m = document.getElementById('tc-limit');
    var t = document.getElementById('tr-limit');
    if (m) { m.value = String(mapMb); }
    if (t) { t.value = String(terrainMb); }
}

function setTileCacheLimit(megabytes) {
    if (bridge) {
        bridge.tileCacheLimitChanged(parseInt(megabytes, 10));
    }
}

function clearTileCache() {
    if (bridge) {
        bridge.tileCacheClearRequested();
    }
}

function fmtSize(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576) return Math.round(bytes / 1048576) + ' MB';
    if (bytes >= 1024) return Math.round(bytes / 1024) + ' KB';
    return bytes + ' B';
}

function setTerrainCacheLimit(megabytes) {
    if (bridge) {
        bridge.terrainCacheLimitChanged(parseInt(megabytes, 10));
    }
}

function clearTerrainCache() {
    if (bridge) {
        bridge.terrainCacheClearRequested();
    }
}

function _renderCacheRow(fillId, textId, tiles, usedBytes, limitBytes) {
    var fill = document.getElementById(fillId);
    var text = document.getElementById(textId);
    if (!fill || !text) return;
    var pct = limitBytes > 0 ? Math.min(100, (usedBytes / limitBytes) * 100) : 0;
    fill.style.width = pct.toFixed(1) + '%';
    // Over ~90% full the oldest tiles start being dropped - worth seeing.
    fill.style.background = pct >= 90 ? '#e0a030' : '#37a8db';
    var t = tiles + (tiles === 1 ? ' tile' : ' tiles');
    text.textContent = limitBytes > 0
        ? fmtSize(usedBytes) + ' / ' + fmtSize(limitBytes) + '  ' + t
        : fmtSize(usedBytes) + ' stored  ' + t + '  (not saving)';
}

// Both pushed from Python (see MapView.update_cache_stats).
function setTileCacheStats(tiles, usedBytes, limitBytes) {
    _renderCacheRow('tc-fill', 'tc-text', tiles, usedBytes, limitBytes);
}

function setTerrainCacheStats(tiles, usedBytes, limitBytes) {
    _renderCacheRow('tr-fill', 'tr-text', tiles, usedBytes, limitBytes);
}

// Drop a marker on a coordinate that was typed rather than clicked, and
// bring it into view - the whole point of typing one is that it may be
// somewhere you are not currently looking.
function showTarget(lat, lon) {
    clearTarget();
    targetMarker = L.marker([lat, lon], {opacity: 0.85}).addTo(map);
    targetMarker.bindTooltip('Fly to ' + lat.toFixed(6) + ', ' + lon.toFixed(6),
                             {direction: 'top', offset: [0, -12]});
    if (!map.getBounds().contains([lat, lon])) {
        map.panTo([lat, lon]);
    }
}

function clearTarget() {
    if (targetMarker) {
        map.removeLayer(targetMarker);
        targetMarker = null;
    }
}

// ---- Weather radar overlay ------------------------------------------------
// Precipitation radar from RainViewer's free public API - no key, no
// account. New frames appear roughly every 10 minutes.
//
// Confined to a circle around the aircraft: the radar is here to show what
// you are about to fly into, and an unclipped national mosaic just buries
// the map underneath it.
var WEATHER_RADIUS_M = 50000;
// ADS-B contacts are shown over the same ground the weather radar covers,
// centred the same way, so the two agree about what counts as "nearby".
// The feed is queried far wider than this - filtering here rather than at
// the request keeps contacts available the moment the aircraft moves,
// instead of waiting for the next poll to widen.
var ADSB_RADIUS_M = WEATHER_RADIUS_M;
// RainViewer's free tiles stop at zoom 7. Ask for anything deeper and it
// serves a "Zoom Level Not Supported" placeholder instead of radar - the
// same 1370-byte image for every tile on earth, which tiles across the map
// looking like a broken overlay.
//
// Leaflet's URL zoom is min(mapZoom, maxNativeZoom) + zoomOffset, so 9 with
// an offset of -2 asks for zoom 7 and never deeper, whatever the map shows
// (the map itself never goes below zoom 9).
//
// Tile size is the one dial left for detail, since the zoom is pinned: a
// 1024px tile covers the same ground as a 256px one but with sixteen times
// the pixels, 234m per radar pixel rather than 937m. Most of that is the
// server resampling rather than new data - measured against upscaling the
// smaller tile ourselves, only a couple of percent of pixels genuinely
// differ - but it upscales more cleanly at flight zoom and costs only tens
// of kilobytes, because the whole 50km circle is one or two tiles.
var WEATHER_MAX_NATIVE_ZOOM = 9;
var WEATHER_TILE_SIZE = 1024;
var WEATHER_ZOOM_OFFSET = -2;
var WEATHER_REFRESH_MS = 5 * 60 * 1000;

var weatherEnabled = false;
var weatherLayer = null;
var weatherFramePath = null;
var weatherTimer = null;

function setWeatherEnabled(enabled) {
    weatherEnabled = enabled;
    if (enabled) {
        if (!map.getPane('weatherPane')) {
            var pane = map.createPane('weatherPane');
            pane.style.zIndex = 250;          // over the map, under the markers
            pane.style.pointerEvents = 'none';
        }
        weatherRefresh();
        weatherTimer = setInterval(weatherRefresh, WEATHER_REFRESH_MS);
    } else {
        if (weatherTimer) { clearInterval(weatherTimer); weatherTimer = null; }
        if (weatherLayer) { map.removeLayer(weatherLayer); weatherLayer = null; }
        weatherFramePath = null;
    }
}

function weatherRefresh() {
    if (!weatherEnabled) return;
    fetch('https://api.rainviewer.com/public/weather-maps.json', {cache: 'no-store'})
        .then(function (resp) { return resp.json(); })
        .then(function (index) {
            if (!weatherEnabled) return;
            var past = index && index.radar && index.radar.past;
            if (!past || !past.length) return;
            var path = past[past.length - 1].path;
            if (path === weatherFramePath) return;      // same frame, nothing to do
            weatherFramePath = path;
            // colour scheme 2 (universal blue), smoothed, snow shown apart
            var fresh = L.tileLayer(
                index.host + path + '/' + WEATHER_TILE_SIZE + '/{z}/{x}/{y}/2/1_1.png', {
                pane: 'weatherPane',
                opacity: 0.65,
                // Their free tier asks for a credit. Leaflet shows it only
                // while the layer is on, and drops it when Weather is off.
                attribution: 'Radar &copy; RainViewer',
                tileSize: WEATHER_TILE_SIZE,
                zoomOffset: WEATHER_ZOOM_OFFSET,
                maxNativeZoom: WEATHER_MAX_NATIVE_ZOOM,
                minZoom: MIN_ZOOM,
                maxZoom: MAX_ZOOM
            });
            fresh.addTo(map);
            // Keep the old frame up until the new one has drawn, so the
            // radar doesn't blink out every time it refreshes.
            var previous = weatherLayer;
            weatherLayer = fresh;
            if (previous) {
                fresh.once('load', function () { map.removeLayer(previous); });
                setTimeout(function () {
                    if (map.hasLayer(previous)) { map.removeLayer(previous); }
                }, 5000);
            }
            updateWeatherClip();
        })
        .catch(function () {
            // Offline, or the API is down. Leave whatever is drawn rather
            // than clearing it - stale radar still beats none.
        });
}

// Confine the radar to WEATHER_RADIUS_M around the aircraft.
function updateWeatherClip() {
    var pane = map.getPane('weatherPane');
    if (!pane) return;
    // Before the aircraft has a position - not connected yet, or no GPS
    // fix - fall back to the middle of the map. Clipping to a zero-radius
    // circle instead just hides the whole layer, which looks exactly like
    // a broken feature. With Follow UAV on, the two are the same point
    // anyway once telemetry arrives.
    var at = marker ? marker.getLatLng() : map.getCenter();
    // Ground resolution shrinks with latitude as well as zoom, so the
    // circle has to be sized from both or 25km is only right at the equator.
    var metresPerPixel = 156543.03392804097 *
        Math.cos(at.lat * Math.PI / 180) / Math.pow(2, map.getZoom());
    var radiusPx = WEATHER_RADIUS_M / metresPerPixel;
    var point = map.latLngToLayerPoint(at);
    pane.style.clipPath = 'circle(' + radiusPx.toFixed(1) + 'px at ' +
                          point.x.toFixed(1) + 'px ' + point.y.toFixed(1) + 'px)';
}

// Layer points are stable while panning (the whole pane is transformed),
// but a zoom or a view reset moves them.
map.on('zoomend viewreset moveend', updateWeatherClip);

// ---- ADS-B traffic overlay ------------------------------------------------
// Nearby manned air traffic from adsb.lol's free public API (no key/account
// needed, no local receiver hardware - one of the same built-in sources
// KiteGCS's own Radar tool uses). The actual polling happens in Python
// (see AdsbWorker in adsb_provider.py) and NOT here via fetch() - the API
// has no CORS headers, so a browser-context fetch() from this page gets
// blocked outright (confirmed directly: "blocked by CORS policy"), while a
// plain server-side request from Python has no such restriction. This side
// just renders whatever contact list Python pushes via renderAdsbContacts().
var adsbMarkers = {};   // ICAO hex -> marker, reused across polls

// Top-down airliner silhouette (swept wings + tailplane), sized and shaped
// so its heading reads at a glance - the previous small arrow made the
// direction ambiguous. The plane rotates to the aircraft's track; the
// callsign label underneath deliberately does NOT rotate (hence the
// separate .adsb-rot element that the rotation is applied to).
function adsbIconFor(callsign) {
    return L.divIcon({
        className: 'adsb-icon',
        html:
            '<div style="width:34px;height:46px;position:relative;">' +
              '<div class="adsb-rot" style="width:34px;height:34px;">' +
                '<svg width="34" height="34" viewBox="-12 -12 24 24" xmlns="http://www.w3.org/2000/svg">' +
                  '<path d="M0,-11 C1.1,-11 1.7,-9.6 1.7,-8 L1.7,-4.2 L10.5,1.6 L10.5,4 L1.7,1.6 ' +
                  'L1.7,6.4 L4.6,8.6 L4.6,10.4 L0,9.2 L-4.6,10.4 L-4.6,8.6 L-1.7,6.4 L-1.7,1.6 ' +
                  'L-10.5,4 L-10.5,1.6 L-1.7,-4.2 L-1.7,-8 C-1.7,-9.6 -1.1,-11 0,-11 Z" ' +
                  'fill="#ff2e63" stroke="#ffffff" stroke-width="0.9" stroke-linejoin="round"/>' +
                '</svg>' +
              '</div>' +
              '<div class="adsb-label">' + callsign + '</div>' +
            '</div>',
        iconSize: [34, 46],
        iconAnchor: [17, 17]   // anchor on the plane itself, not the label
    });
}

var adsbEnabled = false;

function setAdsbEnabled(enabled) {
    adsbEnabled = enabled;
    if (bridge) {
        // Send the centre first so the immediate poll this triggers has
        // somewhere to look.
        sendAdsbCenter();
        bridge.adsbToggled(enabled);
    }
    if (!enabled) {
        clearAdsbMarkers();
    }
}

// Traffic is fetched around the area you're LOOKING AT (the map centre),
// not around the UAV - the same thing KiteGCS does for its online feeds.
// These community feeds only cover where volunteers run receivers, so a
// rural flying site can legitimately show nothing while the map centred on
// a nearby airway/airport shows plenty.
function sendAdsbCenter() {
    if (!bridge) return;
    var c = map.getCenter();
    bridge.adsbCenter(c.lat, c.lng);
}

// Throttled hard: while Follow UAV is on we call map.panTo() on EVERY
// animation frame, and panTo fires 'moveend' each time - an unthrottled
// handler here would fire a QWebChannel message to Python ~60x/second and
// visibly wreck the map's smoothness.
var adsbLastCenterSent = 0;
map.on('moveend', function () {
    if (!adsbEnabled) return;
    var now = Date.now();
    if (now - adsbLastCenterSent < 2000) return;
    adsbLastCenterSent = now;
    sendAdsbCenter();
});

function clearAdsbMarkers() {
    for (var k in adsbMarkers) {
        map.removeLayer(adsbMarkers[k]);
    }
    adsbMarkers = {};
}

// Great-circle distance in km - used for the "how far from me" figure in
// each contact's readout, measured from the UAV when we have its position
// (what actually matters in flight), else from the map centre.
function adsbDistanceKm(lat, lon) {
    var ref = marker ? marker.getLatLng() : map.getCenter();
    var R = 6371, d2r = Math.PI / 180;
    var dLat = (lat - ref.lat) * d2r, dLon = (lon - ref.lng) * d2r;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(ref.lat * d2r) * Math.cos(lat * d2r) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

function adsbReadout(ac, callsign, track) {
    // Feeds report feet / knots / feet-per-minute; shown in metric to match
    // the rest of this app's telemetry.
    var parts = [callsign];
    if (typeof ac.alt_baro === 'number') {
        parts.push(Math.round(ac.alt_baro * 0.3048) + ' m');
    }
    if (typeof ac.vert_rate === 'number' && Math.abs(ac.vert_rate) >= 50) {
        var vs = ac.vert_rate * 0.00508;  // ft/min -> m/s
        parts.push((vs > 0 ? '▲' : '▼') + Math.abs(vs).toFixed(1) + ' m/s');
    }
    if (typeof ac.gs === 'number') {
        parts.push(Math.round(ac.gs * 1.852) + ' km/h');
    }
    parts.push(adsbDistanceKm(ac.lat, ac.lon).toFixed(0) + ' km');
    parts.push(Math.round(track) + '°');
    return parts.join(' · ');
}

// Markers are keyed by ICAO hex and REUSED across polls (moved/relabelled
// in place), not torn down and rebuilt. At ~70 contacts refreshed every 5s,
// recreating every divIcon + tooltip + popup meant a burst of DOM churn on
// each poll - exactly the kind of hitch we just finished removing from the
// map's motion.
// Same centre the weather clip uses: the aircraft, or the middle of the
// map before there is a fix, so the two circles always coincide.
function adsbCentre() {
    return marker ? marker.getLatLng() : map.getCenter();
}

function metresBetween(a, bLat, bLon) {
    var R = 6371008.8;
    var p1 = a.lat * Math.PI / 180, p2 = bLat * Math.PI / 180;
    var dp = p2 - p1;
    var dl = (bLon - a.lng) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

function renderAdsbContacts(contacts) {
    var seen = {};
    var centre = adsbCentre();
    for (var i = 0; i < contacts.length; i++) {
        var ac = contacts[i];
        if (typeof ac.lat !== 'number' || typeof ac.lon !== 'number') continue;
        // Outside the circle it is not drawn, and any marker it already had
        // is dropped below with everything else that went unseen.
        if (metresBetween(centre, ac.lat, ac.lon) > ADSB_RADIUS_M) continue;
        var track = typeof ac.track === 'number' ? ac.track : 0;
        var callsign = (ac.flight || ac.hex || '?').trim();
        var key = ac.hex || callsign;
        seen[key] = true;

        var m = adsbMarkers[key];
        if (!m) {
            m = L.marker([ac.lat, ac.lon], { icon: adsbIconFor(callsign) }).addTo(map);
            m.bindTooltip('', { direction: 'top', offset: [0, -14], className: 'adsb-tip' });
            m.bindPopup('');
            adsbMarkers[key] = m;
        } else {
            m.setLatLng([ac.lat, ac.lon]);
        }

        // Rotated the same way our own plane marker is (see _animateMarker):
        // a CSS transform on the icon's rotating element, since vanilla
        // Leaflet has no marker-rotation option built in.
        var el = m.getElement();
        if (el) {
            var rot = el.querySelector('.adsb-rot');
            if (rot) rot.style.transform = 'rotate(' + track + 'deg)';
            var lbl = el.querySelector('.adsb-label');
            if (lbl && lbl.textContent !== callsign) lbl.textContent = callsign;
        }

        m.setTooltipContent(adsbReadout(ac, callsign, track));
        m.setPopupContent(
            '<b>' + callsign + '</b>' +
            (ac.type ? ('<br>Type: ' + ac.type) : '') +
            (ac.squawk ? ('<br>Squawk: ' + ac.squawk) : '') +
            (ac.hex ? ('<br>ICAO: ' + ac.hex) : '')
        );
    }

    // Drop contacts that have dropped out of the feed.
    for (var k in adsbMarkers) {
        if (!seen[k]) {
            map.removeLayer(adsbMarkers[k]);
            delete adsbMarkers[k];
        }
    }
}

// ---- Terrain Radar overlay -----------------------------------------------
// Forward-looking, track-up terrain-awareness fan (EGPWS-style), ported
// from KiteGCS's own terrain radar widget - same idea, same free Copernicus
// GLO-30 elevation source (see terrain_provider.py), plain SVG here instead
// of Svelte. Python pushes two kinds of updates:
//   setTerrainFan()  - raw elevations. Rare: only when the vehicle's
//                       position/heading/range moved enough to matter,
//                       since a new fan can mean a fresh terrain tile
//                       download on the Python side.
//   setTerrainRef()  - current altitude/speed/climb. Frequent and cheap:
//                       just recolours the already-sampled cells, no new
//                       sampling - mirrors Kite's split between expensive
//                       geometry and live colour.
var TR_SIZE = 200;
var TR_HALF_ANGLE = 60 * Math.PI / 180;  // must match TerrainRadarWorker.HALF_ANGLE_DEG
var TR_RING_R = 6, TR_APEX_Y = TR_SIZE - TR_RING_R - 3, TR_R = TR_APEX_Y - 6;
// Total clearance colour scale in metres - how much terrain clearance the
// red->green ramp spans. Freely typeable (60/120/250 were just KiteGCS's
// presets, not anything the maths depends on); clamped only to keep the
// ramp meaningful.
var TR_SCALE_MIN = 5, TR_SCALE_MAX = 2000;
var trScaleM = 120;
var trPredictive = false;
var trFan = null;  // {elev, rangeM, angCells, radCells, cellEls}
var trAltMsl = 0, trSpeed = 0;
var trVarioBuf = [];

function trPx(thetaRel, dist, range) {
    var r = (dist / range) * TR_R;
    return [TR_SIZE / 2 + r * Math.sin(thetaRel), TR_APEX_Y - r * Math.cos(thetaRel)];
}

var TR_RAMP = [[231, 76, 60], [230, 126, 34], [241, 196, 15], [46, 204, 113]];
function trRamp(t) {
    var x = Math.min(1, Math.max(0, t)) * (TR_RAMP.length - 1);
    var i = Math.min(TR_RAMP.length - 2, Math.floor(x));
    var f = x - i, a = TR_RAMP[i], b = TR_RAMP[i + 1];
    return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * f) + ',' +
        Math.round(a[1] + (b[1] - a[1]) * f) + ',' + Math.round(a[2] + (b[2] - a[2]) * f) + ')';
}

function trSlope() {
    if (trSpeed <= 2 || trVarioBuf.length === 0) return 0;
    var avg = trVarioBuf.reduce(function (a, b) { return a + b; }, 0) / trVarioBuf.length;
    return avg / trSpeed;
}

// Continuous red -> green ramp over 0..scale (clearance < 0 clamps red,
// terrain more than `scale` below the reference is unpainted/transparent).
function trColorFor(elev, dist) {
    if (elev === null || elev === undefined) return null;
    var scale = trScaleM;
    var ref = trPredictive ? (trAltMsl + trSlope() * dist) : trAltMsl;
    var clear = ref - elev;
    if (clear >= scale) return null;
    return trRamp(clear / scale);
}

function setTerrainRef(altMsl, groundSpeed, climbMps) {
    trAltMsl = altMsl;
    trSpeed = groundSpeed;
    trVarioBuf.push(climbMps);
    if (trVarioBuf.length > 5) trVarioBuf.shift();
    trRenderTerrainRadar();
}

function setTerrainFan(elevJson, rangeM, angCells, radCells) {
    trFan = { elev: elevJson, rangeM: rangeM, angCells: angCells, radCells: radCells };
    trBuildTerrainGeometry();
    trRenderTerrainRadar();
}

function trBuildTerrainGeometry() {
    if (!trFan) return;
    document.getElementById('tr-svg').style.display = 'block';
    document.getElementById('tr-placeholder').style.display = 'none';

    var range = trFan.rangeM, ang = trFan.angCells, rad = trFan.radCells;
    var half = TR_HALF_ANGLE;
    var SVGNS = 'http://www.w3.org/2000/svg';

    var cellsG = document.getElementById('tr-cells');
    cellsG.innerHTML = '';
    trFan.cellEls = [];
    for (var a = 0; a < ang; a++) {
        var tA = -half + (2 * half * a) / ang, tB = -half + (2 * half * (a + 1)) / ang;
        for (var b = 0; b < rad; b++) {
            var r0 = range * b / rad, r1 = range * (b + 1) / rad;
            var p0 = trPx(tA, r0, range), p1 = trPx(tB, r0, range);
            var p2 = trPx(tB, r1, range), p3 = trPx(tA, r1, range);
            var path = document.createElementNS(SVGNS, 'path');
            path.setAttribute('d',
                'M' + p0[0].toFixed(1) + ' ' + p0[1].toFixed(1) +
                'L' + p1[0].toFixed(1) + ' ' + p1[1].toFixed(1) +
                'L' + p2[0].toFixed(1) + ' ' + p2[1].toFixed(1) +
                'L' + p3[0].toFixed(1) + ' ' + p3[1].toFixed(1) + 'Z');
            cellsG.appendChild(path);
            trFan.cellEls.push({ el: path, dist: range * (b + 0.5) / rad, elev: trFan.elev[a * rad + b] });
        }
    }

    // Fan sector (apex -> outer arc -> apex), used to clip the cells above.
    var sectorD = 'M' + (TR_SIZE / 2) + ' ' + TR_APEX_Y;
    for (var s = 0; s <= ang; s++) {
        var th = -half + (2 * half * s) / ang;
        var p = trPx(th, range, range);
        sectorD += 'L' + p[0].toFixed(1) + ' ' + p[1].toFixed(1);
    }
    document.getElementById('tr-fan-sector').setAttribute('d', sectorD + 'Z');

    // Range arcs (thirds) + distance labels.
    var arcsG = document.getElementById('tr-arcs');
    var labelsG = document.getElementById('tr-labels');
    arcsG.innerHTML = '';
    labelsG.innerHTML = '';
    for (var k = 1; k <= 3; k++) {
        var dist = range * k / 3;
        var d = '';
        for (var s2 = 0; s2 <= ang; s2++) {
            var th2 = -half + (2 * half * s2) / ang;
            var pa = trPx(th2, dist, range);
            d += (s2 === 0 ? 'M' : 'L') + pa[0].toFixed(1) + ' ' + pa[1].toFixed(1);
        }
        var arcPath = document.createElementNS(SVGNS, 'path');
        arcPath.setAttribute('class', 'tr-arc');
        arcPath.setAttribute('d', d);
        arcsG.appendChild(arcPath);

        var rr = (dist / range) * TR_R;
        var label = document.createElementNS(SVGNS, 'text');
        label.setAttribute('class', 'tr-dist-label');
        label.setAttribute('x', TR_SIZE / 2 + 3);
        label.setAttribute('y', TR_APEX_Y - rr + 4);
        label.setAttribute('style', 'font-size:9px;');
        label.textContent = Math.round(dist);
        labelsG.appendChild(label);
    }

    // Fan edges + heading line (always straight up - the fan is already
    // track-up since terrain was sampled relative to heading, not true bearing).
    var l = trPx(-half, range, range), r = trPx(half, range, range);
    document.getElementById('tr-edges').setAttribute('d',
        'M' + (TR_SIZE / 2) + ' ' + TR_APEX_Y + 'L' + l[0].toFixed(1) + ' ' + l[1].toFixed(1) +
        'M' + (TR_SIZE / 2) + ' ' + TR_APEX_Y + 'L' + r[0].toFixed(1) + ' ' + r[1].toFixed(1));
    var top = trPx(0, range, range);
    var hdgLine = document.getElementById('tr-hdg-line');
    hdgLine.setAttribute('x1', TR_SIZE / 2); hdgLine.setAttribute('y1', TR_APEX_Y);
    hdgLine.setAttribute('x2', top[0].toFixed(1)); hdgLine.setAttribute('y2', top[1].toFixed(1));

    trShowScale();
    document.getElementById('tr-mode-btn').textContent = trPredictive ? 'PRED' : 'REL';
}

function trRenderTerrainRadar() {
    if (!trFan || !trFan.cellEls) return;
    for (var i = 0; i < trFan.cellEls.length; i++) {
        var c = trFan.cellEls[i];
        var fill = trColorFor(c.elev, c.dist);
        if (fill) {
            c.el.setAttribute('fill', fill);
            c.el.style.display = '';
        } else {
            c.el.style.display = 'none';
        }
    }
}

function toggleTerrainMode() {
    trPredictive = !trPredictive;
    document.getElementById('tr-mode-btn').textContent = trPredictive ? 'PRED' : 'REL';
    trRenderTerrainRadar();
}
function trShowScale() {
    var el = document.getElementById('tr-scale-input');
    // Don't fight the user while they're mid-edit.
    if (el && document.activeElement !== el) el.value = trScaleM + 'm';
}

function trApplyScale() {
    var el = document.getElementById('tr-scale-input');
    if (!el) return;
    // Tolerate "120", "120m", " 120 m" alike.
    var v = parseFloat(String(el.value).replace(/[^0-9.]/g, ''));
    if (isFinite(v) && v > 0) {
        trScaleM = Math.min(TR_SCALE_MAX, Math.max(TR_SCALE_MIN, v));
        trRenderTerrainRadar();
    }
    el.value = trScaleM + 'm';   // echo back what was actually applied
}

(function () {
    var el = document.getElementById('tr-scale-input');
    if (!el) return;
    el.addEventListener('keydown', function (e) {
        // The map binds its own keys (+/- zoom, arrows pan) - keep typing
        // here from also driving the map underneath.
        e.stopPropagation();
        if (e.key === 'Enter') { trApplyScale(); el.blur(); }
        else if (e.key === 'Escape') { el.value = trScaleM + 'm'; el.blur(); }
    });
    el.addEventListener('blur', trApplyScale);
    el.addEventListener('focus', function () {
        el.value = String(trScaleM);   // drop the 'm' so it's ready to overtype
        el.select();
    });
    // Clicking the field shouldn't also register as a map click.
    el.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    el.addEventListener('dblclick', function (e) { e.stopPropagation(); });
})();
</script>
</body>
</html>
"""


class Bridge(QObject):
    """
    The Python-side object exposed to JavaScript via QWebChannel - Qt's
    actual supported mechanism for JS-to-Python calls in QWebEngine.
    """
    fly_to_here = Signal(float, float)
    waypoint_added = Signal(float, float, int)
    waypoint_alt_changed = Signal(int, float)
    adsb_toggled = Signal(bool)
    adsb_center_changed = Signal(float, float)
    tile_cache_limit_changed = Signal(int)
    tile_cache_clear_requested = Signal()
    terrain_cache_limit_changed = Signal(int)
    terrain_cache_clear_requested = Signal()

    @Slot(float, float)
    def flyToHere(self, lat, lon):
        self.fly_to_here.emit(lat, lon)

    @Slot(float, float, int)
    def waypointAdded(self, lat, lon, wp_id):
        self.waypoint_added.emit(lat, lon, wp_id)

    @Slot(int, float)
    def waypointAltChanged(self, wp_id, alt):
        self.waypoint_alt_changed.emit(wp_id, alt)

    @Slot(bool)
    def adsbToggled(self, enabled):
        self.adsb_toggled.emit(enabled)

    @Slot(float, float)
    def adsbCenter(self, lat, lon):
        self.adsb_center_changed.emit(lat, lon)

    @Slot(int)
    def tileCacheLimitChanged(self, megabytes):
        self.tile_cache_limit_changed.emit(megabytes)

    @Slot()
    def tileCacheClearRequested(self):
        self.tile_cache_clear_requested.emit()

    @Slot(int)
    def terrainCacheLimitChanged(self, megabytes):
        self.terrain_cache_limit_changed.emit(megabytes)

    @Slot()
    def terrainCacheClearRequested(self):
        self.terrain_cache_clear_requested.emit()


class MapView(QWebEngineView):
    fly_to_here = Signal(float, float)
    waypoint_added = Signal(float, float, int)
    waypoint_alt_changed = Signal(int, float)
    adsb_toggled = Signal(bool)
    adsb_center_changed = Signal(float, float)
    tile_cache_limit_changed = Signal(int)
    tile_cache_clear_requested = Signal()
    terrain_cache_limit_changed = Signal(int)
    terrain_cache_clear_requested = Signal()

    def __init__(self, tile_proxy_port: int, parent=None):
        super().__init__(parent)
        _clear_browser_cache()

        self._bridge = Bridge()
        self._bridge.fly_to_here.connect(self.fly_to_here)
        self._bridge.waypoint_added.connect(self.waypoint_added)
        self._bridge.waypoint_alt_changed.connect(self.waypoint_alt_changed)
        self._bridge.adsb_toggled.connect(self.adsb_toggled)
        self._bridge.adsb_center_changed.connect(self.adsb_center_changed)
        self._bridge.tile_cache_limit_changed.connect(self.tile_cache_limit_changed)
        self._bridge.tile_cache_clear_requested.connect(self.tile_cache_clear_requested)
        self._bridge.terrain_cache_limit_changed.connect(self.terrain_cache_limit_changed)
        self._bridge.terrain_cache_clear_requested.connect(self.terrain_cache_clear_requested)

        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self.page().setWebChannel(self._channel)

        # The tile proxy's port is only known once it's listening, so it's
        # substituted into the page here rather than hardcoded.
        html = LEAFLET_HTML.replace(
            "%%TILE_PROXY%%", f"http://127.0.0.1:{tile_proxy_port}"
        )
        # base URL lets the relative CDN references resolve sanely
        self.setHtml(html, QUrl("https://localhost/"))

    def update_position(self, lat: float, lon: float, heading: float = 0.0):
        self.page().runJavaScript(f"updatePosition({lat}, {lon}, {heading});")

    def set_ground_track(self, course_deg: float, groundspeed: float):
        """Course over ground and speed - the direction of travel, which is
        not the heading whenever there is any wind."""
        self.page().runJavaScript(
            f"setGroundTrack({float(course_deg)}, {float(groundspeed)});"
        )

    def set_nav_target(self, bearing_deg: float, distance_m: float):
        """Where the vehicle's navigation controller is steering."""
        self.page().runJavaScript(
            f"setNavTarget({float(bearing_deg)}, {float(distance_m)});"
        )

    def set_cog_status(self, state: str, text: str, deflection: float = 0.0):
        """The live balance indicator above the credit line.

        `deflection` runs -1 (fully forward) to +1 (fully aft) and only
        positions the marker; it is not a centre-of-gravity measurement.
        """
        self.page().runJavaScript(
            f"setCogStatus({json.dumps(state)}, {json.dumps(text)}, "
            f"{float(deflection)});")

    def show_cache_limits(self, map_mb: int, terrain_mb: int):
        """Point the cache dropdowns at what the app has actually applied."""
        self.page().runJavaScript(
            f"showCacheLimits({int(map_mb)}, {int(terrain_mb)});")

    def set_home(self, lat: float, lon: float):
        """Place (or move) the home marker."""
        self.page().runJavaScript(f"setHome({float(lat)}, {float(lon)});")

    def clear_home(self):
        self.page().runJavaScript("clearHome();")

    def set_home_bearing(self, bearing_deg: float):
        """Which way home lies, for the compass arrow. Negative hides it."""
        self.page().runJavaScript(f"setHomeBearing({float(bearing_deg)});")

    def set_wind(self, direction_from_deg: float, speed_mps: float):
        """Wind for the compass rose. Direction is where it blows FROM, the
        convention the WIND message and the HUD both use."""
        self.page().runJavaScript(
            f"setWind({float(direction_from_deg)}, {float(speed_mps)});"
        )

    def set_turn_rate(self, deg_per_s: float):
        self.page().runJavaScript(f"setTurnRate({float(deg_per_s)});")

    def set_follow(self, follow: bool):
        self.page().runJavaScript(f"setFollow({'true' if follow else 'false'});")

    def clear_trail(self):
        self.page().runJavaScript("clearTrail();")

    def show_target(self, lat: float, lon: float):
        """Mark a typed fly-to coordinate, panning to it if it's off screen."""
        self.page().runJavaScript(f"showTarget({float(lat)}, {float(lon)});")

    def clear_target(self):
        self.page().runJavaScript("clearTarget();")

    def set_waypoint_mode(self, enabled: bool):
        self.page().runJavaScript(f"setWaypointMode({'true' if enabled else 'false'});")

    def clear_waypoints(self):
        self.page().runJavaScript("clearWaypoints();")

    def commit_waypoints(self):
        self.page().runJavaScript("commitWaypoints();")

    def update_terrain_fan(self, elevations: list, range_m: float, ang_cells: int, rad_cells: int):
        """Push a freshly-sampled terrain fan (see TerrainRadarWorker) - rare,
        only called when position/heading/range moved enough to matter."""
        elev_json = json.dumps(elevations)
        self.page().runJavaScript(
            f"setTerrainFan({elev_json}, {range_m}, {ang_cells}, {rad_cells});"
        )

    def update_terrain_reference(self, alt_msl: float, ground_speed: float, climb_mps: float):
        """Push current altitude/speed/climb for the terrain radar's live
        (no new sampling) colour recompute - called on every telemetry tick."""
        self.page().runJavaScript(f"setTerrainRef({alt_msl}, {ground_speed}, {climb_mps});")

    def mark_mission_sent(self):
        """The vehicle has accepted the mission: edited altitudes are live."""
        self.page().runJavaScript("markMissionSent();")

    def set_waypoint_default_alt(self, alt: float):
        """So a waypoint with no altitude of its own shows what it will fly."""
        self.page().runJavaScript(f"setWaypointDefaultAlt({float(alt)});")

    def update_adsb_contacts(self, contacts: list):
        """Push a freshly-fetched ADS-B contact list (see AdsbWorker) for the
        map to render as markers - replaces whatever was shown before."""
        self.page().runJavaScript(f"renderAdsbContacts({json.dumps(contacts)});")

    def update_tile_cache_stats(self, tiles: int, used_bytes: int, limit_bytes: int):
        """Refresh the map-cache readout (bar and figures)."""
        self.page().runJavaScript(
            f"setTileCacheStats({tiles}, {used_bytes}, {limit_bytes});"
        )

    def update_terrain_cache_stats(self, tiles: int, used_bytes: int, limit_bytes: int):
        """Refresh the terrain-cache readout (bar and figures)."""
        self.page().runJavaScript(
            f"setTerrainCacheStats({tiles}, {used_bytes}, {limit_bytes});"
        )

