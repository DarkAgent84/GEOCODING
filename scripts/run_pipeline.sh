#!/usr/bin/env bash
# ==============================================================================
# Credility Geocoding Pipeline Runner (Linux / macOS / Bash Script)
# ==============================================================================

# Resolve project root directory regardless of invocation location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/src/geocode_valid_addresses_v2.py" ]; then
    PROJECT_ROOT="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../src/geocode_valid_addresses_v2.py" ]; then
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    PROJECT_ROOT="$(pwd)"
fi
cd "$PROJECT_ROOT"

# If arguments are provided on command line, pass them directly to the python script
if [ "$#" -gt 0 ]; then
    python3 src/geocode_valid_addresses_v2.py "$@"
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
    echo "  [3] Offline CSV Validation    (data/input/GEOCODING.csv -> data/output/, No API)"
    echo "  [4] Live CSV Geocoding        (data/input/GEOCODING.csv -> data/output/)"
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
            python3 src/geocode_valid_addresses_v2.py --db --no-api --workers 8 --batch-size 50
            ;;
        2)
            echo ""
            echo "[WARNING] This will call Google Maps Geocoding and Places API!"
            read -p "Do you wish to proceed? (y/n): " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo "[INFO] Running Pipeline in Database Live Mode (--db)..."
                python3 src/geocode_valid_addresses_v2.py --db --workers 8 --batch-size 50
            fi
            ;;
        3)
            echo ""
            echo "[INFO] Running Pipeline in CSV Offline Mode (No API)..."
            python3 src/geocode_valid_addresses_v2.py --no-api --workers 8 --batch-size 50
            ;;
        4)
            echo ""
            echo "[WARNING] This will call Google Maps Geocoding and Places API!"
            read -p "Do you wish to proceed? (y/n): " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo "[INFO] Running Pipeline in CSV Live Mode..."
                python3 src/geocode_valid_addresses_v2.py --workers 8 --batch-size 50
            fi
            ;;
        5)
            echo ""
            echo "[INFO] Checking PostgreSQL Database Status..."
            python3 scripts/init_postgres.py --status
            ;;
        6)
            echo ""
            echo "[INFO] Running Pytest Test Suite..."
            pytest tests/test_geocode_v2.py -v
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
