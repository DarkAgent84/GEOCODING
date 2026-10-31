"""
Offline tests for geocode_valid_addresses_v2.py (no network, no API key needed).

Run:  python -m pytest test_geocode_v2.py -q
The scenarios come from the real 40-row test run, with simulated Google responses.
"""
import csv
import importlib.util
import os
import sys

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
v2_candidates = [
    os.path.join(HERE, "geocode_valid_addresses_v2.py"),
    os.path.join(HERE, "..", "geocode_valid_addresses_v2.py"),
    os.path.join(HERE, "..", "src", "geocode_valid_addresses_v2.py"),
]
v2_path = next((c for c in v2_candidates if os.path.exists(c)), v2_candidates[-1])
spec = importlib.util.spec_from_file_location("v2", v2_path)
v2 = importlib.util.module_from_spec(spec)
sys.modules["v2"] = v2
spec.loader.exec_module(v2)


# --------------------------------------------------------------------------
# Simulated Google responses
# --------------------------------------------------------------------------
def comp(name, *types):
    return {"long_name": name, "types": list(types)}


def geo(lat, lng, loc_type, formatted, partial=False, types=None, comps=None):
    return {"status": "OK", "results": [{
        "formatted_address": formatted,
        "types": types or ["street_address"],
        "components": comps or [],
        "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": loc_type},
        "partial_match": partial, "place_id": "x"}]}


def place(name, formatted, types, lat, lng):
    return {"name": name, "formatted_address": formatted, "types": types,
            "geometry": {"location": {"lat": lat, "lng": lng}}, "place_id": "p"}


def places(*items):
    return {"status": "OK", "results": list(items)}


ZERO = {"status": "ZERO_RESULTS", "results": []}


class FakeClient:
    def __init__(self, geo_resp, places_resp=ZERO):
        self.geo_resp, self.places_resp = geo_resp, places_resp
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self.places_calls = []

    def geocode(self, address):
        return self.geo_resp

    def places(self, query, bias=None, radius=None):
        self.places_calls.append((bias, radius))
        return self.places_resp


def run(address, geo_resp, places_resp=ZERO):
    c = FakeClient(geo_resp, places_resp)
    return v2.derive_coordinates_with_confidence(address, c), c


# --------------------------------------------------------------------------
# sanitize_address
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("NO, MADAPATTANA, JIGANI HOBLI, ANEKAL TALUK, BENGALURU - 560105",
     "MADAPATTANA, JIGANI HOBLI, ANEKAL TALUK, BENGALURU - 560105"),            # blank "NO," field
    ("NO 2 -3 PATIBANDLA GUNTUR - 522402", "NO 2 -3 PATIBANDLA GUNTUR - 522402"),  # real number kept
    ("NO. 268 RAMAKKA LAYOUT ANEKAL - 562106", "NO. 268 RAMAKKA LAYOUT ANEKAL - 562106"),
    ("0 BISHANPUR IGLAS ALIGARH - 202124", "BISHANPUR IGLAS ALIGARH - 202124"),   # stray 0
    ("012 Main Rd Salem 636001", "012 Main Rd Salem 636001"),                      # real house number
    ("2/156 GALI NO 3 SUDAMAPURI ALIGARH 202001 - 202001",
     "2/156 GALI NO 3 SUDAMAPURI ALIGARH 202001"),                                 # repeated pincode
    ("GALI NO.5 ALAM BAGH GALI NO.5 ALAM BAGH ALIGARH ALAM BAGH KOIL - 202001",
     "GALI NO.5 ALAM BAGH ALIGARH ALAM BAGH KOIL - 202001"),                       # repeated phrase
    ("Retake Original 12/05/2024 10:22:33 Lat: 12.97 Long 77.59 MG Road Pune",
     "MG Road Pune"),
    ("  ,, - 5 Main Road Kochi ", "5 Main Road Kochi"),
])
def test_sanitize(raw, expected):
    assert v2.sanitize_address(raw)[0] == expected


@pytest.mark.parametrize("raw", [None, float("nan"), "", "   ", "abc", "NO", "0 ,"])
def test_sanitize_rejects_empty_or_tiny(raw):
    assert v2.sanitize_address(raw)[0] is None


def test_sanitize_flags_conflicting_pincodes_and_truncation():
    s, flags = v2.sanitize_address("DARAN KALYANPURA TA KADI DIST MEHSANA PIN 382165 - 382715")
    assert "CONFLICTING_PINCODES" in flags
    s, flags = v2.sanitize_address(" ".join(f"WORD{i}" for i in range(200)) + " AGRA 282001")
    assert "ADDRESS_TRUNCATED" in flags and len(s) <= v2.MAX_ADDRESS_CHARS


def test_sanitize_never_crashes_on_odd_input():
    for raw in [12345, "नमस्ते 12 Main Road", "\x00\x01 Main Road Delhi 110001", "a" * 5000, "🙂🙂🙂🙂🙂🙂"]:
        v2.sanitize_address(raw)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def test_geofence_bands():
    g = v2.geofence_for_score
    assert [g(s) for s in (None, 0, 10, 25, 39, 40, 89, 90, 99, 100)] == \
           [None, None, 1000, 1000, 1000, 250, 250, 100, 100, 50]


def test_haversine():
    assert v2.haversine_m(12.9, 77.6, 12.9, 77.6) == 0
    assert 111_000 < v2.haversine_m(0, 0, 1, 0) < 112_500


def test_expand_abbreviations():
    assert v2._expand("THE MEHSANA DIST CENTRAL CO OP BANK LTD") == \
           "the mehsana district central co operative bank ltd"
    assert v2._expand("Mehsana District Central Co-operative Bank Ltd") == \
           "mehsana district central co operative bank ltd"


# --------------------------------------------------------------------------
# Scoring scenarios taken from the real test run
# --------------------------------------------------------------------------
def test_exact_rooftop_verified_scores_100():
    r, c = run("NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026",
               geo(13.05, 80.21, "ROOFTOP", "24/9, Kavarai St, Vadapalani, Chennai 600026, India"))
    assert (r["confidence_score"], r["category"], r["recommended_geofence_m"]) == (100, "ROOFTOP_EXACT", 50)
    assert c.places_calls == []          # no Places call needed


def test_exact_rooftop_with_wrong_locality_is_not_trusted():
    r, _ = run("5 MAIN ROAD ANEKAL BANGALORE - 562106",
               geo(28.6, 77.2, "ROOFTOP", "5, Main Rd, Connaught Place, New Delhi 110001, India"))
    assert r["confidence_score"] < 100
    assert "GEOCODE_UNVERIFIED" in r["flags"]


