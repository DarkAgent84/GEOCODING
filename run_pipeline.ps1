<#
.SYNOPSIS
    Credility Geocoding Pipeline Runner (PowerShell Script)
.DESCRIPTION
    Automates running the geocoding pipeline in offline, live, DB, or CSV modes.
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

# Resolve project root directory regardless of invocation location
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path "$ScriptDir\geocode_valid_addresses_v2.py") {
    $ProjectRoot = $ScriptDir
} elseif (Test-Path "$ScriptDir\..\geocode_valid_addresses_v2.py") {
    $ProjectRoot = (Resolve-Path "$ScriptDir\..").Path
} else {
    $ProjectRoot = (Get-Location).Path
}
Set-Location $ProjectRoot

# If arguments were provided directly on the CLI, forward them directly to Python
if ($ScriptArgs.Count -gt 0) {
    & python geocode_valid_addresses_v2.py @ScriptArgs
    exit $LASTEXITCODE
}

function Show-Menu {
    Clear-Host
    Write-Host "==============================================================================" -ForegroundColor Cyan
    Write-Host "                 CREDILITY GEOCODING PIPELINE (V2)" -ForegroundColor Cyan
    Write-Host "==============================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Please select an operation mode:"
    Write-Host ""
    Write-Host "  [1] Offline DB Validation     (Stage 1-4, 0 Google API calls, Free)" -ForegroundColor Green
    Write-Host "  [2] Live DB Geocoding         (PostgreSQL + Google Geocoding/Places API)" -ForegroundColor Yellow
    Write-Host "  [3] Offline CSV Validation    (data/input/GEOCODING.csv -> data/output/, No API)" -ForegroundColor Green
    Write-Host "  [4] Live CSV Geocoding        (data/input/GEOCODING.csv -> data/output/)" -ForegroundColor Yellow
    Write-Host "  [5] Check PostgreSQL Status   (Verifies connection and table row counts)" -ForegroundColor White
    Write-Host "  [6] Run Pytest Test Suite     (Executes all 108 unit/regression tests)" -ForegroundColor White
    Write-Host "  [7] Exit"
    Write-Host ""
    Write-Host "==============================================================================" -ForegroundColor Cyan
}

while ($true) {
    Show-Menu
    $choice = Read-Host "Enter choice [1-7]"

    switch ($choice) {
        "1" {
            Write-Host "`n[INFO] Running Pipeline in Database Offline Mode (--db --no-api)..." -ForegroundColor Green
            & python geocode_valid_addresses_v2.py --db --no-api --workers 8 --batch-size 50
        }
        "2" {
            Write-Host "`n[WARNING] This will call Google Maps Geocoding and Places API!" -ForegroundColor Yellow
            $confirm = Read-Host "Do you wish to proceed? (Y/N)"
            if ($confirm -eq "Y" -or $confirm -eq "y") {
                Write-Host "[INFO] Running Pipeline in Database Live Mode (--db)..." -ForegroundColor Yellow
                & python geocode_valid_addresses_v2.py --db --workers 8 --batch-size 50
            }
        }
        "3" {
            Write-Host "`n[INFO] Running Pipeline in CSV Offline Mode (No API)..." -ForegroundColor Green
            & python geocode_valid_addresses_v2.py --no-api --workers 8 --batch-size 50
        }
        "4" {
            Write-Host "`n[WARNING] This will call Google Maps Geocoding and Places API!" -ForegroundColor Yellow
            $confirm = Read-Host "Do you wish to proceed? (Y/N)"
            if ($confirm -eq "Y" -or $confirm -eq "y") {
                Write-Host "[INFO] Running Pipeline in CSV Live Mode..." -ForegroundColor Yellow
                & python geocode_valid_addresses_v2.py --workers 8 --batch-size 50
            }
        }
        "5" {
            Write-Host "`n[INFO] Checking PostgreSQL Database Status..." -ForegroundColor Cyan
            & python init_postgres.py --check
        }
        "6" {
            Write-Host "`n[INFO] Running Pytest Test Suite..." -ForegroundColor Cyan
            & pytest tests/test_geocode_v2.py -v
        }
        "7" {
            Write-Host "Exiting..."
            exit 0
        }
        Default {
            Write-Host "Invalid choice! Please select 1-7." -ForegroundColor Red
            Start-Sleep -Seconds 2
            continue
        }
    }

    Write-Host "`n==============================================================================" -ForegroundColor Cyan
    Write-Host "Task completed."
    Write-Host "==============================================================================" -ForegroundColor Cyan
    Read-Host "Press Enter to return to menu"
}
