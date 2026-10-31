#!/usr/bin/env bash
# ==============================================================================
# Credility Geocoding Pipeline Runner (Linux / macOS / Bash Script)
# ==============================================================================

# If arguments are provided on command line, pass them directly to the python script
if [ "$#" -gt 0 ]; then
    python3 geocode_valid_addresses_v2.py "$@"
    exit $?
fi

while true; do
    clear
    echo "=============================================================================="
    echo "                 CREDILITY GEOCODING PIPELINE (V2)"
    echo "=============================================================================="
    echo ""
    echo "Please select an operation mode:"
    echo ""
    echo "  [1] Offline DB Validation     (Stage 1-4, 0 Google API calls, Free)"
    echo "  [2] Live DB Geocoding         (PostgreSQL + Google Geocoding/Places API)"
    echo "  [3] Offline CSV Validation    (GEOCODING.csv -> GEOCODING_processed.csv, No API)"
    echo "  [4] Live CSV Geocoding        (GEOCODING.csv -> GEOCODING_processed.csv)"
    echo "  [5] Check PostgreSQL Status   (Verifies connection and table row counts)"
    echo "  [6] Run Pytest Test Suite     (Executes all 108 unit/regression tests)"
    echo "  [7] Exit"
    echo ""
    echo "=============================================================================="
    read -p "Enter choice [1-7]: " choice

    case "$choice" in
        1)
            echo ""
            echo "[INFO] Running Pipeline in Database Offline Mode (--db --no-api)..."
            python3 geocode_valid_addresses_v2.py --db --no-api --workers 8 --batch-size 50
            ;;
        2)
            echo ""
            echo "[WARNING] This will call Google Maps Geocoding and Places API!"
            read -p "Do you wish to proceed? (y/n): " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo "[INFO] Running Pipeline in Database Live Mode (--db)..."
                python3 geocode_valid_addresses_v2.py --db --workers 8 --batch-size 50
            fi
            ;;
        3)
            echo ""
            echo "[INFO] Running Pipeline in CSV Offline Mode (--input GEOCODING.csv --no-api)..."
            python3 geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --no-api --workers 8 --batch-size 50
            ;;
        4)
            echo ""
            echo "[WARNING] This will call Google Maps Geocoding and Places API!"
            read -p "Do you wish to proceed? (y/n): " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo "[INFO] Running Pipeline in CSV Live Mode..."
                python3 geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --workers 8 --batch-size 50
            fi
            ;;
        5)
            echo ""
            echo "[INFO] Checking PostgreSQL Database Status..."
            python3 init_postgres.py --check
            ;;
        6)
            echo ""
            echo "[INFO] Running Pytest Test Suite..."
            pytest test_geocode_v2.py -v
            ;;
        7)
            echo "Exiting..."
            exit 0
            ;;
        *)
            echo "Invalid selection! Please enter a number from 1 to 7."
            sleep 2
            continue
            ;;
    esac

    echo ""
    echo "=============================================================================="
    echo "Task completed."
    echo "=============================================================================="
    read -p "Press [Enter] to return to the menu..."
done
