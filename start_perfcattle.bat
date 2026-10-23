@echo off
setlocal
cd /d "%~dp0"
title PerfCattle

where python >nul 2>nul
if errorlevel 1 (
    echo [PerfCattle] Python not found in PATH.
    echo Install Python 3.10+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

python "%~dp0start_perfcattle.py" %*
if errorlevel 1 pause
