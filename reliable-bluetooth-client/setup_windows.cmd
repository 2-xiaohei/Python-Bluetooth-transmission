@echo off
setlocal
cd /d "%~dp0"

echo [%date% %time:~0,8%] [INFO] Creating Python virtual environment...
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 goto :error

echo [%date% %time:~0,8%] [INFO] Installing Reliable Bluetooth...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install .
if errorlevel 1 goto :error

echo [%date% %time:~0,8%] [ OK ] Installation complete. Run run_windows.cmd to start.
exit /b 0

:error
echo [%date% %time:~0,8%] [FAIL] Installation failed. Confirm that Python 3.11 or newer is installed.
exit /b 1
