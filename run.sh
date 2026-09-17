#!/bin/sh
# ---------------------------------------------------------------------
#  Run MavGCS from this source folder, on macOS.
#
#      ./run.sh                              listen on udp 14550 (default)
#      ./run.sh tcp:127.0.0.1:5762           SITL over tcp
#      ./run.sh /dev/cu.usbserial-XXXX:57600 a radio on a serial port
#      ./run.sh --selftest                   check the link only, no window
#
#  To find the name of a serial radio:  ls /dev/cu.*
#  Use the cu.* name and not the tty.* one - opening tty.* on a Mac waits
#  for a carrier signal a telemetry radio never raises, so it hangs.
#
#  Anything typed after run.sh is handed straight to main.py.
#
#  The Windows equivalent is run.bat, and the two differ in one way worth
#  knowing: this one keeps the libraries in .venv/ rather than installing
#  them into whichever Python it found. That is not tidiness. A Homebrew
#  or python.org Python refuses "pip install" outside a virtual
#  environment - PEP 668, which reports itself as
#  "error: externally-managed-environment" - and the Python that comes
#  with the Xcode command line tools is shared with the system and should
#  not be written into either. On a Mac the venv is the only place these
#  packages can go, so the script makes one rather than explaining itself
#  after the fact.
# ---------------------------------------------------------------------

# Work from the folder this file lives in, so running it by a path from
# somewhere else still finds main.py.
cd "$(dirname "$0")" || exit 1

if [ ! -f main.py ]; then
    printf '\n  main.py is not next to this script.\n'
    printf '  Keep run.sh in the MavGCS source folder.\n\n'
    exit 1
fi

VENV=".venv"
PYTHON="$VENV/bin/python"
# The same import line run.bat checks, so both platforms agree on what
# "the libraries are there" means.
NEEDED="import PySide6, pymavlink, serial, numpy, tifffile, imagecodecs"

# --- find a Python, but only when there is no venv yet ----------------
if [ ! -x "$PYTHON" ]; then
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
           'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
           >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done

    if [ -z "$PY" ]; then
        printf '\n  No Python 3.9 or newer was found.\n\n'
        printf '  Install one with:   brew install python3\n'
        printf '  or from:            https://www.python.org/downloads/\n\n'
        exit 1
    fi

    printf '  %s\n' "$("$PY" --version 2>&1)"
    printf '  Creating %s/ ...\n' "$VENV"
    if ! "$PY" -m venv "$VENV"; then
        printf '\n  Could not create the virtual environment in %s/.\n\n' "$VENV"
        exit 1
    fi
fi

printf '  %s\n' "$("$PYTHON" --version 2>&1)"

# --- make sure the libraries are there --------------------------------
if ! "$PYTHON" -c "$NEEDED" >/dev/null 2>&1; then
    printf '\n  Some of the libraries MavGCS needs are missing.\n'
    printf '  Installing them from requirements.txt - this takes a few\n'
    printf '  minutes the first time, PySide6 is a large download.\n\n'

    # pip first, and quietly. The version bundled with an older Python
    # can fail to match the wheel tags PySide6 publishes, and the error
    # it gives for that is "could not find a version that satisfies the
    # requirement" - which reads as a missing package rather than as an
    # old pip.
    "$PYTHON" -m pip install --quiet --upgrade pip

    if ! "$PYTHON" -m pip install -r requirements.txt; then
        printf '\n  The install did not finish. The message above says why.\n\n'
        exit 1
    fi

    if ! "$PYTHON" -c "$NEEDED" >/dev/null 2>&1; then
        printf '\n  The install finished but the libraries still will not\n'
        printf '  import. Run this to see the real error:\n\n'
        printf '      %s -c "import PySide6"\n\n' "$PYTHON"
        exit 1
    fi
fi

printf '  Starting MavGCS...\n\n'
"$PYTHON" main.py "$@"
RC=$?

# run.bat pauses here, because a double-clicked console window on Windows
# closes and takes the traceback with it. A terminal on a Mac keeps its
# scrollback, so the traceback is already above this line and the only
# thing worth adding is the code it died with.
if [ "$RC" -ne 0 ]; then
    printf '\n  MavGCS exited with code %s.\n\n' "$RC"
fi
exit "$RC"
