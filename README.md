# GEOCODING & Geofence Confidence Scoring Engine

An enterprise-grade address geocoding, confidence scoring, and geofence radius recommendation engine designed for complex, unstructured Indian loan applicant addresses.

Built to run locally while integrating seamlessly with **PostgreSQL** for input records, audited results, and a centralized **API response cache** to minimize Google Maps Platform API costs.

---

## 🌟 Key Features

* **Multi-Stage Geocoding Pipeline**:
  1. **Text Sanitization**: Strips OCR artifacts, timestamps, GPS tags, blank field labels (`NO,`), and contiguous repeated phrases.
  2. **Google Geocoding API**: Exact rooftop matches with verified locality/pincodes are accepted immediately (*Fast Path: Score 100, Geofence 50m*).
  3. **Google Places Text Search API**: For partial or locality-level matches, performs biased search near the geocode to detect buildings, named POIs, and landmarks.
  4. **Arbitration Engine**: Distinguishes whether a POI is the address itself (`PREMISE`) or a landmark (`LANDMARK` - e.g., "NEAR X", "OPP X", "ke samne"). Excludes false-positive junctions and deity names with low specificity.
* **Confidence Scoring & Geofencing**:
  * Calculates an objective confidence score from `0` to `100`.
  * Automatically assigns a recommended geofence radius (`50m`, `100m`, `250m`, `1000m`).
  * Generates plain-language audit remarks and audit flags (`review_needed`).
* **PostgreSQL Integration**:
  * Reads input addresses from `public.geocoding`.
  * Writes verified coordinates and scores into `public.geocoding_processed`.
  * Shared API Response Cache in `public.geocode_cache` (`JSONB`), avoiding redundant API spend across team members.
* **100% Offline Automated Test Suite**:
  * 76 unit and regression tests simulating Google responses and network failure recovery without incurring API costs.

---

## 📊 Scoring & Geofence Matrix

| Confidence Score | Score Category | Derivation / Scenario | Geofence Radius |
| :---: | :--- | :--- | :---: |
| **100** | `ROOFTOP_EXACT` | Verified exact rooftop match from Google Geocoding | **50 m** |
| **90–99** | `POI_EXACT` | A named place from Places API that **IS** the target address | **100 m** |
| **70–80** | `LANDMARK_EXACT` | Address states it is **near / opposite / behind** this POI | **250 m** |
| **50** | `PARTIAL_MATCH` | Rooftop match flagged with Google `partial_match=true` | **250 m** |
| **45** | `INTERPOLATED` | Interpolated address along a street segment | **250 m** |
| **40–50** | `SPELLING_MISTAKE`| High fuzzy string match to a named premise | **250 m** |
| **25** | `STREET_LEVEL` | Geocoder found the street only; building unknown | **1000 m** |
| **10** | `INCOMPLETE_ADDRESS`| Locality / village / post office level only | **1000 m** |
| **0** | `NOT_FOUND` / `INVALID` | Blank address or unrecognizable string | *None* |

---

## 🗄️ Database Architecture

The project connects to PostgreSQL (`geo_fence_analysis`):

* `public.geocoding`: Input table storing `soa_loan_no`, `soa_current_address`, `soa_property_address`, etc.
* `public.geocoding_processed`: Output table storing coordinates, confidence score, score category, geofence radius, flags, review needed, remarks, and timestamps.
* `public.geocode_cache`: Shared `JSONB` response cache with 30-day TTL compliance.

---

## 🚀 Quick Start

### 1. Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/DarkAgent84/GEOCODING.git
cd GEOCODING
pip install -r requirements.txt
```

### 2. Configuration
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```
Edit `.env`:
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

#### Check Database Status & Progress:
```bash
python init_postgres.py --status
```

#### Run in Database Mode (Live Geocoding):
```bash
python geocode_valid_addresses_v2.py --db --limit 100
```

#### Free Cache Re-scoring (0 API Calls):
```bash
python geocode_valid_addresses_v2.py --db --limit 100 --cache-only
```

#### Dry Run (Verification & Sanitization Check):
```bash
python geocode_valid_addresses_v2.py --db --limit 100 --dry-run
```

#### Run in CSV Mode (Offline / Local CSV):
```bash
python geocode_valid_addresses_v2.py --input GEOCODING.csv --output GEOCODING_processed.csv --limit 100
```

---

## 🧪 Testing

Run the complete offline test suite (no database or API key required):
```bash
pytest test_geocode_v2.py -v
```

---

## 📄 License
Internal proprietary software for Credility Data Ingestion.
