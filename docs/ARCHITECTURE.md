# Project Architecture & Directory Layout

## Directory Structure

```
GEOCODING/
├── .env                              # Local environment variables & secrets (ignored in git)
├── .env.example                      # Template environment configuration
├── .gitignore                        # Git ignore rules for virtualenv, DBs, caches
├── requirements.txt                  # Python package dependencies
├── README.md                         # High-level project documentation
├── COMMANDS.md                       # Comprehensive CLI command reference cheatsheet
│
├── src/                              # Core Application Engine
│   └── geocode_valid_addresses_v2.py # Comprehensive geocoding, scoring & validation engine
│
├── scripts/                          # Utility & Operations Scripts
│   ├── init_postgres.py              # PostgreSQL database creation, migration & data loader
│   ├── run_pipeline.bat              # Windows 1-click execution menu & argument forwarder
│   ├── run_pipeline.ps1              # PowerShell interactive runner
│   └── run_pipeline.sh               # Linux / macOS / Bash runner for Cloud & Docker
│
├── data/                             # Structured Data Directory
│   ├── input/
│   │   └── GEOCODING.csv             # Raw input address dataset (10,258 records)
│   ├── output/
│   │   └── GEOCODING_processed.csv   # Processed results with scores & coordinates (41 cols)
│   └── cache/
│       └── geocode_cache.db          # Local SQLite persistent API cache (WAL mode)
│
├── tests/                            # Automated Test Suite
│   ├── __init__.py
│   └── test_geocode_v2.py            # 108 unit, mock, integration & regression tests
│
└── docs/                             # Extended Documentation
    └── ARCHITECTURE.md               # System design, dataflow & pipeline architecture
```

---

## Architecture Overview

### 1. Unified Processing Engine (`src/geocode_valid_addresses_v2.py`)
`src/geocode_valid_addresses_v2.py` is maintained as a high-performance, self-contained Python engine containing the entire validation, scoring, geocoding, and database operations logic:
* **Stage 1 (Incomplete Address Detection)**: Validates length $\ge 15$, major words $\ge 3$, and 6-digit PIN code offline (Scores: 0–9).
* **Stage 2 (Conflicting PIN Codes)**: Detects and stops conflicting PINs offline (Scores: 11–20).
* **Stage 3 (Offline Spelling Error Detection)**: Validates geographic names against `ALL_KNOWN_SPELLING_WORDS` offline (Scores: 21–30).
* **Stage 4 (Synonym Normalization)**: Standardizes Indian entity abbreviations (`mandir` $\leftrightarrow$ `temple`, `soc` $\leftrightarrow$ `society`).
* **Stage 5 (Google Places Text Search)**: Localized POI and landmark proximity resolution (Scores: 70–99).
* **Stage 6 (Google Geocoding Rooftop Exact Match)**: Rooftop verification and confidence scoring (Score: 100).
* **Database Engine**: PostgreSQL streaming batches with upsert semantics and table health checks.
* **Concurrency & Rate Pacing**: Multithreading pool with token-bucket QPS rate limiter (`--max-qps 10.0`).

### 2. Isolated Data Architecture (`data/`)
* **`data/input/`**: Houses raw address files separate from application code.
* **`data/output/`**: Isolates output files and prevents accidental overwrite of source files.
* **`data/cache/`**: Manages persistent SQLite API caches (`geocode_cache.db`).

### 3. Operational Scripts (`scripts/`)
* **`scripts/init_postgres.py`**: Handles database connection checks, table provisioning, and loading input CSV records.
* **`scripts/run_pipeline.bat` / `.ps1` / `.sh`**: Interactive launchers with directory-agnostic path resolution for 1-click execution.

### 4. Automated Quality Assurance (`tests/`)
* **`tests/test_geocode_v2.py`**: 108 comprehensive offline unit and integration tests run via `pytest`.

---

## Standard Execution

* **Offline Database Validation (0 API calls)**:
  ```powershell
  python src/geocode_valid_addresses_v2.py --db --no-api --workers 8 --batch-size 50
  ```
* **Database Status Check**:
  ```powershell
  python scripts/init_postgres.py --status
  ```
* **Interactive Menu**:
  ```powershell
  .\scripts\run_pipeline.bat
  ```
* **Automated Unit Tests**:
  ```powershell
  pytest
  ```
