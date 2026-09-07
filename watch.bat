@echo off
rem ---------------------------------------------------------------------
rem  Run MavGCS with everything recorded, so a crash can be explained.
rem
rem      watch.bat        (in PowerShell:  .\watch.bat)
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
%PY% tools\watch_run.py
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
