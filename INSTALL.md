# Running MavGCS (Windows)

No Python, no setup, no terminal.

1. Download `MavGCS-<version>-windows.zip` from the
   [Releases page](https://github.com/kolabuzlu/MavGCS/releases).
2. Right-click the zip → **Extract All...** and pick somewhere to put it
   (Desktop or Documents is fine).
3. Open the extracted `MavGCS` folder and double-click **MavGCS.exe**.

That's it. Optionally right-click `MavGCS.exe` → *Send to* → *Desktop
(create shortcut)* so it's one click next time.

## "Windows protected your PC"

Windows shows this blue warning for any program it hasn't seen signed by a
paid certificate. Click **More info** → **Run anyway**.

This appears because the executable isn't code-signed (a signing
certificate is a paid, per-year thing), not because anything is wrong with
it. If you'd rather not take that on trust, the alternative is to run from
source - see the README.

## Connecting

Pick your connection at the top right, then press **Connect**:

- **Serial** - a telemetry radio on a COM port. Choose the port and the
  matching baud rate (57600 is typical).
- **TCP / UDP** - SITL, a companion computer, or a WiFi telemetry bridge.

## Notes

- **Internet is optional but useful.** The map tiles, the terrain radar's
  elevation data, and ADS-B traffic all come from online sources. Flying
  offline still works - those parts just won't populate. Terrain tiles
  already downloaded are cached and keep working offline.
- **Where files are kept.** Downloaded terrain tiles go in
  `%LOCALAPPDATA%\MavGCS\terrain_cache` (a few tens of MB per area you
  fly). Deleting that folder is safe - it re-downloads as needed.
- **Updating.** Download the new zip and extract it over the old folder,
  or into a new one. Your terrain cache is kept separately and isn't lost.
