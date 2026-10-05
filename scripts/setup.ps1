param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
    }
    & ./.venv/Scripts/python.exe -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
} finally { Pop-Location }
