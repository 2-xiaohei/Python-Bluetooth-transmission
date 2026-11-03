@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [%date% %time:~0,8%] [FAIL] Not installed. Run setup_windows.cmd first.
    exit /b 1
)

.venv\Scripts\python.exe -m reliable_bluetooth peer %*
