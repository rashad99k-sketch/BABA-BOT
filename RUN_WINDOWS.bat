@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PY_BOOT="
where py >nul 2>&1
if not errorlevel 1 set "PY_BOOT=py -3"
if not defined PY_BOOT (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_BOOT=python"
)
if not defined PY_BOOT (
    echo [ERROR] Python 3 was not found. Install Python 3.10+ and enable PATH.
    goto :FAIL
)
%PY_BOOT% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    goto :FAIL
)
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [INFO] Creating .venv...
    %PY_BOOT% -m venv "%ROOT%.venv"
    if errorlevel 1 goto :FAIL
)
set "PY=%ROOT%.venv\Scripts\python.exe"
"%PY%" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 goto :FAIL
"%PY%" "%ROOT%preflight.py"
if errorlevel 1 goto :FAIL
echo [INFO] Starting GGP using project .env. LIVE requires PAPER_MODE=false, ENABLE_LIVE_TRADING=true and valid BingX keys.
"%PY%" "%ROOT%ggpbot.py"
if errorlevel 1 goto :FAIL
goto :END
:FAIL
echo.
echo [FAILED] Review the error above. Press any key to close.
pause >nul
exit /b 1
:END
endlocal
exit /b 0
