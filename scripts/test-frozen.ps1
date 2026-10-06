param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot "dist\ControlCombinationExplorer\ControlCombinationExplorer.exe"
$BuildRoot = Join-Path $ProjectRoot "build\frozen-e2e"
$Fixture = Join-Path $BuildRoot "panel.dta"
$Token = "frozen-e2e-token"
$BaseUrl = "http://127.0.0.1:$Port"
$Headers = @{ Authorization = "Bearer $Token" }

function Wait-RemoteJob {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [int]$Attempts = 3000
    )
    $Progress = $null
    for ($Attempt = 0; $Attempt -lt $Attempts; $Attempt++) {
        $Progress = Invoke-RestMethod -Uri "$BaseUrl$Path" -Headers $Headers
        if ($Progress.status -in @("completed", "failed", "cancelled")) {
            return $Progress
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out waiting for $Path. Last status: $($Progress.status)"
}

function Save-Artifact {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Output
    )
    try {
        Invoke-WebRequest -Uri "$BaseUrl$Path" -Headers $Headers -OutFile $Output
    }
    catch {
        throw "Frozen export failed for $Path`: $($_.Exception.Message)"
    }
    if (-not (Test-Path -LiteralPath $Output) -or (Get-Item $Output).Length -eq 0) {
        throw "Frozen export is empty: $Path"
    }
}

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Frozen executable not found: $Executable"
}
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildRoot | Out-Null

$FixtureCode = @'
import numpy as np
import pandas as pd
import sys

rng = np.random.default_rng(20261006)
firms = 36
years = np.arange(2015, 2023)
frame = pd.DataFrame({
    "firm": np.repeat(np.arange(firms), len(years)),
    "year": np.tile(years, firms),
})
frame["g"] = np.select(
    [frame["firm"] < 12, frame["firm"] < 24],
    [2018, 2020],
    default=0,
)
frame["treat"] = ((frame["g"] > 0) & (frame["year"] >= frame["g"])).astype(int)
firm_component = np.repeat(rng.normal(scale=0.5, size=firms), len(years))
time_component = np.tile(np.linspace(-0.25, 0.25, len(years)), firms)
for name in ["size", "roa", "lev", "gdp", "population", "othercontrol"]:
    frame[name] = rng.normal(size=len(frame))
frame["y"] = (
    1.5 * frame["treat"]
    + 0.25 * frame["size"]
    + 0.1 * frame["othercontrol"]
    + firm_component
    + time_component
    + rng.normal(scale=0.25, size=len(frame))
)
frame.to_stata(sys.argv[1], write_index=False, version=118)
'@
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") -c $FixtureCode $Fixture
if ($LASTEXITCODE -ne 0) { throw "Unable to create the frozen-build fixture" }

$env:CCE_NO_BROWSER = "1"
$env:CCE_PORT = "$Port"
$env:CCE_TOKEN = $Token
$Process = Start-Process -FilePath $Executable -WindowStyle Hidden -PassThru

