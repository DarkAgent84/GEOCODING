# GEOCODING & Geofence Confidence Scoring Engine

An enterprise-grade, cost-optimized address validation, classification, geocoding, and geofence radius recommendation engine designed for complex, unstructured Indian addresses.

Built to run locally or against **PostgreSQL** with an intelligent **6-Stage Sequential Pipeline** that rejects invalid, ambiguous, or misspelled addresses *before* making any Google Maps Platform API calls, slashing API costs while maximizing match accuracy.

---

## 🌟 Cost-Optimized 6-Stage Sequential Pipeline

Every address passes sequentially through 6 deterministic stages. Early failure stages abort immediately with zero API calls:

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
[Stage 4: Synonym & Entity Normalization] ── APPLIED ──► Score: 60–70 (Pre-Geocode)
      │ (Proceeds with normalized address)
      ▼
[Stage 5 & 6: Google Geocoding & Places API]
      ├─► Verified Strict Rooftop Match ────────────────► Score: 100   | Geocoding Only
      ├─► Verified POI Exact Match (Fallback) ──────────► Score: 90–99 | Geocoding + Places
      ├─► Landmark Exact Match ─────────────────────────► Score: 70–80 | Geocoding + Places
      ├─► Partial / Approximate Match ──────────────────► Score: 40–50 | Geocoding + Places
      └─► Street-Level Match ───────────────────────────► Score: 25    | Geocoding Only
