#!/usr/bin/env python3
"""
Geocode addresses, score how much each result can be trusted, and recommend a geofence radius.

Pipeline per address
  1. Sanitize the text (strip junk, repeated phrases, stray tokens).
  2. Google Geocoding API.  An exact rooftop match whose own address agrees with the input
     (pincode / locality words) is accepted at once (score 100).
  3. Otherwise Google Places Text Search, biased toward the geocoded point.  Each of the top
     results is judged on its own: is it the address itself, or a landmark the address mentions
     ("NEAR X", "OPP X")?  Is it in the right locality?  Is it only an area or a junction named
     after the area?  Does it agree with the geocode?
  4. The better of the geocode candidate and the best POI candidate wins.

Score bands (the geofence radius follows the score)
  100      ROOFTOP_EXACT      exact rooftop, address verified                         -> 50 m
  90-99    POI_EXACT          a named place that IS the address                       -> 100 m
  70-80    LANDMARK_EXACT     a named place the address says it is near/opposite      -> 250 m
  50       PARTIAL_MATCH      rooftop Google flagged partial_match, address verified  -> 250 m
  45       INTERPOLATED       street-level estimate, address verified                 -> 250 m
  21-30    SPELLING_MISTAKE   a place whose name is a close misspelling of the        -> 1000 m
                              address words (21 when it is only a landmark)
  11-20    DOUBLE_PINCODE     the address holds two DIFFERENT pincodes. The text is    -> 1000 m
                              left untouched; the score is held in this band, scaled
                              by how good the underlying result was (11 = poor, 20 = exact)
  10       INCOMPLETE_ADDRESS area / locality / street level, or no candidate trusted -> 1000 m
  0        NOT_FOUND / INVALID_INPUT                                                  -> none
LANDMARK_EXACT is an addition to the original bands.  Set SCORE_LANDMARK to 90 below to fold it
back into POI_EXACT.  Every row also gets a plain-language `remarks` column.

Safety features
  * API key comes from the GOOGLE_API_KEY environment variable only (never from the code), and is
    redacted from every message and output cell.
  * Retries with exponential backoff and jitter; bad key / quota exhaustion stop the run cleanly.
  * Rows that failed only because of a network / quota problem are NOT written, so the next run
    retries them automatically.
  * Output is appended batch by batch with fsync; a half-written last line is repaired on resume;
    a lock file stops two runs from writing the same output.
  * Persistent SQLite cache of raw API answers: re-running, resuming and changing the scoring cost
    no API calls.  --cache-only re-scores purely from the cache.
  * --max-api-calls puts a hard ceiling on spend for a run.

Usage examples
  export GOOGLE_API_KEY=...                       (Windows: set GOOGLE_API_KEY=...)
  python geocode_valid_addresses_v2.py --input GEOCODING.csv --output out.csv --limit 20
  python geocode_valid_addresses_v2.py --input GEOCODING.csv --output out.csv --dry-run
  python geocode_valid_addresses_v2.py --input GEOCODING.csv --output rescored.csv --cache-only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import difflib
import functools
import json
import math
import multiprocessing
import os
import random
import re
import signal
import sqlite3
import sys
import time
import traceback
import unicodedata
from collections import Counter
from datetime import datetime

import pandas as pd
import requests

SCRIPT_VERSION = "3.1"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
API_KEY_ENV = "GOOGLE_API_KEY"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"

DEFAULT_INPUT_CSV = "GEOCODING.csv"
DEFAULT_OUTPUT_CSV = "GEOCODING_processed.csv"
DEFAULT_CACHE_DB = "geocode_cache.db"
# Google's terms limit how long geocoded coordinates may be cached (30 days at the time of
# writing - check your own agreement).  Cache rows older than this are ignored.  0 = never expire.
DEFAULT_CACHE_MAX_AGE_DAYS = 30

# Network behaviour
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 15
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
MAX_CONSECUTIVE_ERRORS = 5          # back-to-back rows lost to API trouble -> stop the run
MAX_CONSECUTIVE_INTERNAL_ERRORS = 10
REQUEST_PAUSE_S = 0.1
MAX_ADDRESS_CHARS = 300

# Scores
SCORE_ROOFTOP = 100
SCORE_POI_PREMISE_BASE = 90         # POI that is the address itself
SCORE_POI_MAX = 99
SCORE_LANDMARK = 70                 # POI the address is "near / opposite / behind"
SCORE_LANDMARK_MAX = 80
SCORE_SYNONYM_MIN, SCORE_SYNONYM_MAX = 60, 70  # Synonym normalization preprocessing score band
SCORE_PARTIAL = 50
SCORE_INTERPOLATED = 45
SCORE_STREET = 35                   # Street-level geocode
SCORE_AREA_LEVEL = 10               # Locality / area-level coarse geocode
SCORE_SPELLING_MIN, SCORE_SPELLING_MAX = 21, 30   # close misspelling of a place name
SCORE_LANDMARK_FUZZY = SCORE_SPELLING_MIN         # misspelled name, but only as a "near X" landmark
SCORE_DOUBLE_PIN_MIN, SCORE_DOUBLE_PIN_MAX = 11, 20   # address contains two different pincodes
SCORE_INCOMPLETE = 10               # Post-API incomplete / unresolved legacy fallback
SCORE_INCOMPLETE_MIN, SCORE_INCOMPLETE_MAX = 0, 9     # Stage 1 incomplete address pre-API score band

# Stage 1 subcategory scores (0-9)
INCOMPLETE_SUBCAT_SCORES = {
    "ALL_CRITERIA_FAILED": 0,
    "MISSING_PIN_AND_TOO_SHORT": 2,
    "MISSING_PIN_AND_FEW_WORDS": 3,
    "TOO_SHORT_AND_FEW_WORDS": 4,
    "ADDRESS_TOO_SHORT": 5,
    "TOO_FEW_WORDS": 6,
    "MISSING_PINCODE": 7,
}

# Name matching
SPELLING_SIMILARITY_MIN = 0.80      # mean word similarity needed for a "spelling mistake" match
SINGLE_WORD_SPELLING_MIN = 0.90     # stricter when only one distinctive word is available
MIN_TOKEN_SIMILARITY = 0.70         # every distinctive word must be at least this close
MIN_MEANINGFUL_WORDS = 2            # fewer place words than this -> address too vague

# Stage 3: Offline Spelling Validation Configuration
SPELLING_ERROR_THRESHOLD = 0.82     # Ratio >= 0.82 to known name classified as SPELLING_ERROR
SPELLING_UNCERTAIN_MIN = 0.75       # Ratio in [0.75, 0.82) classified as SPELLING_UNCERTAIN
SPELLING_UNCERTAIN_POLICY = "PROCEED"  # "PROCEED" allows uncertain to proceed to geocoding; "STOP" halts

# Incomplete address detection constraints (3 constraints only)
MIN_ADDRESS_CHARS = 15              # Constraint 1: minimum total characters
MIN_MAJOR_WORDS = 3                 # Constraint 2: requires at least 3 major words (>= 3, so < 3 fails)
STOPWORDS = {
    "the", "of", "and", "in", "at", "to", "for", "from",
    "near", "nr", "opp", "opposite", "behind", "beside",
}

# Geometry
PRECISE_GEOCODE_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}
FAR_LIMIT_M = {"ROOFTOP": 10_000, "RANGE_INTERPOLATED": 10_000,
               "GEOMETRIC_CENTER": 15_000, "APPROXIMATE": 25_000}
FAR_DEFAULT_M = 25_000              # a POI farther than this from a trusted geocode is a different place
BIAS_RADIUS_M = {"ROOFTOP": 5_000, "RANGE_INTERPOLATED": 5_000,
                 "GEOMETRIC_CENTER": 10_000, "APPROXIMATE": 20_000}
BIAS_DEFAULT_M = 20_000
AGREE_M = 300                       # POI this close to the geocode gets a bonus
CORROBORATE_M = 300                 # any Places result this close to the geocode supports the geocode
DISAGREE_CAP_M = 2_000              # POI this far from a verified precise geocode is capped
MAX_PLACES_CANDIDATES = 5

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
OUTPUT_COLUMNS = [
    "input_row", "soa_loan_no", "address_column", "original_address", "sanitized_address",
    "normalized_address",
    "address_length", "major_word_count", "pincode_list", "pincode_count",
    "address_completeness_status", "address_issue_category",
    "spelling_error_detected", "suspected_misspelled_words", "suggested_spelling_corrections",
    "synonym_normalization_applied", "normalized_terms",
    "processing_stage", "confidence_score", "match_classification", "score_category",
    "geocoding_api_called", "places_api_called",
    "latitude", "longitude", "geocode_status", "source_api", "derivation_type",
    "location_type", "geocode_location_type", "distance_between_apis_m",
    "name_match_ratio", "place_name", "place_types",
    "recommended_geofence_radius_m",
    "geocode_formatted_address", "address_verification", "flags", "review_needed",
    "remarks", "script_version",
]
ERROR_DERIVATION = "PROCESSING_ERROR"

# Flags that mean "a human should look at this row"
REVIEW_FLAGS = {"GEOCODE_UNVERIFIED", "PIN_MISMATCH", "PIN_MISMATCH_CORROBORATED",
                "POI_PIN_MISMATCH", "POI_GEOCODE_DISAGREE", "DOUBLE_PINCODE", "MULTIPLE_PINCODES",
                "CONFLICTING_PINCODES", "ADDRESS_TRUNCATED", "INCOMPLETE_ADDRESS",
                "MISSING_PINCODE", "TOO_FEW_WORDS", "ADDRESS_TOO_SHORT", "SPELLING_ERROR",
                "SPELLING_UNCERTAIN", ERROR_DERIVATION}


class ApiAbort(Exception):
    """The run must stop (bad key, quota exhausted, budget reached, repeated failures)."""


class TransientApiError(Exception):
    """An API call kept failing (network / rate limit); the row should be retried later."""


class CacheMiss(Exception):
    """--cache-only and the answer is not in the cache."""


class LockError(Exception):
    """Another run holds the output file."""


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def _normalize(text) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace."""
    t = unicodedata.normalize("NFKC", str(text)).lower().replace("_", " ")
    return " ".join(re.sub(r"[^\w\s]", " ", t).split())


_ABBREVIATIONS = {"dist": "district", "soc": "society", "govt": "government",
                  "hosp": "hospital", "apt": "apartment", "bldg": "building"}


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


def check_address_completeness(address: str, min_len: int = MIN_ADDRESS_CHARS, min_words: int = MIN_MAJOR_WORDS):
    """
    Incomplete address detection based strictly on 3 constraints:
      1. Total length of address >= min_len (default 15 characters)
      2. Total at least 3 major words (>= 3, i.e. < 3 fails)
      3. Pincode present and 6 numeric digits ([1-9]\\d{5})

    Deep categorization subcategories:
      - MISSING_PINCODE: only missing 6-digit pincode
      - TOO_FEW_WORDS: only too few major words (< 3)
      - ADDRESS_TOO_SHORT: only length below min_len
      - TOO_SHORT_AND_FEW_WORDS: both too short and too few words
      - MISSING_PIN_AND_FEW_WORDS: missing pincode and too few words
      - MISSING_PIN_AND_TOO_SHORT: missing pincode and too short
      - ALL_CRITERIA_FAILED: failed all 3 criteria

    Returns:
      (is_incomplete: bool, subcategory: str, fails: list[str], major_words: list[str], pins: set[str])
    """
    s = str(address or "").strip()
    length = len(s)
    major_words = get_major_words(s)
    pins = _pincodes(s)

    fails = []
    if length < min_len:
        fails.append("ADDRESS_TOO_SHORT")
    if len(major_words) < min_words:
        fails.append("TOO_FEW_WORDS")
    if not pins:
        fails.append("MISSING_PINCODE")

    if not fails:
        return False, "COMPLETE", [], major_words, pins

    fail_set = set(fails)
    if len(fails) == 3:
        subcat = "ALL_CRITERIA_FAILED"
    elif fail_set == {"ADDRESS_TOO_SHORT", "TOO_FEW_WORDS"}:
        subcat = "TOO_SHORT_AND_FEW_WORDS"
    elif fail_set == {"MISSING_PINCODE", "TOO_FEW_WORDS"}:
        subcat = "MISSING_PIN_AND_FEW_WORDS"
    elif fail_set == {"MISSING_PINCODE", "ADDRESS_TOO_SHORT"}:
        subcat = "MISSING_PIN_AND_TOO_SHORT"
    elif fails == ["MISSING_PINCODE"]:
        subcat = "MISSING_PINCODE"
    elif fails == ["TOO_FEW_WORDS"]:
        subcat = "TOO_FEW_WORDS"
    elif fails == ["ADDRESS_TOO_SHORT"]:
        subcat = "ADDRESS_TOO_SHORT"
    else:
        subcat = "_".join(fails)

    return True, subcat, fails, major_words, pins


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def geofence_for_score(score):
    """Lower confidence -> larger geofence radius (metres)."""
    if score is None or score <= 0:
        return None
    if score >= 100:
        return 50
    if score >= 90:
        return 100
    if score >= 40:
        return 250
    return 1000


# --------------------------------------------------------------------------
# Address cleaning
# --------------------------------------------------------------------------
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


def sanitize_address(addr):
    """Clean an address for geocoding. Returns (sanitized_or_None, flags)."""
    flags: list = []
    if addr is None or (isinstance(addr, float) and math.isnan(addr)):
        return None, flags
    s = unicodedata.normalize("NFKC", str(addr)[:MAX_ADDRESS_CHARS * 4])   # bound work on junk cells
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)

    # Existing lat/long text, coordinate pairs
    s = re.sub(r"\bLat(?:itude)?\b\s*[:=]?\s*-?[\d.]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bLong(?:itude)?\b\s*[:=]?\s*-?[\d.]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}", "", s)
    # Dates and times, "Retake Original" artifacts (whole words only)
    s = re.sub(r"\b\d{2}/\d{2}/(?:\d{4}|\d{2})\b", "", s)
    s = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "", s)
    s = re.sub(r"\bRetake\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bOriginal\b", "", s, flags=re.IGNORECASE)
    # Leading punctuation; a lone "0" only when it is a stray token ("012 Main Rd" survives)
    s = re.sub(r"^(?:[\s\-\.\,\#\+]|0(?=[\s,]))+", "", s)
    # A "NO" / "NO." with no number after it ("NO, MADAPATTANA") is a blank form field
    s = re.sub(r"\bNO\b\.?(?!\s*[-.]?\s*\d)", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    s = _dedupe_repeats(s)
    # "202001 - 202001" -> "202001"
    s = re.sub(r"\b(\d{6})\b(?:\s*-\s*\1\b)+", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")

    if len(_pincodes(s)) > 1:                 # two DIFFERENT pincodes: flagged, never "fixed"
        flags.append("DOUBLE_PINCODE")
        flags.append("CONFLICTING_PINCODES")
    if len(s) > MAX_ADDRESS_CHARS:
        s = s[:MAX_ADDRESS_CHARS].rsplit(" ", 1)[0]
        flags.append("ADDRESS_TRUNCATED")
    return (s if len(s) >= 5 else None), flags


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
# Words that say WHAT a place is (or are filler), not WHICH place it is.
POI_GENERIC_WORDS = {
    # religious
    "mandir", "mandar", "mandeer", "mandiir", "temple", "devasthan", "devalaya", "shivalaya",
    "thakurbari", "kshetra", "koil", "kovil", "dham", "peeth", "sthan", "asthan",
    "masjid", "majid", "masjeed", "mosque", "jama", "idgah", "mazar", "dargah",
    "church", "girja", "chapel", "gurudwara", "gurdwara", "gurudawara", "sahib",
    "math", "mutt", "samadhi", "ashram",
    # education / health
    "school", "vidyalaya", "vidhyalaya", "vidyamandir", "college", "collage", "mahavidyalaya",
    "university", "academy", "institute", "coaching", "pathshala", "shala", "anganwadi",
    "primary", "secondary", "public", "high",
    "hospital", "aspatal", "haspatal", "clinic", "dispensary", "nursing", "home", "medical",
    "pharmacy", "chemist", "phc", "chc", "centre", "center",
    # government / finance / shops / transport
    "bank", "atm", "branch", "post", "office", "police", "thana", "station", "panchayat", "gram",
    "tehsil", "tahsil", "court", "sachivalaya", "ration",
    "shop", "store", "dukan", "mart", "bazaar", "market", "hotel", "dhaba", "petrol", "pump",
    "showroom", "traders", "enterprises", "agency", "kirana", "godown", "bus", "stand", "stop",
    "railway", "chowk", "chauraha", "tiraha", "circle", "crossing", "naka", "gate",
    "cross", "junction", "square", "flyover", "bridge",
    # company suffixes / building words
    "ltd", "limited", "pvt", "private", "llp", "inc",
    "homes", "apartments", "towers", "complex", "villa", "villas", "heights",
    # titles / directions / filler
    "shri", "shree", "sri", "ji", "the", "of", "and", "ka", "ki", "ko", "ke", "se",
    "wale", "wali", "wala", "near", "nearby", "nr", "opp", "opposite", "behind", "beside",
    "front", "samne", "pass", "paas", "side", "back",
    # address structure
    "house", "plot", "flat", "floor", "no", "number", "gali", "lane", "road", "marg", "street",
    "colony", "vihar", "enclave", "society", "apartment", "tower", "sector", "block", "phase",
    "village", "vill", "ward", "dist", "district", "taluka", "tal",
}
# Junction-type words: "<Name> chauraha" is the junction of the area called <Name>, not a building
JUNCTION_WORDS = {"chowk", "chauraha", "tiraha", "circle", "crossing", "naka", "gate", "stand",
                  "stop", "bus", "railway", "station", "bazaar", "market", "cross", "junction",
                  "square", "flyover", "bridge"}
# Deity / saint names: real names, but nearly every village has several -> weaker evidence
COMMON_DEITY_WORDS = {
    "shiv", "shankar", "mahadev", "hanuman", "bajrang", "ram", "krishna", "ganesh", "balaji",
    "durga", "kali", "mata", "devi", "sai", "baba", "bhairav", "shani", "ambe", "ambaji",
}
# Google types meaning "an area or a road", not a specific landmark
AREA_ONLY_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "sublocality_level_3", "neighborhood", "political", "postal_code", "colloquial_area",
    "country", "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "administrative_area_level_4",
}
AREA_PLACE_TYPES = AREA_ONLY_TYPES | {"route"}
# Words that introduce a landmark BEFORE its name: "NEAR X", "OPP X"
LANDMARK_INTRO_WORDS = {"near", "nr", "nearby", "opp", "opposite", "behind", "bh", "beside",
                        "front", "infront", "samne", "pass", "paas", "adjacent", "adj", "next"}
# Hindi / Gujarati post-positions AFTER the name: "X ke samne", "X ni same", "X wali gali"
POSTPOSITION_WORDS = {"samne", "same", "paas", "pass", "pase", "baju", "bajuma", "bagal",
                      "wali", "wale", "wala"}
LANDMARK_WORDS = LANDMARK_INTRO_WORDS | POSTPOSITION_WORDS

# --------------------------------------------------------------------------
# Stage 4: Synonym & Entity Normalization Dictionary
# --------------------------------------------------------------------------
SYNONYM_DICT = {
    # Religious / Landmarks
    "mandir": "temple",
    "mandiram": "temple",
    "mandeer": "temple",
    "mandiir": "temple",
    "devasthan": "temple",
    "devalaya": "temple",
    "shivalaya": "temple",
    "thakurbari": "temple",
    "kovil": "temple",
    "koil": "temple",
    "masjid": "mosque",
    "majid": "mosque",
    "masjeed": "mosque",
    "girja": "church",
    "gurudwara": "gurdwara",
    "gurudawara": "gurdwara",
    # Road / Thoroughfare
    "rd": "road",
    "st": "street",
    "strt": "street",
    "ave": "avenue",
    "av": "avenue",
    "mrg": "marg",
    "ln": "lane",
    "blvd": "boulevard",
    "rasta": "road",
    "salai": "road",
    "sarani": "road",
    "byepass": "bypass",
    # Buildings / Housing
    "bldg": "building",
    "bldng": "building",
    "apt": "apartment",
    "apts": "apartments",
    "appartments": "apartments",
    "appartment": "apartment",
    "soc": "society",
    "cmplx": "complex",
    "cplx": "complex",
    "hsg": "housing",
    "clny": "colony",
    "col": "colony",
    "twr": "tower",
    "twrs": "towers",
    "ht": "heights",
    "hts": "heights",
    "encl": "enclave",
    "vil": "village",
    "vill": "village",
    # Institutions / Commerce
    "hosp": "hospital",
    "haspatal": "hospital",
    "aspatal": "hospital",
    "disp": "dispensary",
    "univ": "university",
    "coll": "college",
    "inst": "institute",
    "acad": "academy",
    "sch": "school",
    "vid": "vidyalaya",
    "vidy": "vidyalaya",
    "stn": "station",
    "rly": "railway",
    "po": "post office",
    "ps": "police station",
    "mkt": "market",
    "bazar": "bazaar",
    "shwrm": "showroom",
    "ltd": "limited",
    "pvt": "private",
    # Administrative
    "govt": "government",
    "dist": "district",
    "dt": "district",
    "tal": "taluka",
    "tk": "taluk",
    "teh": "tehsil",
    "sec": "sector",
    "blk": "block",
    "flr": "floor",
    # Orientations / Directions
    "opp": "opposite",
    "nr": "near",
    "adj": "adjacent",
    "bh": "behind",
}


def normalize_synonyms(address: str, synonym_dict: dict = SYNONYM_DICT):
    """
    Stage 4: Normalize synonymous and equivalent terms before geocoding.
    Returns:
        (normalized_address: str, was_normalized: bool, normalized_terms: list[str])
    """
    if not address:
        return "", False, []

    normalized_terms = []

    def replace_word(match):
        word = match.group(0)
        lower_word = word.lower()
        if lower_word in synonym_dict:
            canon = synonym_dict[lower_word]
            if canon.lower() != lower_word:
                if word.isupper():
                    rep = canon.upper()
                elif word[0].isupper():
                    rep = canon.capitalize()
                else:
                    rep = canon
                normalized_terms.append(f"{lower_word} -> {canon}")
                return rep
        return word

    norm_addr = re.sub(r"\b[A-Za-z]+(?:\.[A-Za-z]+)*\b", replace_word, str(address))
    unique_terms = list(dict.fromkeys(normalized_terms))
    was_normalized = len(unique_terms) > 0
    return norm_addr, was_normalized, unique_terms


# --------------------------------------------------------------------------
# Stage 3: Offline Geographic Names Dictionary & Spelling Error Detection
# --------------------------------------------------------------------------
KNOWN_GEO_NAMES = {
    # 28 States & 8 UTs (single words & component words)
    "andhra", "pradesh", "arunachal", "assam", "bihar", "chhattisgarh", "goa", "gujarat",
    "haryana", "himachal", "jharkhand", "karnataka", "kerala", "madhya", "maharashtra",
    "manipur", "meghalaya", "mizoram", "nagaland", "odisha", "punjab", "rajasthan",
    "sikkim", "tamil", "nadu", "telangana", "tripura", "uttar", "uttarakhand",
    "bengal", "delhi", "jammu", "kashmir", "ladakh", "andaman", "nicobar", "chandigarh",
    "dadra", "nagar", "haveli", "daman", "diu", "lakshadweep", "puducherry",
    # Top 150+ Indian Cities & Districts
    "mumbai", "bombay", "bengaluru", "bangalore", "hyderabad", "ahmedabad", "chennai", "madras",
    "kolkata", "calcutta", "surat", "pune", "poona", "jaipur", "lucknow", "kanpur", "nagpur",
    "indore", "thane", "bhopal", "visakhapatnam", "vizag", "patna", "vadodara", "baroda",
    "ghaziabad", "ludhiana", "agra", "nashik", "faridabad", "meerut", "rajkot", "kalyan",
    "dombivli", "vasai", "virar", "varanasi", "srinagar", "aurangabad", "dhanbad", "amritsar",
    "allahabad", "prayagraj", "howrah", "ranchi", "gwalior", "jabalpur", "coimbatore",
    "vijayawada", "jodhpur", "madurai", "raipur", "kota", "guwahati", "solapur", "hubli",
    "dharwad", "hubballi", "bareilly", "moradabad", "mysore", "mysuru", "gurgaon", "gurugram",
    "aligarh", "jalandhar", "tiruchirappalli", "trichy", "bhubaneswar", "salem", "bhayandar",
    "warangal", "thiruvananthapuram", "trivandrum", "bhiwandi", "saharanpur", "gorakhpur",
    "guntur", "bikaner", "amravati", "noida", "jamshedpur", "bhilai", "cuttack", "firozabad",
    "kochi", "cochin", "nellore", "bhavnagar", "dehradun", "durgapur", "asansol", "rourkela",
    "nanded", "kolhapur", "ajmer", "akola", "gulbarga", "kalaburagi", "jamnagar", "ujjain",
    "loni", "siliguri", "jhansi", "ulhasnagar", "sangli", "mangalore", "mangaluru", "erode",
    "belgaum", "belagavi", "kurnool", "ambattur", "tirunelveli", "malegaon", "gaya", "tiruppur",
    "davanagere", "kozhikode", "calicut", "mehsana", "kadi", "anand", "navsari", "vapi",
    "gandhinagar", "himatnagar", "palanpur", "porbandar", "bharuch", "valsad", "mathura",
    "vrindavan", "alwar", "bharatpur", "rohtak", "hisar", "panipat", "karnal", "sonipat",
    "ambala", "bathinda", "pathankot", "hoshiarpur", "shimla", "iglas", "hathras", "kasganj",
    "mainpuri", "shikohabad", "tundla", "fatehabad", "bulandshahr", "khurja", "sikandrabad",
    # Prominent Localities / Suburbs
    "andheri", "bandra", "borivali", "malad", "kandivali", "ghatkopar", "kurla", "chembur",
    "dadar", "colaba", "juhu", "powai", "mulund", "bhandup", "goregaon", "jogeshwari",
    "santacruz", "khar", "matunga", "sion", "wadala", "parel", "worli", "byculla", "mazgaon",
    "girgaon", "walkeshwar", "mahalaxmi", "chincholi", "bunder", "lokhandwala", "versova",
    "oshiwara", "marol", "dharavi", "mahim", "koramangala", "indiranagar", "jayanagar",
    "whitefield", "marathahalli", "hennur", "hebbal", "yelahanka", "banashankari",
    "rajajinagar", "malleshwaram", "basavanagudi", "ulsoor", "bellandur", "anekal", "jigani",
    "bommasandra", "vadapalani", "velachery", "mylapore", "thiruvanmiyur", "guindy",
    "tambaram", "chromepet", "porur", "alwarpet", "nungambakkam", "egmore", "royapettah",
    "kilpauk", "perambur", "kodambakkam", "kavarai", "madhapur", "gachibowli", "secunderabad",
    "kukatpally", "dilsukhnagar", "charminar", "begumpet", "ameerpet", "kondapur", "miyapur",
    "navrangpura", "satellite", "bodakdev", "vastrapur", "paldi", "maninagar", "naroda",
    "bapunagar", "chandkheda", "gota", "thaltej", "belanganj", "tinbatti",
    "umaria", "umariya", "sihora", "khitola",
    # Common Geographic Entity Words & Suffixes
    "road", "street", "marg", "rasta", "galli", "gali", "lane", "avenue", "drive", "highway",
    "expressway", "bypass", "chowk", "square", "circle", "crossing", "chauraha", "tiraha",
    "naka", "gate", "dwar", "nagar", "pur", "puri", "puram", "vihar", "enclave", "colony",
    "society", "layout", "apartment", "apartments", "residency", "complex", "arcade",
    "plaza", "tower", "towers", "heights", "estate", "park", "garden", "nagari", "palli",
    "halli", "gaon", "gram", "kheda", "taluk", "taluka", "tehsil", "district", "market",
    "bazaar", "bazar", "bunder", "point", "corner", "block", "sector", "phase",
}

VALID_ALTERNATIVES = {
    "bengaluru": {"bangalore"}, "bangalore": {"bengaluru"},
    "mumbai": {"bombay"}, "bombay": {"mumbai"},
    "kolkata": {"calcutta"}, "calcutta": {"kolkata"},
    "chennai": {"madras"}, "madras": {"chennai"},
    "vadodara": {"baroda"}, "baroda": {"vadodara"},
    "pune": {"poona"}, "poona": {"pune"},
    "kochi": {"cochin"}, "cochin": {"kochi"},
    "thiruvananthapuram": {"trivandrum"}, "trivandrum": {"thiruvananthapuram"},
    "kozhikode": {"calicut"}, "calicut": {"kozhikode"},
    "varanasi": {"banaras", "benares", "kashi"},
    "prayagraj": {"allahabad"}, "allahabad": {"prayagraj"},
    "gurugram": {"gurgaon"}, "gurgaon": {"gurugram"},
    "mysuru": {"mysore"}, "mysore": {"mysuru"},
    "belagavi": {"belgaum"}, "belgaum": {"belagavi"},
    "hubballi": {"hubli"}, "hubli": {"hubballi"},
    "kalaburagi": {"gulbarga"}, "gulbarga": {"kalaburagi"},
    "mangalore": {"mangaluru"}, "mangaluru": {"mangalore"},
    "puducherry": {"pondicherry"}, "pondicherry": {"puducherry"},
    "odisha": {"orissa"}, "orissa": {"odisha"},
    "chhatrapati sambhajinagar": {"aurangabad"}, "aurangabad": {"chhatrapati sambhajinagar"},
    "wadi": {"vadi"}, "vadi": {"wadi"},
    "pur": {"pore"}, "pore": {"pur"},
}

# Standard landmark, civic, commercial and infrastructure words for offline spelling validation
STANDARD_LANDMARK_WORDS = {
    # Religious / Spiritual
    "mandir", "temple", "masjid", "mosque", "church", "gurudwara", "dargah", "ashram",
    "devasthan", "devalaya", "shivalaya", "math", "mutt", "dham", "peeth", "sthan", "chapel",
    "derasar", "bhavan", "bhawan",
    # Education
    "school", "college", "university", "vidyalaya", "academy", "institute", "pathshala",
    "anganwadi", "coaching", "hostel", "campus",
    # Health & Medical
    "hospital", "clinic", "dispensary", "pharmacy", "chemist", "nursing", "medical",
    "centre", "center", "laboratory", "lab",
    # Government / Civic / Finance
    "bank", "office", "police", "thana", "station", "panchayat", "court", "tehsil",
    "sachivalaya", "post", "corporation", "samiti", "collectorate",
    # Commercial & Services
    "basti", "bazaar", "market", "shop", "store", "dukan", "mart", "hotel", "dhaba",
    "petrol", "pump", "showroom", "godown", "agency", "traders", "enterprises",
    # Housing & Layout
    "apartment", "apartments", "society", "tower", "towers", "heights", "complex",
    "villa", "villas", "residency", "enclave", "layout", "colony", "estate", "house",
    "plot", "floor", "flat",
    # Infrastructure & Transit
    "road", "street", "marg", "lane", "gali", "chowk", "chauraha", "tiraha", "circle",
    "crossing", "naka", "gate", "stand", "stop", "railway", "junction", "bridge",
    "flyover", "village", "ward", "block", "sector", "phase", "nagar", "vihar",
    "puram", "puri", "palli", "halli", "kheda", "gaon",
}

ALL_KNOWN_SPELLING_WORDS = (
    KNOWN_GEO_NAMES
    | STANDARD_LANDMARK_WORDS
    | COMMON_DEITY_WORDS
)


def detect_spelling_errors(address: str, known_names: set = ALL_KNOWN_SPELLING_WORDS,
                           error_threshold: float = SPELLING_ERROR_THRESHOLD,
                           uncertain_min: float = SPELLING_UNCERTAIN_MIN):
    """
    Stage 3: Offline spelling error detection on geographic and landmark words.
    Returns:
        (has_error: bool, suspected_words: list[str], suggestions: list[str],
         is_uncertain: bool, confidence_score: int)
    """
    major_words = get_major_words(address)
    suspected_errors = []
    suggested_corrections = []
    uncertain_words = []
    uncertain_suggestions = []
    max_sim_found = 0.0

    for word in major_words:
        w_low = word.lower()
        if len(w_low) < 4:
            continue
        # Exact match to known names or common vocabulary
        if (w_low in known_names or w_low in POI_GENERIC_WORDS or
            w_low in SYNONYM_DICT or w_low in COMMON_DEITY_WORDS):
            continue
        # Recognized valid alternate transliteration / historical name
        is_alt = False
        for canon, alts in VALID_ALTERNATIVES.items():
            if w_low == canon or w_low in alts:
                is_alt = True
                break
        if is_alt:
            continue

        # Search for close matches in known names
        best_k = None
        best_sim = 0.0
        for k in known_names:
            if abs(len(w_low) - len(k)) > 2:
                continue
            s = _sim(w_low, k)
            if s > best_sim:
                best_sim = s
                best_k = k
            elif s == best_sim and best_k is not None:
                # Prefer exact character set transposition (anagram match)
                if set(w_low) == set(k) and set(w_low) != set(best_k):
                    best_k = k

        if best_sim >= error_threshold and best_k != w_low:
            suspected_errors.append(word)
            suggested_corrections.append(best_k)
            if best_sim > max_sim_found:
                max_sim_found = best_sim
        elif best_sim >= uncertain_min and best_k != w_low:
            uncertain_words.append(word)
            uncertain_suggestions.append(best_k)

    if suspected_errors:
        # Scale confidence score in 21-30 band based on similarity
        span = SCORE_SPELLING_MAX - SCORE_SPELLING_MIN
        frac = min(1.0, max(0.0, (max_sim_found - error_threshold) / (1.0 - error_threshold)))
        score = SCORE_SPELLING_MIN + int(frac * span)
        return True, suspected_errors, suggested_corrections, False, score

    if uncertain_words:
        return False, uncertain_words, uncertain_suggestions, True, SCORE_SPELLING_MIN

    return False, [], [], False, 0


# --------------------------------------------------------------------------
# Persistent API cache (SQLite)
# --------------------------------------------------------------------------
class DiskCache:
    """Stores the trimmed raw API answer per request, so scoring changes never need new calls."""

    def __init__(self, path, max_age_days=DEFAULT_CACHE_MAX_AGE_DAYS):
        self.max_age_days = max_age_days if (max_age_days and max_age_days > 0) else None
        self.conn = sqlite3.connect(path, timeout=60)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=60000")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            "key TEXT PRIMARY KEY, response TEXT NOT NULL, saved_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.conn.commit()

    def get(self, key):
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
            return None                      # corrupt row -> treat as a miss

    def set(self, key, data):
        self.conn.execute("INSERT OR REPLACE INTO api_cache (key, response) VALUES (?, ?)",
                          (key, json.dumps(data)))
        self.conn.commit()

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------
# Google API client
# --------------------------------------------------------------------------
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
                 session=None, sleep=time.sleep):
        self.api_key = api_key
        self.cache = cache
        self.region = region
        self.cache_only = cache_only
        self.max_api_calls = max_api_calls
        self.session = session or requests.Session()
        self._sleep = sleep
        self.stats = {"api_calls": 0, "cache_hits": 0}

    # -- public -----------------------------------------------------------
    def geocode(self, address: str) -> dict:
        norm = _normalize(address)
        key = f"geo3|{self.region}|{norm}"
        legacy = f"geo2|{self.region}||{norm}"         # entries written by the v1 script
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
        if not bias or params.get("radius") == 5000:   # v1 only ever used a 5 km radius
            legacy = [f"places3|{self.region}|{loc}|{norm}"]
        return self._get(PLACES_TEXT_SEARCH_URL, params, key, legacy, _trim_places)

    # -- internals ---------------------------------------------------------
    def _redact(self, text) -> str:
        text = str(text)
        if self.api_key:
            text = text.replace(self.api_key, "REDACTED")
        return re.sub(r"(key=)[^&\s'\"]+", r"\1REDACTED", text)

    def _backoff(self, attempt, extra=0.0):
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** attempt)) + extra
        self._sleep(delay + random.uniform(0, 0.5))

    def _get(self, url, params, key, legacy_keys, trim):
        if self.cache is not None:
            for k in [key] + list(legacy_keys):
                cached = self.cache.get(k)
                if cached is not None:
                    self.stats["cache_hits"] += 1
                    return cached
        if self.cache_only:
            raise CacheMiss(key)
        if not self.api_key:
            raise ApiAbort(f"{API_KEY_ENV} is not set")

        full = dict(params, key=self.api_key)
        last_error = "unknown error"
        for attempt in range(MAX_RETRIES):
            if self.max_api_calls is not None and self.stats["api_calls"] >= self.max_api_calls:
                raise ApiAbort(f"--max-api-calls budget of {self.max_api_calls} reached")
            self.stats["api_calls"] += 1
            try:
                resp = self.session.get(url, params=full, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
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
            if code != 200:                              # other 4xx: this request is bad, retrying won't help
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


# --------------------------------------------------------------------------
# Address context and geocode analysis
# --------------------------------------------------------------------------
class AddrCtx:
    """Pre-computed views of one address, shared by every comparison."""

    def __init__(self, address: str):
        self.text = _expand(address)
        self.tokens = self.text.split()
        self.pins = _pincodes(address)
        self.locality_words = [t for t in self.tokens
                               if len(t) >= 4 and not t.isdigit()
                               and t not in POI_GENERIC_WORDS and t not in LANDMARK_WORDS]
        self.meaningful = [t for t in self.tokens
                           if len(t) >= 3 and any(c.isalpha() for c in t)
                           and t not in POI_GENERIC_WORDS and t not in LANDMARK_WORDS]
        self.area_words: set = set()      # filled from an area-level geocode (see derive)


def _locality_overlap(ctx: AddrCtx, target_tokens, exclude_words=()):
    """True/False: do the address's locality words appear in `target_tokens`? None if nothing to compare."""
    words = [w for w in ctx.locality_words if not _fuzzy_in(w, exclude_words, 0.8)]
    if not words or not target_tokens:
        return None
    return any(_fuzzy_in(w, target_tokens) for w in words)


def geocode_lookup(client, address: str) -> dict:
    out = {"found": False, "lat": None, "lng": None, "status": None, "loc_type": None,
           "partial": False, "formatted_address": "", "types": set(), "pins": set(),
           "area_tokens": set()}
    data = client.geocode(address)
    out["status"] = data.get("status")
    results = data.get("results") or []
    if out["status"] == "OK" and results:
        r = results[0]
        loc = r["geometry"]["location"]
        fa = r.get("formatted_address", "")
        comps = r.get("components") or []
        pins = {c["long_name"] for c in comps if "postal_code" in c.get("types", [])
                and re.fullmatch(r"[1-9]\d{5}", c.get("long_name", ""))} or _pincodes(fa)
        area_types = {"locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
                      "neighborhood", "administrative_area_level_1", "administrative_area_level_2",
                      "administrative_area_level_3", "postal_code", "country"}
        if comps:
            area_tokens = set()
            for c in comps:
                if area_types & set(c.get("types", [])):
                    area_tokens.update(_normalize(c.get("long_name", "")).split())
        else:                                    # entries cached by the v1 script have no components
            area_tokens = set(_normalize(" ".join(fa.split(",")[-3:])).split())
        out.update(found=True, lat=loc["lat"], lng=loc["lng"],
                   loc_type=r["geometry"].get("location_type", "UNKNOWN"),
                   partial=bool(r.get("partial_match", False)), formatted_address=fa,
                   types=set(r.get("types") or []), pins=set(pins), area_tokens=area_tokens)
    return out


def geocode_score(g: dict):
    """(score, category) from the geocode alone, before verification."""
    if not g["found"]:
        return 0, "NOT_FOUND"
    lt, types = g["loc_type"], g["types"]
    if lt == "ROOFTOP":
        return (SCORE_PARTIAL, "PARTIAL_MATCH") if g["partial"] else (SCORE_ROOFTOP, "ROOFTOP_EXACT")
    if lt == "RANGE_INTERPOLATED":
        return SCORE_INTERPOLATED, "INTERPOLATED"
    if "route" in types and not (types & {"street_address", "premise", "subpremise",
                                          "establishment", "point_of_interest"}):
        return SCORE_STREET, "STREET_LEVEL"
    return SCORE_AREA_LEVEL, "AREA_LEVEL"        # locality centre / area-level / unknown


def verify_geocode(ctx: AddrCtx, g: dict) -> dict:
    """
    Does Google's own address for the point agree with the input?
    status: MATCH | PIN_MISMATCH | LOCALITY_MISMATCH | NO_DATA
    """
    if not g["found"] or not g["formatted_address"]:
        return {"status": "NO_DATA", "pin_match": None, "locality_match": None}
    pin_match = None if (not ctx.pins or not g["pins"]) else bool(ctx.pins & g["pins"])
    target = _normalize(g["formatted_address"]).split()
    loc = _locality_overlap(ctx, target)
    hits = sum(1 for w in ctx.locality_words if _fuzzy_in(w, target))
    if pin_match is True:
        status = "MATCH"
    elif loc is False:
        status = "LOCALITY_MISMATCH"
    elif pin_match is False:
        status = "PIN_MISMATCH"
    elif pin_match is None and loc is None:
        status = "NO_DATA"
    else:
        status = "MATCH"
    return {"status": status, "pin_match": pin_match, "locality_match": loc, "locality_hits": hits}


# --------------------------------------------------------------------------
# Places candidates
# --------------------------------------------------------------------------
class PlaceEval:
    """Verdict on one Places result (plain class: no import-time magic, works however it is loaded)."""

    def __init__(self, score, category, ratio=None, role=None, reason="", pin_match=None,
                 usable_area=False, flags=None, typos=None):
        self.typos = list(typos or [])  # [(word as written in the address, word in the place name)]
        self.score = score
        self.category = category
        self.ratio = ratio
        self.role = role                # PREMISE (the address itself) | LANDMARK (address is near it)
        self.reason = reason
        self.pin_match = pin_match
        self.usable_area = usable_area  # area-level hit in the right locality (coarse but valid)
        self.flags = list(flags or [])


def _landmark_role(tokens, first: int, last: int) -> str:
    if first < 0:
        return "PREMISE"
    before = tokens[max(0, first - 3):first]
    after = tokens[last + 1:last + 4]
    if any(t in LANDMARK_INTRO_WORDS for t in before) or any(t in POSTPOSITION_WORDS for t in after):
        return "LANDMARK"
    return "PREMISE"


def evaluate_place(place: dict, ctx: AddrCtx) -> PlaceEval:
    """
    Judge one Places result against the address, ignoring the geocode (see derive for that).
      - area / road results and generic-only names are never a landmark
      - a name that is just an area plus a junction word ("Belanganj chauraha") must be introduced
        as a landmark in the address, otherwise it is the junction of the locality
      - the locality words (or the pincode) must agree with the place's own address
      - a place the address says it is "near" scores lower than a place that IS the address
    """
    name = (place.get("name") or "").strip()
    types = set(place.get("types") or [])
    place_fa = place.get("formatted_address") or ""
    place_tokens = _normalize(place_fa).split()
    place_pins = _pincodes(place_fa)
    pin_match = None if (not ctx.pins or not place_pins) else bool(ctx.pins & place_pins)
    name_words = _expand(name).split()

    def locality_ok(exclude):
        if pin_match is True:
            return True
        return _locality_overlap(ctx, place_tokens, exclude) is not False

    def reject(reason, ratio=0.0):
        return PlaceEval(SCORE_INCOMPLETE, "INCOMPLETE_ADDRESS", ratio, reason=reason, pin_match=pin_match)

    if types & AREA_PLACE_TYPES:
        ok = locality_ok(name_words)
        ev = reject("AREA_TYPE" if ok else "WRONG_LOCALITY")
        ev.usable_area = ok
        return ev

    # Words of 1-2 letters are initials or filler ("T. Hosahalli", "At, post", "PO"), never evidence
    evidence = [w for w in name_words
                if len(w) >= 3 and w not in POI_GENERIC_WORDS and w not in ctx.area_words]
    if not evidence:
        return reject("GENERIC_OR_AREA_NAME")
    extras = [w for w in name_words if w not in evidence and len(w) >= 3]
    bare = len(evidence) == 1 and all(w in JUNCTION_WORDS or w in ctx.area_words for w in extras)

    sims, idxs, typos = [], [], []
    for t in evidence:
        best_s, best_i = 0.0, -1
        for i, a in enumerate(ctx.tokens):
            s = _sim(t, a)
            if s > best_s:
                best_s, best_i = s, i
        sims.append(best_s)
        if best_s >= MIN_TOKEN_SIMILARITY:
            idxs.append(best_i)
            if best_s < 1.0:
                typos.append((ctx.tokens[best_i], t))
    exact = all(s == 1.0 for s in sims)
    ratio = sum(sims) / len(sims)
    worst = min(sims)
    role = _landmark_role(ctx.tokens, min(idxs), max(idxs)) if idxs else "PREMISE"

    needed = SINGLE_WORD_SPELLING_MIN if len(evidence) == 1 else SPELLING_SIMILARITY_MIN
    if not exact and not (ratio >= needed and worst >= MIN_TOKEN_SIMILARITY):
        return reject("NAME_MISMATCH", round(ratio, 2))
    if bare and role != "LANDMARK":
        return reject("BARE_NAME", round(ratio, 2))
    if not locality_ok(name_words):
        return reject("WRONG_LOCALITY", round(ratio, 2))

    flags = []
    if exact:
        if role == "PREMISE":
            score, cat = SCORE_POI_PREMISE_BASE, "POI_EXACT"
        else:
            score, cat = SCORE_LANDMARK, "LANDMARK_EXACT"
        ratio = 1.0
    else:
        cat = "SPELLING_MISTAKE"
        if role == "PREMISE":
            span = SCORE_SPELLING_MAX - SCORE_SPELLING_MIN
            frac = (ratio - SPELLING_SIMILARITY_MIN) / (1 - SPELLING_SIMILARITY_MIN)
            score = SCORE_SPELLING_MIN + round(max(0.0, min(1.0, frac)) * span)
        else:
            score = SCORE_LANDMARK_FUZZY
    if pin_match is False and score > SCORE_LANDMARK:
        score = SCORE_LANDMARK
        flags.append("POI_PIN_MISMATCH")
    return PlaceEval(score, cat, round(ratio, 2), role, "", pin_match, False, flags,
                     typos if cat == "SPELLING_MISTAKE" else [])


# --------------------------------------------------------------------------
# Combine all stages
# --------------------------------------------------------------------------
def _blank_result(**kw) -> dict:
    res = {
        "lat": None, "lng": None, "status": None, "source_api": "NONE", "derivation_type": "",
        "category": "", "match_ratio": None, "location_type": None, "geocode_location_type": None,
        "distance_m": None, "confidence_score": 0, "recommended_geofence_m": None,
        "place_name": None, "place_types": None, "formatted_address": "",
        "verification": "", "flags": [],
        # Stage & Validation fields
        "processing_stage": "",
        "match_classification": "",
        "address_completeness_status": "COMPLETE",
        "address_issue_category": "NONE",
        "original_address": "",
        "address_length": 0,
        "major_word_count": 0,
        "pincode_list": "",
        "pincode_count": 0,
        "spelling_error_detected": False,
        "suspected_misspelled_words": "",
        "suggested_spelling_corrections": "",
        "normalized_address": "",
        "synonym_normalization_applied": False,
        "normalized_terms": "",
        "geocoding_api_called": False,
        "places_api_called": False,
        # inputs for the remarks column
        "typos": [], "variants": [], "place_reject": None, "underlying": None, "pins": [],
    }
    res.update(kw)
    return res


def spelling_variants(ctx: AddrCtx, formatted_address: str, limit: int = 3):
    """
    Address words that are NOT in Google's own address but are a near match for one of its words
    ("othavadai" vs "oththavadai"). Remark only: Google already resolved the place, so no score change.
    """
    if not formatted_address:
        return []
    ftoks = set(_normalize(formatted_address).split())
    out = []
    for w in ctx.locality_words:
        if len(w) < 5 or w in ftoks:
            continue
        best, best_s = None, 0.0
        for t in ftoks:
            s = _sim(w, t)
            if s > best_s:
                best, best_s = t, s
        if best and 0.8 <= best_s < 1.0 and best not in ctx.tokens:
            out.append((w, best))
    return out[:limit]


def derive_coordinates_with_confidence(address: str, client) -> dict:
    """
    6-stage cost-optimized address validation, classification & geocoding pipeline:
      Stage 1: Incomplete Address Detection (score 0-9, NO API calls)
      Stage 2: Duplicate / Conflicting PIN Code Detection (score 11-20, NO API calls)
      Stage 3: Offline Spelling Mistake Detection (score 21-30, NO API calls)
      Stage 4: Synonym & Entity Normalization (score 60-70 preprocessing, CONTINUE to API)
      Stage 5: Google Geocoding & Places Fallback (POI exact 90-99, Landmark 70-80)
      Stage 6: Strict Exact Match (ROOFTOP + partial_match=False + verified -> 100)
    """
    return _derive_core(address, client)


def _derive_core(address: str, client) -> dict:
    """Execute the 6-stage sequential validation, normalization & geocoding pipeline."""
    # Stage 1: Incomplete Address Detection (score 0-9, NO API calls)
    is_inc, subcat, fails, major_words, pins = check_address_completeness(address)
    pin_list_str = ", ".join(sorted(pins))
    if is_inc:
        flags = ["INCOMPLETE_ADDRESS"] + [f for f in fails if f != "INCOMPLETE_ADDRESS"]
        inc_score = INCOMPLETE_SUBCAT_SCORES.get(subcat, 5)
        res = _blank_result(
            status="INCOMPLETE_ADDRESS",
            source_api="VALIDATION_RULE",
            derivation_type=f"INCOMPLETE_ADDRESS ({subcat})",
            category="INCOMPLETE_ADDRESS",
            match_classification="INCOMPLETE_ADDRESS",
            processing_stage="STAGE_1_INCOMPLETE_DETECTION",
            confidence_score=inc_score,
            recommended_geofence_m=geofence_for_score(inc_score),
            flags=flags,
            verification="INCOMPLETE",
            pins=sorted(pins),
            pincode_list=pin_list_str,
            pincode_count=len(pins),
            address_length=len(address),
            major_word_count=len(major_words),
            address_completeness_status="INCOMPLETE",
            address_issue_category=subcat,
            original_address=address,
            normalized_address=address,
            geocoding_api_called=False,
            places_api_called=False,
        )
        res["completeness_details"] = {
            "subcat": subcat,
            "fails": fails,
            "length": len(address),
            "major_words": major_words,
            "pins": list(pins),
        }
        return res

    # Stage 2: Duplicate or Conflicting PIN Code Detection (score 11-20, NO API calls)
    distinct_pins = sorted(_pincodes(address))
    if len(distinct_pins) > 1:
        pin_cat = "DOUBLE_PINCODE" if len(distinct_pins) == 2 else "MULTIPLE_PINCODES"
        pin_score = 15 if len(distinct_pins) == 2 else 12
        flags = ["DOUBLE_PINCODE", "CONFLICTING_PINCODES"]
        res = _blank_result(
            status="CONFLICTING_PINCODES",
            source_api="VALIDATION_RULE",
            derivation_type=f"{pin_cat} ({', '.join(distinct_pins)})",
            category="DOUBLE_PINCODE",
            match_classification=pin_cat,
            processing_stage="STAGE_2_PINCODE_VALIDATION",
            confidence_score=pin_score,
            recommended_geofence_m=geofence_for_score(pin_score),
            flags=flags,
            verification="CONFLICTING_PINCODES",
            pins=distinct_pins,
            pincode_list=", ".join(distinct_pins),
            pincode_count=len(distinct_pins),
            address_length=len(address),
            major_word_count=len(major_words),
            address_completeness_status="COMPLETE",
            address_issue_category=pin_cat,
            original_address=address,
            normalized_address=address,
            geocoding_api_called=False,
            places_api_called=False,
        )
        return res

    # Stage 3: Offline Spelling Mistake Detection (score 21-30, NO API calls)
    has_spelling_err, suspected, suggestions, is_uncertain, spell_score = detect_spelling_errors(address)
    if has_spelling_err or (is_uncertain and SPELLING_UNCERTAIN_POLICY == "STOP"):
        spell_cat = "SPELLING_ERROR" if has_spelling_err else "SPELLING_UNCERTAIN"
        flags = [spell_cat]
        res = _blank_result(
            status=spell_cat,
            source_api="VALIDATION_RULE",
            derivation_type=f"{spell_cat} ({', '.join(suspected)})",
            category="SPELLING_MISTAKE",
            match_classification=spell_cat,
            processing_stage="STAGE_3_SPELLING_VALIDATION",
            confidence_score=spell_score,
            recommended_geofence_m=geofence_for_score(spell_score),
            flags=flags,
            verification=spell_cat,
            pins=distinct_pins,
            pincode_list=", ".join(distinct_pins),
            pincode_count=len(distinct_pins),
            address_length=len(address),
            major_word_count=len(major_words),
            address_completeness_status="COMPLETE",
            address_issue_category=spell_cat,
            spelling_error_detected=True,
            suspected_misspelled_words=", ".join(suspected),
            suggested_spelling_corrections=", ".join(suggestions),
            original_address=address,
            normalized_address=address,
            geocoding_api_called=False,
            places_api_called=False,
        )
        return res

    # Stage 4: Synonym & Entity Normalization (score 60-70 preprocessing, CONTINUE to API)
    norm_address, was_normalized, norm_terms = normalize_synonyms(address)
    synonym_terms_str = ", ".join(norm_terms) if norm_terms else ""

    base_meta = {
        "original_address": address,
        "pins": distinct_pins,
        "pincode_list": ", ".join(distinct_pins),
        "pincode_count": len(distinct_pins),
        "address_length": len(address),
        "major_word_count": len(major_words),
        "address_completeness_status": "COMPLETE",
        "address_issue_category": "NONE",
        "normalized_address": norm_address,
        "synonym_normalization_applied": was_normalized,
        "normalized_terms": synonym_terms_str,
        "spelling_error_detected": is_uncertain,
        "suspected_misspelled_words": ", ".join(suspected) if is_uncertain else "",
        "suggested_spelling_corrections": ", ".join(suggestions) if is_uncertain else "",
        "geocoding_api_called": True,
        "places_api_called": False,
    }

    # Stages 5 & 6: Proceed with Google Geocoding API using normalized address
    ctx = AddrCtx(norm_address)
    g = geocode_lookup(client, norm_address)
    ver = verify_geocode(ctx, g)
    g_score, g_cat = geocode_score(g)
    flags: list = []
    if is_uncertain:
        flags.append("SPELLING_UNCERTAIN")

    # Fast path: Stage 6 - Strict Exact Match (ROOFTOP + partial_match=False + verified -> 100)
    if g_cat == "ROOFTOP_EXACT" and not g.get("partial") and (ver["status"] in ("MATCH", "NO_DATA") or
                                     (ver["status"] == "PIN_MISMATCH" and ver.get("locality_hits", 0) >= 2)):
        if ver["status"] == "PIN_MISMATCH":
            flags.append("PIN_MISMATCH")
        res = _blank_result(
            lat=g["lat"], lng=g["lng"], status="EXACT_MATCH", source_api="GEOCODING_API",
            derivation_type="ROOFTOP_EXACT", category="ROOFTOP_EXACT",
            match_classification="EXACT_MATCH",
            processing_stage="STAGE_6_EXACT_MATCH",
            location_type=g["loc_type"], geocode_location_type=g["loc_type"],
            confidence_score=SCORE_ROOFTOP, recommended_geofence_m=geofence_for_score(SCORE_ROOFTOP),
            formatted_address=g["formatted_address"], verification=ver["status"], flags=flags,
            **base_meta)
        return res

    # Places search, biased toward the geocode when it can be believed (Stage 5 fallback)
    lt = g["loc_type"]
    bias = (g["lat"], g["lng"]) if (g["found"] and ver["status"] in ("MATCH", "NO_DATA")) else None
    base_meta["places_api_called"] = True
    p = client.places(norm_address, bias, BIAS_RADIUS_M.get(lt, BIAS_DEFAULT_M) if bias else None)
    results = (p.get("results") or [])[:MAX_PLACES_CANDIDATES]

    corroborated = False
    if g["found"]:
        corroborated = any(haversine_m(g["lat"], g["lng"], r["geometry"]["location"]["lat"],
                                       r["geometry"]["location"]["lng"]) <= CORROBORATE_M
                           for r in results)
    hard_fail = g["found"] and (ver["status"] == "LOCALITY_MISMATCH" or
                                (ver["status"] == "PIN_MISMATCH" and not corroborated))
    unverified = False
    if hard_fail:
        flags.append("GEOCODE_UNVERIFIED")
        if g_score > SCORE_AREA_LEVEL:
            g_score, g_cat, unverified = SCORE_AREA_LEVEL, "AREA_LEVEL", True
    elif g["found"] and ver["status"] == "PIN_MISMATCH":
        flags.append("PIN_MISMATCH_CORROBORATED")
    trusted = g["found"] and not hard_fail

    if g["found"] and lt not in PRECISE_GEOCODE_TYPES:
        ctx.area_words = set(g["area_tokens"])        # area-level geocode: its words name the area, not a place

    # Judge every candidate on its own, then rank
    cands = []
    for i, r in enumerate(results):
        ev = evaluate_place(r, ctx)
        score, cat, cflags = ev.score, ev.category, list(ev.flags)
        d = None
        if g["found"]:
            loc = r["geometry"]["location"]
            d = haversine_m(g["lat"], g["lng"], loc["lat"], loc["lng"])
        if cat in ("POI_EXACT", "LANDMARK_EXACT"):
            name_norm = _normalize(r.get("name", ""))
            if (cat == "POI_EXACT" and not set(_expand(r.get("name", "")).split()) <= (COMMON_DEITY_WORDS | POI_GENERIC_WORDS)
                    and sum(1 for x in results if _normalize(x.get("name", "")) == name_norm) == 1):
                score += 5
        if trusted and d is not None and cat != "INCOMPLETE_ADDRESS":
            if d > FAR_LIMIT_M.get(lt, FAR_DEFAULT_M):
                score, cat, ev.reason = SCORE_AREA_LEVEL, "INCOMPLETE_ADDRESS", "FAR_FROM_GEOCODE"
            elif lt in PRECISE_GEOCODE_TYPES and ver["status"] == "MATCH" \
                    and d > DISAGREE_CAP_M and score > SCORE_PARTIAL:
                score = SCORE_PARTIAL
                cflags.append("POI_GEOCODE_DISAGREE")
            elif d <= AGREE_M and cat == "POI_EXACT":
                score += 4
            elif d <= AGREE_M and cat == "LANDMARK_EXACT":
                score += 5
        if cat == "POI_EXACT":
            score = min(SCORE_POI_MAX, score)
        elif cat == "LANDMARK_EXACT":
            score = min(SCORE_LANDMARK_MAX, score)
        cands.append({"score": score, "idx": i, "place": r, "ev": ev, "cat": cat, "d": d, "flags": cflags})

    best = max(cands, key=lambda c: (c["score"], -c["idx"])) if cands else None

    res = None
    if g["found"]:
        if best and best["score"] > g_score:
            res = _poi_result(best, g, ver)
            res.update(base_meta)
            res["processing_stage"] = "STAGE_5_POI_MATCH" if best["cat"] == "POI_EXACT" else "STAGE_POI_LANDMARK"
            res["match_classification"] = best["cat"]
        else:
            stage = "STAGE_GEOCODING_PARTIAL" if g_cat == "PARTIAL_MATCH" else (
                    "STAGE_GEOCODING_INTERPOLATED" if g_cat == "INTERPOLATED" else (
                    "STAGE_GEOCODING_STREET" if g_cat == "STREET_LEVEL" else "STAGE_GEOCODING_AREA"))
            match_cls = g_cat
            res = _blank_result(
                lat=g["lat"], lng=g["lng"], status=g["status"], source_api="GEOCODING_API_FALLBACK",
                derivation_type=f"PARTIAL_{lt}" + ("_UNVERIFIED" if unverified else ""),
                category=g_cat, match_classification=match_cls, processing_stage=stage,
                location_type=lt, geocode_location_type=lt,
                distance_m=round(best["d"]) if best and best["d"] is not None else None,
                confidence_score=g_score, **base_meta)
    elif best and (best["score"] > SCORE_AREA_LEVEL or best["ev"].usable_area):
        res = _poi_result(best, g, ver)               # geocode failed; a trustworthy POI still helps
        res.update(base_meta)
        res["processing_stage"] = "STAGE_5_POI_MATCH" if best["cat"] == "POI_EXACT" else "STAGE_POI_LANDMARK"
        res["match_classification"] = best["cat"]
    else:
        status = g["status"] or (p.get("status") if p else None) or "NOT_FOUND"
        res = _blank_result(status=status, derivation_type="NOT_FOUND", category="NOT_FOUND",
                            match_classification="NOT_FOUND", processing_stage="STAGE_NOT_FOUND",
                            flags=flags, verification=ver["status"], **base_meta)
        return res

    if best:
        res["place_name"] = (best["place"].get("name") or "").strip() or None
        res["place_types"] = "|".join(best["place"].get("types") or []) or None
        flags.extend(f for f in best["flags"] if f not in flags)
        if best["cat"] == "SPELLING_MISTAKE":
            res["typos"] = best["ev"].typos            # noted in remarks even if the geocode won
        elif best["cat"] == "INCOMPLETE_ADDRESS":
            res["place_reject"] = best["ev"].reason or None
    res["variants"] = spelling_variants(ctx, g["formatted_address"]) if g["found"] else []
    res["formatted_address"] = g["formatted_address"]
    res["verification"] = ver["status"]
    res["flags"] = flags

    # Too few place words to pin anything down
    if len(ctx.meaningful) < MIN_MEANINGFUL_WORDS and res["confidence_score"] < 90:
        res["confidence_score"] = min(res["confidence_score"], SCORE_INCOMPLETE)
        res["category"] = "INCOMPLETE_ADDRESS"
        res["flags"].append("TOO_FEW_WORDS")

    res["recommended_geofence_m"] = geofence_for_score(res["confidence_score"])
    return res


def _poi_result(best: dict, g: dict, ver: dict) -> dict:
    place, ev = best["place"], best["ev"]
    loc = place["geometry"]["location"]
    name = (place.get("name") or "").strip()
    kind = {"LANDMARK_EXACT": "LANDMARK_PLACE_MATCH", "INCOMPLETE_ADDRESS": "AREA_PLACE_MATCH"}.get(
        best["cat"], "POI_PLACE_MATCH")
    return _blank_result(
        lat=loc["lat"], lng=loc["lng"], status="OK (PLACES_API_MATCH)", source_api="PLACES_API",
        derivation_type=f"{kind} ({name})" if name else kind, category=best["cat"],
        match_ratio=ev.ratio, location_type="POI", geocode_location_type=g["loc_type"],
        distance_m=round(best["d"]) if best["d"] is not None else None,
        confidence_score=best["score"])


# --------------------------------------------------------------------------
# Output file handling
# --------------------------------------------------------------------------
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


def repair_truncated_tail(path) -> bool:
    """If the last line was only half-written (crash), cut it off. Returns True if repaired."""
    with open(path, "rb") as f:
        data = f.read()
    if not data or data.endswith(b"\n"):
        return False
    cut = data.rfind(b"\n")
    if cut == -1:
        return False
    with open(path, "r+b") as f:
        f.truncate(cut + 1)
    return True


def read_output(path):
    """(header, rows) of an existing output file."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = list(reader)
    return header, rows


def write_header(path):
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


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, float):
        return repr(round(v, 7)) if not float(v).is_integer() else str(int(v))
    return v


def append_rows(path, rows):
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore", restval="")
        w.writerows([{k: _cell(v) for k, v in r.items()} for r in rows])
        f.flush()
        os.fsync(f.fileno())


_REJECT_TEXT = {
    "AREA_TYPE": "it is an area or road, not a specific place",
    "NAME_MISMATCH": "its name does not match the address",
    "BARE_NAME": "its name is only a bare area/junction name",
    "GENERIC_OR_AREA_NAME": "its name is generic or just the area name",
    "WRONG_LOCALITY": "it is in a different locality",
    "FAR_FROM_GEOCODE": "it is too far from the geocoded point",
}


def _pairs(pairs, a_label="address", b_label="Google"):
    return ", ".join(f"'{a}' ({a_label}) vs '{b}' ({b_label})" for a, b in pairs)


def build_remarks(res: dict, flags) -> str:
    """One plain-language sentence or two per row: what was found, why the score, what to check."""
    flags = set(flags)
    der = res.get("derivation_type") or ""
    if der == "INVALID_INPUT":
        return "Address is empty or too short after cleaning; not geocoded."
    if der == ERROR_DERIVATION:
        return f"Processing error ({str(res.get('status'))[:120]}); run again with --retry-errors."

    under = res.get("underlying")
    score, cat = under if under else (res.get("confidence_score"), res.get("category"))
    place, d, lt = res.get("place_name"), res.get("distance_m"), res.get("geocode_location_type")
    fa = res.get("formatted_address") or ""
    parts = []

    if "DOUBLE_PINCODE" in flags or "MULTIPLE_PINCODES" in flags:
        pins = " and ".join(res.get("pins") or [])
        text = f"Multiple distinct pincodes ({pins}) in the address; address left unchanged, not geocoded to avoid inaccurate match."
        if under:
            text += f" Underlying result before this rule: {score} {cat}."
        parts.append(text)
    elif "SPELLING_ERROR" in flags or cat == "SPELLING_ERROR":
        suspected = res.get("suspected_misspelled_words") or ""
        suggestions = res.get("suggested_spelling_corrections") or ""
        parts.append(f"Suspected spelling mistake in address: '{suspected}' (suggested: '{suggestions}'); validation stopped without API calls.")
    elif "SPELLING_UNCERTAIN" in flags or cat == "SPELLING_UNCERTAIN":
        suspected = res.get("suspected_misspelled_words") or ""
        parts.append(f"Uncertain spelling detected for '{suspected}'; flagged for manual review.")

    near = f" ({d} m from the geocode)" if d is not None else ""
    if cat == "ROOFTOP_EXACT":
        parts.append("Exact rooftop match; Google's address agrees with the input.")
    elif cat == "POI_EXACT":
        parts.append(f"Named place '{place}' appears in the address{near}.")
    elif cat == "LANDMARK_EXACT":
        parts.append(f"Address is near the landmark '{place}'{near}; coordinates are the landmark's, not the house.")
    elif cat == "SPELLING_MISTAKE":
        if (place or res.get("typos")) and "SPELLING_ERROR" not in flags:
            parts.append(f"Spelling mistake: {_pairs(res.get('typos') or [], 'address', 'place name')}; "
                         f"matched place '{place}'{near}.")
    elif cat == "PARTIAL_MATCH":
        parts.append("Google matched only part of the address; house/street detail is not confirmed.")
    elif cat == "INTERPOLATED":
        parts.append("Street-level estimate (interpolated); exact house number not confirmed.")
    elif cat == "STREET_LEVEL":
        parts.append("Only the street was located; no house-level match.")
    elif cat in ("AREA_LEVEL", "INCOMPLETE_ADDRESS"):
        details = res.get("completeness_details")
        if details:
            subcat = details.get("subcat")
            cnt = len(details.get("major_words") or [])
            words_list = details.get("major_words") or []
            words_str = f" ({', '.join(words_list)})" if words_list else ""
            chars_cnt = details.get("length", 0)
            if subcat == "MISSING_PINCODE":
                parts.append("Incomplete address: missing 6-digit Indian postal pincode.")
            elif subcat == "TOO_FEW_WORDS":
                parts.append(f"Incomplete address: only {cnt} major word(s){words_str}; requires at least 3 major words.")
            elif subcat == "ADDRESS_TOO_SHORT":
                parts.append(f"Incomplete address: address length ({chars_cnt} characters) is below the minimum threshold (15 characters).")
            elif subcat == "TOO_SHORT_AND_FEW_WORDS":
                parts.append(f"Incomplete address: address is too short ({chars_cnt} chars) and contains only {cnt} major word(s){words_str}.")
            elif subcat == "MISSING_PIN_AND_FEW_WORDS":
                parts.append(f"Incomplete address: missing 6-digit pincode and contains only {cnt} major word(s){words_str}.")
            elif subcat == "MISSING_PIN_AND_TOO_SHORT":
                parts.append(f"Incomplete address: missing 6-digit pincode and address is too short ({chars_cnt} chars).")
            elif subcat == "ALL_CRITERIA_FAILED":
                parts.append(f"Incomplete address: failed all 3 completeness constraints (length: {chars_cnt} chars, {cnt} major word(s), missing 6-digit pincode).")
            else:
                parts.append(f"Incomplete address ({subcat}).")
        elif "GEOCODE_UNVERIFIED" in flags:
            parts.append(f"Google's result ('{fa[:80]}') disagrees with the input pincode/locality, so it is not trusted.")
        elif "TOO_FEW_WORDS" in flags:
            parts.append("Too few place words to locate the address.")
        elif lt in ("APPROXIMATE", "GEOMETRIC_CENTER"):
            parts.append(f"Area-level match: only the area/locality was found ({lt}); "
                         f"no building, street or landmark matched.")
        else:
            parts.append("Area-level match: nothing specific enough could be matched.")
        if place and res.get("place_reject"):
            parts.append(f"Places returned '{place}' but it was rejected: "
                         f"{_REJECT_TEXT.get(res['place_reject'], res['place_reject'])}.")
    elif cat == "NOT_FOUND":
        parts.append("Google found no result for this address.")
    else:
        parts.append(f"Scored {score} ({cat}).")

    if res.get("typos") and cat != "SPELLING_MISTAKE":
        parts.append(f"Possible spelling difference with '{place}': {_pairs(res['typos'], 'address', 'place name')}.")
    if res.get("variants"):
        parts.append(f"Spelling variant vs Google's address: {_pairs(res['variants'])}.")
    if "PIN_MISMATCH" in flags or "PIN_MISMATCH_CORROBORATED" in flags:
        parts.append("The input pincode differs from Google's pincode for this point.")
    if "POI_PIN_MISMATCH" in flags:
        parts.append("The matched place's pincode differs from the input; score capped.")
    if "POI_GEOCODE_DISAGREE" in flags:
        parts.append("The matched place is more than 2 km from the geocode; score capped.")
    if "ADDRESS_TRUNCATED" in flags:
        parts.append("Address was very long and was shortened before geocoding.")
    if res.get("synonym_normalization_applied"):
        terms = res.get("normalized_terms") or ""
        parts.append(f"Synonym normalization applied ({terms}) before geocoding.")
    return " ".join(parts)


def build_row(idx, loan_no, address_col, original, sanitized, res, extra_flags):
    flags = list(dict.fromkeys(list(extra_flags) + list(res.get("flags") or [])))
    lat = round(res["lat"], 7) if res.get("lat") is not None else None
    lng = round(res["lng"], 7) if res.get("lng") is not None else None

    orig_str = re.sub(r"\s+", " ", str(original)).strip() if original is not None else ""
    san_str = sanitized or ""
    norm_str = res.get("normalized_address") or san_str or orig_str

    pins = res.get("pins") or []
    pin_str = res.get("pincode_list") or (", ".join(pins) if isinstance(pins, (list, set)) else str(pins))
    pin_cnt = res.get("pincode_count") if res.get("pincode_count") is not None else len(pins)

    addr_len = res.get("address_length") or len(san_str or orig_str)
    major_cnt = res.get("major_word_count", 0)

    match_cls = res.get("match_classification") or res.get("category") or ""
    score_cat = res.get("category") or res.get("derivation_type") or match_cls

    return {
        "input_row": idx,
        "soa_loan_no": loan_no,
        "address_column": address_col,
        "original_address": orig_str,
        "sanitized_address": san_str,
        "normalized_address": norm_str,
        "address_length": addr_len,
        "major_word_count": major_cnt,
        "pincode_list": pin_str,
        "pincode_count": pin_cnt,
        "address_completeness_status": res.get("address_completeness_status") or "COMPLETE",
        "address_issue_category": res.get("address_issue_category") or "NONE",
        "spelling_error_detected": bool(res.get("spelling_error_detected")),
        "suspected_misspelled_words": res.get("suspected_misspelled_words") or "",
        "suggested_spelling_corrections": res.get("suggested_spelling_corrections") or "",
        "synonym_normalization_applied": bool(res.get("synonym_normalization_applied")),
        "normalized_terms": res.get("normalized_terms") or "",
        "processing_stage": res.get("processing_stage") or "",
        "confidence_score": res.get("confidence_score"),
        "match_classification": match_cls,
        "score_category": score_cat,
        "geocoding_api_called": bool(res.get("geocoding_api_called")),
        "places_api_called": bool(res.get("places_api_called")),
        "latitude": lat,
        "longitude": lng,
        "geocode_status": res.get("status"),
        "source_api": res.get("source_api"),
        "derivation_type": res.get("derivation_type"),
        "location_type": res.get("location_type"),
        "geocode_location_type": res.get("geocode_location_type"),
        "distance_between_apis_m": res.get("distance_m"),
        "name_match_ratio": res.get("match_ratio"),
        "place_name": res.get("place_name"),
        "place_types": res.get("place_types"),
        "recommended_geofence_radius_m": res.get("recommended_geofence_m"),
        "geocode_formatted_address": res.get("formatted_address"),
        "address_verification": res.get("verification"),
        "flags": ";".join(flags),
        "review_needed": bool(set(flags) & REVIEW_FLAGS),
        "remarks": build_remarks(res, flags),
        "script_version": SCRIPT_VERSION,
    }


# --------------------------------------------------------------------------
# Concurrency and Multiprocessing Worker Helpers
# --------------------------------------------------------------------------
_worker_client = None
_worker_cache = None


def _init_worker_process(api_key, region, cache_db, no_cache, cache_only, max_age_days,
                         max_api_calls=None, db_mode=False, db_config=None):
    """Initializer executed once by each worker process in multiprocessing.Pool."""
    global _worker_client, _worker_cache
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    cache = None
    if not no_cache:
        if db_mode and db_config:
            try:
                import psycopg2
                conn = psycopg2.connect(
                    host=db_config.get("host"),
                    port=db_config.get("port"),
                    dbname=db_config.get("dbname"),
                    user=db_config.get("user"),
                    password=db_config.get("password"),
                    connect_timeout=10,
                )
                cache = PostgresCache(
                    conn,
                    schema=db_config.get("schema", "public"),
                    table=db_config.get("cache_table", "geocode_cache"),
                    max_age_days=max_age_days,
                )
            except Exception as e:
                print(f"[Worker] Warning: could not connect to PostgreSQL cache: {e}")
                cache = None
        elif cache_db:
            try:
                cache = DiskCache(cache_db, max_age_days)
            except Exception as e:
                print(f"[Worker] Warning: could not open SQLite cache {cache_db}: {e}")
                cache = None

    _worker_cache = cache
    _worker_client = ApiClient(api_key, cache, region=region, cache_only=cache_only,
                               max_api_calls=max_api_calls)


def _process_row_task(task: tuple, client=None) -> dict:
    """
    Worker task executing sanitize_address, Stage 1-4 checks, Stage 5-6 geocoding/places,
    and row construction.
    task: (idx, loan_no, address_col, original)
    client: ApiClient instance or None (uses worker-local _worker_client if None)
    """
    idx, loan_no, address_col, original = task
    sanitized, san_flags = sanitize_address(original)

    if not sanitized:
        res = _blank_result(
            status="Invalid/empty address",
            derivation_type="INVALID_INPUT",
            category="INVALID_INPUT",
        )
        row = build_row(idx, loan_no, address_col, original, sanitized, res, san_flags)
        return {
            "status": "INVALID_INPUT",
            "idx": idx,
            "loan_no": loan_no,
            "sanitized": sanitized,
            "res": res,
            "row": row,
            "api_calls": 0,
            "cache_hits": 0,
        }

    c = client or _worker_client
    if c is None:
        raise RuntimeError("Worker ApiClient is not initialized")

    calls_before = getattr(c, "stats", {}).get("api_calls", 0)
    hits_before = getattr(c, "stats", {}).get("cache_hits", 0)

    try:
        res = derive_coordinates_with_confidence(sanitized, c)
    except CacheMiss:
        return {
            "status": "CACHE_MISS",
            "idx": idx,
            "loan_no": loan_no,
            "sanitized": sanitized,
            "api_calls": 0,
            "cache_hits": 0,
        }
    except TransientApiError as e:
        calls_delta = getattr(c, "stats", {}).get("api_calls", 0) - calls_before
        hits_delta = getattr(c, "stats", {}).get("cache_hits", 0) - hits_before
        return {
            "status": "TRANSIENT_API_ERROR",
            "idx": idx,
            "loan_no": loan_no,
            "sanitized": sanitized,
            "error": str(e),
            "api_calls": calls_delta,
            "cache_hits": hits_delta,
        }
    except ApiAbort as e:
        calls_delta = getattr(c, "stats", {}).get("api_calls", 0) - calls_before
        hits_delta = getattr(c, "stats", {}).get("cache_hits", 0) - hits_before
        return {
            "status": "API_ABORT",
            "idx": idx,
            "loan_no": loan_no,
            "sanitized": sanitized,
            "error": str(e),
            "api_calls": calls_delta,
            "cache_hits": hits_delta,
        }
    except Exception as e:
        msg = getattr(c, "_redact", lambda x: str(x))(f"{type(e).__name__}: {e}")
        res = _blank_result(
            status=msg[:200],
            derivation_type=ERROR_DERIVATION,
            category="ERROR",
            flags=[ERROR_DERIVATION],
        )
        row = build_row(idx, loan_no, address_col, original, sanitized, res, san_flags)
        calls_delta = getattr(c, "stats", {}).get("api_calls", 0) - calls_before
        hits_delta = getattr(c, "stats", {}).get("cache_hits", 0) - hits_before
        return {
            "status": "INTERNAL_ERROR",
            "idx": idx,
            "loan_no": loan_no,
            "sanitized": sanitized,
            "error": msg,
            "res": res,
            "row": row,
            "api_calls": calls_delta,
            "cache_hits": hits_delta,
        }

    calls_delta = getattr(c, "stats", {}).get("api_calls", 0) - calls_before
    hits_delta = getattr(c, "stats", {}).get("cache_hits", 0) - hits_before
    row = build_row(idx, loan_no, address_col, original, sanitized, res, san_flags)
    return {
        "status": "OK",
        "idx": idx,
        "loan_no": loan_no,
        "sanitized": sanitized,
        "res": res,
        "row": row,
        "api_calls": calls_delta,
        "cache_hits": hits_delta,
    }


# --------------------------------------------------------------------------
# Database helpers (PostgreSQL)
# --------------------------------------------------------------------------
class PostgresCache:
    """Stores the trimmed raw API answer per request in PostgreSQL, matching DiskCache interface."""

    def __init__(self, conn, schema="public", table="geocode_cache", max_age_days=DEFAULT_CACHE_MAX_AGE_DAYS):
        self.conn = conn
        self.schema = schema
        self.table = table
        self.max_age_days = max_age_days if (max_age_days and max_age_days > 0) else None

    def get(self, key):
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
            if not row:
                return None
            val = row[0]
            if isinstance(val, (dict, list)):
                return val
            return json.loads(val)
        except Exception:
            return None
        finally:
            cur.close()

    def set(self, key, data):
        from psycopg2.extras import Json
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
            self.conn.rollback()
        finally:
            cur.close()

    def close(self):
        pass


def load_env(env_path=None):
    """Load key-value pairs from .env into os.environ if not already set."""
    candidates = []
    if env_path:
        candidates.append(env_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        ".env",
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


def ensure_db_tables(conn, schema: str, input_table: str, output_table: str, cache_table: str = "geocode_cache"):
    """Create input, output, and cache tables in PostgreSQL if they do not already exist."""
    cur = conn.cursor()
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {schema}.{input_table} (
        id BIGSERIAL,
        soa_loan_no TEXT PRIMARY KEY,
        soa_current_address TEXT,
        soa_property_address TEXT,
        follow_new_address TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{input_table}_id ON {schema}.{input_table}(id);
    CREATE INDEX IF NOT EXISTS idx_{input_table}_current_address ON {schema}.{input_table}(soa_current_address);

    CREATE TABLE IF NOT EXISTS {schema}.{output_table} (
        id BIGSERIAL PRIMARY KEY,
        input_row INTEGER,
        soa_loan_no TEXT NOT NULL,
        address_column TEXT NOT NULL DEFAULT 'soa_current_address',
        original_address TEXT,
        sanitized_address TEXT,
        normalized_address TEXT,
        address_length INTEGER,
        major_word_count INTEGER,
        pincode_list TEXT,
        pincode_count INTEGER,
        address_completeness_status TEXT,
        address_issue_category TEXT,
        spelling_error_detected BOOLEAN,
        suspected_misspelled_words TEXT,
        suggested_spelling_corrections TEXT,
        synonym_normalization_applied BOOLEAN,
        normalized_terms TEXT,
        processing_stage TEXT,
        confidence_score INTEGER,
        match_classification TEXT,
        score_category TEXT,
        geocoding_api_called BOOLEAN,
        places_api_called BOOLEAN,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
        geocode_status TEXT,
        source_api TEXT,
        derivation_type TEXT,
        location_type TEXT,
        geocode_location_type TEXT,
        distance_between_apis_m DOUBLE PRECISION,
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
        CONSTRAINT uq_{output_table}_loan_col UNIQUE (soa_loan_no, address_column)
    );
    CREATE INDEX IF NOT EXISTS idx_{output_table}_loan_no ON {schema}.{output_table}(soa_loan_no);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_score_cat ON {schema}.{output_table}(score_category);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_review ON {schema}.{output_table}(review_needed);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_derivation ON {schema}.{output_table}(derivation_type);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_stage ON {schema}.{output_table}(processing_stage);
    CREATE INDEX IF NOT EXISTS idx_{output_table}_match_cls ON {schema}.{output_table}(match_classification);

    CREATE TABLE IF NOT EXISTS {schema}.{cache_table} (
        key TEXT PRIMARY KEY,
        response JSONB NOT NULL,
        saved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{cache_table}_saved_at ON {schema}.{cache_table}(saved_at);
    """)

    # Migrate existing table columns if already created in earlier runs
    migration_columns = [
        ("normalized_address", "TEXT"),
        ("address_length", "INTEGER"),
        ("major_word_count", "INTEGER"),
        ("pincode_list", "TEXT"),
        ("pincode_count", "INTEGER"),
        ("address_completeness_status", "TEXT"),
        ("address_issue_category", "TEXT"),
        ("spelling_error_detected", "BOOLEAN"),
        ("suspected_misspelled_words", "TEXT"),
        ("suggested_spelling_corrections", "TEXT"),
        ("synonym_normalization_applied", "BOOLEAN"),
        ("normalized_terms", "TEXT"),
        ("processing_stage", "TEXT"),
        ("match_classification", "TEXT"),
        ("geocoding_api_called", "BOOLEAN"),
        ("places_api_called", "BOOLEAN"),
    ]
    for col_name, col_type in migration_columns:
        try:
            cur.execute(f"ALTER TABLE {schema}.{output_table} ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
        except Exception:
            conn.rollback()

    conn.commit()
    cur.close()


def save_db_batch(conn, schema: str, output_table: str, batch: list):
    """Upsert a batch of processed rows into PostgreSQL."""
    if not batch:
        return
    from psycopg2 import extras
    cur = conn.cursor()
    cols = [
        "input_row", "soa_loan_no", "address_column", "original_address", "sanitized_address",
        "normalized_address",
        "address_length", "major_word_count", "pincode_list", "pincode_count",
        "address_completeness_status", "address_issue_category",
        "spelling_error_detected", "suspected_misspelled_words", "suggested_spelling_corrections",
        "synonym_normalization_applied", "normalized_terms",
        "processing_stage", "confidence_score", "match_classification", "score_category",
        "geocoding_api_called", "places_api_called",
        "latitude", "longitude", "geocode_status", "source_api", "derivation_type",
        "location_type", "geocode_location_type", "distance_between_apis_m",
        "name_match_ratio", "place_name", "place_types",
        "recommended_geofence_radius_m", "geocode_formatted_address", "address_verification",
        "flags", "review_needed", "remarks", "script_version"
    ]
    records = []
    for r in batch:
        rec = (
            r.get("input_row"),
            r.get("soa_loan_no"),
            r.get("address_column") or "soa_current_address",
            r.get("original_address") or "",
            r.get("sanitized_address") or "",
            r.get("normalized_address") or "",
            r.get("address_length") or 0,
            r.get("major_word_count") or 0,
            r.get("pincode_list") or "",
            r.get("pincode_count") or 0,
            r.get("address_completeness_status") or "COMPLETE",
            r.get("address_issue_category") or "NONE",
            bool(r.get("spelling_error_detected")),
            r.get("suspected_misspelled_words") or "",
            r.get("suggested_spelling_corrections") or "",
            bool(r.get("synonym_normalization_applied")),
            r.get("normalized_terms") or "",
            r.get("processing_stage") or "",
            r.get("confidence_score"),
            r.get("match_classification") or "",
            r.get("score_category"),
            bool(r.get("geocoding_api_called")),
            bool(r.get("places_api_called")),
            r.get("latitude"),
            r.get("longitude"),
            r.get("geocode_status"),
            r.get("source_api"),
            r.get("derivation_type"),
            r.get("location_type"),
            r.get("geocode_location_type"),
            r.get("distance_between_apis_m"),
            r.get("name_match_ratio"),
            r.get("place_name"),
            r.get("place_types"),
            r.get("recommended_geofence_radius_m"),
            r.get("geocode_formatted_address"),
            r.get("address_verification"),
            r.get("flags"),
            bool(r.get("review_needed")),
            r.get("remarks"),
            r.get("script_version") or SCRIPT_VERSION,
        )
        records.append(rec)

    cols_sql = ", ".join(cols)
    update_set = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols if c not in ("soa_loan_no", "address_column")])
    update_set += ", processed_at = CURRENT_TIMESTAMP"
    insert_sql = f"""
    INSERT INTO {schema}.{output_table} ({cols_sql})
    VALUES %s
    ON CONFLICT (soa_loan_no, address_column) DO UPDATE SET
    {update_set};
    """
    extras.execute_values(cur, insert_sql, records, page_size=len(records))
    conn.commit()
    cur.close()


