# Run GPT-Live Voice locally on Windows (slim conversation profile).
# Usage: from gpt_live_voice folder → .\run_local.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "Missing .env — copy .env.example to .env and set OPENAI_API_KEY"
    exit 1
}

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

$env:PYTHONPATH = $PSScriptRoot
$env:PYTHONUNBUFFERED = "1"
Write-Host "Starting slim local GPT-Live Voice on port from .env (default 8080)..."
python -m app.main
