#!/usr/bin/env python3
"""
calculate_followup_distance.py - Calculate distance between Geocoded and Follow-up coordinates.

Reads from:
    soa_follow_curr_addr_analysis (PostgreSQL)
Filters:
    gp_confidence_score > 60 AND coordinates are NOT NULL
Calculates:
    Haversine great-circle distance between (gp_latitude, gp_longitude)
    and (follow_current_lat, follow_current_long)
Stores in:
    A dedicated table (default: soa_follow_curr_addr_distance) containing ONLY:
      - dc_loan_number
      - gp_lat_long
      - follow_current_lat_long
      - distance

Usage:
    python scripts/calculate_followup_distance.py
    python scripts/calculate_followup_distance.py --unit km
    python scripts/calculate_followup_distance.py --unit m
    python scripts/calculate_followup_distance.py --dry-run
    python scripts/calculate_followup_distance.py --target-table soa_follow_curr_addr_distance --export-csv distance_results.csv
"""

import argparse
import math
import os
import sys
import time
from typing import Optional, Tuple, Dict, Any, List

# Ensure scripts and root are on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for p in [_ROOT, _HERE]:
    if p not in sys.path:
        sys.path.insert(0, p)

import psycopg2
from psycopg2 import extras


def load_env():
    """Load environment variables from .env files in project root or parent."""
    candidates = [
        os.path.join(_ROOT, ".env"),
        os.path.join(_HERE, ".env"),
        os.path.join(_ROOT, "..", ".env"),
        ".env",
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[7:]
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
            break


def get_db_connection():
    """Connect to PostgreSQL using environment variables."""
    load_env()
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "13.200.150.31"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ.get("DB_NAME", "geo_fence_analysis"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=15,
    )


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float, unit: str = "km"
) -> Optional[float]:
    """
    Calculate great-circle distance between two (lat, lon) points using Haversine formula.

    :param lat1: Latitude of point 1 in degrees
    :param lon1: Longitude of point 1 in degrees
    :param lat2: Latitude of point 2 in degrees
    :param lon2: Longitude of point 2 in degrees
    :param unit: 'km' for kilometers, 'm' for meters
    :return: Rounded distance float, or None if invalid
    """
    if None in (lat1, lon1, lat2, lon2):
        return None

    # Earth radius
    r = 6371.0 if unit.lower() == "km" else 6371000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    # Clamp for numerical precision
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return round(r * c, 2)


def detect_source_columns(conn, schema: str, table: str) -> Dict[str, str]:
    """
    Inspect source table columns to support alternative column naming.
    Maps logical names to actual column names in PostgreSQL.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = %s AND table_name = %s;
            """,
            (schema, table),
        )
        existing = {row[0].lower(): row[0] for row in cur.fetchall()}

    if not existing:
        raise ValueError(f"Table '{schema}.{table}' does not exist or has no columns.")

    def pick(candidates: List[str], logical: str) -> str:
        for c in candidates:
            if c.lower() in existing:
                return existing[c.lower()]
        raise KeyError(
            f"Could not find any of candidates {candidates} for '{logical}' in {schema}.{table}. "
            f"Available: {sorted(existing.keys())[:15]}..."
        )

    return {
        "loan_no": pick(["dc_loan_number", "follow_loan_no", "gp_soa_loan_no", "loan_no"], "loan_number"),
        "gp_lat": pick(["gp_latitude", "gp_lat", "latitude"], "gp_latitude"),
        "gp_long": pick(["gp_longitude", "gp_long", "longitude"], "gp_longitude"),
        "follow_lat": pick(
            ["follow_current_latitude", "follow_current_lat", "current_lat"],
            "follow_current_latitude",
        ),
        "follow_long": pick(
            ["follow_current_longitude", "follow_current_long", "current_long"],
            "follow_current_longitude",
        ),
        "confidence_score": pick(
            ["gp_confidence_score", "confidence_score"], "gp_confidence_score"
        ),
    }