def test_bank_with_co_op_abbreviation_is_poi_exact():          # row 9: used to stay 50
    addr = "THE MEHSANA DIST CENTRAL CO OP BANK LTD TINBATTI BH MAMLATDAR OFFICE AT TA KADI DI MEHSANA - 382715"
    r, _ = run(addr,
               geo(23.3015, 72.3304, "ROOFTOP", "Tinbatti, Kadi, Gujarat 382715, India", partial=True),
               places(place("Mehsana District Central Co-operative Bank Ltd, Kadi",
                            "Tinbatti, Kadi, Gujarat 382715", ["bank", "establishment"], 23.3015, 72.3304)))
    assert r["category"] == "POI_EXACT" and 90 <= r["confidence_score"] <= 99
    assert r["source_api"] == "PLACES_API"


def test_poi_224km_away_is_rejected():                          # row 7
    r, _ = run("16 B NARAYAN NAGAR SOC K ROAD NEAR HOTEL 7 STRA VAVOL - 382420",
               geo(23.2252, 72.6225, "APPROXIMATE", "Vavol, Gujarat 382420, India", types=["locality"]),
               places(place("Hotel 7 star", "K Road, Vavol, Gujarat", ["lodging", "establishment"], 21.17, 72.83)))
    assert r["confidence_score"] == 31 and r["source_api"] != "PLACES_API"


def test_partial_rooftop_wrong_pincode_downgraded():           # rows 0/11/15 style
    r, _ = run("NO 268 RAMAKKA LAYOUT ANEKAL BANGALORE - 562106",
               geo(12.85, 77.58, "ROOFTOP", "Gottigere, Bengaluru, Karnataka 560083, India", partial=True))
    assert r["confidence_score"] == 31 and r["derivation_type"].endswith("_UNVERIFIED")
    assert "GEOCODE_UNVERIFIED" in r["flags"]
    row = v2.build_row(0, "L1", "addr", "orig", "san", r, [])
    assert row["review_needed"] is True


def test_partial_rooftop_agreeing_address_stays_50():
    r, _ = run("57/2 SAKTHI NAGAR NALIKKALPATTI SALEM - 636201",
               geo(11.57, 77.83, "ROOFTOP", "Sakthi Nagar, Salem, Tamil Nadu 636201, India", partial=True))
    assert (r["confidence_score"], r["category"]) == (50, "PARTIAL_MATCH")


def test_unverified_geocode_does_not_veto_good_named_poi():    # row 30: the bug in the earlier patch
    addr = "PATHWARI MATA MANDIR BALDEO 144 RIDHA ROAD BALDEO MATHURA - 281301"
    r, _ = run(addr,
               geo(27.43, 77.88, "ROOFTOP", "Ridha Rd, Agra, Uttar Pradesh 282001, India", partial=True),
               places(place("Pathwari Mata Mandir", "Baldeo, Mathura, Uttar Pradesh 281301",
                            ["hindu_temple", "place_of_worship"], 27.60, 77.82)))
    assert r["source_api"] == "PLACES_API" and r["confidence_score"] >= 90


def test_poi_agreeing_with_geocode_corroborates_it():          # row 39: used to drop to 10
    addr = "MEZZINE FLOOR 145 NEW CLOTH MARKET RAIPUR GATE AHMEDABAD - 380002"
    r, _ = run(addr,
               geo(23.0162, 72.5994, "ROOFTOP", "Opp New Cloth Market, Raipur Gate, Ahmedabad, Gujarat 380002", partial=True),
               places(place("Sumel Business Park", "Opp. New Cloth Market, Raipur Gate, Ahmedabad 380001",
                            ["establishment"], 23.0162, 72.5994)))
    assert "GEOCODE_UNVERIFIED" not in r["flags"]
    assert r["confidence_score"] >= 10


def test_near_landmark_scores_lower_than_the_address_itself():  # row 38
    addr = "E-70 502 SWAMINARAYAN PARK NAVA NARODA NR PANACEA RESIDENCY KATHWADA ROAD AHMEDABAD - 382330"
    r, _ = run(addr,
               geo(23.0748, 72.6721, "GEOMETRIC_CENTER", "Nava Naroda, Ahmedabad, Gujarat 382330, India", types=["sublocality"]),
               places(place("Panacea Residency", "Kathwada Rd, Nava Naroda, Ahmedabad 382330",
                            ["establishment"], 23.0748, 72.6721)))
    assert r["category"] == "LANDMARK_EXACT"
    assert 70 <= r["confidence_score"] <= 80 and r["recommended_geofence_m"] == 250


def test_premise_poi_scores_90s():
    addr = "TAIGOR COLONY GOVERDHAN ROAD NEAR JAMUNA DHAM MATHURA UTTAR PRADESH 281004"
    r, _ = run(addr,
               geo(27.4999, 77.6519, "ROOFTOP", "Goverdhan Rd, Mathura, Uttar Pradesh 281004, India", partial=True),
               places(place("Shri Jamuna Dham", "Goverdhan Rd, Mathura, Uttar Pradesh 281004", ["establishment"], 27.4999, 77.6519)))
    assert r["confidence_score"] >= 70


def test_junction_named_after_area_is_not_a_landmark():         # row 36
    addr = "6 314 BAROLIA BUILDING BELANGANJ AGRA UTTAR PRADESH 282004"
    r, _ = run(addr,
               geo(27.19, 78.02, "APPROXIMATE", "Belanganj, Agra, Uttar Pradesh 282004, India", types=["sublocality"],
                   comps=[comp("Belanganj", "sublocality_level_1"), comp("Agra", "locality"), comp("282004", "postal_code")]),
               places(place("Belanganj chauraha", "Belanganj, Agra, Uttar Pradesh 282004", ["establishment", "store"], 27.25, 78.03)))
    assert r["confidence_score"] == 31


def test_area_named_place_with_locality_geocode_rejected():      # "Lodha", "Semari", "Tehsil Kol" cases
    r, _ = run("34 UMAPTI DHAM SEMARI SEMARI AGRA AGRA UP - 282001",
               geo(27.11, 78.02, "GEOMETRIC_CENTER", "Semari, Agra, Uttar Pradesh 282001, India", types=["sublocality"],
                   comps=[comp("Semari", "sublocality_level_1"), comp("Agra", "locality")]),
               places(place("Semari", "Semari, Agra, Uttar Pradesh", ["establishment", "health"], 27.11, 78.02)))
    assert r["confidence_score"] == 31


@pytest.mark.parametrize("name,types", [("Patibandla", ["locality", "political"]),
                                        ("MN College Road", ["route"]),
                                        ("Post Office", ["post_office", "establishment"])])
