"""
Filesystem locations that differ between running from source and running
from a packaged (PyInstaller) build.

Running from source, everything lives next to the .py files. Once frozen,
that stops being true in two different ways, so the two cases are kept
apart deliberately:

  resource_path() - read-only assets shipped inside the bundle. PyInstaller
      unpacks these to sys._MEIPASS, which is NOT the directory the .exe
      sits in.

  data_dir() - somewhere writable that survives upgrades. The bundle
      directory is the wrong place for a 40MB-per-tile terrain cache: it
      gets replaced wholesale when the user unzips a new version, and on a
      "Program Files" style install it may not even be writable.
"""

import json
import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than source."""
    return getattr(sys, "frozen", False)


def resource_path(name: str) -> str:
    """Absolute path to a read-only asset shipped with the app."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def data_dir() -> Path:
    """
    Writable directory for caches. Frozen builds use the per-user app data
    folder; from source it stays in the project directory so an existing
    terrain_cache/ keeps working during development.
    """
    if is_frozen():
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = Path(root) / "MavGCS"
    else:
        path = Path(__file__).resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_file() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    """User settings that persist between runs (kept beside the caches, not
    inside the app bundle, so an upgrade doesn't wipe them)."""
    try:
        return json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value):
    settings = load_settings()
    settings[key] = value
    try:
        _settings_file().write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass
