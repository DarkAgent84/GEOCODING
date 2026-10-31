"""
Text normalization, similarity matching, regex extraction, and geospatial math.
"""
from __future__ import annotations

import difflib
import functools
import math
import re
import unicodedata
from .constants import (
    STOPWORDS,
    _ABBREVIATIONS,
)

def _normalize(text) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace."""
    t = unicodedata.normalize("NFKC", str(text)).lower().replace("_", " ")
    return " ".join(re.sub(r"[^\w\s]", " ", t).split())

def _expand(text) -> str:
    """Normalize and expand common abbreviations so 'CO OP BANK' == 'Co-operative Bank'."""
    n = _normalize(text)
    n = re.sub(r"\bco ?op(?:erative)?\b", "co operative", n)
    return " ".join(_ABBREVIATIONS.get(t, t) for t in n.split())

@functools.lru_cache(maxsize=200_000)
def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def _fuzzy_in(token: str, tokens, threshold: float = 0.85) -> bool:
    return any(_sim(token, t) >= threshold for t in tokens)

def _pincodes(text) -> set:
    """All 6-digit Indian pincodes in `text`."""
    return set(re.findall(r"(?<!\d)[1-9]\d{5}(?!\d)", str(text)))

def get_major_words(text: str) -> list:
    """
    Extract major words from an address:
    tokens >= 3 characters, containing at least one letter, not purely numeric, and not in STOPWORDS.
    """
    cleaned = re.sub(r"[^\w\s]", " ", str(text)).replace("_", " ")
    return [
        t for t in cleaned.split()
        if len(t) >= 3 and any(c.isalpha() for c in t) and t.lower() not in STOPWORDS
    ]

def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))

def _dedupe_repeats(text: str) -> str:
    """Remove a multi-word phrase that is immediately repeated ('X Y X Y' -> 'X Y')."""
    toks = text.split()
    changed = True
    while changed:
        changed = False
        for n in range(min(8, len(toks) // 2), 1, -1):
            for i in range(0, len(toks) - 2 * n + 1):
                a = [_normalize(t) for t in toks[i:i + n]]
                b = [_normalize(t) for t in toks[i + n:i + 2 * n]]
                if a == b and all(a) and any(ch.isalpha() for ch in "".join(a)):
                    del toks[i + n:i + 2 * n]
                    changed = True
                    break
            if changed:
                break
    return " ".join(toks)

def _cell(v):
    """Format a single field value for CSV export."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, float):
        return repr(round(v, 7)) if not float(v).is_integer() else str(int(v))
    return v

def _pairs(pairs, a_label="address", b_label="Google"):
    return ", ".join(f"'{a}' ({a_label}) vs '{b}' ({b_label})" for a, b in pairs)
