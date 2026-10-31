"""
Execution pipeline: concurrency pool, strict row ordering, and CSV file persistence.
"""
from __future__ import annotations

import csv
import os
import random
import signal
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .cache import DiskCache
from .constants import (
    ERROR_DERIVATION,
    MAX_CONSECUTIVE_ERRORS,
    MAX_CONSECUTIVE_INTERNAL_ERRORS,
    OUTPUT_COLUMNS,
    ApiAbort,
    CacheMiss,
    LockError,
    RunTally,
    TransientApiError,
)
from .google_client import ApiClient
from .scoring import (
    _blank_result,
    build_row,
    derive_coordinates_with_confidence,
)
from .text_utils import (
    _cell,
    _normalize,
)
from .validation import (
    repair_truncated_tail,
    sanitize_address,
)


class OutputLock:
    """Lock file so two runs cannot write the same output at once."""

    def __init__(self, output_path):
        self.path = output_path + ".lock"
        self.held = False

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockError(f"'{self.path}' exists: another run is using this output file. "
                            f"If no run is active (it crashed), delete the .lock file and retry.")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def read_output(path):
    """(header, rows) of an existing output file."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = list(reader)
    return header, rows


def write_header(path):
    # Ensure parent directory exists
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(OUTPUT_COLUMNS)
        f.flush()
        os.fsync(f.fileno())


def rewrite_without_errors(path) -> int:
    """Drop PROCESSING_ERROR rows so they are processed again. Returns how many were dropped."""
    header, rows = read_output(path)
    di = header.index("derivation_type")
    keep = [r for r in rows if len(r) == len(header) and r[di] != ERROR_DERIVATION]
    dropped = len(rows) - len(keep)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(keep)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return dropped


def append_rows(path, rows):
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore", restval="")
        w.writerows([{k: _cell(v) for k, v in r.items()} for r in rows])
        f.flush()
        os.fsync(f.fileno())


def _process_unique(sanitized, client, stop, no_api=False):
    """Runs in a worker thread for ONE unique address."""
    if stop.is_set():
        return "STOPPED", None
    if no_api:
        return "OK", derive_coordinates_with_confidence(sanitized, client=None, no_api=True)
    try:
        return "OK", derive_coordinates_with_confidence(sanitized, client)
    except CacheMiss:
        return "CACHE_MISS", None
    except TransientApiError as e:
        return "TRANSIENT", str(e)
    except ApiAbort as e:
        stop.set()
        return "ABORT", str(e)
    except Exception as e:
        redact = getattr(client, "_redact", lambda x: str(x)) if client else str
        return "INTERNAL", (redact(f"{type(e).__name__}: {e}"), redact(traceback.format_exc(limit=3)))


def execute_rows(rows, address_col, client, workers, batch, flush, batch_size, tally, show_id, saved_note, no_api=False):
    """
    rows: list of (row_key, loan_no, original_address).
    Identical addresses are sent to the API once.
    Emits rows strictly in input order so output order is 100% deterministic and resume-safe.
    """
    total = len(rows)

    def note(key, loan_no, line):
        tally.done += 1
        print(f"[{tally.done}/{total}] row {show_id(key)} [{loan_no}]: {line}")

    def emit(key, loan_no, original, sanitized, san_flags, res, line):
        row = build_row(key, loan_no, address_col, original, sanitized, res, san_flags)
        tally.categories[row["score_category"]] += 1
        tally.review += 1 if row["review_needed"] else 0
        batch.append(row)
        note(key, loan_no, line)
        if len(batch) >= batch_size:
            flush()
            print(saved_note())

    cleaned_rows = []
    unique_addrs = {}
    for key, loan_no, original in rows:
        sanitized, san_flags = sanitize_address(original)
        cleaned_rows.append((key, loan_no, original, sanitized, san_flags))
        if sanitized:
            ckey = _normalize(sanitized)
            if ckey not in unique_addrs:
                unique_addrs[ckey] = sanitized

    stop = threading.Event()
    if hasattr(client, "stop_event"):
        client.stop_event = stop
    workers = max(1, int(workers or 1))
    if unique_addrs and not no_api:
        print(f"Processing {len(unique_addrs)} unique addresses with {workers} worker(s)")
    elif unique_addrs and no_api:
        print(f"Validating {len(unique_addrs)} unique addresses offline with {workers} worker(s)")

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="geo")
    futures = {}
    try:
        for ckey, saddr in unique_addrs.items():
            futures[ckey] = pool.submit(_process_unique, saddr, client, stop, no_api)

        consecutive_api = consecutive_internal = 0
        seen_ckeys = set()

        for key, loan_no, original, sanitized, san_flags in cleaned_rows:
            if not sanitized:
                res = _blank_result(status="Invalid/empty address", derivation_type="INVALID_INPUT",
                                    category="INVALID_INPUT")
                emit(key, loan_no, original, sanitized, san_flags, res, "invalid/empty address")
                continue

            ckey = _normalize(sanitized)
            fut = futures[ckey]
            kind, payload = fut.result()

            if kind == "STOPPED":
                continue
            if kind == "ABORT":
                raise ApiAbort(payload)
            if kind == "CACHE_MISS":
                tally.skipped_cache += 1
                note(key, loan_no, "not in cache, skipped (--cache-only)")
                continue
            if kind == "TRANSIENT":
                consecutive_api += 1
                tally.skipped_api += 1
                note(key, loan_no, f"API trouble ({payload}); row not saved, it will be retried next run")
                if consecutive_api >= MAX_CONSECUTIVE_ERRORS:
                    raise ApiAbort(f"{consecutive_api} addresses in a row failed because of API/network "
                                   f"errors (last: {payload}). Stopping; progress is saved.")
                continue
            if kind == "INTERNAL":
                consecutive_internal += 1
                msg, tb = payload
                print(f"INTERNAL ERROR {msg}\n{tb}")
                res = _blank_result(status=msg[:200], derivation_type=ERROR_DERIVATION,
                                    category="ERROR", flags=[ERROR_DERIVATION])
                emit(key, loan_no, original, sanitized, san_flags, res, f"INTERNAL ERROR {msg[:80]}")
                if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                    raise ApiAbort(f"{consecutive_internal} internal errors in a row; stopping.")
                continue

            consecutive_api = consecutive_internal = 0
            res = payload
            if ckey in seen_ckeys:
                line = "duplicate address -> reused"
            else:
                seen_ckeys.add(ckey)
                line = f"{res['confidence_score']} {res['category']}  {sanitized[:50]}"
            emit(key, loan_no, original, sanitized, san_flags, res, line)

    finally:
        stop.set()
        if sys.exc_info()[0] is not None:
            print("Stopping: waiting for requests already in flight to finish...")
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=True)


def _run(args, df, addresses, loans, address_col, loan_col, api_key, client_factory) -> int:
    total_records = len(addresses)
    output_csv = args.output

    if args.overwrite and os.path.exists(output_csv):
        backup = f"{output_csv}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(output_csv, backup)
        print(f"Existing output renamed to {backup}")
    processed_rows = set()
    if os.path.exists(output_csv):
        if repair_truncated_tail(output_csv):
            print("Note: removed a half-written last line from the existing output (previous run was cut off).")
        try:
            header, rows = read_output(output_csv)
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            print(f"\nERROR: cannot read existing output '{output_csv}': {e}")
            return 1
        if header != OUTPUT_COLUMNS:
            print(f"\nERROR: existing output '{output_csv}' was written with a different column layout "
                  f"(an older version of the script).\nNothing was processed or changed. "
                  f"Use a new --output file name, or pass --overwrite (the old file is renamed, not deleted).")
            return 1
        if args.retry_errors:
            dropped = rewrite_without_errors(output_csv)
            print(f"--retry-errors: {dropped} previously failed rows will be processed again")
            header, rows = read_output(output_csv)
        ci = header.index("input_row")
        for r in rows:
            if len(r) == len(header) and r[ci].strip().isdigit():
                processed_rows.add(int(r[ci]))
        print(f"Resuming: found {len(processed_rows)} already-processed rows")
    else:
        write_header(output_csv)

    todo = [i for i in range(total_records) if i not in processed_rows]
    if args.random:
        seed = args.seed if args.seed is not None else random.randrange(2 ** 32)
        random.Random(seed).shuffle(todo)
        print(f"Random sampling (seed = {seed}; add --seed {seed} to repeat this exact order)")
    if args.limit is not None:
        todo = todo[:max(0, args.limit)]
        print(f"Processing up to {len(todo)} new {'random ' if args.random else ''}rows (limit = {args.limit})")
    print(f"Rows to process: {len(todo)}")

    cache = None
    if not args.no_cache and not getattr(args, "no_api", False):
        cache = DiskCache(args.cache_db, args.cache_max_age_days)
        print(f"Using disk cache: {args.cache_db}")
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

    def flush():
        if batch:
            append_rows(output_csv, batch)
            tally.saved += len(batch)
            batch.clear()

    try:
        execute_rows([(i, loans[i], addresses[i]) for i in todo], address_col, client, args.workers,
                     batch, flush, args.batch_size, tally, show_id=lambda k: k + 1,
                     saved_note=lambda: f"   [saved {tally.saved} rows so far]",
                     no_api=getattr(args, 'no_api', False))
    except ApiAbort as e:
        print(f"\nSTOPPED: {e}")
        exit_code = 1
    except KeyboardInterrupt:
        print("\nInterrupted; saving what is done.")
        exit_code = 130
    finally:
        flush()
        if cache is not None:
            cache.close()

    saved_total, categories, review = tally.saved, tally.categories, tally.review
    skipped_api, skipped_cache = tally.skipped_api, tally.skipped_cache
    print(f"\nDone. {saved_total} rows written this run -> {output_csv}")
    if categories:
        print("Categories: " + ", ".join(f"{k}={v}" for k, v in sorted(categories.items())))
    print(f"Rows flagged for review: {review}")
    if skipped_api:
        print(f"Not saved because of API/network errors (will be retried next run): {skipped_api}")
    if skipped_cache:
        print(f"Skipped because they are not in the cache (--cache-only): {skipped_cache}")
    print(f"API calls made: {stats.get('api_calls', 0)} | served from disk cache: {stats.get('cache_hits', 0)}")
    return exit_code