def _run_db(args, api_key, client_factory) -> int:
    """Run geocoding directly against PostgreSQL tables."""
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 is required for database mode. Install it with: pip install psycopg2-binary")
        return 1

    db_host = args.db_host or os.environ.get("DB_HOST", "13.200.150.31")
    db_port = args.db_port or int(os.environ.get("DB_PORT", 5432))
    db_name = args.db_name or os.environ.get("DB_NAME", "geo_fence_analysis")
    db_user = args.db_user or os.environ.get("DB_USER", "postgres")
    db_password = args.db_password or os.environ.get("DB_PASSWORD", "")
    db_schema = args.db_schema or os.environ.get("DB_SCHEMA", "public")
    input_table = args.input_table
    output_table = args.output_table
    address_col = args.address_col or "soa_current_address"
    loan_col = args.loan_col or "soa_loan_no"

    print(f"Connecting to PostgreSQL: {db_user}@{db_host}:{db_port}/{db_name} (schema: {db_schema})")
    try:
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password,
            connect_timeout=10
        )
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}")
        return 1

    ensure_db_tables(conn, db_schema, input_table, output_table, args.cache_table)

    cur = conn.cursor()
    if args.retry_errors:
        cur.execute(f"DELETE FROM {db_schema}.{output_table} WHERE derivation_type = %s AND address_column = %s;",
                    (ERROR_DERIVATION, address_col))
        dropped = cur.rowcount
        conn.commit()
        if dropped > 0:
            print(f"--retry-errors: {dropped} previously failed rows removed from {output_table} and will be retried.")

    # Fetch pending rows (rows in input_table not yet in output_table for this address column)
    query = f"""
    SELECT g.id, g.{loan_col}, g.{address_col}
    FROM {db_schema}.{input_table} g
    LEFT JOIN {db_schema}.{output_table} p
      ON g.{loan_col} = p.{loan_col} AND p.address_column = %s
    WHERE p.{loan_col} IS NULL
    ORDER BY g.id;
    """
    cur.execute(query, (address_col,))
    rows = cur.fetchall()
    total_pending = len(rows)

    if args.random:
        if args.seed is not None:
            random.seed(args.seed)
        random.shuffle(rows)
    if args.limit is not None:
        rows = rows[:max(0, args.limit)]
        print(f"Found {total_pending} pending rows; processing up to {len(rows)} {'random ' if args.random else ''}(limit = {args.limit})")
    else:
        print(f"Rows to process from {db_schema}.{input_table}: {len(rows)}")

    # ---- dry run ----------------------------------------------------------
    if args.dry_run:
        cleaned = [sanitize_address(r[2])[0] for r in rows]
        valid = [c for c in cleaned if c]
        unique = {_normalize(c) for c in valid}
        print(f"Dry run (DB mode): {len(valid)} usable addresses ({len(rows) - len(valid)} empty/invalid), "
              f"{len(unique)} unique.")
        print(f"Worst case without any cache: {len(unique) * 2} API calls "
              f"({len(unique)} Geocoding + up to {len(unique)} Places).")
        cur.close()
        conn.close()
        return 0

    cache = None
    if not args.no_cache:
        cache = PostgresCache(conn, db_schema, args.cache_table, args.cache_max_age_days)
        print(f"Using PostgreSQL cache table: {db_schema}.{args.cache_table}")
    if client_factory is not None:
        client = client_factory(args, cache)
    else:
        client = ApiClient(api_key, cache, region=args.region, cache_only=args.cache_only,
                           max_api_calls=args.max_api_calls)
    stats = getattr(client, "stats", {"api_calls": 0, "cache_hits": 0})

    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, AttributeError):
        pass

    batch = []
    memo = {}
    saved_total = skipped_api = skipped_cache = 0
    consecutive_api = consecutive_internal = 0
    categories = Counter()
    review = 0
    exit_code = 0

    def flush_db():
        nonlocal saved_total
        if batch:
            save_db_batch(conn, db_schema, output_table, batch)
            saved_total += len(batch)
            batch.clear()

    workers = getattr(args, "workers", 1) or 1
    worker_mode = getattr(args, "worker_mode", "process") or "process"
    if workers < 1:
        workers = 1

    if workers == 1:
        try:
            for n, (row_id, loan_no, original) in enumerate(rows, start=1):
                sanitized, san_flags = sanitize_address(original)

                if not sanitized:
                    res = _blank_result(status="Invalid/empty address", derivation_type="INVALID_INPUT",
                                        category="INVALID_INPUT")
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: invalid/empty address")
                else:
                    ckey = _normalize(sanitized)
                    if ckey in memo:
                        res = memo[ckey]
                        print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: duplicate address -> reused")
                    else:
                        try:
                            res = derive_coordinates_with_confidence(sanitized, client)
                        except CacheMiss:
                            skipped_cache += 1
                            print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: not in cache, skipped (--cache-only)")
                            continue
                        except TransientApiError as e:
                            skipped_api += 1
                            consecutive_api += 1
                            print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: API trouble ({e}); "
                                  f"row not saved, it will be retried next run")
                            if consecutive_api >= MAX_CONSECUTIVE_ERRORS:
                                raise ApiAbort(f"{consecutive_api} rows in a row failed because of API/network "
                                               f"errors (last: {e}). Stopping; progress is saved in PostgreSQL.")
                            continue
                        except (ApiAbort, KeyboardInterrupt):
                            raise
                        except Exception as e:
                            consecutive_internal += 1
                            msg = getattr(client, "_redact", lambda x: str(x))(f"{type(e).__name__}: {e}")
                            print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: INTERNAL ERROR {msg}")
                            traceback.print_exc(limit=3)
                            res = _blank_result(status=msg[:200], derivation_type=ERROR_DERIVATION,
                                                category="ERROR", flags=[ERROR_DERIVATION])
                            if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                                batch.append(build_row(row_id, loan_no, address_col, original, sanitized, res, san_flags))
                                raise ApiAbort(f"{consecutive_internal} internal errors in a row; stopping.")
                        else:
                            consecutive_api = consecutive_internal = 0
                            memo[ckey] = res
                            time.sleep(REQUEST_PAUSE_S)
                            print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: "
                                  f"{res['confidence_score']} {res['category']}  {sanitized[:50]}")

                row = build_row(row_id, loan_no, address_col, original, sanitized, res, san_flags)
                categories[row["score_category"]] += 1
                review += 1 if row["review_needed"] else 0
                batch.append(row)
                if len(batch) >= args.batch_size:
                    flush_db()
                    print(f"   [saved {saved_total} rows so far to PostgreSQL ({db_schema}.{output_table})]")

        except ApiAbort as e:
            print(f"\nSTOPPED: {e}")
            exit_code = 1
        except KeyboardInterrupt:
            print("\nInterrupted; saving what is done to PostgreSQL.")
            exit_code = 130
        finally:
            flush_db()
            cur.close()
            conn.close()
            if cache is not None:
                cache.close()
    else:
        effective_mode = worker_mode
        if client_factory is not None and effective_mode == "process":
            effective_mode = "thread"

        print(f"Running with {workers} concurrent workers (mode: {effective_mode})")
        tasks = [(row_id, loan_no, address_col, original) for (row_id, loan_no, original) in rows]

        pool_context = None
        try:
            if effective_mode == "process":
                db_config = {
                    "host": db_host,
                    "port": db_port,
                    "dbname": db_name,
                    "user": db_user,
                    "password": db_password,
                    "schema": db_schema,
                    "cache_table": args.cache_table,
                }
                init_args = (
                    api_key,
                    args.region,
                    None,
                    args.no_cache,
                    args.cache_only,
                    args.cache_max_age_days,
                    args.max_api_calls,
                    True,
                    db_config,
                )
                pool_context = multiprocessing.Pool(
                    processes=workers,
                    initializer=_init_worker_process,
                    initargs=init_args,
                )
                task_iter = pool_context.imap(_process_row_task, tasks, chunksize=1)
            else:
                pool_context = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
                worker_func = functools.partial(_process_row_task, client=client)
                task_iter = pool_context.map(worker_func, tasks)

            for n, res_dict in enumerate(task_iter, start=1):
                status = res_dict["status"]
                row_id = res_dict["idx"]
                loan_no = res_dict["loan_no"]
                sanitized = res_dict.get("sanitized", "")
                stats["api_calls"] += res_dict.get("api_calls", 0)
                stats["cache_hits"] += res_dict.get("cache_hits", 0)

                if status == "INVALID_INPUT":
                    consecutive_api = consecutive_internal = 0
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: invalid/empty address")
                    row = res_dict["row"]
                elif status == "CACHE_MISS":
                    skipped_cache += 1
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: not in cache, skipped (--cache-only)")
                    continue
                elif status == "TRANSIENT_API_ERROR":
                    skipped_api += 1
                    consecutive_api += 1
                    err_msg = res_dict.get("error", "API error")
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: API trouble ({err_msg}); "
                          f"row not saved, it will be retried next run")
                    if consecutive_api >= MAX_CONSECUTIVE_ERRORS:
                        raise ApiAbort(f"{consecutive_api} rows in a row failed because of API/network "
                                       f"errors (last: {err_msg}). Stopping; progress is saved in PostgreSQL.")
                    continue
                elif status == "API_ABORT":
                    err_msg = res_dict.get("error", "API abort")
                    raise ApiAbort(f"API aborted: {err_msg}")
                elif status == "INTERNAL_ERROR":
                    consecutive_internal += 1
                    err_msg = res_dict.get("error", "Internal error")
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: INTERNAL ERROR {err_msg}")
                    row = res_dict["row"]
                    if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                        batch.append(row)
                        raise ApiAbort(f"{consecutive_internal} internal errors in a row; stopping.")
                else:  # status == "OK"
                    consecutive_api = consecutive_internal = 0
                    res = res_dict["res"]
                    row = res_dict["row"]
                    print(f"[{n}/{len(rows)}] row {row_id} [{loan_no}]: "
                          f"{res['confidence_score']} {res['category']}  {sanitized[:50]}")

                categories[row["score_category"]] += 1
                review += 1 if row["review_needed"] else 0
                batch.append(row)
                if len(batch) >= args.batch_size:
                    flush_db()
                    print(f"   [saved {saved_total} rows so far to PostgreSQL ({db_schema}.{output_table})]")

                if args.max_api_calls is not None and stats["api_calls"] >= args.max_api_calls:
                    raise ApiAbort(f"--max-api-calls budget of {args.max_api_calls} reached")

        except ApiAbort as e:
            print(f"\nSTOPPED: {e}")
            exit_code = 1
        except KeyboardInterrupt:
            print("\nInterrupted; saving what is done to PostgreSQL.")
            exit_code = 130
        finally:
            if pool_context is not None:
                if effective_mode == "process":
                    pool_context.terminate()
                    pool_context.join()
                else:
                    pool_context.shutdown(wait=False, cancel_futures=True)
            flush_db()
            cur.close()
            conn.close()
            if cache is not None:
                cache.close()

    print(f"\nDone. {saved_total} rows written to PostgreSQL -> {db_schema}.{output_table}")
    if categories:
        print("Categories: " + ", ".join(f"{k}={v}" for k, v in sorted(categories.items())))
    print(f"Rows flagged for review: {review}")
    if skipped_api:
        print(f"Not saved because of API/network errors (will be retried next run): {skipped_api}")
    if skipped_cache:
        print(f"Skipped because they are not in the cache (--cache-only): {skipped_cache}")
    print(f"API calls made: {stats.get('api_calls', 0)} | served from disk cache: {stats.get('cache_hits', 0)}")
    return exit_code


