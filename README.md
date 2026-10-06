# Control Combination Explorer / 控制变量组合筛选器

[![CI](https://github.com/moyu-xu/control-combination-explorer/actions/workflows/ci.yml/badge.svg)](https://github.com/moyu-xu/control-combination-explorer/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/moyu-xu/control-combination-explorer)](https://github.com/moyu-xu/control-combination-explorer/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-0f766e.svg)](LICENSE)

Current version: **2026.10.6**. Download the ready-to-run Windows package from
the [latest GitHub Release](https://github.com/moyu-xu/control-combination-explorer/releases/latest).

A bilingual, offline Windows application that exhaustively searches an exact
number of candidate control variables and ranks qualifying models by the focal
explanatory variable's unadjusted, two-sided p value.

这是一个中英双语、完全离线的 Windows 工具。它读取 Stata `.dta` 数据，穷举指定
数量的候选控制变量组合，并按照核心解释变量未经多重检验校正的双侧 p 值排序。

## Implemented v1 capabilities

- `.dta` import with variable labels, storage types, missing counts, and examples.
- `ln(x)` and `x/y` derived variables with explicit invalid-value handling.
- A safe Stata-like filter subset: comparisons, parentheses, `&`, `|`, `!`,
  `missing()`, and `inlist()`; arbitrary code is never evaluated.
- A versioned, transparent offline dictionary that suggests firm, city/regional,
  and other control levels plus auditable economic dimensions.
- A reviewable classification table with confidence, match rationale, bulk edits,
  and explicit overrides for required-control dimension conflicts.
- Separate two-pane firm and city/regional candidate pools, exact level targets,
  fixed other-level controls, and grouped pre-run combination previews.
- Exhaustive Cartesian search across the two level-specific pools, with duplicate
  economic dimensions filtered before estimation and a shared complete-case sample.
- OLS or arbitrary multi-way absorbed fixed effects through PyFixest.
- Conventional, HC1 robust, or one-way CRV1 clustered standard errors.
- Required focal-coefficient direction and a fixed two-sided `p < 0.05` rule.
- Top 5/10/20/50/100 results, full model details, and Stata reproduction commands.
- Excel and Word exports.
- Cohort-saturated and Gardner DID2S event studies, three placebo designs, and
  publication-ready PNG/PDF/Excel/Word diagnostic exports.
- Guided workflow and a synchronized advanced workspace, both in Chinese and English.
- Localhost-only API, per-launch token, and dependency-aware cache cleanup: historical
  resources expire after one idle hour and are LRU-evicted above a 512 MB budget while
  current and running work remains protected.

## Development setup

Requirements: Node.js 22 or newer and [uv](https://docs.astral.sh/uv/).

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
$env:UV_PYTHON_INSTALL_DIR = "$PWD\.uv-python"
$env:UV_PYTHON_PREFERENCE = "only-managed"
uv python install 3.13.2
uv sync --all-groups --python 3.13.2

Set-Location frontend
npm ci
Set-Location ..
pwsh .\scripts\run-dev.ps1
```

The development UI is available at `http://127.0.0.1:5173`; the API runs at
`http://127.0.0.1:8000`.

## Privacy and data safety

The application runs locally and does not require any large-model API key or
cloud service. Imported datasets and generated results stay on the user's
computer. Stata and Parquet data files, local data directories, environment
files, credentials, secrets, private keys, caches, and build outputs are
excluded from Git by default. Never commit real research data or credentials.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check backend
Set-Location frontend
npm run build
```

The test suite covers the filter parser, transparent classification rules,
level-specific combination enumeration, economic-dimension constraints,
sign/significance rules, real OLS and fixed-effect estimation, clustered
inference, and valid Excel/Word package output.
Cache tests cover active leases, expiry boundaries, dependency-safe cleanup, the
historical memory budget, atomic dataset replacement, and expired-resource responses.

## Windows build

Private binary release:

```powershell
pwsh .\scripts\build-windows.ps1 -ReleaseVisibility private
```

Public release with an additional MIT-licensed source archive:

```powershell
pwsh .\scripts\build-windows.ps1 -ReleaseVisibility public
```

The script builds the React UI and a PyInstaller `onedir` application. Release
archives use the local build date, for example
`ControlCombinationExplorer-20261006-portable.zip`; public releases also create
the matching clean, reproducible source archive and SHA-256 checksum file. Older
project release ZIPs and checksum files are removed before a new build, while the
unpacked application directory is retained. Pass `-BuildInstaller` explicitly to
request an Inno Setup build. No Python or Node installation is required on the
target computer.

## Statistical interpretation

The application performs specification search. Reported p values are raw and
are not adjusted for the number of combinations tested. The export records the
number of attempted combinations, exclusions, common-sample construction, and
the exact winning specification so the search remains auditable.

The included tests verify internal behavior and cross-engine consistency. Before
claiming release-level Stata parity for a new dependency version, refresh and
run golden fixtures produced by the target Stata `regress` and `reghdfe` versions.

