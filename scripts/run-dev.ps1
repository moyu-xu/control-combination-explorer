param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 5173
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment is missing. Run: uv sync --all-groups"
}

$Api = Start-Process -FilePath $Python `
    -ArgumentList "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", $ApiPort `
    -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden

try {
    Push-Location (Join-Path $ProjectRoot "frontend")
    npm run dev -- --host 127.0.0.1 --port $UiPort
}
finally {
    Pop-Location
    if (-not $Api.HasExited) {
        Stop-Process -Id $Api.Id
    }
}

