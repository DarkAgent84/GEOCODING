# Cost-Optimized Address Validation, Classification & Geocoding Pipeline

An enterprise-grade, cost-optimized address validation, classification, geocoding, and geofence radius recommendation engine designed for complex, unstructured Indian addresses.

Built to run locally or against **PostgreSQL** with an intelligent **6-Stage Sequential Pipeline** that rejects incomplete, ambiguous, or misspelled addresses *before* making any Google Maps Platform API calls, maximizing address resolution accuracy while slashing API spend.

---

## 🌟 Cost-Optimized 6-Stage Sequential Pipeline

Every address passes sequentially through 6 deterministic stages. Early rejection stages stop immediately with zero API calls:

```
Address Input
      │
      ▼
[Stage 1: Incomplete Address Detection] ──── FAILED ───► Score: 0–9   | 0 API Calls
      │ PASSED
      ▼
[Stage 2: Conflicting PIN Code Detection] ── FAILED ───► Score: 11–20 | 0 API Calls
      │ PASSED
      ▼
[Stage 3: Offline Spelling Error Detection] ─ FAILED ───► Score: 21–30 | 0 API Calls
      │ PASSED
      ▼
[Stage 4: Synonym & Entity Normalization] ── APPLIED ──► Preprocessing Score: 60–70
      │ (Proceeds to Geocoding with normalized address)
      ▼
[Stage 5 & 6: Google Geocoding & Places API]
      ├─► Verified Strict Rooftop Match ────────────────► Score: 100   | Geocoding Only
      ├─► Verified POI Exact Match (Fallback) ──────────► Score: 90–99 | Geocoding + Places
      ├─► Landmark Exact Match ─────────────────────────► Score: 70–80 | Geocoding + Places
      ├─► Partial Match (partial_match=True) ───────────► Score: 50    | Geocoding + Places
      ├─► Interpolated Match ───────────────────────────► Score: 45    | Geocoding + Places
      ├─► Street-Level Match ───────────────────────────► Score: 35    | Geocoding Only
      ├─► Area / Locality Centroid ─────────────────────► Score: 10    | Geocoding Only
      └─► Unresolved / Not Found ───────────────────────► Score: 0     | Geocoding Attempted
```

### Stage Details

1. **Stage 1: Incomplete Address Detection (Score 0–9 | 0 API Calls)**
   * Validates 3 strict completeness constraints:
     * Minimum 15 characters total length (`ADDRESS_TOO_SHORT`).
     * At least 3 major words ($\ge 3$ words; $< 3$ fails with `TOO_FEW_WORDS`).
     * Valid 6-digit Indian PIN code `[1-9]\d{5}` (`MISSING_PINCODE`).
   * Deeply categorized into subcategories with deterministic scoring within 0–9:
     * `ALL_CRITERIA_FAILED` (Score: 0)
     * `MISSING_PIN_AND_TOO_SHORT` (Score: 2)
     * `MISSING_PIN_AND_FEW_WORDS` (Score: 3)
     * `TOO_SHORT_AND_FEW_WORDS` (Score: 4)
     * `ADDRESS_TOO_SHORT` (Score: 5)
     * `TOO_FEW_WORDS` (Score: 6)
     * `MISSING_PINCODE` (Score: 7)

2. **Stage 2: Conflicting PIN Code Detection (Score 11–20 | 0 API Calls)**
   * Extracts all 6-digit PIN code candidates and deduplicates identical occurrences (e.g. repeated pincodes like `202001 - 202001` are correctly handled as a single pincode).
   * If $> 1$ distinct PIN codes are present (e.g. `400064` and `400097`), classifies as `DOUBLE_PINCODE` (Score: 15) or `MULTIPLE_PINCODES` (Score: 12) and stops immediately without API requests.

3. **Stage 3: Offline Spelling Mistake Detection (Score 21–30 | 0 API Calls)**
   * High-speed offline token validation against `ALL_KNOWN_SPELLING_WORDS` encompassing Indian states, union territories, major cities/districts, prominent localities, road suffixes, and canonical `STANDARD_LANDMARK_WORDS` (religious, educational, healthcare, civic, and commercial POIs such as `mandir`, `temple`, `hospital`, `school`, `college`, `masjid`, `church`, `basti`, `station`, etc.) and `COMMON_DEITY_WORDS`.
   * Identifies typos (e.g. `MANIDR` $\rightarrow$ `mandir`, `Mumbay` $\rightarrow$ `mumbai`, `Maharastra` $\rightarrow$ `maharashtra`) and records suspected words and suggested corrections.
   * Employs transposition/anagram prioritization so letter-swap typos cleanly resolve to their standard canonical terms.
   * Distinguishes legitimate regional variants and transliterations (e.g. `Bengaluru`/`Bangalore`, `Mumbai`/`Bombay`, `Chennai`/`Madras`, `Vadodara`/`Baroda`).
   * Unfamiliar local names (e.g. `Kavarai`, `Patibandla`) without close matches are recognized as valid proper nouns and never falsely flagged.
   * Ambiguous words are handled via configurable `SPELLING_UNCERTAIN_POLICY`.

