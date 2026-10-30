#!/usr/bin/env python3
"""
Initialize and manage PostgreSQL tables for the GEOCODING project.

Usage:
  python init_postgres.py --status
  python init_postgres.py --reload-input
  python init_postgres.py --reload-processed
"""
import argparse
import os
import sys
import psycopg2
from psycopg2 import extras
import pandas as pd
import numpy as np
from geocode_valid_addresses_v2 import ensure_db_tables

def load_env():
    for cand in [".env", os.path.join(os.path.dirname(__file__), ".env"), os.path.join(os.path.dirname(__file__), "..", ".env")]:
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
    args = p.parse_args()

    conn = get_conn()
    schema = os.environ.get("DB_SCHEMA", "public")
    print_status(conn, schema)
    conn.close()

if __name__ == "__main__":
    main()