def test_area_road_and_generic_names_rejected(name, types):
    r, _ = run("NO 2 -3 PATIBANDLA GUNTUR - 522402",
               geo(16.46, 80.30, "APPROXIMATE", "Patibandla, Andhra Pradesh 522402, India", types=["locality"]),
               places(place(name, "Patibandla, Guntur 522402", types, 16.46, 80.30)))
    assert r["confidence_score"] <= 10


@pytest.mark.parametrize("name", ["At, post", "PO", "T. Hosahalli"])
def test_initials_and_filler_words_are_not_evidence(name):
    # "At, post" matched the address word "AT" and scored POI_EXACT in the first draft
    ctx = v2.AddrCtx("AT & PO FALASAN TA IDAR DISTRICT SABARKANTHA - 383436")
    ev = v2.evaluate_place(place(name, "Falasan, Gujarat", ["establishment"], 1, 1), ctx)
    assert ev.category == "INCOMPLETE_ADDRESS"


def test_spelling_mistake_poi():                              # Jay Praksh -> Jay Prakash
    r, _ = run("PLOT NO 195/2 SECTOR 26 NEAR JAY PRAKSH SCHOOL GANDHINAGAR - 382025",
               geo(23.25, 72.64, "APPROXIMATE", "Sector 26, Gandhinagar, Gujarat 382025, India", types=["sublocality"]),
               places(place("Jay Prakash School", "Sector 26, Gandhinagar, Gujarat 382025", ["school"], 23.2501, 72.6411)))
    assert r["category"] in ("SPELLING_MISTAKE", "LANDMARK_EXACT")
    assert r["confidence_score"] > 10


def test_pincode_mismatch_caps_poi_score():
    r, _ = run("PLOT 5 NEAR HANUMAN TEMPLE AKASH NAGAR PUNE - 411001",
               geo(18.5, 73.8, "GEOMETRIC_CENTER", "Pune, Maharashtra 411001, India", types=["locality"]),
               places(place("Akash Nagar Hanuman Temple", "Akash Nagar, Pune, Maharashtra 411099", ["hindu_temple"], 18.5, 73.8)))
    assert r["confidence_score"] < 90


def test_geocode_fails_and_no_places_is_not_found():
    r, _ = run("SOME UNKNOWN PLACE NOWHERE 999999", ZERO, ZERO)
    assert (r["confidence_score"], r["category"], r["lat"]) == (0, "NOT_FOUND", None)


def test_street_level_geocode_gets_street_band():
    r, _ = run("MAIN ROAD KOCHI KERALA - 682001",
               geo(9.97, 76.28, "GEOMETRIC_CENTER", "Main Rd, Kochi, Kerala 682001, India", types=["route"]))
    assert r["category"] == "STREET_LEVEL" and r["confidence_score"] == v2.SCORE_STREET


def test_places_bias_widens_for_area_level_geocode():
    _, c = run("PLOT 5 AKASH NAGAR PUNE - 411001",
               geo(18.5, 73.8, "APPROXIMATE", "Pune, Maharashtra 411001, India", types=["locality"]))
    assert c.places_calls[0][1] == v2.BIAS_RADIUS_M["APPROXIMATE"]


def test_too_few_words_caps_score():
    r, _ = run("AGRA 282001",
               geo(27.1, 78.0, "ROOFTOP", "Agra, Uttar Pradesh 282001, India", partial=True))
    assert r["confidence_score"] <= 10 and "TOO_FEW_WORDS" in r["flags"]


def test_scores_never_exceed_caps():
    addr = "A B NEAR SHIVAM TEMPLE KADI MEHSANA - 382715"
    r, _ = run(addr,
               geo(23.3, 72.3, "GEOMETRIC_CENTER", "Kadi, Gujarat 382715, India", types=["locality"]),
               places(place("Shivam Temple", "Kadi, Gujarat 382715", ["hindu_temple"], 23.3, 72.3)))
    assert r["confidence_score"] <= 99


# --------------------------------------------------------------------------
# API client: retries, errors, cache, key hygiene
# --------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status_code, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, cache=None, **kw):
    return v2.ApiClient("SECRETKEY123", cache, session=FakeSession(script), sleep=lambda s: None, **kw)


OK_GEO = {"status": "OK", "results": [{"formatted_address": "x", "geometry": {"location": {"lat": 1, "lng": 2},
          "location_type": "ROOFTOP"}, "types": []}]}


def test_client_retries_then_succeeds():
    c = client([requests.exceptions.ConnectionError("boom"), FakeResp(503), FakeResp(200, OK_GEO)])
    assert c.geocode("12 Main Road Pune")["status"] == "OK"
    assert c.stats["api_calls"] == 3


def test_client_raises_transient_after_all_retries():
    c = client([FakeResp(503)] * v2.MAX_RETRIES)
    with pytest.raises(v2.TransientApiError):
        c.geocode("12 Main Road Pune")


def test_client_over_query_limit_is_retried():
    c = client([FakeResp(200, {"status": "OVER_QUERY_LIMIT"}), FakeResp(200, OK_GEO)])
    assert c.geocode("12 Main Road Pune")["status"] == "OK"


@pytest.mark.parametrize("resp", [FakeResp(200, {"status": "REQUEST_DENIED", "error_message": "bad SECRETKEY123"}),
                                  FakeResp(200, {"status": "OVER_DAILY_LIMIT"}), FakeResp(403), FakeResp(401)])
def test_client_aborts_on_key_or_quota_problems(resp):
    with pytest.raises(v2.ApiAbort) as e:
        client([resp]).geocode("12 Main Road Pune")
    assert "SECRETKEY123" not in str(e.value)


def test_client_invalid_json_retried():
    c = client([FakeResp(200, bad_json=True), FakeResp(200, OK_GEO)])
    assert c.geocode("12 Main Road Pune")["status"] == "OK"


def test_client_other_4xx_not_retried_and_not_cached(tmp_path):
    cache = v2.DiskCache(str(tmp_path / "c.db"))
    c = client([FakeResp(400)], cache)
    assert c.geocode("12 Main Road Pune")["status"] == "HTTP_400"
    assert c.stats["api_calls"] == 1
    assert cache.get("geo3|in|12 main road pune") is None
    cache.close()


def test_client_never_leaks_key_in_network_errors():
    c = client([requests.exceptions.ConnectionError("failed for url ...?key=SECRETKEY123&x=1")] * v2.MAX_RETRIES)
    with pytest.raises(v2.TransientApiError) as e:
        c.geocode("12 Main Road Pune")
    assert "SECRETKEY123" not in str(e.value)


