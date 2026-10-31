#!/usr/bin/env python3
"""
Initialize and manage PostgreSQL tables for the GEOCODING project.

Usage:
  python scripts/init_postgres.py --status
  python scripts/init_postgres.py --reload-input
"""
import argparse
import os
import sys
import psycopg2
from psycopg2 import extras
import pandas as pd
import numpy as np

# Ensure project root and src/ are in sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src")
for p in [_SRC, _ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from geocode_valid_addresses_v2 import ensure_db_tables

def load_env():
    for cand in [".env", os.path.join(_ROOT, ".env"), os.path.join(_HERE, "..", ".env"), os.path.join(_HERE, ".env")]:
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
        candidate = os.path.join(_ROOT, "data", "input", os.path.basename(csv_path))
        if os.path.isfile(candidate):
            csv_path = candidate
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
    if loan_col not in df.columns:
        print(f"ERROR: Primary key column '{loan_col}' not found in {csv_path}. Columns found: {list(df.columns)}")
        return

    curr_addr_col = "soa_current_address" if "soa_current_address" in df.columns else None
    prop_addr_col = "soa_property_address" if "soa_property_address" in df.columns else None
    foll_addr_col = "follow_new_address" if "follow_new_address" in df.columns else None

    total = len(df)
    print(f"Uploading {total} records into {schema}.{table}...")

    cur = conn.cursor()
    insert_sql = f"""
    INSERT INTO {schema}.{table} (soa_loan_no, soa_current_address, soa_property_address, follow_new_address)
    VALUES %s
    ON CONFLICT (soa_loan_no) DO UPDATE SET
        soa_current_address = EXCLUDED.soa_current_address,
        soa_property_address = EXCLUDED.soa_property_address,
        follow_new_address = EXCLUDED.follow_new_address;
    """

    records = []
    for i, row in df.iterrows():
        rec = (
            str(row[loan_col]).strip(),
            str(row[curr_addr_col]).strip() if curr_addr_col else "",
            str(row[prop_addr_col]).strip() if prop_addr_col else "",
            str(row[foll_addr_col]).strip() if foll_addr_col else ""
        )
        records.append(rec)
        if len(records) >= batch_size:
            extras.execute_values(cur, insert_sql, records, page_size=len(records))
            conn.commit()
            records.clear()
            print(f"  [Progress] Uploaded {i + 1}/{total} rows...")

    if records:
        extras.execute_values(cur, insert_sql, records, page_size=len(records))
        conn.commit()
        records.clear()

    cur.close()
    print(f"Done! Successfully pushed {total} rows to {schema}.{table}.\n")


def print_status(conn, schema="public"):
    create_tables(conn, schema)
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM {schema}.geocoding;")
    in_cnt = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {schema}.geocoding_processed;")
    out_cnt = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {schema}.geocode_cache;")
    cache_cnt = cur.fetchone()[0]
    cur.execute(f"""
        SELECT count(*)
        FROM {schema}.geocoding g
        LEFT JOIN {schema}.geocoding_processed p
          ON g.soa_loan_no = p.soa_loan_no AND p.address_column = 'soa_current_address'
        WHERE p.soa_loan_no IS NULL;
    """)
    pending = cur.fetchone()[0]
    print("=" * 55)
    print(f"DATABASE: {os.environ.get('DB_NAME')} ({os.environ.get('DB_HOST')})")
    print(f"Input records ({schema}.geocoding):               {in_cnt}")
    print(f"Processed records ({schema}.geocoding_processed):    {out_cnt}")
    print(f"Cached API responses ({schema}.geocode_cache):       {cache_cnt}")
    print(f"Pending records to geocode:                       {pending}")
    print("=" * 55)
    cur.close()

def main():
    p = argparse.ArgumentParser(description="Manage PostgreSQL tables for geocoding.")
    p.add_argument("--status", action="store_true", help="Print table row counts and pending items")
    p.add_argument("--reload-input", action="store_true", help="Push/reload GEOCODING.csv into PostgreSQL table 'geocoding'")
    p.add_argument("--input", "-i", default="GEOCODING.csv", help="Input CSV path (default: GEOCODING.csv)")
    p.add_argument("--schema", default=None, help="PostgreSQL schema (default: from .env or public)")
    args = p.parse_args()

    conn = get_conn()
    schema = args.schema or os.environ.get("DB_SCHEMA", "public")

    if args.reload_input:
        load_input_csv(conn, csv_path=args.input, schema=schema, table="geocoding")
        print_status(conn, schema)
    else:
        print_status(conn, schema)

    conn.close()

if __name__ == "__main__":
    main()
