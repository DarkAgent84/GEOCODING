"""
Pre-API Validation Stages 1 through 4:
  Stage 1: Incomplete Address Detection (Length >= 15, Major Words >= 3, Valid 6-digit PIN)
  Stage 2: Multiple / Conflicting PIN Codes Detection
  Stage 3: Offline Spelling Error Detection on Landmark and Geographic Names
  Stage 4: Synonym and Entity Normalization
"""
from __future__ import annotations

import math
import os
import re
import unicodedata

from .constants import (
    ALL_KNOWN_SPELLING_WORDS,
    COMMON_DEITY_WORDS,
    INCOMPLETE_SUBCAT_SCORES,
    MAX_ADDRESS_CHARS,
    MIN_ADDRESS_CHARS,
    MIN_MAJOR_WORDS,
    POI_GENERIC_WORDS,
    SCORE_SPELLING_MAX,
    SCORE_SPELLING_MIN,
    SPELLING_ERROR_THRESHOLD,
    SPELLING_UNCERTAIN_MIN,
    SYNONYM_DICT,
    VALID_ALTERNATIVES,
)
from .text_utils import (
    _dedupe_repeats,
    _normalize,
    _pincodes,
    _sim,
    get_major_words,
)


# --------------------------------------------------------------------------
# Stage 1: Incomplete Address Validation
# --------------------------------------------------------------------------
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


def sanitize_address(addr):
    """Clean an address for geocoding. Returns (sanitized_or_None, flags)."""
    flags: list = []
    if addr is None or (isinstance(addr, float) and math.isnan(addr)):
        return None, flags
    s = unicodedata.normalize("NFKC", str(addr)[:MAX_ADDRESS_CHARS * 4])
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)

    # Existing lat/long text, coordinate pairs
    s = re.sub(r"\bLat(?:itude)?\b\s*[:=]?\s*-?[\d.]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bLong(?:itude)?\b\s*[:=]?\s*-?[\d.]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}", "", s)
    # Dates and times, "Retake Original" artifacts
    s = re.sub(r"\b\d{2}/\d{2}/(?:\d{4}|\d{2})\b", "", s)
    s = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "", s)
    s = re.sub(r"\bRetake\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bOriginal\b", "", s, flags=re.IGNORECASE)
    # Leading punctuation; a lone "0" only when it is a stray token
    s = re.sub(r"^(?:[\s\-\.\,\#\+]|0(?=[\s,]))+", "", s)
    # A "NO" / "NO." with no number after it
    s = re.sub(r"\bNO\b\.?(?!\s*[-.]?\s*\d)", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    s = _dedupe_repeats(s)
    # "202001 - 202001" -> "202001"
    s = re.sub(r"\b(\d{6})\b(?:\s*-\s*\1\b)+", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")

    if len(_pincodes(s)) > 1:
        flags.append("DOUBLE_PINCODE")
        flags.append("CONFLICTING_PINCODES")
    if len(s) > MAX_ADDRESS_CHARS:
        s = s[:MAX_ADDRESS_CHARS].rsplit(" ", 1)[0]
        flags.append("ADDRESS_TRUNCATED")
    return (s if len(s) >= 5 else None), flags


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


# --------------------------------------------------------------------------
# Stage 3: Offline Spelling Error Detection
# --------------------------------------------------------------------------
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
        if (w_low in known_names or w_low in POI_GENERIC_WORDS or
            w_low in SYNONYM_DICT or w_low in COMMON_DEITY_WORDS):
            continue
        is_alt = False
        for canon, alts in VALID_ALTERNATIVES.items():
            if w_low == canon or w_low in alts:
                is_alt = True
                break
        if is_alt:
            continue

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
        span = SCORE_SPELLING_MAX - SCORE_SPELLING_MIN
        frac = min(1.0, max(0.0, (max_sim_found - error_threshold) / (1.0 - error_threshold)))
        score = SCORE_SPELLING_MIN + int(frac * span)
        return True, suspected_errors, suggested_corrections, False, score

    if uncertain_words:
        return False, uncertain_words, uncertain_suggestions, True, SCORE_SPELLING_MIN

    return False, [], [], False, 0


def spelling_variants(ctx, formatted_address: str, limit: int = 3):
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


# --------------------------------------------------------------------------
# Stage 4: Synonym & Entity Normalization
# --------------------------------------------------------------------------
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