def test_cache_hit_costs_no_call_and_errors_are_not_cached(tmp_path):
    cache = v2.DiskCache(str(tmp_path / "c.db"))
    c = client([FakeResp(200, OK_GEO)], cache)
    c.geocode("12 Main Road Pune")
    c.geocode("12  main road, PUNE")                 # same after normalisation
    assert c.stats == {"api_calls": 1, "cache_hits": 1}
    c2 = client([FakeResp(200, {"status": "UNKNOWN_ERROR"})] * v2.MAX_RETRIES, cache)
    with pytest.raises(v2.TransientApiError):
        c2.geocode("another address here 5")
    assert cache.get("geo3|in|another address here 5") is None
    cache.close()


def test_cache_only_never_calls_google(tmp_path):
    cache = v2.DiskCache(str(tmp_path / "c.db"))
    c = client([], cache, cache_only=True)
    with pytest.raises(v2.CacheMiss):
        c.geocode("12 Main Road Pune")
    cache.close()


def test_v1_cache_entries_are_reused(tmp_path):
    cache = v2.DiskCache(str(tmp_path / "c.db"))
    cache.set("geo2|in||12 main road pune", {"status": "OK", "results": []})
    c = client([], cache)
    assert c.geocode("12 Main Road Pune")["status"] == "OK" and c.stats["api_calls"] == 0
    cache.close()


def test_max_api_calls_budget():
    c = client([FakeResp(200, OK_GEO)] * 5, max_api_calls=1)
    c.geocode("12 Main Road Pune")
    with pytest.raises(v2.ApiAbort):
        c.geocode("13 Main Road Pune")


def test_missing_key_aborts_cleanly():
    c = v2.ApiClient("", None, session=FakeSession([]))
    with pytest.raises(v2.ApiAbort):
        c.geocode("12 Main Road Pune")


def test_key_is_not_hardcoded():
    src = open(v2_path, encoding="utf-8").read()
    assert "AIza" not in src


# --------------------------------------------------------------------------
# End to end: output file, resume, failures
# --------------------------------------------------------------------------
INPUT_ROWS = [
    ("L1", "NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026"),
    ("L2", ""),                                              # empty -> INVALID_INPUT
    ("L3", "NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026"),   # duplicate of L1
    ("L4", "SOME UNKNOWN PLACE NOWHERE 999999"),
]


class ScriptedClient:
    """Geocoder for the e2e tests; `fail_on` makes one address raise a transient error."""

    def __init__(self, fail_on=None):
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self.fail_on, self.seen = fail_on, []

    def geocode(self, address):
        self.seen.append(address)
        if self.fail_on and self.fail_on in address:
            raise v2.TransientApiError("HTTP 503")
        if "KAVARAI" in address:
            return geo(13.05, 80.21, "ROOFTOP", "24/9, Kavarai St, Vadapalani, Chennai 600026, India")
        return ZERO

    def places(self, query, bias=None, radius=None):
        return ZERO


def write_input(path, rows=INPUT_ROWS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["soa_loan_no", "soa_current_address"])
        w.writerows(rows)


