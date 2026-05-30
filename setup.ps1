#!/usr/bin/env pwsh
# =============================================================================
#  AI_smart — Local Development Startup Script
#  Run this from the project root: .\setup.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host ""
Write-Host "=== AI Smart Store Intelligence — Local Setup ===" -ForegroundColor Cyan
Write-Host ""

# ── 1. Check / create Python 3.11 venv ───────────────────────────────────────
$venvPython = Join-Path $root "venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[1/4] Creating Python 3.11 virtual environment..." -ForegroundColor Yellow
    uv venv venv --python 3.11
    uv pip install --python $venvPython --upgrade pip setuptools wheel
} else {
    Write-Host "[1/4] Virtual environment already exists (Python 3.11)" -ForegroundColor Green
}

# ── 2. Install Python requirements ───────────────────────────────────────────
Write-Host "[2/4] Installing Python requirements..." -ForegroundColor Yellow
uv pip install --python $venvPython torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python $venvPython -r (Join-Path $root "requirements.txt")

# ── 3. Install torchreid (person ReID model) ─────────────────────────────────
Write-Host "[3/4] Installing torchreid (ReID model)..." -ForegroundColor Yellow
$torchreidCheck = ""
try {
    $torchreidCheck = & $venvPython -c "import torchreid; print('ok')" 2>$null
} catch {}

if ($torchreidCheck -ne "ok") {
    & $venvPython -m pip install gdown tensorboard h5py
    
    $localReidPath = Join-Path $root "deep-person-reid"
    if (-not (Test-Path $localReidPath)) {
        Write-Host "Cloning deep-person-reid..." -ForegroundColor Yellow
        git clone --depth 1 https://github.com/KaiyangZhou/deep-person-reid.git $localReidPath
    }
    
    # Modify setup.py to disable Cython/MSVC compilation
    $setupPyPath = Join-Path $localReidPath "setup.py"
    if (Test-Path $setupPyPath) {
        Write-Host "Modifying setup.py to allow Windows compilation without MSVC..." -ForegroundColor Yellow
        $setupContent = Get-Content $setupPyPath -Raw
        
        # Remove imports
        $setupContent = $setupContent -replace "import numpy as np`r?`n", ""
        $setupContent = $setupContent -replace "from distutils.extension import Extension`r?`n", ""
        $setupContent = $setupContent -replace "from Cython.Build import cythonize`r?`n", ""
        
        # Remove ext_modules definition
        $setupContent = $setupContent -replace "(?ms)ext_modules = \[.*?\]\s*`r?`n", ""
        
        # Remove ext_modules reference in setup call
        $setupContent = $setupContent -replace ",\s*ext_modules=cythonize\(ext_modules\)", ""
        
        # Fix find_version to parse rather than execute
        $setupContent = $setupContent -replace '(?ms)def find_version\(\):.*?return locals\(\)\[\x27__version__\x27\]', "def find_version():`r`n    version_file = osp.join(osp.dirname(osp.realpath(__file__)), 'torchreid', '__init__.py')`r`n    with open(version_file, 'r') as f:`r`n        for line in f:`r`n            if line.startswith('__version__'):`r`n                return line.split('=')[1].strip().strip(\x27`\x27).strip('\x22')`r`n    return '1.4.0'"
        
        Set-Content $setupPyPath $setupContent -Force
    }

    & $venvPython -m pip install --no-build-isolation -e $localReidPath
} else {
    Write-Host "  torchreid already installed" -ForegroundColor Green
}

# ── 4. Install Node.js dashboard dependencies ────────────────────────────────
Write-Host "[4/4] Installing Node.js dashboard dependencies..." -ForegroundColor Yellow
$backendDir = Join-Path $root "backend"
Set-Location $backendDir
npm install --silent
Set-Location $root

Write-Host ""
Write-Host "=== Setup Complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "To start all services, run:" -ForegroundColor Cyan
Write-Host "  .\start.ps1" -ForegroundColor White
Write-Host ""
