#!/usr/bin/env python3
"""
Initialize and manage PostgreSQL tables for the GEOCODING project.

Usage:
  python init_postgres.py --status
  python init_postgres.py --reload-input
"""
import argparse
import os
import sys
import psycopg2
from psycopg2 import extras
import pandas as pd
import numpy as np

# Support import from src or root
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from geocoding import ensure_db_tables

def load_env():
    for cand in [".env", os.path.join(_HERE, ".env"), os.path.join(_HERE, "..", ".env")]:
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

def get_conn():
    load_env()
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "13.200.150.31"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ.get("DB_NAME", "geo_fence_analysis"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=10
    )

def create_tables(conn, schema="public"):
    ensure_db_tables(conn, schema, input_table="geocoding", output_table="geocoding_processed", cache_table="geocode_cache")

def load_input_csv(conn, csv_path="GEOCODING.csv", schema="public", table="geocoding", batch_size=1000):
    """Load or reload GEOCODING.csv into PostgreSQL with upsert."""
    if not os.path.isfile(csv_path):
        data_candidate = os.path.join(_HERE, "data", "input", os.path.basename(csv_path))
        if os.path.isfile(data_candidate):
            csv_path = data_candidate
        else:
            print(f"ERROR: File not found: {csv_path}")
            return
    create_tables(conn, schema)
    print(f"Reading {csv_path}...")
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="cp1252")
    df.columns = [c.strip() for c in df.columns]

    loan_col = "soa_loan_no"
    addr_cols = ["soa_current_address", "soa_property_address", "follow_new_address"]

    for c in [loan_col] + addr_cols:
        if c not in df.columns:
            df[c] = ""

    records = []
    for _, row in df.iterrows():
        loan = str(row[loan_col]).strip()
        if not loan:
            continue
        cur_addr = str(row["soa_current_address"]).strip()
        prop_addr = str(row["soa_property_address"]).strip()
        follow_addr = str(row["follow_new_address"]).strip()
        records.append((loan, cur_addr, prop_addr, follow_addr))

    print(f"Found {len(records)} valid records. Uploading in batches of {batch_size}...")
    cur = conn.cursor()
    sql = f"""
    INSERT INTO {schema}.{table} (soa_loan_no, soa_current_address, soa_property_address, follow_new_address)
    VALUES %s
    ON CONFLICT (soa_loan_no) DO UPDATE SET
        soa_current_address = EXCLUDED.soa_current_address,
        soa_property_address = EXCLUDED.soa_property_address,
        follow_new_address = EXCLUDED.follow_new_address;
    """
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        extras.execute_values(cur, sql, chunk)
        conn.commit()
        print(f"  Uploaded {min(i + batch_size, len(records))}/{len(records)} rows...")

    cur.close()
    print(f"Successfully loaded {len(records)} records into {schema}.{table}")

def check_status(conn, schema="public", input_table="geocoding", output_table="geocoding_processed", cache_table="geocode_cache"):
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s;", (schema, input_table))
    has_input = cur.fetchone()[0] > 0
    cur.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s;", (schema, output_table))
    has_output = cur.fetchone()[0] > 0
    cur.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s;", (schema, cache_table))
    has_cache = cur.fetchone()[0] > 0

    print(f"\n=== Database Status (Schema: {schema}) ===")
    if has_input:
        cur.execute(f"SELECT count(*) FROM {schema}.{input_table};")
        in_cnt = cur.fetchone()[0]
        print(f"  Input table ({input_table}): {in_cnt:,} records")
    else:
        print(f"  Input table ({input_table}): NOT FOUND")

    if has_output:
        cur.execute(f"SELECT count(*) FROM {schema}.{output_table};")
        out_cnt = cur.fetchone()[0]
        print(f"  Output table ({output_table}): {out_cnt:,} processed records")
    else:
        print(f"  Output table ({output_table}): NOT FOUND")

    if has_cache:
        cur.execute(f"SELECT count(*) FROM {schema}.{cache_table};")
        cache_cnt = cur.fetchone()[0]
        print(f"  Cache table ({cache_table}): {cache_cnt:,} cached API calls")
    else:
        print(f"  Cache table ({cache_table}): NOT FOUND")

    if has_input and has_output:
        cur.execute(f"""
        SELECT count(*)
        FROM {schema}.{input_table} g
        LEFT JOIN {schema}.{output_table} p ON g.soa_loan_no = p.soa_loan_no
        WHERE p.soa_loan_no IS NULL;
        """)
        pending = cur.fetchone()[0]
        print(f"  Pending to geocode: {pending:,} records")
    print("=========================================\n")
    cur.close()

def main():
    load_env()
    p = argparse.ArgumentParser(description="Manage PostgreSQL tables for geocoding.")
    p.add_argument("--status", "--check", action="store_true", help="Print table row counts and pending items")
    p.add_argument("--reload-input", action="store_true", help="Push/reload GEOCODING.csv into PostgreSQL table 'geocoding'")
    p.add_argument("--input", "-i", default="GEOCODING.csv", help="Input CSV path (default: GEOCODING.csv)")
    p.add_argument("--schema", default=os.environ.get("DB_SCHEMA", "public"), help="PostgreSQL schema (default: from .env or public)")
    args = p.parse_args()

    try:
        conn = get_conn()
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}")
        sys.exit(1)

    try:
        if args.status:
            check_status(conn, schema=args.schema)
        elif args.reload_input:
            load_input_csv(conn, csv_path=args.input, schema=args.schema)
            check_status(conn, schema=args.schema)
        else:
            check_status(conn, schema=args.schema)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
