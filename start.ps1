#!/usr/bin/env pwsh
# =============================================================================
#  AI_smart - Local Development Start Script
#  Starts Python API + Dashboard without Docker
#  Run from project root: .\start.ps1
# =============================================================================

$root  = $PSScriptRoot
$venvPy = Join-Path $root "venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "[ERROR] Virtual environment not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== AI Smart - Starting Local Services ===" -ForegroundColor Cyan
Write-Host ""

# -- Kill any previously running services on these ports ----------------------
$ports = @(8000, 3000)
foreach ($port in $ports) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        if ($connection) {
            foreach ($conn in $connection) {
                if ($conn.OwningProcess -gt 0) {
                    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
                }
            }
            Start-Sleep -Milliseconds 300
        }
    } catch {}
}

# -- Start Python API (FastAPI) ------------------------------------------------
Write-Host "[1/2] Starting Python API on http://localhost:8000..." -ForegroundColor Yellow
$apiJob = Start-Process -FilePath $venvPy `
    -ArgumentList "start_api.py" `
    -WorkingDirectory $root `
    -PassThru -WindowStyle Minimized

Write-Host "      API PID: $($apiJob.Id)" -ForegroundColor DarkGray
Start-Sleep -Seconds 3

# -- Start Dashboard (Node.js) ------------------------------------------------─
Write-Host "[2/2] Starting Dashboard on http://localhost:3000..." -ForegroundColor Yellow
$env:DEMO_MODE = "false"
$dashJob = Start-Process -FilePath "node" `
    -ArgumentList "server.js" `
    -WorkingDirectory (Join-Path $root "backend") `
    -PassThru -WindowStyle Minimized

Write-Host "      Dashboard PID: $($dashJob.Id)" -ForegroundColor DarkGray
Start-Sleep -Seconds 3

# -- Verify both are up --------------------------------------------------------
Write-Host ""
Write-Host "Checking services..." -ForegroundColor Cyan

try {
    $api = Invoke-RestMethod -Uri http://localhost:8000/health -TimeoutSec 5
    Write-Host "[OK] Python API - status: $($api.status), version: $($api.version)" -ForegroundColor Green
} catch {
    Write-Host "[WARN] Python API not responding yet - it may still be starting" -ForegroundColor Yellow
}

try {
    $dash = Invoke-RestMethod -Uri http://localhost:3000/api/health -TimeoutSec 5
    Write-Host "[OK] Dashboard - mode: $($dash.mode), python_connected: $($dash.python_connected)" -ForegroundColor Green
} catch {
    Write-Host "[WARN] Dashboard not responding yet - it may still be starting" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Services Started ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:  http://localhost:3000" -ForegroundColor White
Write-Host "  Python API: http://localhost:8000" -ForegroundColor White
Write-Host "  API Docs:   http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host "To run the pipeline against footage:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe main.py --serve --source footage\BRIGADE_BLR" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop this script (services keep running in background)" -ForegroundColor DarkGray
Write-Host "To stop services, run: Stop-Process -Id $($apiJob.Id),$($dashJob.Id)" -ForegroundColor DarkGray
