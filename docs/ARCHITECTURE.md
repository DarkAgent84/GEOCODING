# Project Architecture & Directory Layout

## Directory Structure

```
GEOCODING/
├── .env                       # Local environment variables & secrets (ignored in git)
├── .env.example               # Template environment configuration
├── .gitignore                 # Git ignore rules for virtualenv, DBs, caches
├── README.md                  # High-level project documentation
├── COMMANDS.md                # Comprehensive CLI command reference cheatsheet
├── requirements.txt           # Python package dependencies
├── geocode_valid_addresses_v2.py  # Root CLI entrypoint & backward-compatible shim
├── init_postgres.py           # Root database initialization & table status tool
├── run_pipeline.bat           # Windows 1-click execution menu & argument runner
├── run_pipeline.ps1           # PowerShell interactive runner
├── run_pipeline.sh            # Linux / macOS / Bash runner for Cloud & Docker
│
├── data/                      # Structured Data Directory
│   ├── input/
│   │   └── GEOCODING.csv      # Raw input address dataset (10,258 records)
│   ├── output/
│   │   ├── GEOCODING_processed.csv       # Processed results with scores & coordinates
│   │   └── geocoding_processed_result1.csv
│   └── cache/
│       ├── geocode_cache.db   # Local SQLite persistent API cache (WAL mode)
│       └── scratch_test.db
│
├── docs/                      # Extended Documentation
│   └── ARCHITECTURE.md        # Modular code architecture & system design
│
├── scripts/                   # Utility & Operations Scripts
│   ├── init_postgres.py       # Database schema creation, migration & data loader
│   ├── run_pipeline.bat       # Script copy for modular deployment
│   ├── run_pipeline.ps1
│   └── run_pipeline.sh
│
├── src/                       # Modular Python Source Code
│   └── geocoding/
│       ├── __init__.py        # Public package interface & symbol exports
│       ├── constants.py       # Scores, thresholds, vocabulary sets, exceptions, classes
│       ├── config.py          # Dynamic path resolution & .env loader
│       ├── text_utils.py      # Normalization, token similarity, regex pincodes, Haversine
│       ├── validation.py      # Stages 1-4 (Incomplete, Pincodes, Spelling, Synonyms)
│       ├── google_client.py   # ApiClient, QPS rate limiter, backoff, response trimming
│       ├── cache.py           # DiskCache (SQLite) & PostgresCache (PostgreSQL)
│       ├── scoring.py         # Place evaluation, geofence radius, remarks builder, derive
│       ├── db.py              # PostgreSQL streaming batches, migrations, upserts
│       ├── pipeline.py        # Worker thread pool, deterministic row order, CSV locking
│       └── cli.py             # CLI argument parser & main execution runner
│
└── tests/                     # Automated Test Suite
    ├── __init__.py
    └── test_geocode_v2.py     # 108 unit, mock, integration & regression tests
```

---

## Modular Architecture Overview

### 1. Separation of Concerns
* **`constants.py`**: Holds all immutable domain constants, score scales (0–100), geofence distances (50m–1000m), Indian geographic dictionaries (states, districts, prominent localities), landmark vocabulary, and custom exception classes.
* **`config.py`**: Resolves file paths dynamically. If `data/input/GEOCODING.csv` exists, it uses it; otherwise falls back gracefully to root `GEOCODING.csv`.
* **`text_utils.py`**: Pure utility functions without side-effects for normalizing text, measuring fuzzy Levenshtein ratio, extracting 6-digit Indian postal codes, and computing Haversine spherical distance.
* **`validation.py`**: Contains Stages 1–4 pre-filtering:
  * Stage 1: Incomplete address verification (length $\ge 15$, major words $\ge 3$, 6-digit PIN).
  * Stage 2: Conflicting PIN codes detection.
  * Stage 3: Offline spelling validation against known names dictionary.
  * Stage 4: Synonym and abbreviation normalization (`mandir` $\to$ `temple`, `opp` $\to$ `opposite`, etc.).
* **`cache.py`**: Dual-backend persistent caching (SQLite WAL mode on disk, and PostgreSQL JSONB table in DB mode).
* **`google_client.py`**: Production-grade HTTP client with automated exponential backoff, jitter, multi-worker QPS rate-pacing, and key redaction.
* **`scoring.py`**: Confidence scoring logic, landmark proximity evaluation, geofence radius assignment, and plain-language remarks generation.
* **`pipeline.py`**: Orchestrates parallel worker threads, preserves strict input row ordering, and safely flushes atomic CSV batches with lockfile concurrency protection.
* **`db.py`**: Manages PostgreSQL database schemas, indexing, table migrations, and chunked batch upserts.
* **`cli.py`**: Parses CLI parameters and connects inputs to either the CSV or Database execution pipeline.

---

## 100% Backward Compatibility
Existing workflows remain completely unaffected:
* Running `python geocode_valid_addresses_v2.py --db --no-api` from root works seamlessly.
* Running `python init_postgres.py --check` from root works seamlessly.
* Running `pytest` discovers all 108 tests under `tests/` and passes with 0 errors.
* Running `.\run_pipeline.bat` launches the interactive terminal menu.