4. **Stage 4: Synonym and Entity Normalization (Score 60–70 Preprocessing | Proceeds to Geocoding)**
   * Context-aware entity synonym normalization before geocoding (e.g. `Mandir` $\leftrightarrow$ `Temple`, `Rd` $\leftrightarrow$ `Road`, `St` $\leftrightarrow$ `Street`, `Soc` $\leftrightarrow$ `Society`, `Marg` $\leftrightarrow$ `Road`, `Kovil` $\leftrightarrow$ `Temple`).
   * Preserves `original_address` completely unchanged while storing `normalized_address` separately for auditability.
   * Tracks exact normalized replacements in `normalized_terms` and sets `synonym_normalization_applied: YES`.

5. **Stage 5: Verified POI Exact Match (Score 90–99 | Geocoding API + Places API Fallback)**
   * When rooftop match is partial or unavailable, falls back to Google Places Text Search biased around the geocoded locality.
   * Verifies that the returned POI corresponds to the intended entity and agrees in locality/PIN.

6. **Stage 6: Strict Exact Match (Score 100 | Geocoding API Only)**
   * Confirms `location_type = ROOFTOP` and `partial_match = False`.
   * Verifies returned address components against the input address.

---

## 📊 Scoring & Geofence Matrix

| Confidence Score | Match Classification | Stage | Geocoding API | Places API | Geofence Radius |
| :---: | :--- | :--- | :---: | :---: | :---: |
| **100** | `EXACT_MATCH` | Stage 6: Strict Exact | Yes | No | **50 m** |
| **90–99** | `POI_EXACT` | Stage 5: POI Match | Yes | Fallback | **100 m** |
| **70–80** | `LANDMARK_EXACT` | Stage 5: Landmark Match | Yes | Fallback | **250 m** |
| **60–70** | `SYNONYM_NORMALIZED` | Stage 4: Preprocessing | Yes | Fallback | *Follows Geocode* |
| **50** | `PARTIAL_MATCH` | Post-API Partial Rooftop | Yes | Attempted | **250 m** |
| **45** | `INTERPOLATED` | Post-API Street Interpolated | Yes | Attempted | **250 m** |
| **35** | `STREET_LEVEL` | Post-API Route / Street | Yes | Attempted | **1000 m** |
| **21–30** | `SPELLING_ERROR` | Stage 3: Spelling Error | **No** | **No** | **1000 m** |
| **11–20** | `DOUBLE_PINCODE` | Stage 2: Conflicting PINs | **No** | **No** | **1000 m** |
| **10** | `AREA_LEVEL` | Post-API Locality Centroid | Yes | Attempted | **1000 m** |
| **0–9** | `INCOMPLETE_ADDRESS` | Stage 1: Incomplete Address | **No** | **No** | **1000 m** |
| **0** | `NOT_FOUND` / `INVALID` | Unresolved or Empty Input | Attempted | Attempted | *None* |

---

## 📋 Complete Output Columns

The output CSV and PostgreSQL table `public.geocoding_processed` produce all required columns:

| Column | Type | Description |
| :--- | :---: | :--- |
| `input_row` | INTEGER | 0-indexed row number from input |
| `soa_loan_no` | TEXT | Primary key loan identifier |
| `address_column` | TEXT | Input address column name |
| `original_address` | TEXT | Raw input address (never modified) |
| `sanitized_address` | TEXT | Cleaned address text |
| `normalized_address` | TEXT | Address after Stage 4 synonym normalization |
| `address_length` | INTEGER | Character length of the address |
| `major_word_count` | INTEGER | Count of major meaningful words ($\ge 3$ chars, non-stopword) |
| `pincode_list` | TEXT | Comma-separated list of extracted 6-digit PIN codes |
| `pincode_count` | INTEGER | Number of distinct 6-digit PIN codes |
| `address_completeness_status` | TEXT | `COMPLETE` or `INCOMPLETE` |
| `address_issue_category` | TEXT | Specific validation subcategory or `NONE` |
| `spelling_error_detected` | BOOLEAN | `True` if spelling error or uncertainty detected |
| `suspected_misspelled_words` | TEXT | Words identified as likely spelling mistakes |
| `suggested_spelling_corrections` | TEXT | Dictionary correction suggestions |
| `synonym_normalization_applied` | BOOLEAN | `True` if Stage 4 normalized equivalent terms |
| `normalized_terms` | TEXT | Replacements applied (e.g. `mandir -> temple`) |
| `processing_stage` | TEXT | Final pipeline stage reaching decision |
| `confidence_score` | INTEGER | Score from `0` to `100` |
| `match_classification` | TEXT | Standardized classification category |
| `score_category` | TEXT | Detailed categorization code |
| `geocoding_api_called` | BOOLEAN | `True` if Google Geocoding API was executed |
| `places_api_called` | BOOLEAN | `True` if Google Places API was executed |
| `latitude` | DOUBLE | Latitude coordinate (empty on early failure) |
| `longitude` | DOUBLE | Longitude coordinate (empty on early failure) |
| `geocode_status` | TEXT | API status code (`OK`, `ZERO_RESULTS`, etc.) |
| `source_api` | TEXT | API source (`GEOCODING_API`, `PLACES_API`, `VALIDATION_RULE`) |
| `derivation_type` | TEXT | Detailed algorithm path |
| `location_type` | TEXT | Precision (`ROOFTOP`, `RANGE_INTERPOLATED`, `POI`, etc.) |
| `geocode_location_type` | TEXT | Geocoder location type |
| `distance_between_apis_m` | DOUBLE | Distance between Geocoding and Places results |
| `name_match_ratio` | DOUBLE | String similarity ratio for POI match |
| `place_name` | TEXT | Name of identified POI |
| `place_types` | TEXT | Google place types |
| `recommended_geofence_radius_m` | INTEGER | Recommended geofence radius (`50`, `100`, `250`, `1000`) |
| `geocode_formatted_address` | TEXT | Formatted address from Google |
| `address_verification` | TEXT | Verification status (`MATCH`, `PIN_MISMATCH`, etc.) |
| `flags` | TEXT | Semicolon-delimited audit flags |
| `review_needed` | BOOLEAN | `True` if human inspection is advised |
| `remarks` | TEXT | Plain-language audit sentence |
| `script_version` | TEXT | Script version identifier (`4.0`) |

