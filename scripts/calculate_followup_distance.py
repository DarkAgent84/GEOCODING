#!/usr/bin/env python3
"""
calculate_followup_distance.py - Calculate distance between Geocoded and Follow-up coordinates.

Reads from:
    soa_follow_curr_addr_analysis (PostgreSQL)
Calculates:
    Haversine great-circle distance between (gp_latitude, gp_longitude)
    and (follow_current_lat, follow_current_long) for all eligible records.
Stores in:
    1) The source table itself (soa_follow_curr_addr_analysis):
       Adds column 'distance' (if not present) and updates the matching records.
    2) A dedicated separate table (default: soa_follow_curr_addr_distance) containing ONLY:
       - dc_loan_number
       - gp_lat_long
       - follow_current_lat_long
       - distance

Usage:
    # 1. Calculate distance for EACH AND EVERY record (default: all records with coordinates)
    python scripts/calculate_followup_distance.py

    # 2. In kilometers or meters
    python scripts/calculate_followup_distance.py --unit km
    python scripts/calculate_followup_distance.py --unit m

    # 3. Include all rows (even those without coordinates, with distance = NULL)
    python scripts/calculate_followup_distance.py --include-all-rows

    # 4. Filter only high confidence records if desired
    python scripts/calculate_followup_distance.py --min-score 60

    # 5. Dry-run preview
    python scripts/calculate_followup_distance.py --dry-run
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
    lat1: Optional[float], lon1: Optional[float], lat2: Optional[float], lon2: Optional[float], unit: str = "km"
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

    try:
        lat1_f, lon1_f = float(lat1), float(lon1)
        lat2_f, lon2_f = float(lat2), float(lon2)
        if math.isnan(lat1_f) or math.isnan(lon1_f) or math.isnan(lat2_f) or math.isnan(lon2_f):
            return None
    except (ValueError, TypeError):
        return None

    # Earth radius
    r = 6371.0 if unit.lower() == "km" else 6371000.0

    phi1 = math.radians(lat1_f)
    phi2 = math.radians(lat2_f)
    delta_phi = math.radians(lat2_f - lat1_f)
    delta_lambda = math.radians(lon2_f - lon1_f)

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


def update_source_table_distance(
    conn,
    schema: str,
    table: str,
    cols: Dict[str, str],
    distance_col: str = "distance",
    min_confidence: Optional[int] = None,
    unit: str = "km",
) -> int:
    """
    Ensures distance column exists on the source table and updates it
    directly using PostgreSQL's trigonometric functions for all eligible rows.
    """
    r = 6371.0 if unit.lower() == "km" else 6371000.0
    where_clauses = [
        f"{cols['gp_lat']} IS NOT NULL",
        f"{cols['gp_long']} IS NOT NULL",
        f"{cols['follow_lat']} IS NOT NULL",
        f"{cols['follow_long']} IS NOT NULL",
    ]
    params: List[Any] = []
    if min_confidence is not None:
        where_clauses.append(f"{cols['confidence_score']} > %s")
        params.append(min_confidence)

    where_str = " AND ".join(where_clauses)

    with conn.cursor() as cur:
        # Add column if not exists
        cur.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN IF NOT EXISTS {distance_col} DOUBLE PRECISION;")

        # Update distance values
        update_sql = f"""
            UPDATE {schema}.{table}
            SET {distance_col} = ROUND(
                ({r} * acos(
                    least(1.0, greatest(-1.0, 
                        cos(radians({cols['gp_lat']})) * cos(radians({cols['follow_lat']})) *
                        cos(radians({cols['follow_long']}) - radians({cols['gp_long']})) +
                        sin(radians({cols['gp_lat']})) * sin(radians({cols['follow_lat']}))
                    ))
                ))::numeric, 2
            )::double precision
            WHERE {where_str};
        """
        cur.execute(update_sql, tuple(params))
        updated_count = cur.rowcount

    conn.commit()
    return updated_count


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
    source_dist_col: str = "distance",
    update_source: bool = True,
    min_confidence: Optional[int] = None,
    include_all_rows: bool = False,
    unit: str = "km",
    batch_size: int = 1000,
    dry_run: bool = False,
    truncate: bool = True,
    export_csv: Optional[str] = None,
) -> int:
    """
    Extracts rows, computes Haversine distance, updates source table,
    and bulk inserts into dedicated 4-column target table.
    """
    cols = detect_source_columns(conn, schema, source_table)
    filter_desc = f"{cols['confidence_score']} > {min_confidence}" if min_confidence is not None else "ALL records"
    print(f"[1/5] Source table columns mapped:")
    print(f"      Loan No          -> {cols['loan_no']}")
    print(f"      GP Coords        -> ({cols['gp_lat']}, {cols['gp_long']})")
    print(f"      Follow Coords    -> ({cols['follow_lat']}, {cols['follow_long']})")
    print(f"      Filter Scope     -> {filter_desc}")

    # Build WHERE conditions
    where_clauses = []
    params: List[Any] = []

    if not include_all_rows:
        where_clauses.extend([
            f"{cols['gp_lat']} IS NOT NULL",
            f"{cols['gp_long']} IS NOT NULL",
            f"{cols['follow_lat']} IS NOT NULL",
            f"{cols['follow_long']} IS NOT NULL",
        ])

    if min_confidence is not None:
        where_clauses.append(f"{cols['confidence_score']} > %s")
        params.append(min_confidence)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    fetch_sql = f"""
        SELECT 
            {cols['loan_no']} AS dc_loan_number,
            {cols['gp_lat']} AS gp_lat,
            {cols['gp_long']} AS gp_long,
            {cols['follow_lat']} AS follow_lat,
            {cols['follow_long']} AS follow_long,
            {cols['confidence_score']} AS gp_score
        FROM {schema}.{source_table}
        {where_sql};
    """

    print(f"\n[2/5] Querying {schema}.{source_table} ({filter_desc})...")
    start_time = time.time()
    with conn.cursor() as cur:
        cur.execute(fetch_sql, tuple(params))
        raw_rows = cur.fetchall()

    print(f"      Retrieved {len(raw_rows):,} records in {time.time() - start_time:.2f}s")
    if not raw_rows:
        print("      No records found matching criteria.")
        return 0

    # Calculate distances
    print(f"\n[3/5] Calculating Haversine distance for records (unit: {unit.upper()})...")
    records_to_insert = []
    distances = []
    for r in raw_rows:
        loan_no, gp_lat, gp_lon, f_lat, f_lon, score = r
        dist = haversine_distance(gp_lat, gp_lon, f_lat, f_lon, unit=unit)
        gp_ll = f"{gp_lat}, {gp_lon}" if (gp_lat is not None and gp_lon is not None) else None
        follow_ll = f"{f_lat}, {f_lon}" if (f_lat is not None and f_lon is not None) else None
        
        # If not include_all_rows, only keep rows with valid distance
        if not include_all_rows and dist is None:
            continue

        records_to_insert.append((str(loan_no) if loan_no is not None else None, gp_ll, follow_ll, dist))
        if dist is not None:
            distances.append(dist)

    total_records = len(records_to_insert)
    if distances:
        print(f"      Calculated {len(distances):,} distances.")
        print(f"      Min Distance : {min(distances):.2f} {unit}")
        print(f"      Max Distance : {max(distances):.2f} {unit}")
        print(f"      Mean Distance: {sum(distances) / len(distances):.2f} {unit}")

    # Dry-run check
    if dry_run:
        print(f"\n[DRY RUN] Skipping database writes. Sample 5 calculated records (out of {total_records:,}):")
        for rec in records_to_insert[:5]:
            print(f"  Loan: {rec[0]} | GP: {rec[1]} | Follow: {rec[2]} | Dist: {rec[3]} {unit}")
        return total_records

    # Update distance column directly in the source table
    if update_source:
        print(f"\n[4/5] Adding/Updating column '{source_dist_col}' in source table {schema}.{source_table}...")
        u_start = time.time()
        updated_source = update_source_table_distance(
            conn=conn,
            schema=schema,
            table=source_table,
            cols=cols,
            distance_col=source_dist_col,
            min_confidence=min_confidence,
            unit=unit,
        )
        print(f"      Updated {updated_source:,} rows in {schema}.{source_table} with '{source_dist_col}' ({unit.upper()}) in {time.time() - u_start:.2f}s!")
    else:
        print(f"\n[4/5] Skipping source table column update (--no-source-update specified).")

    # Ensure destination table exists with only the 4 requested columns
    ensure_target_table(conn, schema, target_table)

    if truncate:
        print(f"\n[5/5] Truncating {schema}.{target_table} before inserting fresh records...")
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {schema}.{target_table};")
        conn.commit()

    # Bulk Insert into dedicated table
    insert_sql = f"""
        INSERT INTO {schema}.{target_table} (
            dc_loan_number,
            gp_lat_long,
            follow_current_lat_long,
            distance
        ) VALUES (%s, %s, %s, %s);
    """

    print(f"      Inserting {total_records:,} rows into {schema}.{target_table} (batch size {batch_size})...")
    ins_start = time.time()
    with conn.cursor() as cur:
        extras.execute_batch(cur, insert_sql, records_to_insert, page_size=batch_size)
    conn.commit()
    print(f"      Successfully inserted {total_records:,} rows into {schema}.{target_table} in {time.time() - ins_start:.2f}s!")

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

    return total_records


def main():
    parser = argparse.ArgumentParser(
        description="Calculate distance between Geocoded and Follow-up coordinates for all records."
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
        "--source-dist-col",
        default="distance",
        help="Column name to add/update in source table (default: distance)",
    )
    parser.add_argument(
        "--no-source-update",
        action="store_true",
        help="Skip adding/updating the distance column in the source table",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("DB_SCHEMA", "public"),
        help="Database schema (default: public)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Optional minimum gp_confidence_score filter (default: None, calculates for ALL records)",
    )
    parser.add_argument(
        "--include-all-rows",
        action="store_true",
        default=False,
        help="Include all table rows in the target table even if coordinates are null (default: False)",
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
            source_dist_col=args.source_dist_col,
            update_source=not args.no_source_update,
            min_confidence=args.min_score,
            include_all_rows=args.include_all_rows,
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
