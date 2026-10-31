"""
Configuration, path resolution, and environment variables loading.
"""
from __future__ import annotations

import os
from .constants import (
    API_KEY_ENV,
    CONNECT_TIMEOUT_S,
    DEFAULT_CACHE_MAX_AGE_DAYS,
    DEFAULT_MAX_QPS,
    DEFAULT_WORKERS,
    MAX_ADDRESS_CHARS,
    MAX_CONSECUTIVE_ERRORS,
    MAX_CONSECUTIVE_INTERNAL_ERRORS,
    MAX_RETRIES,
    READ_TIMEOUT_S,
)

# Intelligent path resolution with fallback
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

def resolve_input_path(filename: str = "GEOCODING.csv") -> str:
    """Resolve input CSV path: checks data/input/<filename> then root <filename>."""
    data_input = os.path.join(_PROJECT_ROOT, "data", "input", filename)
    if os.path.isfile(data_input):
        return data_input
    root_input = os.path.join(_PROJECT_ROOT, filename)
    if os.path.isfile(root_input):
        return root_input
    return data_input  # Default to data/input even if not yet created

def resolve_output_path(filename: str = "GEOCODING_processed.csv") -> str:
    """Resolve output CSV path, defaulting to data/output/."""
    out_dir = os.path.join(_PROJECT_ROOT, "data", "output")
    if os.path.isdir(out_dir):
        return os.path.join(out_dir, filename)
    return os.path.join(_PROJECT_ROOT, filename)

def resolve_cache_db(filename: str = "geocode_cache.db") -> str:
    """Resolve SQLite cache DB path, defaulting to data/cache/."""
    cache_dir = os.path.join(_PROJECT_ROOT, "data", "cache")
    if os.path.isdir(cache_dir):
        return os.path.join(cache_dir, filename)
    return os.path.join(_PROJECT_ROOT, filename)

DEFAULT_INPUT_CSV = resolve_input_path("GEOCODING.csv")
DEFAULT_OUTPUT_CSV = resolve_output_path("GEOCODING_processed.csv")
DEFAULT_CACHE_DB = resolve_cache_db("geocode_cache.db")

def load_env(env_path=None):
    """Load key-value pairs from .env into os.environ if not already set."""
    candidates = []
    if env_path:
        candidates.append(env_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        ".env",
        os.path.join(_PROJECT_ROOT, ".env"),
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, "..", ".env"),
    ])
    for cand in candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        if line.startswith("export "):
                            line = line[7:]
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        os.environ.setdefault(k, v)
                return cand
            except Exception:
                pass
    return None