---

## 🗄️ Database Architecture

The system connects to PostgreSQL (`geo_fence_analysis`):

* `public.geocoding`: Input table storing `soa_loan_no`, `soa_current_address`, `soa_property_address`, etc.
* `public.geocoding_processed`: Output table storing coordinates, scores, and all 41 columns. Automatically auto-migrates existing tables on startup.
* `public.geocode_cache`: Shared `JSONB` response cache with 30-day TTL compliance.

---

## 🚀 Quick Start

### 1. Installation
```bash
git clone https://github.com/DarkAgent84/GEOCODING.git
cd GEOCODING
pip install -r requirements.txt
```

### 2. Configuration
Copy `.env.example` to `.env` and configure your API key and DB credentials:
```ini
DB_HOST=13.200.150.31
DB_PORT=5432
DB_NAME=geo_fence_analysis
DB_USER=postgres
DB_PASSWORD=your_password
DB_SCHEMA=public

GOOGLE_API_KEY=your_google_maps_api_key
```

### 3. Usage Commands

#### Offline Pre-API Validation Mode (Zero API Calls, Complete Dataset Analysis):
Run through all addresses to detect and classify incomplete addresses, conflicting PIN codes, spelling mistakes, and synonym normalization WITHOUT making any Google API calls and without needing an API key:
```bash
# Process entire CSV offline with 4 concurrent workers (zero API calls, zero cost):
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_pre_api.csv --no-api --workers 4

# Aliases supported (--pre-api-only / --validate-only):
python geocode_valid_addresses_v2.py --input GEOCODING.csv --pre-api-only -w 8

# Run in PostgreSQL Database mode offline without API calls:
python geocode_valid_addresses_v2.py --db --no-api
```

#### Dry Run (Pre-validation & Cost Analysis without API Calls):
```bash
python geocode_valid_addresses_v2.py --input GEOCODING.csv --dry-run
```

#### Run N Random Rows (Sampling):
```bash
# Process 20 random addresses from CSV:
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output random_sample.csv --limit 20 --random

# Reproducible random sampling with seed:
python geocode_valid_addresses_v2.py --input GEOCODING.csv --limit 20 --random --seed 42

# In Database Mode:
python geocode_valid_addresses_v2.py --db --limit 20 --random
```

#### High-Performance Multiprocessing / Parallel Execution:
```bash
# Process CSV with 4 concurrent worker processes (multiprocessing pool):
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --workers 4

# Run 8 concurrent worker processes in PostgreSQL Database mode:
python geocode_valid_addresses_v2.py --db --workers 8

# Short flag (-w) with custom batch size:
python geocode_valid_addresses_v2.py --input GEOCODING.csv -w 4 --batch-size 50

# Concurrency backend ('thread' multithreading, default; or 'process' multiprocessing):
python geocode_valid_addresses_v2.py --input GEOCODING.csv --workers 4 --worker-mode thread
```

#### Run in CSV Mode:
```bash
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --batch-size 20
```

#### Run in PostgreSQL Database Mode:
```bash
python geocode_valid_addresses_v2.py --db --batch-size 20
```

#### Zero-Cost Cache Re-scoring:
```bash
python geocode_valid_addresses_v2.py --cache-only
```

---

## 🧪 Testing

Run the offline test suite (no network or API key required):
```bash
pytest test_geocode_v2.py -v
```
**Results**: 108 tests passing in ~7.9 seconds.

---

## 📄 License
Internal proprietary software for Credility Data Ingestion.
