@echo off
rem ---------------------------------------------------------------------
rem  Reproduce the WebEngine compositor crash on the bench - no aircraft.
rem
rem      bench.bat [moving|notrail|frozen]
rem                       (in PowerShell:  .\bench.bat)
rem
rem  Flies a synthetic circuit at 2Hz with every overlay off, and says
rem  REPRODUCED or SURVIVED at the end. About seven minutes to reach the
rem  fault, eleven to clear it. Leave the window alone while it runs.
rem
rem  Writes one file per run into logs\.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "tools\bench_trail.py" (
    echo.
    echo   tools\bench_trail.py is missing. Keep bench.bat in the MavGCS
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
%PY% tools\bench_trail.py %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