```

### Stage Details

1. **Stage 1: Incomplete Address Detection (Score 0–9 | 0 API Calls)**
   * Validates 3 strict completeness constraints:
     * Minimum 15 characters total length (`ADDRESS_TOO_SHORT`).
     * More than 3 major words (`> 3`, i.e., $\ge 4$ words; $\le 3$ fails with `INSUFFICIENT_MAJOR_WORDS`).
     * Valid 6-digit Indian PIN code `[1-9]\d{5}` (`PINCODE_MISSING`).
   * Categorizes failures deeply (`ADDRESS_TOO_SHORT`, `INSUFFICIENT_MAJOR_WORDS`, `PINCODE_MISSING`, `MULTIPLE_COMPLETENESS_FAILURES`, `ALL_CRITERIA_FAILED`).

2. **Stage 2: Conflicting PIN Code Detection (Score 11–20 | 0 API Calls)**
   * Extracts all 6-digit PIN code candidates and deduplicates identical occurrences.
   * If $> 1$ distinct PIN codes are present (e.g. `400064` and `400097`), classifies as `DOUBLE_PINCODE` / `MULTIPLE_PINCODES` and stops immediately without API requests.

3. **Stage 3: Offline Spelling Mistake Detection (Score 21–30 | 0 API Calls)**
   * High-speed offline token validation against Indian states, union territories, major cities, and common geographic/structural terminology using length-bucketed candidate filtering with LRU caching (~0.3 ms per address).
   * Identifies typos (e.g. `hospitl` $\rightarrow$ `hospital`, `roadd` $\rightarrow$ `road`) and stores suspected words and suggested corrections.
   * Protects legitimate regional variants and transliterations (e.g. `Bengaluru`/`Bangalore`, `Mumbai`/`Bombay`, `Chennai`/`Madras`, `Vadodara`/`Baroda`).
   * Ambiguous words are handled via configurable `SPELLING_UNCERTAIN_POLICY`.

4. **Stage 4: Synonym and Entity Normalization (Score 60–70 | Proceeds to Geocoding)**
   * Context-aware bidirectional entity synonym normalization before geocoding (e.g. `Mandir` $\leftrightarrow$ `Temple`, `Rd` $\leftrightarrow$ `Road`, `St` $\leftrightarrow$ `Street`, `Soc` $\leftrightarrow$ `Society`, `Marg` $\leftrightarrow$ `Road`).
   * Preserves `original_address` completely unchanged while storing `normalized_address` separately for auditability.
   * Tracks exact normalized replacements in `normalized_terms`.

5. **Stage 5: Verified POI Exact Match (Score 90–99 | Geocoding API + Places API Fallback)**
   * When rooftop match is partial or unavailable, falls back to Google Places Text Search.
   * Verifies that the returned POI name corresponds to the intended establishment (bank, school, hospital, temple, etc.) and is geographically consistent.

6. **Stage 6: Strict Exact Match (Score 100 | Google Geocoding API Only)**
   * Requires Google `location_type = ROOFTOP` and `partial_match = False`.
   * Cross-verifies locality and PIN code components against the input address.
   * Assigns minimum geofence radius of **50m**.

---

## 📊 Confidence Scoring & Geofence Matrix

| Stage / Classification | Score Range | Geocoding API | Places API | Geofence Radius | Review Needed |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Stage 1: Incomplete Address** | `0–9` | **No** | **No** | 1000 m | Yes |
| **Stage 2: Conflicting PIN Codes** | `11–20` | **No** | **No** | 1000 m | Yes |
| **Stage 3: Spelling Error** | `21–30` | **No** | **No** | 1000 m | Yes |
| **Stage 4: Synonym Normalization** | `60–70` | **Yes** | Fallback only | *Per match* | No |
| **Stage 5: Verified POI Exact Match** | `90–99` | **Yes** | **Yes** | 100 m | No |
| **Stage 6: Strict Exact Match** | `100` | **Yes** | **No** | 50 m | No |
| **Landmark Match** | `70–80` | **Yes** | **Yes** | 250 m | No |
| **Partial Rooftop / Approximate** | `40–50` | **Yes** | As needed | 250 m | Yes |
| **Street Level Match** | `25` | **Yes** | **No** | 1000 m | Yes |
| **Unresolved / Input Error** | `0` | Attempted | Attempted | *None* | Yes |

---

## 📁 41-Column Output Schema

The output CSV and PostgreSQL table `geocoding_processed` capture the complete validation and geocoding audit trail:

| Column | Type | Description |
| :--- | :--- | :--- |
| `input_row` | INTEGER | Original zero-based row index |
| `soa_loan_no` | TEXT | Primary key loan account number |
| `address_column` | TEXT | Name of processed address field |
| `original_address` | TEXT | Raw input address (never modified) |
| `sanitized_address` | TEXT | Address stripped of OCR/GPS noise |
| `normalized_address` | TEXT | Address after Stage 4 synonym normalization |
| `address_length` | INTEGER | Total character length of sanitized address |
| `major_word_count` | INTEGER | Count of meaningful words ($> 2$ characters) |
| `pincode_list` | TEXT | Semicolon-delimited list of detected 6-digit PIN codes |
| `pincode_count` | INTEGER | Distinct count of detected PIN codes |
| `address_completeness_status` | TEXT | `COMPLETE` or `INCOMPLETE` |
| `address_issue_category` | TEXT | Specific Stage 1 reason (`ADDRESS_TOO_SHORT`, `PINCODE_MISSING`, etc.) |
| `spelling_error_detected` | BOOLEAN | `True` if Stage 3 identified a spelling error |
| `suspected_misspelled_words` | TEXT | Semicolon-delimited suspected misspelled tokens |
| `suggested_spelling_corrections` | TEXT | Semicolon-delimited suggested correct terms |
| `synonym_normalization_applied` | BOOLEAN | `True` if Stage 4 normalized synonymous words |
| `normalized_terms` | TEXT | Exact term mappings applied (e.g. `mandir->temple; rd->road`) |
| `processing_stage` | TEXT | Final pipeline stage reached (`STAGE_1_INCOMPLETE`, `STAGE_6_STRICT_EXACT`, etc.) |
| `confidence_score` | INTEGER | Score from `0` to `100` |
| `match_classification` | TEXT | Standardized classification category |
| `score_category` | TEXT | Granular scoring bucket |
| `geocoding_api_called` | BOOLEAN | Whether Google Geocoding API was executed |
| `places_api_called` | BOOLEAN | Whether Google Places API was executed |
| `latitude` | DOUBLE | Latitude (up to 7 decimal digits precision) |
| `longitude` | DOUBLE | Longitude (up to 7 decimal digits precision) |
| `geocode_status` | TEXT | Google API status (`OK`, `ZERO_RESULTS`, etc.) |
| `source_api` | TEXT | API provider (`GEOCODING_API`, `PLACES_API`, `GEOCODING_API_FALLBACK`) |
| `derivation_type` | TEXT | Specific derivation algorithm path |
| `location_type` | TEXT | Result precision (`ROOFTOP`, `RANGE_INTERPOLATED`, `GEOMETRIC_CENTER`, `APPROXIMATE`) |
| `geocode_location_type` | TEXT | Original Geocoding location type |
| `distance_between_apis_m` | DOUBLE | Distance in meters between Geocoding and Places results |
| `name_match_ratio` | DOUBLE | Fuzzy string match similarity ratio (0.0 to 1.0) |
| `place_name` | TEXT | Name of identified POI / establishment |
| `place_types` | TEXT | Google place types (e.g. `bank\|establishment`) |
| `recommended_geofence_radius_m` | INTEGER | Recommended geofence radius (`50`, `100`, `250`, `1000`) |
| `geocode_formatted_address` | TEXT | Formatted address returned by Google |
| `address_verification` | TEXT | Locality and PIN verification verdict (`MATCH`, `PIN_MISMATCH`, etc.) |
| `flags` | TEXT | Semicolon-delimited diagnostic audit flags |
| `review_needed` | BOOLEAN | `True` if human review is advised |
| `remarks` | TEXT | Human-readable explanation of scoring, issues, and sources |
| `script_version` | TEXT | Version identifier of the processing script |

---

## 🗄️ Database Architecture

* `public.geocoding`: Input table storing `soa_loan_no`, `soa_current_address`, `soa_property_address`, etc.
* `public.geocoding_processed`: Output table storing coordinates, scores, audit metadata, and all 41 columns. Automatically auto-migrates existing tables on startup.
* `public.geocode_cache`: Shared `JSONB` response cache with 30-day TTL compliance.

---

## 🚀 Usage

### 1. Dry Run (Validates and reports cost estimates without API calls)
```bash
python geocode_valid_addresses_v2.py --dry-run
# Or Database Mode:
python geocode_valid_addresses_v2.py --db --dry-run
```

### 2. Live Run (CSV Mode)
```bash
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --batch-size 20
```

### 3. Live Run (PostgreSQL Mode)
```bash
python geocode_valid_addresses_v2.py --db --limit 100 --batch-size 20
```

### 4. Zero-Cost Re-scoring from Cache
```bash
python geocode_valid_addresses_v2.py --cache-only
```

---

## 🧪 Testing

Run the comprehensive offline test suite covering all 6 stages, error edge cases, and database migrations:
```bash
pytest test_geocode_v2.py -v
```
**Results**: 92 passed in ~3.5 seconds.

---

## 📄 License
Internal proprietary software for Credility Data Ingestion.

