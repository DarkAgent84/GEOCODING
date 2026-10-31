# Complete Command Reference Guide: Address Geocoding & Validation Pipeline

A complete, purpose-driven CLI command cheatsheet for running, testing, managing, and benchmarking the **Cost-Optimized Address Validation, Classification & Geocoding Pipeline**.

---

## Table of Contents
1. [Initial Setup & Environment](#1-initial-setup--environment)
2. [Database Operations (`init_postgres.py`)](#2-database-operations-init_postgrespy)
3. [Offline Pre-API Validation (Zero API Calls, Zero Spend)](#3-offline-pre-api-validation-zero-api-calls-zero-spend)
4. [Dry Run & Cost Estimation](#4-dry-run--cost-estimation)
5. [Live Geocoding Execution (Google APIs Active)](#5-live-geocoding-execution-google-apis-active)
6. [Sampling & Reproducible Testing](#6-sampling--reproducible-testing)
7. [Cache & Cost Optimization](#7-cache--cost-optimization)
8. [Error Recovery & Resuming](#8-error-recovery--resuming)
9. [Automated Testing & QA](#9-automated-testing--qa)
10. [Comprehensive Flag Reference Table](#10-comprehensive-flag-reference-table)

---

## 1. Initial Setup & Environment

### Install Python Dependencies
```powershell
pip install -r requirements.txt
```

### Install Optional PostgreSQL Driver (for DB Mode)
```powershell
pip install psycopg2-binary
```

### Configure Environment Variables
Copy `.env.example` to `.env` or set environment variables:
```ini
DB_HOST=13.200.150.31
DB_PORT=5432
DB_NAME=geo_fence_analysis
DB_USER=postgres
DB_PASSWORD=your_password
DB_SCHEMA=public

GOOGLE_API_KEY=your_google_maps_api_key
```

---

## 2. Database Operations (`init_postgres.py`)

### Check Database Connection & Table Row Counts
Prints input records, processed records, cached API responses, and pending records to geocode:
```powershell
python init_postgres.py --status
```

### Push / Reload `GEOCODING.csv` to PostgreSQL
Uploads/upserts all 10,258 records into `public.geocoding`:
```powershell
python init_postgres.py --reload-input
```

### Push a Custom CSV File into a Specific Schema
```powershell
python init_postgres.py --reload-input --input custom_addresses.csv --schema public
```

---

## 3. Offline Pre-API Validation (Zero API Calls, Zero Spend)

Runs Stages 1–4 (Incomplete check, Conflicting PINs, Spelling check, Synonym normalization) across your dataset **without calling Google Maps APIs** and without requiring `GOOGLE_API_KEY`.

### Run Entire CSV Offline with 8 Parallel Workers
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_validated.csv --no-api --workers 8
```

### Run with Short Flag (`-w`) and Larger Batch Flushing
```powershell
python geocode_valid_addresses_v2.py -i GEOCODING.csv -o GEOCODING_validated.csv --no-api -w 8 -b 50 --overwrite
```

### Using Pre-API Aliases
```powershell
# Using --pre-api-only:
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output validated.csv --pre-api-only -w 8

# Using --validate-only:
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output validated.csv --validate-only -w 8
```

### Run Offline Pre-API Validation in PostgreSQL Database Mode
Reads from `public.geocoding` and saves to `public.geocoding_processed` with 0 API calls:
```powershell
python geocode_valid_addresses_v2.py --db --no-api --workers 8
```

### Validate an Offline Sample (e.g. 100 Rows)
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output sample_pre_api.csv --no-api --limit 100 --overwrite
```

---

## 4. Dry Run & Cost Estimation

Cleans and sanitizes all addresses, counts usable vs invalid rows, deduplicates identical addresses, and calculates the exact worst-case Google API call cost:

### Dry Run on CSV
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --dry-run
```

### Dry Run on PostgreSQL
```powershell
python geocode_valid_addresses_v2.py --db --dry-run
```

---

## 5. Live Geocoding Execution (Google APIs Active)

Executes the full 6-stage pipeline (Stages 1–4 pre-filtering + Stage 5/6 Google Geocoding and Places API).

### Standard CSV Run with Multi-Worker Parallelism
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --workers 4 --max-qps 10
```

### High-Throughput Production Run (8 Workers, 15 QPS)
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv -w 8 --max-qps 15 -b 50
```

### Standard PostgreSQL Database Mode
```powershell
python geocode_valid_addresses_v2.py --db --workers 8 --max-qps 12 --batch-size 50
```

### Custom Input and Output Columns
```powershell
python geocode_valid_addresses_v2.py --input custom_leads.csv --output custom_out.csv --address-col "customer_address" --loan-col "lead_id"
```

### Start Fresh Overwrite (Renames Old File with Timestamp Backup)
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --overwrite
```

---

## 6. Sampling & Reproducible Testing

### Geocode N Random Unprocessed Rows
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output sample.csv --limit 20 --random
```

### Quick Shortcut Flag (`--sample N`)
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output sample.csv --sample 25
```

### Reproducible Random Sampling (with Fixed Seed)
Repeat the exact same sample across multiple test runs:
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output sample.csv --sample 50 --seed 42
```

### Database Mode Sampling
```powershell
python geocode_valid_addresses_v2.py --db --sample 20 --seed 123
```

---

## 7. Cache & Cost Optimization

### Zero-Cost Re-scoring from Disk Cache (`--cache-only`)
Re-evaluates scores, classifications, and geofences purely from the local SQLite/PostgreSQL cache without making any Google API calls. Rows not found in the cache are skipped:
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output rescored.csv --cache-only
```

### Set Hard Ceiling on API Spend (`--max-api-calls`)
Automatically halts processing once the API call budget is reached while safely flushing all completed progress:
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output out.csv --max-api-calls 200
```

### Custom Cache Expiration (Google 30-Day TTL Compliance)
```powershell
# Cache expires after 15 days:
python geocode_valid_addresses_v2.py --cache-max-age-days 15

# Never expire cache (0 = unlimited):
python geocode_valid_addresses_v2.py --cache-max-age-days 0
```

### Disable Cache Completely (Forces Live API Requests)
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output fresh.csv --no-cache
```

---

## 8. Error Recovery & Resuming

### Resume an Interrupted Run
Simply re-run the same command! The script reads the existing output file, skips already completed rows, and resumes where it left off:
```powershell
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --workers 8
```

### Retry Failed Processing Errors (`--retry-errors`)
Removes previously logged `PROCESSING_ERROR` rows from the output CSV / database and re-processes them:
```powershell
# CSV mode:
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --retry-errors

# Database mode:
python geocode_valid_addresses_v2.py --db --retry-errors
```

---

## 9. Automated Testing & QA

Run the offline pytest test suite (108 tests, 100% offline, requires no network or Google API key):

### Run Entire Test Suite
```powershell
pytest test_geocode_v2.py
```

### Run with Verbose Test Details
```powershell
pytest test_geocode_v2.py -v
```

### Run Tests Matching Specific Keywords
```powershell
# Run all concurrency and worker tests:
pytest test_geocode_v2.py -k "worker" -v

# Run all offline pre-API validation tests:
pytest test_geocode_v2.py -k "no_api" -v

# Run all Stage 1 incomplete address tests:
pytest test_geocode_v2.py -k "incomplete" -v

# Run all low confidence / area match tests:
pytest test_geocode_v2.py -k "low_confidence" -v

# Run all Stage 3 spelling tests:
pytest test_geocode_v2.py -k "spelling" -v
```

---

## 10. Comprehensive Flag Reference Table

| Flag | Short | Default | Description |
| :--- | :---: | :---: | :--- |
| `--input` | `-i` | `GEOCODING.csv` | Path to input CSV file. |
| `--output` | `-o` | `GEOCODING_processed.csv` | Path to output CSV file. |
| `--address-col` | `-a` | *Auto* | Address column name (auto-detects `soa_current_address`, `Addresses`, etc.). |
| `--loan-col` | `-l` | `soa_loan_no` | Identifier column name for row matching. |
| `--no-api` | | `False` | Run Stages 1–4 offline with 0 API calls (aliases: `--pre-api-only`, `--validate-only`). |
| `--workers` | `-w` | `4` | Number of concurrent workers processing addresses in parallel. |
| `--max-qps` | | `10.0` | Maximum combined Google API requests per second (0 = unlimited). |
| `--batch-size` | `-b` | `20` | Batch size for periodic disk flushing with `fsync`. |
| `--limit` | `-n` | `None` | Process at most N new rows this run. |
| `--random` | `-r` | `False` | Pick rows in randomized order instead of file sequence. |
| `--sample` | | `None` | Shortcut for `--random --limit N`. |
| `--seed` | | `None` | Random seed integer for reproducible sampling order. |
| `--dry-run` | | `False` | Clean addresses, report deduplication and API cost without calling APIs. |
| `--overwrite` | | `False` | Backs up old output file with timestamp and starts fresh. |
| `--retry-errors` | | `False` | Clears previously logged error rows so they are re-queried. |
| `--cache-only` | | `False` | Re-score purely from cache without any live Google API calls. |
| `--no-cache` | | `False` | Disable local caching entirely. |
| `--cache-max-age-days`| | `30` | Maximum cache age in days (0 = never expire). |
| `--max-api-calls` | | `None` | Stop run after making N live API calls. |
| `--region` | | `in` | Regional bias code for Google Geocoding (default: `in` for India). |
| `--db` | | `False` | Enable PostgreSQL database mode. |
| `--input-table` | | `geocoding` | PostgreSQL input table name. |
| `--output-table` | | `geocoding_processed` | PostgreSQL output table name. |
| `--cache-table` | | `geocode_cache` | PostgreSQL cache table name. |
