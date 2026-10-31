"""
PostgreSQL database layer: table creation, batch upserting, and DB execution loop.
"""
from __future__ import annotations

import os
import random
import signal
from .cache import PostgresCache
from .constants import (
    DEFAULT_CACHE_MAX_AGE_DAYS,
    ERROR_DERIVATION,
    ApiAbort,
    RunTally,
)
from .google_client import ApiClient
from .text_utils import _normalize
from .validation import sanitize_address


def ensure_db_tables(conn, schema: str, input_table: str, output_table: str, cache_table: str = "geocode_cache"):
    """Create input, output, and cache tables in PostgreSQL if they do not already exist."""
    cur = conn.cursor()
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {schema}.{input_table} (
        id BIGSERIAL,
        soa_loan_no TEXT PRIMARY KEY,
        soa_current_address TEXT,
        soa_property_address TEXT,
        follow_new_address TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{input_table}_id ON {schema}.{input_table}(id);
    CREATE INDEX IF NOT EXISTS idx_{input_table}_current_address ON {schema}.{input_table}(soa_current_address);

    CREATE TABLE IF NOT EXISTS {schema}.{output_table} (
        id BIGSERIAL PRIMARY KEY,
        input_row INTEGER,
        soa_loan_no TEXT NOT NULL,
        address_column TEXT NOT NULL DEFAULT 'soa_current_address',
        original_address TEXT,
        sanitized_address TEXT,
        normalized_address TEXT,
        address_length INTEGER,
        major_word_count INTEGER,
        pincode_list TEXT,
        pincode_count INTEGER,
        address_completeness_status TEXT,
        address_issue_category TEXT,
        spelling_error_detected BOOLEAN,
        suspected_misspelled_words TEXT,
        suggested_spelling_corrections TEXT,
        synonym_normalization_applied BOOLEAN,
        normalized_terms TEXT,
        processing_stage TEXT,
        confidence_score INTEGER,
        match_classification TEXT,
        score_category TEXT,
        geocoding_api_called BOOLEAN,
        places_api_called BOOLEAN,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
        geocode_status TEXT,
        source_api TEXT,
        derivation_type TEXT,
        location_type TEXT,
        geocode_location_type TEXT,
        distance_between_apis_m DOUBLE PRECISION,
        name_match_ratio DOUBLE PRECISION,
        place_name TEXT,
        place_types TEXT,
        recommended_geofence_radius_m INTEGER,
        geocode_formatted_address TEXT,
        address_verification TEXT,
        flags TEXT,
        review_needed BOOLEAN,
        remarks TEXT,
        script_version TEXT,
        processed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_{output_table}_loan_col UNIQUE (soa_loan_no, address_column)
    );
    CREATE INDEX IF NOT EXISTS idx_{output_table}_loan_no ON {schema}.{output_table}(soa_loan_no);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_score_cat ON {schema}.{output_table}(score_category);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_review ON {schema}.{output_table}(review_needed);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_derivation ON {schema}.{output_table}(derivation_type);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_stage ON {schema}.{output_table}(processing_stage);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_match_cls ON {schema}.{output_table}(match_classification);

    CREATE TABLE IF NOT EXISTS {schema}.{cache_table} (
        key TEXT PRIMARY KEY,
        response JSONB NOT NULL,
        saved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{cache_table}_saved_at ON {schema}.{cache_table}(saved_at);
    """)

    migration_columns = [
        ("normalized_address", "TEXT"),
        ("address_length", "INTEGER"),
        ("major_word_count", "INTEGER"),
        ("pincode_list", "TEXT"),
        ("pincode_count", "INTEGER"),
        ("address_completeness_status", "TEXT"),
        ("address_issue_category", "TEXT"),
        ("spelling_error_detected", "BOOLEAN"),
        ("suspected_misspelled_words", "TEXT"),
        ("suggested_spelling_corrections", "TEXT"),
        ("synonym_normalization_applied", "BOOLEAN"),
        ("normalized_terms", "TEXT"),
        ("processing_stage", "TEXT"),
        ("match_classification", "TEXT"),
        ("geocoding_api_called", "BOOLEAN"),
        ("places_api_called", "BOOLEAN"),
    ]
    for col_name, col_type in migration_columns:
        try:
            cur.execute(f"ALTER TABLE {schema}.{output_table} ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
        except Exception:
            conn.rollback()

    conn.commit()
    cur.close()


def save_db_batch(conn, schema: str, output_table: str, batch: list):
    """Upsert a batch of processed rows into PostgreSQL."""
    if not batch:
        return
    from psycopg2 import extras
    cur = conn.cursor()
    cols = [
        "input_row", "soa_loan_no", "address_column", "original_address", "sanitized_address",
        "normalized_address",
        "address_length", "major_word_count", "pincode_list", "pincode_count",
        "address_completeness_status", "address_issue_category",
        "spelling_error_detected", "suspected_misspelled_words", "suggested_spelling_corrections",
        "synonym_normalization_applied", "normalized_terms",
        "processing_stage", "confidence_score", "match_classification", "score_category",
        "geocoding_api_called", "places_api_called",
        "latitude", "longitude", "geocode_status", "source_api", "derivation_type",
        "location_type", "geocode_location_type", "distance_between_apis_m",
        "name_match_ratio", "place_name", "place_types",
        "recommended_geofence_radius_m", "geocode_formatted_address", "address_verification",
        "flags", "review_needed", "remarks", "script_version"
    ]
    records = []
    for r in batch:
        from .constants import SCRIPT_VERSION
        rec = (
            r.get("input_row"),
            r.get("soa_loan_no"),
            r.get("address_column") or "soa_current_address",
            r.get("original_address") or "",
            r.get("sanitized_address") or "",
            r.get("normalized_address") or "",
            r.get("address_length") or 0,
            r.get("major_word_count") or 0,
            r.get("pincode_list") or "",
            r.get("pincode_count") or 0,
            r.get("address_completeness_status") or "COMPLETE",
            r.get("address_issue_category") or "NONE",
            bool(r.get("spelling_error_detected")),
            r.get("suspected_misspelled_words") or "",
            r.get("suggested_spelling_corrections") or "",
            bool(r.get("synonym_normalization_applied")),
            r.get("normalized_terms") or "",
            r.get("processing_stage") or "",
            r.get("confidence_score"),
            r.get("match_classification") or "",
            r.get("score_category"),
            bool(r.get("geocoding_api_called")),
            bool(r.get("places_api_called")),
            r.get("latitude"),
            r.get("longitude"),
            r.get("geocode_status"),
            r.get("source_api"),
            r.get("derivation_type"),
            r.get("location_type"),
            r.get("geocode_location_type"),
            r.get("distance_between_apis_m"),
            r.get("name_match_ratio"),
            r.get("place_name"),
            r.get("place_types"),
            r.get("recommended_geofence_radius_m"),
            r.get("geocode_formatted_address"),
            r.get("address_verification"),
            r.get("flags"),
            bool(r.get("review_needed")),
            r.get("remarks"),
            r.get("script_version") or SCRIPT_VERSION,
        )
        records.append(rec)

    cols_sql = ", ".join(cols)
    update_set = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols if c not in ("soa_loan_no", "address_column")])
    update_set += ", processed_at = CURRENT_TIMESTAMP"
    insert_sql = f"""
    INSERT INTO {schema}.{output_table} ({cols_sql})
    VALUES %s
    ON CONFLICT (soa_loan_no, address_column) DO UPDATE SET
    {update_set};
    """
    extras.execute_values(cur, insert_sql, records, page_size=len(records))
    conn.commit()
    cur.close()


def _run_db(args, api_key, client_factory) -> int:
    """Run geocoding directly against PostgreSQL tables."""
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 is required for database mode. Install it with: pip install psycopg2-binary")
        return 1

    db_host = args.db_host or os.environ.get("DB_HOST", "13.200.150.31")
    db_port = args.db_port or int(os.environ.get("DB_PORT", 5432))
    db_name = args.db_name or os.environ.get("DB_NAME", "geo_fence_analysis")
    db_user = args.db_user or os.environ.get("DB_USER", "postgres")
    db_password = args.db_password or os.environ.get("DB_PASSWORD", "")
    db_schema = args.db_schema or os.environ.get("DB_SCHEMA", "public")
    input_table = args.input_table
    output_table = args.output_table
    address_col = args.address_col or "soa_current_address"
    loan_col = args.loan_col or "soa_loan_no"

    print(f"Connecting to PostgreSQL: {db_user}@{db_host}:{db_port}/{db_name} (schema: {db_schema})")
    try:
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password,
            connect_timeout=10
        )
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}")
        return 1

    ensure_db_tables(conn, db_schema, input_table, output_table, args.cache_table)

    cur = conn.cursor()
    if args.retry_errors:
        cur.execute(f"DELETE FROM {db_schema}.{output_table} WHERE derivation_type = %s AND address_column = %s;",
                    (ERROR_DERIVATION, address_col))
        dropped = cur.rowcount
        conn.commit()
        if dropped > 0:
            print(f"--retry-errors: {dropped} previously failed rows removed from {output_table} and will be retried.")

    query = f"""
    SELECT g.id, g.{loan_col}, g.{address_col}
    FROM {db_schema}.{input_table} g
    LEFT JOIN {db_schema}.{output_table} p
      ON g.{loan_col} = p.{loan_col} AND p.address_column = %s
    WHERE p.{loan_col} IS NULL
    ORDER BY g.id;
    """
    cur.execute(query, (address_col,))
    rows = cur.fetchall()
    total_pending = len(rows)

    if args.random:
        seed = args.seed if args.seed is not None else random.randrange(2 ** 32)
        random.Random(seed).shuffle(rows)
        print(f"Random sampling (seed = {seed}; add --seed {seed} to repeat this exact order)")
    if args.limit is not None:
        rows = rows[:max(0, args.limit)]
        print(f"Found {total_pending} pending rows; processing up to {len(rows)} {'random ' if args.random else ''}(limit = {args.limit})")
    else:
        print(f"Rows to process from {db_schema}.{input_table}: {len(rows)}")

    if args.dry_run:
        cleaned = [sanitize_address(r[2])[0] for r in rows]
        valid = [c for c in cleaned if c]
        unique = {_normalize(c) for c in valid}
        print(f"Dry run (DB mode): {len(valid)} usable addresses ({len(rows) - len(valid)} empty/invalid), "
              f"{len(unique)} unique.")
        print(f"Worst case without any cache: {len(unique) * 2} API calls "
              f"({len(unique)} Geocoding + up to {len(unique)} Places).")
        cur.close()
        conn.close()
        return 0

    cache = None
    if not args.no_cache and not getattr(args, "no_api", False):
        try:
            cache_conn = psycopg2.connect(host=db_host, port=db_port, dbname=db_name, user=db_user,
                                          password=db_password, connect_timeout=10)
        except Exception as e:
            print(f"ERROR: Cannot open the cache connection to PostgreSQL: {e}")
            cur.close()
            conn.close()
            return 1
        cache = PostgresCache(cache_conn, db_schema, args.cache_table, args.cache_max_age_days, owns_conn=True)
        print(f"Using PostgreSQL cache table: {db_schema}.{args.cache_table}")
    if getattr(args, "no_api", False):
        client = None
    elif client_factory is not None:
        client = client_factory(args, cache)
    else:
        client = ApiClient(api_key, cache, region=args.region, cache_only=args.cache_only,
                           max_api_calls=args.max_api_calls, max_qps=args.max_qps)
    stats = getattr(client, "stats", {"api_calls": 0, "cache_hits": 0})

    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, AttributeError):
        pass

    batch: list = []
    tally = RunTally()
    exit_code = 0

    def flush_db():
        if batch:
            save_db_batch(conn, db_schema, output_table, batch)
            tally.saved += len(batch)
            batch.clear()

    # Import execute_rows locally from pipeline to avoid circular import
    from .pipeline import execute_rows

    try:
        execute_rows(rows, address_col, client, args.workers, batch, flush_db, args.batch_size, tally,
                     show_id=lambda k: k,
                     saved_note=lambda: f"   [saved {tally.saved} rows so far to PostgreSQL ({db_schema}.{output_table})]",
                     no_api=getattr(args, 'no_api', False))
    except ApiAbort as e:
        print(f"\nSTOPPED: {e}")
        exit_code = 1
    except KeyboardInterrupt:
        print("\nInterrupted; saving what is done to PostgreSQL.")
        exit_code = 130
    finally:
        flush_db()
        cur.close()
        conn.close()
        if cache is not None:
            cache.close()

    saved_total, categories, review = tally.saved, tally.categories, tally.review
    skipped_api, skipped_cache = tally.skipped_api, tally.skipped_cache
    print(f"\nDone. {saved_total} rows written to PostgreSQL -> {db_schema}.{output_table}")
    if categories:
        print("Categories: " + ", ".join(f"{k}={v}" for k, v in sorted(categories.items())))
    print(f"Rows flagged for review: {review}")
    if skipped_api:
        print(f"Not saved because of API/network errors (will be retried next run): {skipped_api}")
    if skipped_cache:
        print(f"Skipped because they are not in the cache (--cache-only): {skipped_cache}")
    print(f"API calls made: {stats.get('api_calls', 0)} | served from disk cache: {stats.get('cache_hits', 0)}")
    if getattr(args, "no_api", False):
        valid_cnt = categories.get("VALID_ADDRESS", 0) + categories.get("SYNONYM_NORMALIZED", 0)
        spell_cnt = categories.get("SPELLING_MISTAKE", 0) + categories.get("SPELLING_ERROR", 0)
        print("\n=== Pre-API Detection Summary ===")
        print(f"Total Rows Processed: {saved_total}")
        print(f"  * Valid Addresses (Ready for API): {valid_cnt}")
        print(f"    - Clean Complete: {categories.get('VALID_ADDRESS', 0)}")
        print(f"    - Synonym Normalized: {categories.get('SYNONYM_NORMALIZED', 0)}")
        print(f"  * Flagged / Pre-API Issues (No API Needed):")
        print(f"    - Incomplete Address (Stage 1): {categories.get('INCOMPLETE_ADDRESS', 0)}")
        print(f"    - Conflicting PIN Codes (Stage 2): {categories.get('DOUBLE_PINCODE', 0)}")
        print(f"    - Spelling Errors (Stage 3): {spell_cnt}")
        print(f"    - Empty / Invalid Input: {categories.get('INVALID_INPUT', 0)}")
        print(f"Total Flagged for Review: {review}")
        print("=================================\n")
    return exit_code
