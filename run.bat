@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Lecture Recorder
if not exist ".venv\Scripts\python.exe" (
    call install.bat
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo The program exited with an error. Check the message above.
    pause
    exit /b 1
)
exit /b 0
