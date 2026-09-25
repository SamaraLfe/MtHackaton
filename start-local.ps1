$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$preparedPackages = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../work/packages'))
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython scripts/run_local.py
} elseif ((Test-Path -LiteralPath $bundledPython) -and (Test-Path -LiteralPath $preparedPackages)) {
    $env:PYTHONPATH = $preparedPackages
    & $bundledPython scripts/run_local.py
} else {
    Write-Host 'Install Python 3.12+, then: python -m venv .venv'
    Write-Host '.venv/Scripts/python -m pip install -r requirements.txt'
    Write-Host 'Or use: docker compose up --build'
    exit 1
}