try {
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
        try {
            $Health = Invoke-RestMethod -Uri "$BaseUrl/api/health" -Headers $Headers
            if ($Health.status -eq "ok") { $Ready = $true; break }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $Ready) { throw "Frozen application did not become ready" }

    $Unauthorized = Invoke-WebRequest -Uri "$BaseUrl/api/cache/status" -SkipHttpErrorCheck
    if ($Unauthorized.StatusCode -ne 401) {
        throw "Frozen authentication check failed: expected 401, received $($Unauthorized.StatusCode)"
    }
    $OpenApi = Invoke-RestMethod -Uri "$BaseUrl/openapi.json" -Headers $Headers
    if ($OpenApi.info.version -ne "2026.10.6") {
        throw "Frozen application version mismatch: $($OpenApi.info.version)"
    }
    $Landing = Invoke-WebRequest -Uri "$BaseUrl/" -Headers $Headers
    if ($Landing.Content -notmatch "控制变量组合筛选器") {
        throw "Frozen application did not serve the current frontend"
    }

    $Dataset = Invoke-RestMethod -Uri "$BaseUrl/api/datasets" -Method Post -Headers $Headers -Form @{ file = Get-Item $Fixture }
    $Classification = Invoke-RestMethod -Uri "$BaseUrl/api/datasets/$($Dataset.id)/control-classifications" -Headers $Headers
    $Spec = @{
        dataset_id = $Dataset.id
        language = "zh"
        dependent = "y"
        core = "treat"
        required_controls = @("size")
        firm_candidate_controls = @("roa", "lev")
        regional_candidate_controls = @("gdp", "population")
        fixed_other_controls = @("othercontrol")
        firm_control_target = 2
        regional_control_target = 1
        control_classifications = $Classification.classifications
        classification_rule_version = $Classification.rule_version
        classification_confirmed = $true
        dimension_conflict_overrides = @()
        model_type = "ols"
        fixed_effects = @()
        standard_error = "cluster"
        cluster_variable = "firm"
        expected_sign = "positive"
        top_n = 20
        significance = 0.05
    }
    $Body = $Spec | ConvertTo-Json -Depth 8
    $Preview = Invoke-RestMethod -Uri "$BaseUrl/api/analysis/preview" -Method Post -Headers $Headers -ContentType "application/json" -Body $Body
    if (-not $Preview.valid -or $Preview.total_combinations -ne 4) {
        throw "Unexpected frozen preview: valid=$($Preview.valid), combinations=$($Preview.total_combinations)"
    }

    $Job = Invoke-RestMethod -Uri "$BaseUrl/api/jobs" -Method Post -Headers $Headers -ContentType "application/json" -Body $Body
    $Progress = Wait-RemoteJob -Path "/api/jobs/$($Job.job_id)"
    if ($Progress.status -ne "completed" -or $Progress.completed -ne 4) {
        throw "Frozen analysis failed: $($Progress.status) $($Progress.message)"
    }
    $Result = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$($Job.job_id)/result" -Headers $Headers
    if (-not $Result.best_model) { throw "Frozen analysis returned no qualifying model" }
    Save-Artifact -Path "/api/jobs/$($Job.job_id)/export.xlsx" -Output (Join-Path $BuildRoot "analysis.xlsx")
    Save-Artifact -Path "/api/jobs/$($Job.job_id)/export.docx" -Output (Join-Path $BuildRoot "analysis.docx")

    $DiagnosticSpec = @{
        analysis_job_id = $Job.job_id
        model_rank = 1
        language = "zh"
        panel_id = "firm"
        time_variable = "year"
        treatment = @{
            mode = "cohort"
            variable = "g"
            never_treated_mode = "zero"
            never_treated_value = $null
        }
        window_start = -3
        window_end = 3
        extra_fixed_effects = @()
        event_methods = @("saturated", "did2s")
        run_pretrend_test = $true
        placebo_methods = @("random_group", "random_timing", "permute_outcome")
        placebo_estimator = "did2s"
        repetitions = 100
        random_seed = 12345
        show_density = $true
        labels = @{
            title_zh = "政策动态效应"
            title_en = "Dynamic policy effects"
            y_axis_zh = "政策效应"
            y_axis_en = "Treatment effect"
            unit_zh = ""
            unit_en = ""
        }
    }
    $DiagnosticBody = $DiagnosticSpec | ConvertTo-Json -Depth 8
    $DiagnosticPreview = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$($Job.job_id)/diagnostics/preview" -Method Post -Headers $Headers -ContentType "application/json" -Body $DiagnosticBody
    if ($DiagnosticPreview.treated_units -ne 24 -or $DiagnosticPreview.never_treated_units -ne 12) {
        throw "Unexpected frozen diagnostic preview"
    }

    $EventJob = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$($Job.job_id)/diagnostics/event-study" -Method Post -Headers $Headers -ContentType "application/json" -Body $DiagnosticBody
    $EventProgress = Wait-RemoteJob -Path "/api/diagnostic-jobs/$($EventJob.job_id)"
    if ($EventProgress.status -ne "completed") {
        throw "Frozen event study failed: $($EventProgress.message)"
    }
    $EventResult = Invoke-RestMethod -Uri "$BaseUrl/api/diagnostic-jobs/$($EventJob.job_id)/result" -Headers $Headers
    if ($EventResult.event_studies.Count -ne 2) { throw "Frozen event study did not return both methods" }
    Save-Artifact -Path "/api/diagnostic-jobs/$($EventJob.job_id)/figures/event_saturated.png" -Output (Join-Path $BuildRoot "event-saturated.png")
    Save-Artifact -Path "/api/diagnostic-jobs/$($EventJob.job_id)/figures/event_did2s.pdf" -Output (Join-Path $BuildRoot "event-did2s.pdf")
    Save-Artifact -Path "/api/diagnostic-jobs/$($EventJob.job_id)/export.xlsx" -Output (Join-Path $BuildRoot "event-study.xlsx")
    Save-Artifact -Path "/api/diagnostic-jobs/$($EventJob.job_id)/export.docx" -Output (Join-Path $BuildRoot "event-study.docx")

    $PlaceboJob = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$($Job.job_id)/diagnostics/placebo" -Method Post -Headers $Headers -ContentType "application/json" -Body $DiagnosticBody
    $PlaceboProgress = Wait-RemoteJob -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)"
    if ($PlaceboProgress.status -ne "completed" -or $PlaceboProgress.successful -lt 300) {
        throw "Frozen placebo run failed: $($PlaceboProgress.message)"
    }
    $PlaceboResult = Invoke-RestMethod -Uri "$BaseUrl/api/diagnostic-jobs/$($PlaceboJob.job_id)/result" -Headers $Headers
    if ($PlaceboResult.placebos.Count -ne 3) { throw "Frozen placebo run did not return all three methods" }
    foreach ($Method in @("random_group", "random_timing", "permute_outcome")) {
        Save-Artifact -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)/figures/placebo_$Method.png" -Output (Join-Path $BuildRoot "placebo-$Method.png")
    }
    Save-Artifact -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)/figures/placebo_all.png" -Output (Join-Path $BuildRoot "placebo-triptych.png")
    Save-Artifact -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)/figures/placebo_all.pdf" -Output (Join-Path $BuildRoot "placebo-triptych.pdf")
    Save-Artifact -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)/export.xlsx" -Output (Join-Path $BuildRoot "placebo.xlsx")
    Save-Artifact -Path "/api/diagnostic-jobs/$($PlaceboJob.job_id)/export.docx" -Output (Join-Path $BuildRoot "placebo.docx")

    $LeaseBody = @{
        dataset_id = $Dataset.id
        analysis_job_ids = @($Job.job_id)
        diagnostic_job_ids = @($EventJob.job_id, $PlaceboJob.job_id)
    } | ConvertTo-Json -Depth 4
    $Lease = Invoke-RestMethod -Uri "$BaseUrl/api/cache/active" -Method Put -Headers $Headers -ContentType "application/json" -Body $LeaseBody
    if ($Lease.missing_resources.Count -ne 0) { throw "Frozen cache lease reported missing resources" }
    $CachePreview = Invoke-RestMethod -Uri "$BaseUrl/api/cache/cleanup/preview" -Method Post -Headers $Headers
    if ($CachePreview.resources -ne 0) { throw "Protected frozen resources were marked reclaimable" }
    $CacheCleanup = Invoke-RestMethod -Uri "$BaseUrl/api/cache/cleanup" -Method Post -Headers $Headers
    if ($CacheCleanup.reclaimed_resources -ne 0) { throw "Protected frozen resources were reclaimed" }

    Write-Output "Frozen E2E passed: auth, version, 4/4 models, 2 event studies, 3x100 placebos, all exports, and cache protection."
}
finally {
    try { Invoke-RestMethod -Uri "$BaseUrl/api/shutdown" -Method Post -Headers $Headers | Out-Null } catch {}
    if (-not $Process.HasExited) {
        $Process.WaitForExit(5000) | Out-Null
    }
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
    }
}
