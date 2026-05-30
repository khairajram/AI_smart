@echo off
REM ─── ReID Analytics Dashboard — Quick Start ───────────────────────────────
REM  Starts the Node.js backend on http://localhost:3000
REM  The dashboard opens automatically. Python ReID engine is optional.
REM ──────────────────────────────────────────────────────────────────────────

TITLE ReID Dashboard

cd /d "%~dp0backend"

IF NOT EXIST "node_modules" (
    echo [Setup] Installing dependencies...
    npm install
    IF ERRORLEVEL 1 (
        echo [Error] npm install failed. Is Node.js installed?
        pause
        exit /b 1
    )
)

echo.
echo  Starting ReID Analytics Dashboard...
echo  Dashboard: http://localhost:3000
echo  API:       http://localhost:3000/api/health
echo  Press Ctrl+C to stop
echo.

node server.js
