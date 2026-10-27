@echo off
setlocal
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
powershell.exe -NoProfile -NonInteractive -Command "try { $mhHealth=Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2; if ($mhHealth.status -eq 'ok' -and $mhHealth.mode -eq 'transcript') { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
 start "" "http://127.0.0.1:8765"
 exit /b 0
)
if defined MEDIAHARVEST_PYTHON (set "PY=%MEDIAHARVEST_PYTHON%") else (set "PY=%~dp0.venv\Scripts\python.exe")
if not exist "%PY%" if exist "data\runtime-python.txt" set /p PY=<"data\runtime-python.txt"
if not exist "%PY%" (
 echo Set MEDIAHARVEST_PYTHON to a Python with requirements installed.
 pause
 exit /b 1
)
"%PY%" web.py --open
if errorlevel 1 pause
endlocal
