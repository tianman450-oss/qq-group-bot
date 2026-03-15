Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PythonExecutable {
    $venvPython = Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\\Scripts\\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    throw "Python was not found. Run .\\scripts\\bootstrap.ps1 first."
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pythonExe = Get-PythonExecutable

Write-Host "[1/2] Running py_compile..."
& $pythonExe -m py_compile `
    bot.py `
    src/utils/study.py `
    src/utils/roleplay.py `
    src/plugins/chat_plugin/__init__.py `
    src/plugins/course_plugin/__init__.py `
    src/plugins/draw_plugin/__init__.py `
    src/plugins/scheduler_plugin/__init__.py

Write-Host "[2/2] Loading plugins..."
& $pythonExe -X utf8 -c "import nonebot; nonebot.init(); nonebot.load_plugins('src/plugins'); print('plugins_loaded')"

Write-Host ""
Write-Host "All basic checks passed."
