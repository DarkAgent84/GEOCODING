"""
Command-line interface (CLI) and entrypoint orchestrator.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import pandas as pd

from .config import (
    DEFAULT_CACHE_DB,
    DEFAULT_CACHE_MAX_AGE_DAYS,
    DEFAULT_INPUT_CSV,
    DEFAULT_MAX_QPS,
    DEFAULT_OUTPUT_CSV,
    DEFAULT_WORKERS,
    load_env,
)
from .constants import (
    API_KEY_ENV,
    LockError,
)
from .db import _run_db
from .pipeline import (
    OutputLock,
    _run,
)
from .text_utils import _normalize
from .validation import sanitize_address


def parse_arguments(argv=None):
    p = argparse.ArgumentParser(description="Geocode addresses; score confidence and geofence radius.")
    p.add_argument("--input", "-i", default=DEFAULT_INPUT_CSV, help=f"Input CSV (default: {DEFAULT_INPUT_CSV})")
    p.add_argument("--output", "-o", default=DEFAULT_OUTPUT_CSV, help=f"Output CSV (default: {DEFAULT_OUTPUT_CSV})")
    p.add_argument("--address-col", "-a", default=None, help="Address column (auto-detects soa_current_address / Addresses / address)")
    p.add_argument("--loan-col", "-l", default="soa_loan_no", help="Loan number column (default: soa_loan_no)")
    p.add_argument("--limit", "-n", type=int, default=None, help="Process at most N NEW rows this run")
    p.add_argument("--random", "-r", action="store_true", help="Pick rows in random order instead of file order (use with --limit)")
    p.add_argument("--sample", type=int, default=None, metavar="N",
                   help="Shortcut for --random --limit N: process N random rows that are not done yet")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducible sampling (e.g. --seed 42)")
    p.add_argument("--no-api", "--pre-api-only", "--validate-only",
                   dest="no_api", action="store_true",
                   help="Run all pre-API validation stages (Stage 1 Incomplete, Stage 2 Conflicting PINs, "
                        "Stage 3 Spelling, Stage 4 Synonym) across all rows without making any Google API calls.")
    p.add_argument("--worker-mode", choices=["process", "thread"], default="thread",
                   help="Concurrency backend: 'process' (multiprocessing) or 'thread' (multithreading, default)")
    p.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS,
                   help=f"Addresses processed at the same time (default {DEFAULT_WORKERS}; 1 = one at a time)")
    p.add_argument("--max-qps", type=float, default=DEFAULT_MAX_QPS,
                   help=f"Max Google requests per second across all workers (default {DEFAULT_MAX_QPS:g}; 0 = unlimited)")
    p.add_argument("--batch-size", "-b", type=int, default=20, help="Rows per save to CSV / PostgreSQL (default: 20)")
    p.add_argument("--region", default="in", help="Region bias for Google (default: in)")
    p.add_argument("--cache-db", default=DEFAULT_CACHE_DB, help=f"SQLite cache file (default: {DEFAULT_CACHE_DB})")
    p.add_argument("--no-cache", action="store_true", help="Disable the cache (every address hits the APIs)")
    p.add_argument("--cache-only", action="store_true",
                   help="Never call Google; re-score from the cache. Rows missing from the cache are skipped.")
    p.add_argument("--cache-max-age-days", type=int, default=DEFAULT_CACHE_MAX_AGE_DAYS,
                   help=f"Ignore cache rows older than this (default {DEFAULT_CACHE_MAX_AGE_DAYS}; 0 = never expire)")
    p.add_argument("--max-api-calls", type=int, default=None, help="Stop after this many API calls this run")
    p.add_argument("--retry-errors", action="store_true", help="Re-process rows previously saved as PROCESSING_ERROR")
    p.add_argument("--overwrite", action="store_true", help="Start a fresh output file (the old one is renamed, not deleted)")
    p.add_argument("--dry-run", action="store_true", help="Clean addresses and report counts; no API calls")

    # PostgreSQL Database mode arguments
    p.add_argument("--db", action="store_true", help="Run in PostgreSQL Database mode")
    p.add_argument("--db-host", default=None, help="Postgres host (default: from .env / DB_HOST)")
    p.add_argument("--db-port", type=int, default=None, help="Postgres port (default: from .env / DB_PORT or 5432)")
    p.add_argument("--db-name", default=None, help="Postgres database name (default: from .env / DB_NAME)")
    p.add_argument("--db-user", default=None, help="Postgres user (default: from .env / DB_USER)")
    p.add_argument("--db-password", default=None, help="Postgres password (default: from .env / DB_PASSWORD)")
    p.add_argument("--db-schema", default=None, help="Postgres schema (default: from .env / DB_SCHEMA or public)")
    p.add_argument("--input-table", default="geocoding", help="Postgres input table name (default: geocoding)")
    p.add_argument("--output-table", default="geocoding_processed", help="Postgres output table name (default: geocoding_processed)")
    p.add_argument("--cache-table", default="geocode_cache", help="Postgres cache table name (default: geocode_cache)")
    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be at least 1")
    if args.max_qps < 0:
        p.error("--max-qps cannot be negative")
    if args.sample is not None:
        args.random, args.limit = True, args.sample
    return args


def read_csv_safe(path):
    """Read a CSV as strings (keeps loan numbers intact); fall back to cp1252 if not UTF-8."""
    kw = dict(dtype=str, keep_default_na=False)
    try:
        return pd.read_csv(path, encoding="utf-8-sig", **kw)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="cp1252", **kw)


def _detect_address_col(df, wanted):
    cols = {c.strip(): c for c in df.columns}
    if wanted:
        return cols.get(wanted.strip())
    for cand in ("soa_current_address", "Addresses", "address", "Address"):
        if cand in cols:
            return cols[cand]
    return None


def main(argv=None, client_factory=None) -> int:
    load_env()
    args = parse_arguments(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    # Database Mode
    if args.db or (args.input and args.input.lower() in ("db", "database", "postgres")):
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if client_factory is None and not args.cache_only and not args.dry_run and not getattr(args, "no_api", False) and not api_key:
            print(f"Error: set the {API_KEY_ENV} environment variable (or use --cache-only / --dry-run).")
            return 2
        return _run_db(args, api_key, client_factory)

    # File check: allow fallback from data/input/ to root and vice-versa
    if not os.path.exists(args.input):
        root_alt = os.path.basename(args.input)
        if os.path.exists(root_alt):
            args.input = root_alt
        else:
            print(f"Error: input file '{args.input}' not found.")
            return 2

    df = read_csv_safe(args.input)
    df.columns = [c.strip() for c in df.columns]
    total_records = len(df)
    print(f"Reading addresses from: {args.input}  ({total_records} rows)")

    address_col = _detect_address_col(df, args.address_col)
    if not address_col:
        print(f"Error: could not find the address column. Available columns: {list(df.columns)}")
        return 2
    loan_col = args.loan_col.strip() if args.loan_col and args.loan_col.strip() in df.columns else None
    addresses = df[address_col].tolist()
    loans = df[loan_col].tolist() if loan_col else [""] * total_records
    print(f"Using address column: '{address_col}'" + (f", loan column: '{loan_col}'" if loan_col else ""))

    # ---- dry run ----------------------------------------------------------
    if args.dry_run:
        cleaned = [sanitize_address(a)[0] for a in addresses]
        valid = [c for c in cleaned if c]
        unique = {_normalize(c) for c in valid}
        print(f"Dry run: {len(valid)} usable addresses ({total_records - len(valid)} empty/invalid), "
              f"{len(unique)} unique.")
        print(f"Worst case without any cache: {len(unique) * 2} API calls "
              f"({len(unique)} Geocoding + up to {len(unique)} Places).")
        return 0

    # ---- API key ----------------------------------------------------------
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if client_factory is None and not args.cache_only and not getattr(args, "no_api", False) and not api_key:
        print(f"Error: set the {API_KEY_ENV} environment variable (or use --cache-only / --dry-run).")
        return 2

    exit_code = 0
    try:
        with OutputLock(args.output):
            exit_code = _run(args, df, addresses, loans, address_col, loan_col, api_key, client_factory)
    except LockError as e:
        print(f"\nERROR: {e}")
        return 1
    return exit_code
