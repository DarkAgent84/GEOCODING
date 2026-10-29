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
    cur = conn.cursor()
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {schema}.geocoding (
        id BIGSERIAL,
        soa_loan_no TEXT PRIMARY KEY,
        soa_current_address TEXT,
        soa_property_address TEXT,
        follow_new_address TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_geocoding_id ON {schema}.geocoding(id);
    CREATE INDEX IF NOT EXISTS idx_geocoding_current_address ON {schema}.geocoding(soa_current_address);

    CREATE TABLE IF NOT EXISTS {schema}.geocoding_processed (
        id BIGSERIAL PRIMARY KEY,
        input_row INTEGER,
        soa_loan_no TEXT NOT NULL,
        address_column TEXT NOT NULL DEFAULT 'soa_current_address',
        original_address TEXT,
        sanitized_address TEXT,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
        geocode_status TEXT,
        source_api TEXT,
        derivation_type TEXT,
        location_type TEXT,
        geocode_location_type TEXT,
        distance_between_apis_m DOUBLE PRECISION,
        confidence_score INTEGER,
        score_category TEXT,
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
        CONSTRAINT uq_geocoding_processed_loan_col UNIQUE (soa_loan_no, address_column)
    );
    CREATE INDEX IF NOT EXISTS idx_geocoding_proc_loan_no ON {schema}.geocoding_processed(soa_loan_no);
    CREATE INDEX IF NOT EXISTS idx_geocoding_proc_score_category ON {schema}.geocoding_processed(score_category);
    CREATE INDEX IF NOT EXISTS idx_geocoding_proc_review_needed ON {schema}.geocoding_processed(review_needed);
    CREATE INDEX IF NOT EXISTS idx_geocoding_proc_derivation ON {schema}.geocoding_processed(derivation_type);

    CREATE TABLE IF NOT EXISTS {schema}.geocode_cache (
        key TEXT PRIMARY KEY,
        response JSONB NOT NULL,
        saved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_geocode_cache_saved_at ON {schema}.geocode_cache(saved_at);
    """)
    conn.commit()
    cur.close()

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

