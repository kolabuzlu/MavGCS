## Installing & Running MavGCS

### Windows

Download `MavGCS-<version>-windows.zip` from the
[Releases page](https://github.com/kolabuzlu/MavGCS/releases), extract it,
and run **MavGCS.exe**. No setup needed.

### macOS

Download `MavGCS-<version>-macos-<arch>.zip` from the
[Releases page](https://github.com/kolabuzlu/MavGCS/releases) and drag
**MavGCS.app** to your Applications folder.

Pick the file that matches your Mac: `arm64` for Apple Silicon (M1 and
later), `x86_64` for an Intel Mac. About This Mac will tell you which you
have.

#### "MavGCS is damaged and can't be opened"

It isn't damaged. MavGCS is not signed with an Apple Developer
certificate, and macOS puts every unsigned app it downloads into
quarantine. "Damaged" is simply the wrong message for it.

Clear the quarantine flag once:

```
xattr -dr com.apple.quarantine /Applications/MavGCS.app
```

Then open it normally. You only need to do this once per download - not
every launch - and it does nothing except remove the download marker
macOS attached to the file.

If you would rather not use Terminal: right-click the app and choose
**Open**, then confirm at the prompt. That works on some macOS versions
and not others, which is why the command above is given first.

#### The camera

The first time you press Start in the Video window, macOS asks whether
MavGCS may use the camera. Allow it, then press Start again - the request
and the first attempt to open the device happen together, so that first
attempt does not succeed.

If you refuse and change your mind, the switch is in System Settings ->
Privacy & Security -> Camera.

### Running from source

Either platform, if you want to change MavGCS rather than just use it.
You need Python 3.9 or newer and about 1GB for the libraries.

**Windows** - `run.bat` (in PowerShell, `.\run.bat`).

**macOS** - `./run.sh`.

Both find a Python, install what is missing from `requirements.txt` on
the first run, and then start the program. Anything you type after them
is passed to `main.py`, so:

```
./run.sh                              listen on udp 14550 (the default)
./run.sh tcp:127.0.0.1:5762           SITL over tcp
./run.sh /dev/cu.usbserial-XXXX:57600 a radio on a serial port
./run.sh --selftest                   check the link only, no window
```

`ls /dev/cu.*` lists the serial devices. Use the `cu.` name rather than
the `tty.` one for the same device: opening `tty.` on a Mac waits for a
carrier signal that a telemetry radio never raises, so it appears to
hang.

The one difference between the two scripts: `run.sh` keeps the libraries
in `.venv/` instead of installing them into the Python it found, because
a Homebrew or python.org Python refuses to install outside a virtual
environment and the Xcode one is shared with the system.
