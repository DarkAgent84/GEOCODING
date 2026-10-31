"""
Google Geocoding and Places API client with rate-limiting, retries, exponential backoff, and caching.
"""
from __future__ import annotations

import random
import re
import threading
import time
from typing import Optional

import requests

from .constants import (
    API_KEY_ENV,
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    BIAS_DEFAULT_M,
    CONNECT_TIMEOUT_S,
    DEFAULT_MAX_QPS,
    GEOCODING_URL,
    MAX_RETRIES,
    PLACES_TEXT_SEARCH_URL,
    READ_TIMEOUT_S,
    ApiAbort,
    CacheMiss,
    TransientApiError,
)
from .text_utils import _normalize


def _trim_geocode(data: dict) -> dict:
    results = []
    for r in (data.get("results") or [])[:1]:
        try:
            geom = r["geometry"]
            results.append({
                "formatted_address": r.get("formatted_address", ""),
                "types": r.get("types", []),
                "components": [{"long_name": c.get("long_name", ""), "types": c.get("types", [])}
                               for c in r.get("address_components", [])],
                "geometry": {"location": {"lat": geom["location"]["lat"], "lng": geom["location"]["lng"]},
                             "location_type": geom.get("location_type", "UNKNOWN")},
                "partial_match": bool(r.get("partial_match", False)),
                "place_id": r.get("place_id"),
            })
        except (KeyError, TypeError):
            continue
    return {"status": data.get("status"), "results": results}


def _trim_places(data: dict) -> dict:
    results = []
    for r in (data.get("results") or [])[:10]:
        try:
            loc = r["geometry"]["location"]
            results.append({
                "name": r.get("name"),
                "formatted_address": r.get("formatted_address", ""),
                "types": r.get("types", []),
                "geometry": {"location": {"lat": loc["lat"], "lng": loc["lng"]}},
                "place_id": r.get("place_id"),
            })
        except (KeyError, TypeError):
            continue
    return {"status": data.get("status"), "results": results}


class ApiClient:
    """Cached, retrying client for the two Google endpoints."""

    def __init__(self, api_key, cache=None, region="in", cache_only=False, max_api_calls=None,
                 session=None, sleep=time.sleep, max_qps=DEFAULT_MAX_QPS):
        self.api_key = api_key
        self.cache = cache
        self.region = region
        self.cache_only = cache_only
        self.max_api_calls = max_api_calls
        self._shared_session = session
        self._local = threading.local()
        self._sleep = sleep
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._min_interval = (1.0 / max_qps) if (max_qps and max_qps > 0) else 0.0
        self._next_slot = 0.0
        self.stop_event = None

    def geocode(self, address: str) -> dict:
        norm = _normalize(address)
        key = f"geo3|{self.region}|{norm}"
        legacy = f"geo2|{self.region}||{norm}"
        params = {"address": address, "region": self.region, "language": "en"}
        return self._get(GEOCODING_URL, params, key, [legacy], _trim_geocode)

    def places(self, query: str, bias=None, radius=None) -> dict:
        norm = _normalize(query)
        params = {"query": query, "region": self.region, "language": "en"}
        loc = ""
        if bias:
            loc = f"{bias[0]:.3f},{bias[1]:.3f}"
            params["location"] = loc
            params["radius"] = int(radius or BIAS_DEFAULT_M)
        key = f"places4|{self.region}|{loc}|{params.get('radius', '')}|{norm}"
        legacy = []
        if not bias or params.get("radius") == 5000:
            legacy = [f"places3|{self.region}|{loc}|{norm}"]
        return self._get(PLACES_TEXT_SEARCH_URL, params, key, legacy, _trim_places)

    def _redact(self, text) -> str:
        text = str(text)
        if self.api_key:
            text = text.replace(self.api_key, "REDACTED")
        return re.sub(r"(key=)[^&\s'\"]+", r"\1REDACTED", text)

    def _session(self):
        if self._shared_session is not None:
            return self._shared_session
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._local.session = requests.Session()
        return sess

    def _pace(self):
        """Space real HTTP requests so ALL workers together stay under --max-qps."""
        if not self._min_interval:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, attempt, extra=0.0):
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** attempt)) + extra
        self._sleep(delay + random.uniform(0, 0.5))

    def _get(self, url, params, key, legacy_keys, trim):
        if self.cache is not None:
            for k in [key] + list(legacy_keys):
                cached = self.cache.get(k)
                if cached is not None:
                    with self._lock:
                        self.stats["cache_hits"] += 1
                    return cached
        if self.cache_only:
            raise CacheMiss(key)
        if not self.api_key:
            raise ApiAbort(f"{API_KEY_ENV} is not set")

        full = dict(params, key=self.api_key)
        last_error = "unknown error"
        for attempt in range(MAX_RETRIES):
            if self.stop_event is not None and self.stop_event.is_set():
                raise ApiAbort("run is stopping")
            with self._lock:
                if self.max_api_calls is not None and self.stats["api_calls"] >= self.max_api_calls:
                    raise ApiAbort(f"--max-api-calls budget of {self.max_api_calls} reached")
                self.stats["api_calls"] += 1
            self._pace()
            try:
                resp = self._session().get(url, params=full, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
            except requests.exceptions.RequestException as e:
                last_error = f"{type(e).__name__}: {self._redact(e)}"
                self._backoff(attempt)
                continue

            code = resp.status_code
            if code in (401, 403):
                raise ApiAbort(f"HTTP {code} from Google (key rejected or API not enabled)")
            if code == 429 or code >= 500:
                last_error = f"HTTP {code}"
                self._backoff(attempt)
                continue
            if code != 200:
                return {"status": f"HTTP_{code}", "results": []}
            try:
                data = resp.json()
            except ValueError:
                last_error = "invalid JSON in response"
                self._backoff(attempt)
                continue

            status = data.get("status")
            if status in ("REQUEST_DENIED", "OVER_DAILY_LIMIT"):
                raise ApiAbort(f"{status}: {self._redact(data.get('error_message', 'no message'))}")
            if status in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"):
                last_error = status
                self._backoff(attempt, extra=2.0 if status == "OVER_QUERY_LIMIT" else 0.0)
                continue

            trimmed = trim(data)
            if self.cache is not None and status in ("OK", "ZERO_RESULTS"):
                self.cache.set(key, trimmed)
            return trimmed

        raise TransientApiError(last_error)
