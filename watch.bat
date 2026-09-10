@echo off
rem ---------------------------------------------------------------------
rem  Run MavGCS with everything recorded, so a crash can be explained.
rem
rem      watch.bat            (in PowerShell:  .\watch.bat)
rem      watch.bat notrail    same, but the trail is held short
rem      watch.bat nogpu      draw the page on the CPU instead of
rem                           through ANGLE and Direct3D 11. The
rem                           compositor crash is an overflow of a
rem                           D3D11-only cache, so this route has no
rem                           such cache. For machines with no
rem                           discrete card to switch to.
rem
rem  No connection to give it - start it, then connect from the
rem  Connection panel as usual.
rem
rem  Writes one file per run into logs\. When it dies, give Claude that
rem  file: it holds the stack of every thread at the moment of the fault,
rem  Chromium's own log, and what Windows recorded afterwards.
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
