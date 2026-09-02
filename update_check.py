"""
Checking GitHub for a newer MavGCS release, and fetching it.

This deliberately does NOT replace the running installation. A PyInstaller
one-folder build cannot overwrite its own .exe or _internal/ while those
files are open, so a true self-updater has to hand off to a helper process
that waits for the app to exit, swaps the folders and relaunches. When that
helper fails halfway - antivirus holding a handle, a half-extracted folder,
no permission to write the install directory - the user is left with no
working install at all. Downloading the zip and showing them where it
landed keeps the worst case at "there is a zip you have to unpack", which
is recoverable without a working copy of the program.

Everything here is best-effort: no network, a rate-limited API and a
malformed response are all normal conditions, not errors worth interrupting
a flight over.
"""

import hashlib
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal

REPO = "kolabuzlu/MavGCS"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

REQUEST_TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 60
CHUNK_BYTES = 256 * 1024

# GitHub requires a User-Agent on API requests and rejects the default one.
HEADERS = {
    "User-Agent": "MavGCS",
    "Accept": "application/vnd.github+json",
}


def parse_version(tag):
    """(1, 16, 0) from "V1.16.0", or None if it isn't a version at all.

    Compared as numbers rather than text on purpose: "V1.9.0" sorts after
    "V1.16.0" as a string, which would offer a downgrade as an update.
    """
    if not tag:
        return None
    m = re.match(r"\s*[vV]?(\d+)\.(\d+)\.(\d+)", str(tag))
    return tuple(int(g) for g in m.groups()) if m else None


def is_newer(candidate, current) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        # Unparseable either way: say no. Offering an update we cannot
        # reason about is worse than staying quiet.
        return False
    return a > b


class UpdateChecker(QThread):
    """Asks GitHub what the newest release is. One shot, then finishes."""

    # dict, described in _result() below
    result_ready = Signal(dict)

    def __init__(self, current_version: str, parent=None):
        super().__init__(parent)
        self._current = current_version

    @staticmethod
    def _result(**kw):
        base = {
            "ok": False, "error": None, "tag": "", "notes": "",
            "page_url": RELEASES_PAGE, "newer": False,
            "asset_name": "", "asset_url": "", "asset_size": 0,
            "asset_sha256": None,
        }
        base.update(kw)
        return base

    def run(self):
        try:
            req = urllib.request.Request(LATEST_URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # Unauthenticated callers get 60 requests an hour per IP.
                msg = ("GitHub is rate-limiting this connection. "
                       "Try again in a few minutes.")
            elif e.code == 404:
                msg = "No published release found."
            else:
                msg = f"GitHub returned HTTP {e.code}."
            self.result_ready.emit(self._result(error=msg))
            return
        except (urllib.error.URLError, OSError) as e:
            self.result_ready.emit(
                self._result(error=f"Could not reach GitHub: {e}"))
            return
        except (ValueError, TypeError):
            self.result_ready.emit(
                self._result(error="GitHub sent a response I couldn't read."))
            return

        tag = data.get("tag_name") or ""
        if parse_version(tag) is None:
            self.result_ready.emit(
                self._result(error=f"Latest release is tagged '{tag}', "
                                   "which isn't a version number."))
            return

        # The Windows zip, if this release has one. A release with no asset
        # is a valid state (source-only), not a failure.
        asset = None
        for a in data.get("assets") or []:
            name = a.get("name") or ""
            if name.lower().endswith(".zip"):
                asset = a
                break

        digest = (asset or {}).get("digest") or ""
        sha = digest.split("sha256:", 1)[1] if digest.startswith("sha256:") else None

        self.result_ready.emit(self._result(
            ok=True,
            tag=tag,
            newer=is_newer(tag, self._current),
            notes=(data.get("body") or "").strip(),
            page_url=data.get("html_url") or RELEASES_PAGE,
            asset_name=(asset or {}).get("name") or "",
            asset_url=(asset or {}).get("browser_download_url") or "",
            asset_size=(asset or {}).get("size") or 0,
            asset_sha256=sha,
        ))


class UpdateDownloader(QThread):
    """Fetches the release zip, reporting progress and verifying the result.

    Writes to a .part file and only renames it into place once the whole
    body has arrived and its hash matches, so an interrupted download can
    never be mistaken for a usable one.
    """

    progress = Signal(int, int)      # bytes received, total (0 if unknown)
    finished_ok = Signal(str)        # final path
    failed = Signal(str)

    def __init__(self, url: str, dest: Path, expected_size: int = 0,
                 expected_sha256: str = None, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest = Path(dest)
        self._expected_size = expected_size
        self._expected_sha = (expected_sha256 or "").lower() or None
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        part = self._dest.with_suffix(self._dest.suffix + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(self._url, headers={"User-Agent": "MavGCS"})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
                total = int(resp.headers.get("Content-Length") or
                            self._expected_size or 0)
                self.progress.emit(0, total)
                with open(part, "wb") as f:
                    while True:
                        if self._cancel.is_set():
                            f.close()
                            part.unlink(missing_ok=True)
                            self.failed.emit("Download cancelled.")
                            return
                        chunk = resp.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        f.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        self.progress.emit(received, total)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            part.unlink(missing_ok=True)
            self.failed.emit(f"Download failed: {e}")
            return

        if self._expected_size and received != self._expected_size:
            part.unlink(missing_ok=True)
            self.failed.emit(
                f"Download is the wrong size ({received} bytes, "
                f"expected {self._expected_size}). Nothing was saved.")
            return

        if self._expected_sha and digest.hexdigest() != self._expected_sha:
            part.unlink(missing_ok=True)
            self.failed.emit(
                "The downloaded file does not match the checksum GitHub "
                "published for it. Nothing was saved.")
            return

        try:
            self._dest.unlink(missing_ok=True)
            part.rename(self._dest)
        except OSError as e:
            part.unlink(missing_ok=True)
            self.failed.emit(f"Could not save the file: {e}")
            return

        self.finished_ok.emit(str(self._dest))
