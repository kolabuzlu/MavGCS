# Release history

Every version this project has tagged, and the commit it was cut from.

Kept as a file because the older tags were removed from GitHub once
their releases were gone - the commits are all still in main's history,
but without this the only way back to a given version would be hunting
through dates. Restore any of them with:

    git tag -a V1.15.0 -m 'MavGCS V1.15.0' <commit>
    git push origin refs/tags/V1.15.0

| Version | Commit | Date | Cut from |
| --- | --- | --- | --- |
| V1.10.0 | `920433bce2` | 2026-08-28 | MavGCS V1.10.0: vehicle state indicator + Messages logo watermark |
| V1.11.0 | `c1d5db631d` | 2026-08-28 | MavGCS V1.11.0: ADS-B traffic, Change Loiter Radius, maximized window |
| V1.12.0 | `d2813fca97` | 2026-08-29 | Open the map at a fixed home view on ESRI World Imagery |
| V1.13.0 | `009984657c` | 2026-08-30 | Place the 3D camera from terrain height, not sea level |
| V1.14.0 | `4fc7affc49` | 2026-09-01 | Only follow the message log when already at its tail |
| V1.14.1 | `33d6936a3f` | 2026-09-01 | V1.14.1 |
| V1.15.0 | `b6210e67c5` | 2026-09-01 | Play the 3D view back smoothly on a slow telemetry link |
| V1.16.0 | `fc8421b530` | 2026-09-02 | Update CHANGELOG.md |
| V1.17.0 | `9d549a61e4` | 2026-09-02 | Update CHANGELOG.md |
| V1.17.1 | `3fe3f2aa1c` | 2026-09-02 | Mark home on the map |
| V1.17.2 | `eae31f4ade` | 2026-09-02 | Export the flown track as a KMZ for Google Earth |
| V1.17.3 | `72a58fb31b` | 2026-09-02 | Offer the flight track as KMZ, KML or GPX |
| V1.17.4 | `9d203915ab` | 2026-09-02 | Let a drag take the map back from Follow UAV |
| V1.17.5 | `82388c8f5d` | 2026-09-03 | Release V1.17.5 |
| V1.17.6 | `d78059d84d` | 2026-09-03 | Stop the throttle caption reaching the frame on any display |
| V1.18.0 | `11c696152a` | 2026-09-04 | Darken the wind arrow away from the wind speed text |
| V1.18.1 | `02f5a73063` | 2026-09-05 | Release V1.18.1 |
| V1.18.2 | `8a9c3f6105` | 2026-09-05 | Release V1.18.2 |
| V1.19.0 | `f9d7d64395` | 2026-09-05 | Check the legs between waypoints, not only the waypoints |
| V2.0.0 | `81af4b13d9` | 2026-09-05 | Record V2.0.0 in the release history |
| V2.0.1 | `9248c6fc62` | 2026-09-05 | Record V2.0.1 in the release history |
| V2.0.2 | `adc6540162` | 2026-09-05 | Record V2.0.2 in the release history |