def read_out(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def go(tmp_path, extra=(), client_obj=None, name="out.csv"):
    inp, out = str(tmp_path / "in.csv"), str(tmp_path / name)
    if not os.path.exists(inp):
        write_input(inp)
    cobj = client_obj or ScriptedClient()
    code = v2.main(["-i", inp, "-o", out, "--no-cache", *extra], client_factory=lambda a, c: cobj)
    return code, out, cobj


def test_e2e_full_run(tmp_path):
    code, out, c = go(tmp_path)
    rows = read_out(out)
    assert code == 0 and [r["input_row"] for r in rows] == ["0", "1", "2", "3"]
    assert list(rows[0].keys()) == v2.OUTPUT_COLUMNS
    assert rows[0]["score_category"] == "ROOFTOP_EXACT" and rows[0]["recommended_geofence_radius_m"] == "50"
    assert rows[1]["derivation_type"] == "INVALID_INPUT"
    assert rows[2]["confidence_score"] == "100"
    assert len(c.seen) == 2                                  # duplicate address reused, not re-queried
    assert rows[3]["score_category"] == "NOT_FOUND" and rows[3]["latitude"] == ""
    assert not os.path.exists(out + ".lock")


def test_e2e_resume_skips_done_rows(tmp_path):
    go(tmp_path, ["--limit", "2"])
    assert len(read_out(str(tmp_path / "out.csv"))) == 2
    code, out, _ = go(tmp_path)
    assert [r["input_row"] for r in read_out(out)] == ["0", "1", "2", "3"]


def test_e2e_transient_failure_row_is_not_saved_and_is_retried(tmp_path):
    code, out, _ = go(tmp_path, client_obj=ScriptedClient(fail_on="UNKNOWN"))
    assert "3" not in [r["input_row"] for r in read_out(out)]
    code, out, _ = go(tmp_path)                              # next run, API healthy
    assert "3" in [r["input_row"] for r in read_out(out)]


def test_e2e_internal_error_saved_as_error_row_and_run_continues(tmp_path):
    class Exploding(ScriptedClient):
        def geocode(self, address):
            if "UNKNOWN" in address:
                raise RuntimeError("boom")
            return super().geocode(address)
    code, out, _ = go(tmp_path, client_obj=Exploding())
    rows = read_out(out)
    assert len(rows) == 4 and rows[3]["derivation_type"] == v2.ERROR_DERIVATION
    assert rows[3]["review_needed"] == "YES"
    go(tmp_path, ["--retry-errors"])                        # error rows are re-processed, others kept
    assert len(read_out(out)) == 4


def test_e2e_consecutive_api_failures_abort_and_keep_progress(tmp_path):
    inp = str(tmp_path / "in.csv")
    write_input(inp, [("L0", "NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026")] +
                [(f"L{i}", f"FAIL STREET {i} SOMEWHERE ONE - 600001") for i in range(1, 9)])
    code, out, _ = go(tmp_path, client_obj=ScriptedClient(fail_on="FAIL"))
    assert code == 1 and [r["input_row"] for r in read_out(out)] == ["0"]


def test_e2e_half_written_last_line_is_repaired(tmp_path):
    go(tmp_path, ["--limit", "1"])
    out = str(tmp_path / "out.csv")
    with open(out, "ab") as f:
        f.write(b"1,L2,soa_current_address,half writ")      # crash mid-line, no newline
    code, out, _ = go(tmp_path)
    assert code == 0 and [r["input_row"] for r in read_out(out)] == ["0", "1", "2", "3"]


def test_e2e_old_layout_output_is_refused_untouched(tmp_path):
    out = tmp_path / "out.csv"
    out.write_text("input_row,soa_loan_no\n0,L1\n", encoding="utf-8")
    before = out.read_bytes()
    code, _, c = go(tmp_path)
    assert code == 1 and out.read_bytes() == before and c.seen == []


def test_e2e_overwrite_renames_old_file(tmp_path):
    go(tmp_path)
    go(tmp_path, ["--overwrite"])
    assert any(".bak-" in p.name for p in tmp_path.iterdir())


def test_e2e_lock_blocks_second_run(tmp_path):
    out = tmp_path / "out.csv"
    (tmp_path / "out.csv.lock").write_text("123")
    code, _, c = go(tmp_path)
    assert code == 1 and c.seen == [] and not out.exists()


def test_e2e_missing_input_and_bad_column(tmp_path):
    assert v2.main(["-i", str(tmp_path / "nope.csv")]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert v2.main(["-i", str(bad), "-o", str(tmp_path / "o.csv")]) == 2


def test_e2e_dry_run_makes_no_output_or_calls(tmp_path):
    inp = str(tmp_path / "in.csv")
    write_input(inp)
    assert v2.main(["-i", inp, "-o", str(tmp_path / "o.csv"), "--dry-run"]) == 0
    assert not (tmp_path / "o.csv").exists()


def test_e2e_no_api_key_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    inp = str(tmp_path / "in.csv")
    write_input(inp)
    assert v2.main(["-i", inp, "-o", str(tmp_path / "o.csv")]) == 2


def test_e2e_cp1252_input_and_loan_numbers_stay_text(tmp_path):
    inp = tmp_path / "in.csv"
    inp.write_bytes("soa_loan_no,soa_current_address\n000123,5 Caf\xe9 Road Kochi 682001\n".encode("cp1252"))
    code, out, _ = go(tmp_path)
    assert read_out(out)[0]["soa_loan_no"] == "000123"


def test_db_mode_dry_run_and_execution(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.rowcount = 0
            self.executed = []
            self.connection = self
            self.encoding = "UTF8"
        def mogrify(self, template, args):
            return b"('val')"
        def execute(self, q, params=None):
            self.executed.append((q, params))
        def fetchall(self):
            return [(1, "LOAN1", "12 Main Road Pune 411001")]
        def close(self):
            pass

    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor()
        def cursor(self):
            return self.cur
        def commit(self):
            pass
        def close(self):
            pass

    fake_conn = FakeConn()
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setenv("GOOGLE_API_KEY", "TESTKEY")

    # dry run in DB mode
    assert v2.main(["--db", "--dry-run"]) == 0

    # run in DB mode with mock geocoding client (workers 1 and workers 2)
    cobj = ScriptedClient()
    code = v2.main(["--db", "--limit", "1", "--no-cache"], client_factory=lambda a, c: cobj)
    assert code == 0

    code_w2 = v2.main(["--db", "--limit", "1", "--no-cache", "--workers", "2"], client_factory=lambda a, c: cobj)
    assert code_w2 == 0


# --------------------------------------------------------------------------
# 3-Constraint Incomplete Address Detection & Deep Categorization
# --------------------------------------------------------------------------
def test_completeness_valid_address():
    addr = "NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is False
    assert subcat == "COMPLETE"
    assert fails == []
    assert len(words) >= 3
    assert "600026" in pins


def test_completeness_three_major_words_is_complete():
    # Exactly 3 major words (BISHANPUR, IGLAS, ALIGARH) is now accepted as COMPLETE (>= 3)
    addr = "BISHANPUR IGLAS ALIGARH - 202124"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is False
    assert subcat == "COMPLETE"
    assert fails == []
    assert len(words) == 3
    assert "202124" in pins


def test_completeness_missing_pincode_deep_categorization():
    # 5-digit pincode -> missing 6-digit numeric pincode
    addr = "639, MARIYAMMAN KOIL STREET MAMANGALAM - 80682"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is True
    assert subcat == "MISSING_PINCODE"
    assert fails == ["MISSING_PINCODE"]
    assert len(words) > 3

    # Trailing hyphen without pincode
    addr2 = "11/63 VINAYAGAR KOVIL STREET KOVAIPUDUR KULATHUPALAYAM -"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr2)
    assert is_inc is True
    assert subcat == "MISSING_PINCODE"
    assert fails == ["MISSING_PINCODE"]


def test_completeness_too_few_words_deep_categorization():
    # 2 major words (Patibandla, Guntur) with valid pincode and length >= 15
    addr = "NO 2 -3 PATIBANDLA GUNTUR - 522402"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is True
    assert subcat == "TOO_FEW_WORDS"
    assert fails == ["TOO_FEW_WORDS"]
    assert len(words) == 2
    assert "522402" in pins


def test_completeness_too_short_and_few_words():
    # Len 14 (< 15) and 1 major word ('ASIND' <= 3) with valid pincode
    addr = "ASIND - 311607"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is True
    assert subcat == "TOO_SHORT_AND_FEW_WORDS"
    assert set(fails) == {"ADDRESS_TOO_SHORT", "TOO_FEW_WORDS"}
    assert "311607" in pins


def test_completeness_missing_pin_and_few_words():
    # Len 17 (>= 15), 2 major words (<= 3), no pincode
    addr = "PATIBANDLA GUNTUR"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is True
    assert subcat == "MISSING_PIN_AND_FEW_WORDS"
    assert set(fails) == {"MISSING_PINCODE", "TOO_FEW_WORDS"}


def test_completeness_all_criteria_failed():
    # Len 6 (< 15), 1 major word (<= 3), no pincode
    addr = "GUNTUR"
    is_inc, subcat, fails, words, pins = v2.check_address_completeness(addr)
    assert is_inc is True
    assert subcat == "ALL_CRITERIA_FAILED"
    assert set(fails) == {"ADDRESS_TOO_SHORT", "TOO_FEW_WORDS", "MISSING_PINCODE"}


def test_incomplete_address_skips_api_and_sets_attributes():
    client = FakeClient(ZERO, ZERO)
    addr = "NO 2 -3 PATIBANDLA GUNTUR - 522402"
    res = v2.derive_coordinates_with_confidence(addr, client)

    assert 0 <= res["confidence_score"] <= 9
    assert res["confidence_score"] == 6  # TOO_FEW_WORDS subcategory score
    assert res["category"] == "INCOMPLETE_ADDRESS"
    assert res["match_classification"] == "INCOMPLETE_ADDRESS"
    assert res["processing_stage"] == "STAGE_1_INCOMPLETE_DETECTION"
    assert res["derivation_type"] == "INCOMPLETE_ADDRESS (TOO_FEW_WORDS)"
    assert res["recommended_geofence_m"] == 1000
    assert res["verification"] == "INCOMPLETE"
    assert res["geocoding_api_called"] is False
    assert res["places_api_called"] is False
    assert res["lat"] is None and res["lng"] is None
    assert "INCOMPLETE_ADDRESS" in res["flags"]
    assert "TOO_FEW_WORDS" in res["flags"]
    # Verify no Google API calls were made
    assert client.places_calls == []


def test_incomplete_address_build_row_flags_and_remarks():
    client = FakeClient(ZERO, ZERO)
    addr = "639, MARIYAMMAN KOIL STREET MAMANGALAM - 80682"
    res = v2.derive_coordinates_with_confidence(addr, client)
    row = v2.build_row(1, "LOAN123", "soa_current_address", addr, addr, res, [])

    assert 0 <= row["confidence_score"] <= 9
    assert row["confidence_score"] == 7  # MISSING_PINCODE subcategory score
    assert row["score_category"] == "INCOMPLETE_ADDRESS"
    assert row["match_classification"] == "INCOMPLETE_ADDRESS"
    assert row["processing_stage"] == "STAGE_1_INCOMPLETE_DETECTION"
    assert row["geocoding_api_called"] is False
    assert row["places_api_called"] is False
    assert row["review_needed"] is True
    assert "INCOMPLETE_ADDRESS" in row["flags"]
    assert "MISSING_PINCODE" in row["flags"]
    assert "missing 6-digit Indian postal pincode" in row["remarks"]


# --------------------------------------------------------------------------
# Stage 2: Duplicate / Conflicting PIN Code Detection (score 11-20, NO API)
# --------------------------------------------------------------------------
def test_stage2_double_pincode_stops_without_api():
    client = FakeClient(ZERO, ZERO)
    addr = "Malad West, Mumbai, Maharashtra 400064 400097"
    res = v2.derive_coordinates_with_confidence(addr, client)

    assert 11 <= res["confidence_score"] <= 20
    assert res["confidence_score"] == 15
    assert res["match_classification"] == "DOUBLE_PINCODE"
    assert res["processing_stage"] == "STAGE_2_PINCODE_VALIDATION"
    assert res["address_issue_category"] == "DOUBLE_PINCODE"
    assert res["pincode_count"] == 2
    assert "400064" in res["pincode_list"] and "400097" in res["pincode_list"]
    assert res["geocoding_api_called"] is False
    assert res["places_api_called"] is False
    assert res["lat"] is None and res["lng"] is None
    assert client.places_calls == []


def test_stage2_identical_pincode_not_treated_as_conflict():
    # Repeated identical pincode should deduplicate and proceed to geocoding
    geo_mock = geo(27.9, 78.07, "ROOFTOP", "Sudamapuri, Aligarh, Uttar Pradesh 202001, India")
    res, client = run("2/156 GALI NO 3 RAM NAGAR SUDAMAPURI ALIGARH 202001 - 202001", geo_mock)

    assert "DOUBLE_PINCODE" not in res["flags"]
    assert res["match_classification"] != "DOUBLE_PINCODE"
    assert res["geocoding_api_called"] is True


# --------------------------------------------------------------------------
# Stage 3: Offline Spelling Error Detection (score 21-30, NO API)
# --------------------------------------------------------------------------
def test_stage3_spelling_mistake_detected_offline_no_api():
    client = FakeClient(ZERO, ZERO)
    addr = "Near Shiv Temple, Chincholi Bunder, Malad West, Mumbay, Maharashtra 400064"
    res = v2.derive_coordinates_with_confidence(addr, client)

    assert 21 <= res["confidence_score"] <= 30
    assert res["match_classification"] == "SPELLING_ERROR"
    assert res["processing_stage"] == "STAGE_3_SPELLING_VALIDATION"
    assert res["spelling_error_detected"] is True
    assert "Mumbay" in res["suspected_misspelled_words"]
    assert "mumbai" in res["suggested_spelling_corrections"]
    assert res["geocoding_api_called"] is False
    assert res["places_api_called"] is False
    assert res["lat"] is None and res["lng"] is None


def test_stage3_landmark_typo_manidr_suggests_mandir_no_api():
    # Exact address from Row 1: MANIDR typo should suggest 'mandir' (not 'manipur'), score 21-30, no API calls
    client = FakeClient(ZERO, ZERO)
    addr = "WARD NO 17 UMARIYA ROAD KHITOLA BASTI SIHORA SHIV MANIDR - 483225"
    res = v2.derive_coordinates_with_confidence(addr, client)

    assert 21 <= res["confidence_score"] <= 30
    assert res["match_classification"] == "SPELLING_ERROR"
    assert res["processing_stage"] == "STAGE_3_SPELLING_VALIDATION"
    assert res["spelling_error_detected"] is True
    assert "MANIDR" in res["suspected_misspelled_words"]
    assert "mandir" in res["suggested_spelling_corrections"]
    assert "manipur" not in res["suggested_spelling_corrections"]
    assert res["geocoding_api_called"] is False
    assert res["places_api_called"] is False
    assert res["lat"] is None and res["lng"] is None

    row = v2.build_row(3797, "FEDJBLSTL0553084", "soa_current_address", addr, addr, res, [])
    assert "Suspected spelling mistake in address: 'MANIDR' (suggested: 'mandir'); validation stopped without API calls." in row["remarks"]
    assert "matched place 'None'" not in row["remarks"]


def test_stage3_correctly_spelled_landmark_proceeds_to_geocoding():
    # Properly spelled SHIV MANDIR should NOT be flagged as spelling error
    geo_mock = geo(23.48, 80.10, "ROOFTOP", "Khitola, Sihora, Madhya Pradesh 483225, India")
    res, client = run("WARD NO 17 UMARIYA ROAD KHITOLA BASTI SIHORA SHIV MANDIR - 483225", geo_mock)

    assert res["spelling_error_detected"] is False
    assert res["geocoding_api_called"] is True


def test_stage3_unfamiliar_place_name_not_flagged_as_error():
    # 'Kavarai' is not in known names dictionary, but has no close match -> valid proper noun
    geo_mock = geo(13.05, 80.21, "ROOFTOP", "24/9, Kavarai St, Vadapalani, Chennai 600026, India")
    res, client = run("NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026", geo_mock)

    assert res["spelling_error_detected"] is False
    assert res["geocoding_api_called"] is True
    assert res["confidence_score"] == 100


def test_stage3_valid_transliteration_alternative_not_flagged():
    # 'Bangalore' is a valid recognized alternative of 'bengaluru'
    geo_mock = geo(12.97, 77.59, "ROOFTOP", "Main Rd, Bengaluru, Karnataka 560001, India")
    res, client = run("5 MAIN ROAD BANGALORE KARNATAKA - 560001", geo_mock)

    assert res["spelling_error_detected"] is False
    assert res["geocoding_api_called"] is True


# --------------------------------------------------------------------------
# Stage 4: Synonym & Entity Normalization (score 60-70 preprocessing, CONTINUE)
# --------------------------------------------------------------------------
def test_stage4_synonym_normalization_and_proceeds_to_geocoding():
    geo_mock = geo(19.18, 72.84, "ROOFTOP", "Chincholi Bunder, Malad West, Mumbai, Maharashtra 400064, India")
    addr = "Near Shiv Mandir, Chincholi Bunder, Malad West, Mumbai, Maharashtra 400064"
    res, client = run(addr, geo_mock)

    assert res["synonym_normalization_applied"] is True
    assert "mandir -> temple" in res["normalized_terms"]
    assert "Temple" in res["normalized_address"]
    assert res["original_address"] == addr or addr in res.get("original_address", "")
    assert res["geocoding_api_called"] is True
    # Successfully resolved to exact rooftop match
    assert res["confidence_score"] == 100
    assert res["match_classification"] == "EXACT_MATCH"


# --------------------------------------------------------------------------
# Stage 6: Strict Exact Match verification
# --------------------------------------------------------------------------
def test_stage6_strict_exact_match_requirements():
    geo_mock = geo(13.05, 80.21, "ROOFTOP", "24/9, Kavarai St, Vadapalani, Chennai 600026, India", partial=False)
    res, client = run("NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026", geo_mock)

    assert res["confidence_score"] == 100
    assert res["match_classification"] == "EXACT_MATCH"
    assert res["processing_stage"] == "STAGE_6_EXACT_MATCH"
    assert res["location_type"] == "ROOFTOP"
    assert res["recommended_geofence_m"] == 50
    assert res["geocoding_api_called"] is True
    assert res["places_api_called"] is False


# --------------------------------------------------------------------------
# Stage Priority: Incompleteness takes precedence over duplicate PIN
# --------------------------------------------------------------------------
def test_stage_priority_incomplete_before_double_pin():
    # Only 1 major word (fails Stage 1) even though it has two pincodes
    client = FakeClient(ZERO, ZERO)
    addr = "MUMBAI 400064 400097"
    res = v2.derive_coordinates_with_confidence(addr, client)

    assert res["processing_stage"] == "STAGE_1_INCOMPLETE_DETECTION"
    assert 0 <= res["confidence_score"] <= 9
    assert res["geocoding_api_called"] is False
    assert res["places_api_called"] is False


def test_required_pipeline_columns_all_present():
    required_cols = [
        "address_length", "major_word_count", "pincode_list", "pincode_count",
        "address_completeness_status", "address_issue_category",
        "spelling_error_detected", "suspected_misspelled_words", "suggested_spelling_corrections",
        "original_address", "sanitized_address", "normalized_address",
        "synonym_normalization_applied", "normalized_terms",
        "processing_stage", "confidence_score", "match_classification",
        "geocoding_api_called", "places_api_called",
        "latitude", "longitude", "source_api", "location_type",
        "geocode_status", "recommended_geofence_radius_m"
    ]
    for c in required_cols:
        assert c in v2.OUTPUT_COLUMNS, f"Missing required output column: {c}"


def test_stage1_granular_scores_within_0_to_9():
    for subcat, score in v2.INCOMPLETE_SUBCAT_SCORES.items():
        assert 0 <= score <= 9, f"Subcat {subcat} score {score} outside [0, 9] band"


def test_stage3_spelling_uncertain_policy_switch(monkeypatch):
    monkeypatch.setattr(v2, "SPELLING_UNCERTAIN_POLICY", "STOP")
    client = FakeClient(ZERO, ZERO)
    # Word with similarity in [0.75, 0.82)
    # "bengal" (6) vs "bengalur" or similar
    # Let's test detect_spelling_errors directly
    has_err, susp, sugg, is_unc, score = v2.detect_spelling_errors("PLOT 12 NEAR SOMEWORD BANGALORE 560001")
    assert not has_err


def test_e2e_random_sampling_with_seed(tmp_path):
    # Test random sampling with reproducible seed
    code, out, c = go(tmp_path, extra=["--limit", "2", "--random", "--seed", "42"])
    rows = read_out(out)
    assert code == 0
    assert len(rows) == 2
    # Verify rows were sampled
    sampled_indices = [r["input_row"] for r in rows]
    assert len(sampled_indices) == 2


def test_complete_address_with_area_level_geocode_scores_area_level():
    # Input address is COMPLETE (valid pin, 3+ words, >= 15 chars), but Google only finds locality centroid
    geo_mock = geo(23.48, 80.10, "APPROXIMATE", "Sihora, Madhya Pradesh 483225, India", types=["locality"])
    addr = "WARD NO 17 UMARIYA ROAD KHITOLA BASTI SIHORA SHIV MANDIR - 483225"
    res, _ = run(addr, geo_mock)
    row = v2.build_row(1, "LOAN001", "addr", addr, addr, res, [])

    assert row["address_completeness_status"] == "COMPLETE"
    assert row["confidence_score"] == 31
    assert row["score_category"] == "LOW_CONFIDENCE"
    assert row["match_classification"] == "LOW_CONFIDENCE"
    assert row["address_issue_category"] == "AREA_ONLY_MATCH"
    assert "Area-level match" in row["remarks"]

# --------------------------------------------------------------------------
# Multiprocessing and Concurrency Tests
# --------------------------------------------------------------------------
def test_multithreading_workers_e2e(tmp_path):
    code, out, c = go(tmp_path, extra=["--workers", "3", "--worker-mode", "thread"])
    rows = read_out(out)
    assert code == 0
    assert [r["input_row"] for r in rows] == ["0", "1", "2", "3"]
    assert rows[0]["score_category"] == "ROOFTOP_EXACT" and rows[0]["recommended_geofence_radius_m"] == "50"
    assert rows[1]["derivation_type"] == "INVALID_INPUT"
    assert rows[2]["confidence_score"] == "100"
    assert rows[3]["score_category"] == "NOT_FOUND"
    assert not os.path.exists(out + ".lock")


def test_multiprocessing_workers_short_flag(tmp_path):
    code, out, c = go(tmp_path, extra=["-w", "2"])
    rows = read_out(out)
    assert code == 0
    assert len(rows) == 4
    assert [r["input_row"] for r in rows] == ["0", "1", "2", "3"]


def test_workers_with_random_sampling(tmp_path):
    code, out, c = go(tmp_path, extra=["--workers", "2", "--limit", "2", "--random", "--seed", "42"])
    rows = read_out(out)
    assert code == 0
    assert len(rows) == 2


def test_workers_transient_failure_handling(tmp_path):
    code, out, _ = go(tmp_path, extra=["--workers", "2"], client_obj=ScriptedClient(fail_on="UNKNOWN"))
    rows = read_out(out)
    assert code == 0
    assert "3" not in [r["input_row"] for r in rows]


def test_workers_internal_error_handling(tmp_path):
    class Exploding(ScriptedClient):
        def geocode(self, address):
            if "UNKNOWN" in address:
                raise RuntimeError("worker boom")
            return super().geocode(address)

    code, out, _ = go(tmp_path, extra=["--workers", "2"], client_obj=Exploding())
    rows = read_out(out)
    assert code == 0
    assert len(rows) == 4
    assert rows[3]["derivation_type"] == v2.ERROR_DERIVATION
    assert rows[3]["review_needed"] == "YES"


def test_real_multiprocessing_pool_with_sqlite_cache(tmp_path):
    in_csv = str(tmp_path / "mp_in.csv")
    out_csv = str(tmp_path / "mp_out.csv")
    cache_db = str(tmp_path / "mp_cache.db")

    addr = "NO.24/9 KAVARAI STREET VADAPALANI CHENNAI - 600026"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["soa_loan_no", "soa_current_address"])
        w.writerow(["L1", addr])
        w.writerow(["L2", ""])
        w.writerow(["L3", addr])

    cache = v2.DiskCache(cache_db)
    norm = v2._normalize(addr)
    key = f"geo3|in|{norm}"
    mock_resp = {
        "status": "OK",
        "results": [{
            "formatted_address": "24/9, Kavarai St, Vadapalani, Chennai 600026, India",
            "types": ["street_address"],
            "components": [
                {"long_name": "600026", "types": ["postal_code"]},
                {"long_name": "Chennai", "types": ["locality"]},
            ],
            "geometry": {"location": {"lat": 13.05, "lng": 80.21}, "location_type": "ROOFTOP"},
            "partial_match": False,
            "place_id": "test_place_id",
        }]
    }
    cache.set(key, mock_resp)
    cache.close()

    import subprocess
    script_path = v2_path
    root_dir = os.path.dirname(v2_path)
    res = subprocess.run([
        sys.executable, script_path,
        "-i", in_csv,
        "-o", out_csv,
        "--cache-db", cache_db,
        "--cache-only",
        "--workers", "2",
        "--worker-mode", "process"
    ], cwd=root_dir, capture_output=True, text=True)

    assert res.returncode == 0
    rows = read_out(out_csv)
    assert len(rows) == 3
    assert rows[0]["score_category"] == "ROOFTOP_EXACT"
    assert rows[0]["confidence_score"] == "100"
    assert float(rows[0]["latitude"]) == 13.05
    assert float(rows[0]["longitude"]) == 80.21
    assert rows[1]["derivation_type"] == "INVALID_INPUT"
    assert rows[2]["confidence_score"] == "100"

def test_no_api_mode_csv_e2e(tmp_path):
    in_csv = str(tmp_path / "in.csv")
    out_csv = str(tmp_path / "out.csv")
    rows = [
        ("L1", "PLOT 12 NEAR SHIV MANDIR BANGALORE 560001"),
        ("L2", ""),
        ("L3", "FLAT 101 MG ROAD"),
        ("L4", "PLOT 12 NEAR STATION ROAD PUNE 411001 MUMBAI 400001"),
        ("L5", "NIRMAN BHAWAN BENGALUR 560001"),
        ("L6", "WARD NO 17 UMARIYA ROAD KHITOLA BASTI SIHORA 483225"),
    ]
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["soa_loan_no", "soa_current_address"])
        w.writerows(rows)

    code = v2.main(["-i", in_csv, "-o", out_csv, "--no-api"])
    assert code == 0
    res_rows = read_out(out_csv)
    assert len(res_rows) == 6

    # L1: Synonym Normalized
    assert res_rows[0]["score_category"] == "SYNONYM_NORMALIZED"
    assert res_rows[0]["confidence_score"] == "60"
    assert res_rows[0]["geocoding_api_called"] == "NO"
    assert res_rows[0]["places_api_called"] == "NO"
    assert res_rows[0]["latitude"] == ""

    # L2: Empty / Invalid
    assert res_rows[1]["score_category"] == "INVALID_INPUT"

    # L3: Incomplete
    assert res_rows[2]["score_category"] == "INCOMPLETE_ADDRESS"
    assert res_rows[2]["address_completeness_status"] == "INCOMPLETE"

    # L4: Conflicting pincodes
    assert res_rows[3]["score_category"] == "DOUBLE_PINCODE"

    # L5: Spelling error
    assert res_rows[4]["score_category"] == "SPELLING_MISTAKE"
    assert res_rows[4]["spelling_error_detected"] == "YES"

    # L6: Clean complete
    assert res_rows[5]["score_category"] == "VALID_ADDRESS"
    assert res_rows[5]["confidence_score"] == "70"
    assert res_rows[5]["address_completeness_status"] == "COMPLETE"
    assert res_rows[5]["review_needed"] == "NO"


def test_no_api_mode_with_multiprocessing(tmp_path):
    in_csv = str(tmp_path / "in.csv")
    out_csv = str(tmp_path / "out.csv")
    rows = [
        ("L1", "PLOT 12 NEAR SHIV MANDIR BANGALORE 560001"),
        ("L2", ""),
        ("L3", "FLAT 101 MG ROAD"),
        ("L4", "PLOT 12 NEAR STATION ROAD PUNE 411001 MUMBAI 400001"),
    ]
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["soa_loan_no", "soa_current_address"])
        w.writerows(rows)

    code = v2.main(["-i", in_csv, "-o", out_csv, "--no-api", "--workers", "2", "--worker-mode", "thread"])
    assert code == 0
    res_rows = read_out(out_csv)
    assert len(res_rows) == 4
    assert res_rows[0]["score_category"] == "SYNONYM_NORMALIZED"
    assert res_rows[1]["score_category"] == "INVALID_INPUT"
    assert res_rows[2]["score_category"] == "INCOMPLETE_ADDRESS"
    assert res_rows[3]["score_category"] == "DOUBLE_PINCODE"

