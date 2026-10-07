#!/bin/sh
# ---------------------------------------------------------------------
#  Double-click this in Finder to run MavGCS from this source folder.
#
#  It is a wrapper around run.sh and does nothing run.sh does not do.
#  Finder needs the .command extension to launch a script by
#  double-click - a .sh opens in an editor instead - so this exists to
#  be double-clicked, and run.sh stays the one to type.
#
#  A Terminal window opens with it and stays open while MavGCS runs.
#  That is deliberate: if the program stops with a traceback, the
#  traceback is in that window. Closing the window stops MavGCS.
#
#  This is not the packaged application. It runs the source you have
#  right now, which is what makes it useful while changing things - no
#  rebuild between edits. Two things follow from that:
#
#    * The camera will not work. macOS credits a camera request to the
#      responsible process, which here is Terminal rather than MavGCS,
#      so it is refused without ever asking. The camera needs the
#      bundle: open dist/MavGCS.app.
#    * Only one copy can run at a time. The second cannot bind the tile
#      proxy's port and its map comes up blank.
# ---------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1

if [ ! -x ./run.sh ]; then
    printf '\n  run.sh is missing or not executable beside this file.\n'
    printf '  Keep run.command in the MavGCS source folder.\n\n'
    exit 1
fi

exec ./run.sh "$@"
