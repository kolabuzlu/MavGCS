"""
Build the distributable MavGCS release, zipped up ready to attach to a
GitHub release.

    python build_release.py

On Windows:  dist/MavGCS/MavGCS.exe  and  dist/MavGCS-<version>-windows.zip
On macOS:    dist/MavGCS.app         and  dist/MavGCS-<version>-macos-<arch>.zip

The build is always for the machine it runs on - PyInstaller freezes the
interpreter and the wheels it finds, so a release for the other platform
has to be built on the other platform. The architecture is in the macOS
name for the same reason: the wheels this depends on are single-arch, so
an Apple Silicon build will not run on an Intel Mac and the download
should say so before it is unzipped rather than after.

Not needed to run MavGCS from source - this is only for cutting a release.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACOS = sys.platform == "darwin"
# The Qt/WebEngine payload gets written out twice (build/ then dist/,
# roughly 400MB each) before the ~200MB zip is added on top, so peak usage
# is around 1GB. 3GB leaves clear headroom while still bailing out early
# rather than dying halfway through with a full disk.
REQUIRED_FREE_GB = 3


def built_app() -> Path:
    """The thing PyInstaller was asked to produce, which must now exist."""
    if MACOS:
        return HERE / "dist" / "MavGCS.app"
    return HERE / "dist" / "MavGCS" / "MavGCS.exe"


def payload_dir() -> Path:
    """The directory that gets zipped, and that LICENSE is copied into.

    On Windows this is the one-folder build itself. On macOS it is the
    .app, because the folder inside it is an implementation detail of
    the bundle and only the bundle is the application.
    """
    return built_app() if MACOS else HERE / "dist" / "MavGCS"


def zip_name(version: str) -> str:
    if MACOS:
        return "MavGCS-%s-macos-%s.zip" % (version, platform.machine())
    return "MavGCS-%s-windows.zip" % version


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


def archive(src: Path, zip_path: Path):
    """Zip the build, keeping a single top-level folder inside it.

    macOS gets ditto rather than zipfile, and not for convenience.
    A .app depends on two things zipfile discards: the executable bit on
    Contents/MacOS/MavGCS, without which the app cannot be launched at
    all, and the symlinks inside Qt's .framework directories, which
    zipfile follows and stores as duplicate copies of every library.
    ditto is the system's own archiver and preserves both.
    """
    if MACOS:
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             str(src), str(zip_path)],
            check=True,
        )
        return
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in src.rglob("*"):
            if path.is_file():
                # Keep a top-level MavGCS/ folder inside the zip so it
                # can't explode loose files into the user's Downloads.
                z.write(path, Path(src.name) / path.relative_to(src))


def check_signature():
    """Refuse to ship a bundle whose own seal does not check out.

    PyInstaller ad-hoc signs the .app, and anything added afterwards
    breaks that signature. It is worth catching here rather than in a
    bug report, because the damage is uneven: Intel Macs run an
    invalidly signed app quite happily, so a broken build looks fine on
    the machine that made it and fails only for everyone on Apple
    Silicon, where the kernel refuses to execute it.
    """
    result = subprocess.run(["codesign", "--verify", str(payload_dir())],
                            stderr=subprocess.PIPE, text=True)
    if result.returncode != 0 or "invalid" in (result.stderr or ""):
        sys.exit("The .app signature does not verify, so it would not "
                 "launch on Apple Silicon:\n  "
                 + (result.stderr or "").strip())
    print("Signature verifies.")


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

    app = built_app()
    if not app.exists():
        sys.exit(f"Build finished but {app} is missing.")

    # GPL v3 asks that the licence travel with the binary, and a zip of
    # just the program conveys none of it. Copied into dist/ rather than
    # written straight into the archive, so it lands beside MavGCS.exe
    # both in the folder that gets run from and in the zip - the rglob
    # in archive() picks it up with everything else.
    #
    # macOS does not come through here: the .app is ad-hoc signed by the
    # time this runs, and adding a file to a signed bundle invalidates
    # it, which on Apple Silicon stops the app launching at all. Its
    # copy is placed by the spec instead, before the signature, and only
    # checked for here.
    licence = HERE / "LICENSE"
    if not licence.exists():
        sys.exit("LICENSE is missing, and the release has to carry it.")
    if MACOS:
        carried = payload_dir() / "Contents" / "Resources" / "LICENSE"
        if not carried.exists():
            sys.exit(f"The build did not carry the licence to {carried}.")
        check_signature()
    else:
        shutil.copy2(licence, payload_dir() / "LICENSE")

    version = app_version()
    zip_path = HERE / "dist" / zip_name(version)
    print(f"Zipping -> {zip_path.name} ...")
    archive(payload_dir(), zip_path)

    size_mb = zip_path.stat().st_size / 1e6
    print("\nDone.")
    # Spelled out per platform rather than interpolated, so the Windows
    # lines stay character for character what they were.
    if MACOS:
        print(f"  Application: {app}")
        print(f"  Zip:         {zip_path}  ({size_mb:.0f} MB)")
    else:
        print(f"  Executable: {app}")
        print(f"  Zip:        {zip_path}  ({size_mb:.0f} MB)")
    if MACOS:
        # Said here because it is not discoverable anywhere else: the zip
        # is unsigned, so the copy the user downloads arrives quarantined
        # and Finder calls it "damaged". See INSTALL.md.
        print("\n  Unsigned, so a downloaded copy needs:"
              "\n    xattr -dr com.apple.quarantine /Applications/MavGCS.app")


if __name__ == "__main__":
    main()
