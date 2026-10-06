param(
    [ValidateSet("private", "public")]
    [string]$ReleaseVisibility = "private",
    [switch]$SkipDependencyRestore,
    [switch]$BuildInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $ProjectRoot "frontend"
$BuildRoot = Join-Path $ProjectRoot "build"
$FrontendStage = Join-Path $BuildRoot "frontend-stage"
$PackagingRoot = Join-Path $ProjectRoot "packaging"
$DistRoot = Join-Path $ProjectRoot "dist"
$ReleaseDate = Get-Date -Format "yyyyMMdd"
$ReleaseStem = "ControlCombinationExplorer-$ReleaseDate"

$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot ".uv-python"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$env:npm_config_cache = Join-Path $ProjectRoot ".npm-cache"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Push-Location $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath $DistRoot)) {
        New-Item -ItemType Directory -Path $DistRoot | Out-Null
    }
    Get-ChildItem -LiteralPath $DistRoot -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^ControlCombinationExplorer-(?:\d{8}-)?(?:portable|source)\.zip$' -or
            $_.Name -match '^ControlCombinationExplorer-(?:\d{8}-)?SHA256SUMS\.txt$'
        } |
        Remove-Item -Force

    Invoke-CheckedNative { uv sync --all-groups --python 3.13.2 } "Python dependency restore"
    if (-not $SkipDependencyRestore) {
        if (Test-Path -LiteralPath $FrontendStage) {
            Remove-Item -LiteralPath $FrontendStage -Recurse -Force
        }
        New-Item -ItemType Directory -Path $FrontendStage | Out-Null
        Copy-Item -LiteralPath `
            (Join-Path $Frontend "package.json"), `
            (Join-Path $Frontend "package-lock.json"), `
            (Join-Path $Frontend "tsconfig.json"), `
            (Join-Path $Frontend "tsconfig.app.json"), `
            (Join-Path $Frontend "tsconfig.node.json"), `
            (Join-Path $Frontend "vite.config.ts"), `
            (Join-Path $Frontend "vitest.config.ts"), `
            (Join-Path $Frontend "index.html") `
            -Destination $FrontendStage
        Copy-Item -LiteralPath (Join-Path $Frontend "src") -Destination $FrontendStage -Recurse
        Push-Location $FrontendStage
        try {
            Invoke-CheckedNative { npm ci --no-audit --no-fund } "Frontend dependency restore"
            Invoke-CheckedNative { npm run build } "Frontend build"
        }
        finally {
            Pop-Location
        }
        $FrontendDist = Join-Path $Frontend "dist"
        if (Test-Path -LiteralPath $FrontendDist) {
            Remove-Item -LiteralPath $FrontendDist -Recurse -Force
        }
        Copy-Item -LiteralPath (Join-Path $FrontendStage "dist") -Destination $FrontendDist -Recurse
    }
    elseif (-not (Test-Path -LiteralPath (Join-Path $Frontend "dist\index.html"))) {
        throw "-SkipDependencyRestore requires an existing frontend/dist build"
    }

    Invoke-CheckedNative {
        & (Join-Path $ProjectRoot ".venv\Scripts\pyinstaller.exe") `
            --noconfirm --clean (Join-Path $PackagingRoot "ControlCombinationExplorer.spec")
    } "PyInstaller build"

    $PortableFolder = Join-Path $DistRoot "ControlCombinationExplorer"
    Copy-Item (Join-Path $ProjectRoot "README.md") -Destination $PortableFolder
    Copy-Item (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") -Destination $PortableFolder

    $Portable = Join-Path $DistRoot "$ReleaseStem-portable.zip"
    Compress-Archive -Path (Join-Path $DistRoot "ControlCombinationExplorer\*") `
        -DestinationPath $Portable

    if ($BuildInstaller) {
        $Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $Iscc) {
            throw "-BuildInstaller requires Inno Setup (ISCC.exe)"
        }
        Invoke-CheckedNative {
            & $Iscc.Source (Join-Path $PackagingRoot "installer.iss")
        } "Inno Setup build"
    }

    if ($ReleaseVisibility -eq "public") {
        $SourceStage = Join-Path $BuildRoot "public-source"
        if (Test-Path -LiteralPath $SourceStage) {
            Remove-Item -LiteralPath $SourceStage -Recurse -Force
        }
        New-Item -ItemType Directory -Path $SourceStage | Out-Null
        Copy-Item backend, scripts, packaging, .github -Destination $SourceStage -Recurse
        $FrontendSource = Join-Path $SourceStage "frontend"
        New-Item -ItemType Directory -Path $FrontendSource | Out-Null
        Copy-Item (Join-Path $Frontend "src") -Destination $FrontendSource -Recurse
        Copy-Item `
            (Join-Path $Frontend "package.json"), `
            (Join-Path $Frontend "package-lock.json"), `
            (Join-Path $Frontend "tsconfig.json"), `
            (Join-Path $Frontend "tsconfig.app.json"), `
            (Join-Path $Frontend "tsconfig.node.json"), `
            (Join-Path $Frontend "vite.config.ts"), `
            (Join-Path $Frontend "vitest.config.ts"), `
            (Join-Path $Frontend "index.html") `
            -Destination $FrontendSource
        Copy-Item `
            pyproject.toml, uv.lock, README.md, THIRD_PARTY_NOTICES.md, `
            LICENSE, .gitignore, run_app.py `
            -Destination $SourceStage
        Get-ChildItem -LiteralPath $SourceStage -Directory -Recurse -Force |
            Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".ruff_cache") } |
            Sort-Object FullName -Descending |
            Remove-Item -Recurse -Force
        Get-ChildItem -LiteralPath $SourceStage -File -Recurse -Force |
            Where-Object { $_.Extension -in @(".pyc", ".pyo", ".tsbuildinfo") } |
            Remove-Item -Force
        $SourceArchive = Join-Path $DistRoot "$ReleaseStem-source.zip"
        Compress-Archive -Path (Join-Path $SourceStage "*") -DestinationPath $SourceArchive
    }

    $ChecksumFile = Join-Path $DistRoot "$ReleaseStem-SHA256SUMS.txt"
    $ReleaseFiles = @($Portable)
    if ($ReleaseVisibility -eq "public") {
        $ReleaseFiles += $SourceArchive
    }
    $ChecksumLines = $ReleaseFiles | ForEach-Object {
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash.ToLowerInvariant()
        "$Hash  $(Split-Path -Leaf $_)"
    }
    [System.IO.File]::WriteAllLines($ChecksumFile, $ChecksumLines, [System.Text.UTF8Encoding]::new($false))

    Write-Output "Portable package: $Portable"
    if ($ReleaseVisibility -eq "public") {
        Write-Output "Source package: $SourceArchive"
    }
    Write-Output "SHA-256 checksums: $ChecksumFile"
}
finally {
    Pop-Location
}

