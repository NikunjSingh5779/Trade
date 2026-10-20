$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -e ".\backend"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
if (-not (Test-Path ".\frontend\.env.local")) {
    Copy-Item ".\frontend\.env.local.example" ".\frontend\.env.local"
}
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root'; .\.venv\Scripts\Activate.ps1; python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000"
Set-Location "$root\frontend"
npm install
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\frontend'; npm run dev"
Write-Host "Backend: http://127.0.0.1:8000/docs"
Write-Host "Dashboard: http://127.0.0.1:3000"
