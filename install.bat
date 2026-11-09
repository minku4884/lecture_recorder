@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Lecture Recorder - Install
echo Lecture Recorder installation
echo Python 3.10 or newer is required. Python 3.12 is recommended.
set "LR_PY_COMMAND="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "LR_PY_COMMAND=py -3"
if not defined LR_PY_COMMAND (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "LR_PY_COMMAND=python"
)
if not defined LR_PY_COMMAND (
    echo Python was not found. Install Python from https://www.python.org/downloads/windows/
    echo Enable "Add python.exe to PATH" in the installer, then run install.bat again.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    %LR_PY_COMMAND% -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import tkinter; print('Python / Tk: OK')"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
echo Installing fallback Chromium browser...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo Chromium download failed. Installed Chrome or Edge can still be used.
    echo Choose Chrome or Edge in the app. Retry install.bat to install Chromium later.
)
echo Installation complete. Start OBS Studio, then run run.bat.
pause
exit /b 0
:failed
echo Installation failed. Check the messages above and run install.bat again.
pause
exit /b 1
