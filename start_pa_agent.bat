@echo off
REM ============================================================
REM  PA Agent Launcher (Web backend default; desktop GUI fallback)
REM  Usage   : Double-click this file to start.
REM  Project : Price Action AI Analysis Agent
REM ============================================================
title PA Agent

REM 切换到脚本所在目录（支持任意位置双击）
cd /d "%~dp0"

echo ============================================================
echo  Starting PA Agent (Price Action AI Analysis)...
echo  Project dir: %CD%
echo  Mode: Web backend (FastAPI + SSE) at http://localhost:8000
echo ============================================================
echo.

REM 优先使用 PATH 中的 python；找不到则提示安装
where python >nul 2>nul
if %errorlevel%==0 (
    echo Starting Web backend...
    echo Press Ctrl+C to stop. Browser: http://localhost:8000
    echo.
    python -m uvicorn web.server:app --host 0.0.0.0 --port 8000
) else (
    echo [ERROR] python not found in PATH.
    echo Please install Python 3.11+ from https://www.python.org/downloads/
    echo Or run via: python run.py
)

echo.
echo ============================================================
echo  PA Agent has exited.
echo  If the window closed unexpectedly, check:
echo    logs\pa_agent.log
echo    logs\crash.log
echo ============================================================
pause