# --------------------------------------------------------------------------
# CLI / main
# --------------------------------------------------------------------------
def parse_arguments(argv=None):
    p = argparse.ArgumentParser(description="Geocode addresses; score confidence and geofence radius.")
    p.add_argument("--input", "-i", default=DEFAULT_INPUT_CSV, help=f"Input CSV (default: {DEFAULT_INPUT_CSV})")
    p.add_argument("--output", "-o", default=DEFAULT_OUTPUT_CSV, help=f"Output CSV (default: {DEFAULT_OUTPUT_CSV})")
    p.add_argument("--address-col", "-a", default=None, help="Address column (auto-detects soa_current_address / Addresses / address)")
    p.add_argument("--loan-col", "-l", default="soa_loan_no", help="Loan number column (default: soa_loan_no)")
    p.add_argument("--limit", "-n", type=int, default=None, help="Process at most N NEW rows this run")
    p.add_argument("--random", "-r", action="store_true", help="Sample N random rows instead of first N sequential rows")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducible sampling (e.g. --seed 42)")
    p.add_argument("--batch-size", "-b", type=int, default=20, help="Rows per save to CSV / PostgreSQL (default: 20)")
    p.add_argument("--workers", "-w", type=int, default=1,
                   help="Number of concurrent workers (default: 1; >1 enables multiprocessing)")
    p.add_argument("--worker-mode", choices=["process", "thread"], default="process",
                   help="Concurrency backend: 'process' (multiprocessing, default) or 'thread' (multithreading)")
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
    return p.parse_args(argv)


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
            stream.reconfigure(errors="replace")      # odd characters must not crash printing
        except (AttributeError, ValueError):
            pass

    # Database Mode
    if args.db or (args.input and args.input.lower() in ("db", "database", "postgres")):
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if client_factory is None and not args.cache_only and not args.dry_run and not api_key:
            print(f"Error: set the {API_KEY_ENV} environment variable (or use --cache-only / --dry-run).")
            return 2
        return _run_db(args, api_key, client_factory)

    if not os.path.exists(args.input):
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
    if client_factory is None and not args.cache_only and not api_key:
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


