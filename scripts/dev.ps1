# Starts the TriageX backend (port 8000) and frontend (port 5173) in two new windows.
# Usage (from the repo root):  .\scripts\dev.ps1

$root = Split-Path -Parent $PSScriptRoot

Start-Process powershell -WorkingDirectory (Join-Path $root "backend") -ArgumentList @(
    "-NoExit", "-Command", "uv run uvicorn app.main:app --reload --port 8000"
)
Start-Process powershell -WorkingDirectory (Join-Path $root "frontend") -ArgumentList @(
    "-NoExit", "-Command", "npm run dev"
)

Write-Host "TriageX backend:  http://localhost:8000/api/health"
Write-Host "TriageX frontend: http://localhost:5173"
