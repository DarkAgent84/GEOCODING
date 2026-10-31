"""
Persistent API Caches:
  - DiskCache: Local SQLite cache with optional WAL mode and age expiration
  - PostgresCache: Shared PostgreSQL key-value JSONB cache
"""
from __future__ import annotations

import json
import sqlite3
import threading
from .constants import DEFAULT_CACHE_MAX_AGE_DAYS


class DiskCache:
    """Stores the trimmed raw API answer per request, so scoring changes never need new calls."""

    def __init__(self, path, max_age_days=DEFAULT_CACHE_MAX_AGE_DAYS):
        self.max_age_days = max_age_days if (max_age_days and max_age_days > 0) else None
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._lock = threading.Lock()
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            "key TEXT PRIMARY KEY, response TEXT NOT NULL, saved_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.conn.commit()

    def get(self, key):
        with self._lock:
            if self.max_age_days is None:
                row = self.conn.execute("SELECT response FROM api_cache WHERE key=?", (key,)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT response FROM api_cache WHERE key=? AND saved_at >= datetime('now', ?)",
                    (key, f"-{int(self.max_age_days)} days")).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set(self, key, data):
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO api_cache (key, response) VALUES (?, ?)",
                              (key, json.dumps(data)))
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()


class PostgresCache:
    """Stores the trimmed raw API answer per request in PostgreSQL, matching DiskCache interface."""

    def __init__(self, conn, schema="public", table="geocode_cache", max_age_days=DEFAULT_CACHE_MAX_AGE_DAYS,
                 owns_conn=False):
        self.conn = conn
        self.owns_conn = owns_conn
        self._lock = threading.Lock()
        self.schema = schema
        self.table = table
        self.max_age_days = max_age_days if (max_age_days and max_age_days > 0) else None

    def get(self, key):
        with self._lock:
            cur = self.conn.cursor()
            try:
                if self.max_age_days is None:
                    cur.execute(f"SELECT response FROM {self.schema}.{self.table} WHERE key = %s;", (key,))
                else:
                    cur.execute(
                        f"SELECT response FROM {self.schema}.{self.table} "
                        f"WHERE key = %s AND saved_at >= CURRENT_TIMESTAMP - (%s || ' days')::INTERVAL;",
                        (key, str(int(self.max_age_days)))
                    )
                row = cur.fetchone()
                self.conn.commit()
                if not row:
                    return None
                val = row[0]
                if isinstance(val, (dict, list)):
                    return val
                return json.loads(val)
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return None
            finally:
                cur.close()

    def set(self, key, data):
        from psycopg2.extras import Json
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(f"""
                INSERT INTO {self.schema}.{self.table} (key, response, saved_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET
                    response = EXCLUDED.response,
                    saved_at = CURRENT_TIMESTAMP;
                """, (key, Json(data)))
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            finally:
                cur.close()

    def close(self):
        if self.owns_conn:
            try:
                self.conn.close()
            except Exception:
                pass
