"""
Scoring engine, place evaluation, geofence radius calculation, and result derivation.
"""
from __future__ import annotations

import re
from .constants import (
    AGREE_M,
    AREA_PLACE_TYPES,
    BIAS_DEFAULT_M,
    BIAS_RADIUS_M,
    COMMON_DEITY_WORDS,
    CORROBORATE_M,
    DISAGREE_CAP_M,
    ERROR_DERIVATION,
    FAR_DEFAULT_M,
    FAR_LIMIT_M,
    INCOMPLETE_SUBCAT_SCORES,
    JUNCTION_WORDS,
    LANDMARK_INTRO_WORDS,
    LANDMARK_WORDS,
    MAX_ADDRESS_CHARS,
    MAX_PLACES_CANDIDATES,
    MIN_ADDRESS_CHARS,
    MIN_MAJOR_WORDS,
    MIN_TOKEN_SIMILARITY,
    POI_GENERIC_WORDS,
    POSTPOSITION_WORDS,
    PRECISE_GEOCODE_TYPES,
    REVIEW_FLAGS,
    SCORE_AREA_LEVEL,
    SCORE_INCOMPLETE,
    SCORE_INTERPOLATED,
    SCORE_LANDMARK,
    SCORE_LANDMARK_FUZZY,
    SCORE_LANDMARK_MAX,
    SCORE_LOW_CONFIDENCE,
    SCORE_PARTIAL,
    SCORE_POI_MAX,
    SCORE_POI_PREMISE_BASE,
    SCORE_ROOFTOP,
    SCORE_SPELLING_MAX,
    SCORE_SPELLING_MIN,
    SCORE_STREET,
    SCORE_SYNONYM_MAX,
    SCORE_SYNONYM_MIN,
    SCRIPT_VERSION,
    SINGLE_WORD_SPELLING_MIN,
    SPELLING_ERROR_POLICY,
    SPELLING_SIMILARITY_MIN,
    SPELLING_UNCERTAIN_POLICY,
    _REJECT_TEXT,
    AddrCtx,
    PlaceEval,
)
from .text_utils import (
    _expand,
    _fuzzy_in,
    _normalize,
    _pairs,
    _pincodes,
    _sim,
    haversine_m,
)
from .validation import (
    check_address_completeness,
    detect_spelling_errors,
    normalize_synonyms,
    spelling_variants,
)


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
        else:
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
    return SCORE_AREA_LEVEL, "AREA_LEVEL"


def verify_geocode(ctx: AddrCtx, g: dict) -> dict:
    """Does Google's own address for the point agree with the input?"""
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


def _landmark_role(tokens, first: int, last: int) -> str:
    if first < 0:
        return "PREMISE"
    before = tokens[max(0, first - 3):first]
    after = tokens[last + 1:last + 4]
    if any(t in LANDMARK_INTRO_WORDS for t in before) or any(t in POSTPOSITION_WORDS for t in after):
        return "LANDMARK"
    return "PREMISE"


def evaluate_place(place: dict, ctx: AddrCtx) -> PlaceEval:
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


def _blank_result(**kw) -> dict:
    res = {
        "lat": None, "lng": None, "status": None, "source_api": "NONE", "derivation_type": "",
        "category": "", "match_ratio": None, "location_type": None, "geocode_location_type": None,
        "distance_m": None, "confidence_score": 0, "recommended_geofence_m": None,
        "place_name": None, "place_types": None, "formatted_address": "",
        "verification": "", "flags": [],
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
        "typos": [], "variants": [], "place_reject": None, "underlying": None, "pins": [],
    }
    res.update(kw)
    return res


def _poi_result(best: dict, g: dict, ver: dict) -> dict:
    place, ev = best["place"], best["ev"]
    loc = place["geometry"]["location"]
    name = (place.get("name") or "").strip()
    kind = {"LANDMARK_EXACT": "LANDMARK_PLACE_MATCH", "AREA_LEVEL": "AREA_PLACE_MATCH"}.get(
        best["cat"], "POI_PLACE_MATCH")
    return _blank_result(
        lat=loc["lat"], lng=loc["lng"], status="OK (PLACES_API_MATCH)", source_api="PLACES_API",
        derivation_type=f"{kind} ({name})" if name else kind, category=best["cat"],
        match_ratio=ev.ratio, location_type="POI", geocode_location_type=g["loc_type"],
        distance_m=round(best["d"]) if best["d"] is not None else None,
        confidence_score=best["score"])


def derive_coordinates_with_confidence(address: str, client=None, no_api: bool = False) -> dict:
    """6-stage cost-optimized address validation, classification & geocoding pipeline."""
    return _derive_core(address, client, no_api=no_api)