def _run(args, df, addresses, loans, address_col, loan_col, api_key, client_factory) -> int:
    total_records = len(addresses)
    output_csv = args.output

    # ---- prepare the output file / resume --------------------------------
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
        if args.seed is not None:
            random.seed(args.seed)
        random.shuffle(todo)
    if args.limit is not None:
        todo = todo[:max(0, args.limit)]
        print(f"Processing up to {len(todo)} new {'random ' if args.random else ''}rows (limit = {args.limit})")
    print(f"Rows to process: {len(todo)}")

    # ---- client ------------------------------------------------------------
    cache = None
    if not args.no_cache:
        cache = DiskCache(args.cache_db, args.cache_max_age_days)
        print(f"Using disk cache: {args.cache_db}")
    if client_factory is not None:
        client = client_factory(args, cache)
    else:
        client = ApiClient(api_key, cache, region=args.region, cache_only=args.cache_only,
                           max_api_calls=args.max_api_calls)
    stats = getattr(client, "stats", {"api_calls": 0, "cache_hits": 0})

    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, AttributeError):
        pass

    batch: list = []
    memo: dict = {}                      # normalized address -> result, for duplicates inside this run
    saved_total = skipped_api = skipped_cache = 0
    consecutive_api = consecutive_internal = 0
    categories: Counter = Counter()
    review = 0
    exit_code = 0

    def flush():
        nonlocal saved_total
        if batch:
            append_rows(output_csv, batch)
            saved_total += len(batch)
            batch.clear()

    workers = getattr(args, "workers", 1) or 1
    worker_mode = getattr(args, "worker_mode", "process") or "process"
    if workers < 1:
        workers = 1

    if workers == 1:
        try:
            for n, idx in enumerate(todo, start=1):
                loan_no = loans[idx]
                original = addresses[idx]
                sanitized, san_flags = sanitize_address(original)

                if not sanitized:
                    res = _blank_result(status="Invalid/empty address", derivation_type="INVALID_INPUT",
                                        category="INVALID_INPUT")
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: invalid/empty address")
                else:
                    ckey = _normalize(sanitized)
                    if ckey in memo:
                        res = memo[ckey]
                        print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: duplicate address -> reused")
                    else:
                        try:
                            res = derive_coordinates_with_confidence(sanitized, client)
                        except CacheMiss:
                            skipped_cache += 1
                            print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: not in cache, skipped (--cache-only)")
                            continue
                        except TransientApiError as e:
                            skipped_api += 1
                            consecutive_api += 1
                            print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: API trouble ({e}); "
                                  f"row not saved, it will be retried next run")
                            if consecutive_api >= MAX_CONSECUTIVE_ERRORS:
                                raise ApiAbort(f"{consecutive_api} rows in a row failed because of API/network "
                                               f"errors (last: {e}). Stopping; progress is saved.")
                            continue
                        except (ApiAbort, KeyboardInterrupt):
                            raise
                        except Exception as e:            # a bug or odd data in ONE row must not kill the run
                            consecutive_internal += 1
                            msg = getattr(client, "_redact", lambda x: str(x))(f"{type(e).__name__}: {e}")
                            print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: INTERNAL ERROR {msg}")
                            traceback.print_exc(limit=3)
                            res = _blank_result(status=msg[:200], derivation_type=ERROR_DERIVATION,
                                                category="ERROR", flags=[ERROR_DERIVATION])
                            if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                                batch.append(build_row(idx, loan_no, address_col, original, sanitized, res, san_flags))
                                raise ApiAbort(f"{consecutive_internal} internal errors in a row; stopping.")
                        else:
                            consecutive_api = consecutive_internal = 0
                            memo[ckey] = res
                            time.sleep(REQUEST_PAUSE_S)
                            print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: "
                                  f"{res['confidence_score']} {res['category']}  {sanitized[:50]}")

                row = build_row(idx, loan_no, address_col, original, sanitized, res, san_flags)
                categories[row["score_category"]] += 1
                review += 1 if row["review_needed"] else 0
                batch.append(row)
                if len(batch) >= args.batch_size:
                    flush()
                    print(f"   [saved {saved_total} rows so far]")

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
    else:
        effective_mode = worker_mode
        if client_factory is not None and effective_mode == "process":
            effective_mode = "thread"

        print(f"Running with {workers} concurrent workers (mode: {effective_mode})")
        tasks = [(idx, loans[idx], address_col, addresses[idx]) for idx in todo]

        pool_context = None
        try:
            if effective_mode == "process":
                init_args = (
                    api_key,
                    args.region,
                    args.cache_db,
                    args.no_cache,
                    args.cache_only,
                    args.cache_max_age_days,
                    args.max_api_calls,
                    False,
                    None,
                )
                pool_context = multiprocessing.Pool(
                    processes=workers,
                    initializer=_init_worker_process,
                    initargs=init_args,
                )
                task_iter = pool_context.imap(_process_row_task, tasks, chunksize=1)
            else:
                pool_context = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
                worker_func = functools.partial(_process_row_task, client=client)
                task_iter = pool_context.map(worker_func, tasks)

            for n, res_dict in enumerate(task_iter, start=1):
                status = res_dict["status"]
                idx = res_dict["idx"]
                loan_no = res_dict["loan_no"]
                sanitized = res_dict.get("sanitized", "")
                stats["api_calls"] += res_dict.get("api_calls", 0)
                stats["cache_hits"] += res_dict.get("cache_hits", 0)

                if status == "INVALID_INPUT":
                    consecutive_api = consecutive_internal = 0
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: invalid/empty address")
                    row = res_dict["row"]
                elif status == "CACHE_MISS":
                    skipped_cache += 1
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: not in cache, skipped (--cache-only)")
                    continue
                elif status == "TRANSIENT_API_ERROR":
                    skipped_api += 1
                    consecutive_api += 1
                    err_msg = res_dict.get("error", "API error")
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: API trouble ({err_msg}); "
                          f"row not saved, it will be retried next run")
                    if consecutive_api >= MAX_CONSECUTIVE_ERRORS:
                        raise ApiAbort(f"{consecutive_api} rows in a row failed because of API/network "
                                       f"errors (last: {err_msg}). Stopping; progress is saved.")
                    continue
                elif status == "API_ABORT":
                    err_msg = res_dict.get("error", "API abort")
                    raise ApiAbort(f"API aborted: {err_msg}")
                elif status == "INTERNAL_ERROR":
                    consecutive_internal += 1
                    err_msg = res_dict.get("error", "Internal error")
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: INTERNAL ERROR {err_msg}")
                    row = res_dict["row"]
                    if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                        batch.append(row)
                        raise ApiAbort(f"{consecutive_internal} internal errors in a row; stopping.")
                else:  # status == "OK"
                    consecutive_api = consecutive_internal = 0
                    res = res_dict["res"]
                    row = res_dict["row"]
                    print(f"[{n}/{len(todo)}] row {idx + 1} [{loan_no}]: "
                          f"{res['confidence_score']} {res['category']}  {sanitized[:50]}")

                categories[row["score_category"]] += 1
                review += 1 if row["review_needed"] else 0
                batch.append(row)
                if len(batch) >= args.batch_size:
                    flush()
                    print(f"   [saved {saved_total} rows so far]")

                if args.max_api_calls is not None and stats["api_calls"] >= args.max_api_calls:
                    raise ApiAbort(f"--max-api-calls budget of {args.max_api_calls} reached")

        except ApiAbort as e:
            print(f"\nSTOPPED: {e}")
            exit_code = 1
        except KeyboardInterrupt:
            print("\nInterrupted; saving what is done.")
            exit_code = 130
        finally:
            if pool_context is not None:
                if effective_mode == "process":
                    pool_context.terminate()
                    pool_context.join()
                else:
                    pool_context.shutdown(wait=False, cancel_futures=True)
            flush()
            if cache is not None:
                cache.close()

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


if __name__ == "__main__":
    sys.exit(main())