def ensure_target_table(conn, schema: str, table: str, drop_existing: bool = False):
    """
    Creates target table with ONLY the 4 requested columns:
      - dc_loan_number
      - gp_lat_long
      - follow_current_lat_long
      - distance
    """
    with conn.cursor() as cur:
        if drop_existing:
            cur.execute(f"DROP TABLE IF EXISTS {schema}.{table};")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {schema}.{table} (
                dc_loan_number TEXT,
                gp_lat_long TEXT,
                follow_current_lat_long TEXT,
                distance DOUBLE PRECISION
            );
            CREATE INDEX IF NOT EXISTS idx_{table}_loan_no ON {schema}.{table}(dc_loan_number);
            """
        )
    conn.commit()


def process_and_store_distances(
    conn,
    schema: str = "public",
    source_table: str = "soa_follow_curr_addr_analysis",
    target_table: str = "soa_follow_curr_addr_distance",
    min_confidence: int = 60,
    unit: str = "km",
    batch_size: int = 1000,
    dry_run: bool = False,
    truncate: bool = True,
    export_csv: Optional[str] = None,
) -> int:
    """
    Extracts matching rows, computes Haversine distance, and bulk inserts into target table.
    """
    cols = detect_source_columns(conn, schema, source_table)
    print(f"[1/4] Source table columns mapped:")
    print(f"      Loan No          -> {cols['loan_no']}")
    print(f"      GP Coords        -> ({cols['gp_lat']}, {cols['gp_long']})")
    print(f"      Follow Coords    -> ({cols['follow_lat']}, {cols['follow_long']})")
    print(f"      Confidence Score -> {cols['confidence_score']} (filter: > {min_confidence})")

    # Fetch rows
    fetch_sql = f"""
        SELECT 
            {cols['loan_no']} AS dc_loan_number,
            {cols['gp_lat']} AS gp_lat,
            {cols['gp_long']} AS gp_long,
            {cols['follow_lat']} AS follow_lat,
            {cols['follow_long']} AS follow_long,
            {cols['confidence_score']} AS gp_score
        FROM {schema}.{source_table}
        WHERE {cols['confidence_score']} > %s
          AND {cols['gp_lat']} IS NOT NULL
          AND {cols['gp_long']} IS NOT NULL
          AND {cols['follow_lat']} IS NOT NULL
          AND {cols['follow_long']} IS NOT NULL;
    """

    print(f"\n[2/4] Querying {schema}.{source_table} for records with {cols['confidence_score']} > {min_confidence}...")
    start_time = time.time()
    with conn.cursor() as cur:
        cur.execute(fetch_sql, (min_confidence,))
        raw_rows = cur.fetchall()

    print(f"      Retrieved {len(raw_rows):,} eligible records in {time.time() - start_time:.2f}s")
    if not raw_rows:
        print("      No records found matching criteria.")
        return 0

    # Calculate distances
    print(f"\n[3/4] Calculating Haversine distance (unit: {unit.upper()})...")
    records_to_insert = []
    distances = []
    for r in raw_rows:
        loan_no, gp_lat, gp_lon, f_lat, f_lon, score = r
        dist = haversine_distance(gp_lat, gp_lon, f_lat, f_lon, unit=unit)
        if dist is not None:
            gp_ll = f"{gp_lat}, {gp_lon}"
            follow_ll = f"{f_lat}, {f_lon}"
            records_to_insert.append((str(loan_no), gp_ll, follow_ll, dist))
            distances.append(dist)

    total_valid = len(records_to_insert)
    if distances:
        print(f"      Calculated {total_valid:,} distances.")
        print(f"      Min Distance : {min(distances):.2f} {unit}")
        print(f"      Max Distance : {max(distances):.2f} {unit}")
        print(f"      Mean Distance: {sum(distances) / len(distances):.2f} {unit}")

    # Dry-run check
    if dry_run:
        print("\n[DRY RUN] Skipping database insert. Sample 5 calculated records:")
        for rec in records_to_insert[:5]:
            print(f"  Loan: {rec[0]} | GP: {rec[1]} | Follow: {rec[2]} | Dist: {rec[3]} {unit}")
        return total_valid

    # Ensure destination table exists with only the 4 requested columns
    ensure_target_table(conn, schema, target_table)

    if truncate:
        print(f"\n[4/4] Truncating {schema}.{target_table} before inserting fresh records...")
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {schema}.{target_table};")
        conn.commit()

    # Bulk Insert
    insert_sql = f"""
        INSERT INTO {schema}.{target_table} (
            dc_loan_number,
            gp_lat_long,
            follow_current_lat_long,
            distance
        ) VALUES (%s, %s, %s, %s);
    """

    print(f"      Inserting {total_valid:,} rows into {schema}.{target_table} (batch size {batch_size})...")
    ins_start = time.time()
    with conn.cursor() as cur:
        extras.execute_batch(cur, insert_sql, records_to_insert, page_size=batch_size)
    conn.commit()
    print(f"      Successfully inserted {total_valid:,} rows in {time.time() - ins_start:.2f}s!")

    # Optional CSV Export
    if export_csv:
        import csv
        csv_out_path = os.path.abspath(export_csv)
        print(f"\n[CSV EXPORT] Exporting to {csv_out_path}...")
        with open(csv_out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["dc_loan_number", "gp_lat_long", "follow_current_lat_long", "distance"])
            writer.writerows(records_to_insert)
        print(f"             Exported {len(records_to_insert):,} rows to CSV.")

    return total_valid


def main():
    parser = argparse.ArgumentParser(
        description="Calculate distance between Geocoded and Follow-up coordinates (> 60 confidence score)."
    )
    parser.add_argument(
        "--source-table",
        default="soa_follow_curr_addr_analysis",
        help="Source table name in PostgreSQL (default: soa_follow_curr_addr_analysis)",
    )
    parser.add_argument(
        "--target-table",
        default="soa_follow_curr_addr_distance",
        help="Target table name with only 4 columns (default: soa_follow_curr_addr_distance)",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("DB_SCHEMA", "public"),
        help="Database schema (default: public)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=60,
        help="Minimum gp_confidence_score strictly greater than this value (default: 60)",
    )
    parser.add_argument(
        "--unit",
        choices=["km", "m"],
        default="km",
        help="Distance unit: 'km' (default) or 'm'",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for database bulk insert (default: 1000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate and preview records without writing to the database",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append records instead of truncating target table first",
    )
    parser.add_argument(
        "--export-csv",
        default=None,
        help="Optional file path to also save output records to CSV",
    )

    args = parser.parse_args()

    try:
        conn = get_db_connection()
        print(f"Connected to PostgreSQL database '{conn.info.dbname}' on {conn.info.host}:{conn.info.port}")
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

    try:
        total = process_and_store_distances(
            conn=conn,
            schema=args.schema,
            source_table=args.source_table,
            target_table=args.target_table,
            min_confidence=args.min_score,
            unit=args.unit,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            truncate=not args.append,
            export_csv=args.export_csv,
        )
        print(f"\nDone! Processed {total:,} records successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