def _derive_core(address: str, client=None, no_api: bool = False) -> dict:
    """Execute the 6-stage sequential validation, normalization & geocoding pipeline."""
    # Stage 1: Incomplete Address Detection
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

    # Stage 2: Duplicate or Conflicting PIN Code Detection
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

    # Stage 3: Offline Spelling Mistake Detection
    has_spelling_err, suspected, suggestions, is_uncertain, spell_score = detect_spelling_errors(address)
    if (has_spelling_err and SPELLING_ERROR_POLICY == "STOP") or \
            (is_uncertain and SPELLING_UNCERTAIN_POLICY == "STOP"):
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

    # Stage 4: Synonym & Entity Normalization
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
        "spelling_error_detected": bool(has_spelling_err or is_uncertain),
        "suspected_misspelled_words": ", ".join(suspected) if (has_spelling_err or is_uncertain) else "",
        "suggested_spelling_corrections": ", ".join(suggestions) if (has_spelling_err or is_uncertain) else "",
        "geocoding_api_called": not no_api,
        "places_api_called": False,
    }

    if no_api or client is None:
        flags = []
        if is_uncertain:
            flags.append("SPELLING_UNCERTAIN")
        if was_normalized:
            flags.append("SYNONYM_NORMALIZED")
        score = SCORE_SYNONYM_MAX if not was_normalized else SCORE_SYNONYM_MIN
        cat = "SYNONYM_NORMALIZED" if was_normalized else "VALID_ADDRESS"
        res = _blank_result(
            status="PASSED_PRE_API",
            source_api="VALIDATION_RULE",
            derivation_type="OFFLINE_VALIDATION_PASSED",
            category=cat,
            match_classification="VALID_ADDRESS",
            processing_stage="STAGE_4_SYNONYM_NORMALIZATION" if was_normalized else "STAGE_PRE_API_VALIDATION",
            confidence_score=score,
            recommended_geofence_m=geofence_for_score(score),
            flags=flags,
            verification="OFFLINE_PASSED",
            **base_meta
        )
        return res

    # Stages 5 & 6: Proceed with Google Geocoding API
    ctx = AddrCtx(norm_address)
    g = geocode_lookup(client, norm_address)
    ver = verify_geocode(ctx, g)
    g_score, g_cat = geocode_score(g)
    flags: list = []
    if has_spelling_err:
        flags.append("SPELLING_ERROR")
    elif is_uncertain:
        flags.append("SPELLING_UNCERTAIN")

    # Fast path: Stage 6 - Strict Exact Match
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

    # Places search fallback (Stage 5)
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
        ctx.area_words = set(g["area_tokens"])

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
        if trusted and d is not None and cat != "AREA_LEVEL":
            if d > FAR_LIMIT_M.get(lt, FAR_DEFAULT_M):
                score, cat, ev.reason = SCORE_AREA_LEVEL, "AREA_LEVEL", "FAR_FROM_GEOCODE"
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
        res = _poi_result(best, g, ver)
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
            res["typos"] = best["ev"].typos
        elif best["cat"] == "AREA_LEVEL":
            res["place_reject"] = best["ev"].reason or None
    res["variants"] = spelling_variants(ctx, g["formatted_address"]) if g["found"] else []
    res["formatted_address"] = g["formatted_address"]
    res["verification"] = ver["status"]
    res["flags"] = flags

    if res.get("category") == "AREA_LEVEL":
        res["category"] = res["match_classification"] = "LOW_CONFIDENCE"
        res["confidence_score"] = SCORE_LOW_CONFIDENCE

    res["recommended_geofence_m"] = geofence_for_score(res["confidence_score"])
    return res


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
        if res.get("geocoding_api_called"):
            parts.append(f"Suspected spelling mistake in address: '{suspected}' (suggested: '{suggestions}'); "
                         f"the address was still sent to Google, so check the match.")
        else:
            parts.append(f"Suspected spelling mistake in address: '{suspected}' (suggested: '{suggestions}'); "
                         f"validation stopped without API calls.")
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
    elif cat in ("LOW_CONFIDENCE", "INCOMPLETE_ADDRESS"):
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
                parts.append(f"Incomplete address: only {cnt} major word(s){words_str}; requires at least {MIN_MAJOR_WORDS} major words.")
            elif subcat == "ADDRESS_TOO_SHORT":
                parts.append(f"Incomplete address: address length ({chars_cnt} characters) is below the minimum threshold ({MIN_ADDRESS_CHARS} characters).")
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

    completeness = res.get("address_completeness_status") or "COMPLETE"
    issue = res.get("address_issue_category") or "NONE"
    if score_cat == "LOW_CONFIDENCE":
        issue = "UNVERIFIED_MATCH" if "GEOCODE_UNVERIFIED" in flags else "AREA_ONLY_MATCH"

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
        "address_completeness_status": completeness,
        "address_issue_category": issue,
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
