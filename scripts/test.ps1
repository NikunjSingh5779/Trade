$ErrorActionPreference = "Stop"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Virtual environment not found. Create it with: py -3.12 -m venv .venv"
}
& .\.venv\Scripts\python.exe -m pip install -e ".\backend[test]"
& .\.venv\Scripts\python.exe -m pytest .\backend\tests -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
