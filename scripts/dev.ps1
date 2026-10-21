$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -e ".\backend"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

# Provision a high-entropy local token once and share it with both server processes.
$envPath = Join-Path $root ".env"
$envText = Get-Content -Raw -Path $envPath
$tokenMatch = [regex]::Match($envText, "(?m)^LOCAL_API_TOKEN\s*=\s*(.*)$")
$localToken = if ($tokenMatch.Success) { $tokenMatch.Groups[1].Value.Trim() } else { "" }
if ([string]::IsNullOrWhiteSpace($localToken) -or $localToken -eq "CHANGE_ME") {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $tokenBytes = New-Object byte[] 32
    $rng.GetBytes($tokenBytes)
    $rng.Dispose()
    $localToken = ([System.BitConverter]::ToString($tokenBytes) -replace "-", "").ToLowerInvariant()
    if ($tokenMatch.Success) {
        $envText = [regex]::Replace($envText, "(?m)^LOCAL_API_TOKEN\s*=.*$", "LOCAL_API_TOKEN=$localToken")
    } else {
        $envText = $envText.TrimEnd() + [Environment]::NewLine + "LOCAL_API_TOKEN=$localToken" + [Environment]::NewLine
    }
    Set-Content -Path $envPath -Value $envText -Encoding utf8
}
$env:LOCAL_API_TOKEN = $localToken

$frontendEnvPath = Join-Path $root "frontend\.env.local"
if (-not (Test-Path $frontendEnvPath)) {
    Copy-Item ".\frontend\.env.local.example" $frontendEnvPath
}
$frontendEnv = Get-Content -Raw -Path $frontendEnvPath
if ($frontendEnv -match "(?m)^NEXT_PUBLIC_API_BASE_URL=.*$") {
    $frontendEnv = [regex]::Replace($frontendEnv, "(?m)^NEXT_PUBLIC_API_BASE_URL=.*$", "NEXT_PUBLIC_API_BASE_URL=/api/backend")
} else {
    $frontendEnv = $frontendEnv.TrimEnd() + [Environment]::NewLine + "NEXT_PUBLIC_API_BASE_URL=/api/backend" + [Environment]::NewLine
}
Set-Content -Path $frontendEnvPath -Value $frontendEnv -Encoding utf8
$env:NEXT_PUBLIC_API_BASE_URL = "/api/backend"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root'; .\.venv\Scripts\Activate.ps1; python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000"
Set-Location "$root\frontend"
npm install
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\frontend'; npm run dev"
Write-Host "Backend: http://127.0.0.1:8000/docs"
Write-Host "Dashboard: http://127.0.0.1:3000"
