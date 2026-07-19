@echo off
REM ============================================================
REM  PA Agent Launcher
REM  Project : Price Action AI Analysis Agent
REM  Usage   : Double-click this file to start the GUI.
REM  Location: C:\PA_Agent\start_pa_agent.bat
REM ============================================================
title PA Agent

cd /d C:\PA_Agent
echo ============================================================
echo  Starting PA Agent (Price Action AI Analysis)...
echo  Project dir: C:\PA_Agent
echo ============================================================
echo.

REM Try python in PATH first; fall back to the managed runtime path.
where python >nul 2>nul
if %errorlevel%==0 (
    python run.py
) else (
    "C:\Users\MAC\.workbuddy\binaries\python\versions\3.13.12\python.exe" run.py
)

echo.
echo ============================================================
echo  PA Agent has exited.
echo  If the window closed unexpectedly, check:
echo    C:\PA_Agent\logs\pa_agent.log
echo    C:\PA_Agent\logs\crash.log
echo ============================================================
pause
