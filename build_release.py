"""
Build the distributable MavGCS release: a folder containing MavGCS.exe,
zipped up ready to attach to a GitHub release.

    python build_release.py

Produces  dist/MavGCS/MavGCS.exe  and  dist/MavGCS-<version>-windows.zip

Not needed to run MavGCS from source - this is only for cutting a release.
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The Qt/WebEngine payload gets written out twice (build/ then dist/,
# roughly 400MB each) before the ~200MB zip is added on top, so peak usage
# is around 1GB. 3GB leaves clear headroom while still bailing out early
# rather than dying halfway through with a full disk.
REQUIRED_FREE_GB = 3


def app_version() -> str:
    text = (HERE / "main.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "dev"


def check_free_space():
    free_gb = shutil.disk_usage(HERE).free / 1e9
    print(f"Free disk space: {free_gb:.1f} GB")
    if free_gb < REQUIRED_FREE_GB:
        sys.exit(
            f"Need about {REQUIRED_FREE_GB} GB free to build (Qt WebEngine is large).\n"
            "Tip: deleting terrain_cache/ frees a few hundred MB - it re-downloads on demand."
        )


def main():
    os.chdir(HERE)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is not installed. Run:  pip install pyinstaller")

    # Clear the previous build BEFORE checking free space - those two
    # directories are most of a gigabyte, and counting them as "used" made
    # the space check reject a build that would have fitted comfortably.
    # A stale build/ also silently reuses old analysis results, which is a
    # classic source of "I fixed that but the exe still misbehaves".
    for stale in ("build", "dist"):
        if Path(stale).exists():
            print(f"Removing stale {stale}/ ...")
            shutil.rmtree(stale, ignore_errors=True)

    check_free_space()

    print("Running PyInstaller (this takes several minutes) ...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "MavGCS.spec"],
        check=True,
    )

    exe = HERE / "dist" / "MavGCS" / "MavGCS.exe"
    if not exe.exists():
        sys.exit(f"Build finished but {exe} is missing.")

    version = app_version()
    zip_path = HERE / "dist" / f"MavGCS-{version}-windows.zip"
    print(f"Zipping -> {zip_path.name} ...")
    src = HERE / "dist" / "MavGCS"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in src.rglob("*"):
            if path.is_file():
                # Keep a top-level MavGCS/ folder inside the zip so it
                # can't explode loose files into the user's Downloads.
                z.write(path, Path("MavGCS") / path.relative_to(src))

    size_mb = zip_path.stat().st_size / 1e6
    print("\nDone.")
    print(f"  Executable: {exe}")
    print(f"  Zip:        {zip_path}  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
