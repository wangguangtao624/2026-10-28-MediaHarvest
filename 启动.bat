@echo off
setlocal
title MediaHarvest
cd /d "%~dp0"

rem ============================================================
rem  MediaHarvest launcher -- double click this file.
rem
rem  NOTE: keep this file PURE ASCII (no Chinese characters).
rem  cmd.exe reads .bat files using the system ANSI codepage
rem  (GBK on Chinese Windows). A UTF-8 file turns Chinese
rem  comments into garbage that cmd tries to execute as
rem  commands, so the window closes instantly with errors.
rem ============================================================

if not exist "gui.py" (
    echo [x] gui.py not found. Put this launcher in the mediaharvest folder.
    pause
    exit /b 1
)

set "PY=..\videocaptioner\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [x] Python venv not found at:
    echo     %CD%\%PY%
    echo     Make sure videocaptioner\.venv still exists.
    pause
    exit /b 1
)

rem Clear env vars that make Python import the wrong packages.
set "PYTHONPATH="
set "PYTHONHOME="

echo Starting MediaHarvest GUI ...
echo First start warms up the GPU and takes about 5-10 seconds.
echo.

"%PY%" gui.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [!] MediaHarvest exited with code %RC%
    echo     If it is 139 or -1073741819 the CUDA init order broke.
    pause
    exit /b %RC%
)

echo.
echo MediaHarvest closed normally.
pause
endlocal
