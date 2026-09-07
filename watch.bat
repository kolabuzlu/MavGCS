@echo off
rem ---------------------------------------------------------------------
rem  Run MavGCS with everything recorded, so a crash can be explained.
rem
rem      watch.bat                       listen on UDP 14550
rem      watch.bat tcp:127.0.0.1:5762    SITL over TCP
rem      watch.bat COM5:460800           a radio on a serial port
rem
rem  In PowerShell put .\ in front - it will not run a
rem  script from the folder you are standing in without it.
rem
rem  Writes one file per run into logs\. When it dies, give Claude that
rem  file - it holds the stack of every thread at the moment of the
rem  fault, Chromium's own log, and what Windows recorded afterwards.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "tools\watch_run.py" (
    echo.
    echo   tools\watch_run.py is missing. Keep watch.bat in the MavGCS
    echo   source folder.
    echo.
    pause
    exit /b 1
)

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
echo   Python was not found. See INSTALL.md.
echo.
pause
exit /b 1

:got_python
%PY% tools\watch_run.py %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
