$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw 'Run ./setup.ps1 first' }
    & ./.venv/Scripts/python.exe -m qwork terminal @args
    if ($LASTEXITCODE -ne 0) { throw 'Terminal startup failed' }
} finally { Pop-Location }
