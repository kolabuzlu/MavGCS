## Installing & Running MavGCS (Windows)

Download `MavGCS-<version>-windows.zip` from the
[Releases page](https://github.com/kolabuzlu/MavGCS/releases), extract it,
and run **MavGCS.exe**. No setup needed.

## Running from source

For working on MavGCS rather than just flying with it. Needs
[Python](https://www.python.org/downloads/) 3.10 or newer, with
**Add python.exe to PATH** ticked during setup.

Double-click **`run.bat`**, or from a terminal in this folder:

```
.\run.bat                        listen on UDP 14550 (the default)
.\run.bat tcp:127.0.0.1:5762     SITL over TCP
.\run.bat COM5:460800            a radio on a serial port
.\run.bat --selftest             check the link only, no window
```

The `.\` matters in PowerShell: it will not run a script from the folder
you are standing in without it. In the old Command Prompt the plain name
works either way.

It finds Python, installs anything missing from `requirements.txt` the
first time, and passes whatever you type after it straight to `main.py`.
If MavGCS stops with an error the window stays open so you can read it.

## When it crashes

`watch.bat` runs MavGCS and records everything needed to explain a crash
afterwards. Same arguments as `run.bat`:

```
.\watch.bat                       listen on UDP 14550
.\watch.bat tcp:127.0.0.1:5762    SITL over TCP
.\watch.bat COM5:460800           a radio on a serial port
```

Fly as normal. If it dies, the log under `logs\` holds the stack of every
thread at the moment of the fault, whatever Chromium was complaining
about beforehand, a memory sample every five seconds, and what Windows
recorded afterwards.

It only writes things down - MavGCS behaves exactly as it does under
`run.bat`.
