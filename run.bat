@echo off
rem ---------------------------------------------------------------------
rem  Run MavGCS from this source folder.
rem
rem  Double-click it, or from a terminal:
rem
rem      run.bat                        listen on udp 14550 (the default)
rem      run.bat tcp:127.0.0.1:5762     SITL over tcp
rem      run.bat COM5,57600             a radio on a serial port
rem      run.bat --selftest             check the link only, no window
rem
rem  Anything typed after run.bat is handed straight to main.py.
rem ---------------------------------------------------------------------
setlocal

rem Work from the folder this file lives in, so a double-click from
rem anywhere still finds main.py.
cd /d "%~dp0"

if not exist "main.py" (
    echo.
    echo   main.py is not next to this script.
    echo   Keep run.bat in the MavGCS source folder.
    echo.
    pause
    exit /b 1
)

rem --- find a Python ---------------------------------------------------
rem The py launcher first: it is what a normal Windows install provides,
rem and it picks a sane version without depending on PATH order.
set "PY="
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
    goto :got_python
)
where python >nul 2>&1
if not errorlevel 1 (
    set "PY=python"
    goto :got_python
)

echo.
echo   Python was not found.
echo.
echo   Install Python 3.10 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then run this again.
echo.
pause
exit /b 1

:got_python
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo   %PYVER%

rem --- make sure the libraries are there --------------------------------
%PY% -c "import PySide6, pymavlink, serial, numpy, tifffile, imagecodecs" >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo   Some of the libraries MavGCS needs are missing.
echo   Installing them from requirements.txt - this takes a few minutes
echo   the first time, PySide6 is a large download.
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   The install did not finish. The message above says why.
    echo   A proxy or a missing Visual C++ runtime are the usual causes.
    echo.
    pause
    exit /b 1
)

%PY% -c "import PySide6, pymavlink, serial, numpy, tifffile, imagecodecs" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   The install finished but the libraries still will not import.
    echo   Run this to see the real error:
    echo       %PY% -c "import PySide6"
    echo.
    pause
    exit /b 1
)

:run
echo   Starting MavGCS...
echo.
%PY% main.py %*
set "RC=%errorlevel%"

rem A window that vanishes takes the traceback with it, which is the one
rem thing somebody running from source needs to see.
if not "%RC%"=="0" (
    echo.
    echo   MavGCS exited with code %RC%.
    echo.
    pause
)
exit /b %RC%
