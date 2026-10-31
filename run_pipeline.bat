@echo off
setlocal enabledelayedexpansion

:: ==============================================================================
:: Credility Geocoding Pipeline Runner (Windows Batch Script)
:: ==============================================================================

:: Resolve project root directory regardless of invocation location
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%geocode_valid_addresses_v2.py" (
    set "PROJECT_ROOT=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%..\geocode_valid_addresses_v2.py" (
    set "PROJECT_ROOT=%SCRIPT_DIR%..\"
) else (
    set "PROJECT_ROOT=%CD%\"
)
cd /d "%PROJECT_ROOT%"

:: If arguments are provided on command line, pass them directly to the python script
if not "%~1"=="" (
    python geocode_valid_addresses_v2.py %*
    goto :eof
)

:MENU
cls
echo ==============================================================================
echo                 CREDILITY GEOCODING PIPELINE (V2)
echo ==============================================================================
echo.
echo Please select an operation mode:
echo.
echo   [1] Offline DB Validation     (Stage 1-4, 0 Google API calls, Free)
echo   [2] Live DB Geocoding         (PostgreSQL + Google Geocoding/Places API)
echo   [3] Offline CSV Validation    (data/input/GEOCODING.csv -^> data/output/, No API)
echo   [4] Live CSV Geocoding        (data/input/GEOCODING.csv -^> data/output/)
echo   [5] Check PostgreSQL Status   (Verifies connection and table row counts)
echo   [6] Run Pytest Test Suite     (Executes all 108 unit/regression tests)
echo   [7] Exit
echo.
echo ==============================================================================
set /p choice="Enter choice [1-7]: "

if "%choice%"=="1" goto OFFLINE_DB
if "%choice%"=="2" goto LIVE_DB
if "%choice%"=="3" goto OFFLINE_CSV
if "%choice%"=="4" goto LIVE_CSV
if "%choice%"=="5" goto CHECK_DB
if "%choice%"=="6" goto RUN_TESTS
if "%choice%"=="7" goto EXIT

echo Invalid selection! Please enter a number from 1 to 7.
timeout /t 2 >nul
goto MENU

:OFFLINE_DB
echo.
echo [INFO] Running Pipeline in Database Offline Mode (--db --no-api)...
python geocode_valid_addresses_v2.py --db --no-api --workers 8 --batch-size 50
goto FINISHED

:LIVE_DB
echo.
echo [WARNING] This will call Google Maps Geocoding and Places API!
set /p confirm="Do you wish to proceed? (Y/N): "
if /i not "%confirm%"=="Y" goto MENU
echo [INFO] Running Pipeline in Database Live Mode (--db)...
python geocode_valid_addresses_v2.py --db --workers 8 --batch-size 50
goto FINISHED

:OFFLINE_CSV
echo.
echo [INFO] Running Pipeline in CSV Offline Mode (No API)...
python geocode_valid_addresses_v2.py --no-api --workers 8 --batch-size 50
goto FINISHED

:LIVE_CSV
echo.
echo [WARNING] This will call Google Maps Geocoding and Places API!
set /p confirm="Do you wish to proceed? (Y/N): "
if /i not "%confirm%"=="Y" goto MENU
echo [INFO] Running Pipeline in CSV Live Mode...
python geocode_valid_addresses_v2.py --workers 8 --batch-size 50
goto FINISHED

:CHECK_DB
echo.
echo [INFO] Checking PostgreSQL Database Status...
python init_postgres.py --status
goto FINISHED

:RUN_TESTS
echo.
echo [INFO] Running Pytest Test Suite...
pytest tests/test_geocode_v2.py -v
goto FINISHED

:FINISHED
echo.
echo ==============================================================================
echo Task completed.
echo ==============================================================================
pause
goto MENU

:EXIT
echo Exiting...
exit /b 0
