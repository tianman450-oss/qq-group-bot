Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py -3"
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    throw "Python 3 was not found in PATH."
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pythonCmd = Get-PythonCommand
$venvPath = Join-Path $root ".venv"
$venvPython = Join-Path $venvPath "Scripts\\python.exe"

Write-Host "[1/4] Preparing virtual environment..."
if (-not (Test-Path $venvPython)) {
    & cmd /c "$pythonCmd -m venv .venv"
}

Write-Host "[2/4] Upgrading pip..."
& $venvPython -m pip install --upgrade pip

Write-Host "[3/4] Installing dependencies..."
& $venvPython -m pip install -r requirements.txt

Write-Host "[4/4] Preparing .env..."
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
} else {
    Write-Host ".env already exists, keeping current file."
}

Write-Host ""
Write-Host "Bootstrap finished."
Write-Host "Next steps:"
Write-Host "1. Edit .env"
Write-Host "2. Activate venv: .\\.venv\\Scripts\\activate"
Write-Host "3. Run checks: .\\scripts\\check.ps1"
Write-Host "4. Start bot: python bot.py"
