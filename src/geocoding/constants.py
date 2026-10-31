"""
Constants, vocabulary dictionaries, score bands, exception types, and evaluation data classes.
"""
from __future__ import annotations

import difflib
import functools
import re
import unicodedata
from collections import Counter

SCRIPT_VERSION = "2.0"

# --------------------------------------------------------------------------
# Google Maps API Endpoints and Environment
# --------------------------------------------------------------------------
API_KEY_ENV = "GOOGLE_API_KEY"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"

# --------------------------------------------------------------------------
# Network Configuration Defaults
# --------------------------------------------------------------------------
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 15
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
MAX_CONSECUTIVE_ERRORS = 5
MAX_CONSECUTIVE_INTERNAL_ERRORS = 10
DEFAULT_WORKERS = 4
DEFAULT_MAX_QPS = 10.0
MAX_ADDRESS_CHARS = 300
DEFAULT_CACHE_MAX_AGE_DAYS = 30

# --------------------------------------------------------------------------
# Score Bands & Radii
# --------------------------------------------------------------------------
SCORE_ROOFTOP = 100
SCORE_POI_PREMISE_BASE = 90
SCORE_POI_MAX = 99
SCORE_LANDMARK = 70
SCORE_LANDMARK_MAX = 80
SCORE_SYNONYM_MIN, SCORE_SYNONYM_MAX = 60, 70
SCORE_PARTIAL = 50
SCORE_INTERPOLATED = 45
SCORE_STREET = 35
SCORE_AREA_LEVEL = 10               # internal ranking value for area-level results
SCORE_INCOMPLETE = 10               # Post-API incomplete / unresolved fallback
SCORE_LOW_CONFIDENCE = 31           # complete address with only an area-level / untrusted result
SCORE_SPELLING_MIN, SCORE_SPELLING_MAX = 21, 30
SCORE_LANDMARK_FUZZY = SCORE_SPELLING_MIN
SCORE_DOUBLE_PIN_MIN, SCORE_DOUBLE_PIN_MAX = 11, 20
SCORE_INCOMPLETE_MIN, SCORE_INCOMPLETE_MAX = 0, 9

# Stage 1 Subcategory Scores
INCOMPLETE_SUBCAT_SCORES = {
    "ALL_CRITERIA_FAILED": 0,
    "MISSING_PIN_AND_TOO_SHORT": 2,
    "MISSING_PIN_AND_FEW_WORDS": 3,
    "TOO_SHORT_AND_FEW_WORDS": 4,
    "ADDRESS_TOO_SHORT": 5,
    "TOO_FEW_WORDS": 6,
    "MISSING_PINCODE": 7,
}

# --------------------------------------------------------------------------
# Name Matching & Spelling Thresholds
# --------------------------------------------------------------------------
SPELLING_SIMILARITY_MIN = 0.80
SINGLE_WORD_SPELLING_MIN = 0.90
MIN_TOKEN_SIMILARITY = 0.70

SPELLING_ERROR_THRESHOLD = 0.82
SPELLING_UNCERTAIN_MIN = 0.75
SPELLING_ERROR_POLICY = "STOP"
SPELLING_UNCERTAIN_POLICY = "PROCEED"

# Incomplete address detection constraints
MIN_ADDRESS_CHARS = 15
MIN_MAJOR_WORDS = 3
STOPWORDS = {
    "the", "of", "and", "in", "at", "to", "for", "from",
    "near", "nr", "opp", "opposite", "behind", "beside",
}

# Geometry thresholds
PRECISE_GEOCODE_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}
FAR_LIMIT_M = {
    "ROOFTOP": 10_000,
    "RANGE_INTERPOLATED": 10_000,
    "GEOMETRIC_CENTER": 15_000,
    "APPROXIMATE": 25_000
}
FAR_DEFAULT_M = 25_000
BIAS_RADIUS_M = {
    "ROOFTOP": 5_000,
    "RANGE_INTERPOLATED": 5_000,
    "GEOMETRIC_CENTER": 10_000,
    "APPROXIMATE": 20_000
}
BIAS_DEFAULT_M = 20_000
AGREE_M = 300
CORROBORATE_M = 300
DISAGREE_CAP_M = 2_000
MAX_PLACES_CANDIDATES = 5

# --------------------------------------------------------------------------
# Output Schema & Flags
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

REVIEW_FLAGS = {
    "GEOCODE_UNVERIFIED", "PIN_MISMATCH", "PIN_MISMATCH_CORROBORATED",
    "POI_PIN_MISMATCH", "POI_GEOCODE_DISAGREE", "DOUBLE_PINCODE", "MULTIPLE_PINCODES",
    "CONFLICTING_PINCODES", "ADDRESS_TRUNCATED", "INCOMPLETE_ADDRESS",
    "MISSING_PINCODE", "TOO_FEW_WORDS", "ADDRESS_TOO_SHORT", "SPELLING_ERROR",
    "SPELLING_UNCERTAIN", ERROR_DERIVATION
}

# --------------------------------------------------------------------------
# Custom Pipeline Exceptions
# --------------------------------------------------------------------------
class ApiAbort(Exception):
    """The run must stop (bad key, quota exhausted, budget reached, repeated failures)."""

class TransientApiError(Exception):
    """An API call kept failing (network / rate limit); the row should be retried later."""

class CacheMiss(Exception):
    """--cache-only and the answer is not in the cache."""

class LockError(Exception):
    """Another run holds the output file."""

# --------------------------------------------------------------------------
# Vocabulary Sets
# --------------------------------------------------------------------------
_ABBREVIATIONS = {
    "dist": "district", "soc": "society", "govt": "government",
    "hosp": "hospital", "apt": "apartment", "bldg": "building"
}

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

JUNCTION_WORDS = {
    "chowk", "chauraha", "tiraha", "circle", "crossing", "naka", "gate", "stand",
    "stop", "bus", "railway", "station", "bazaar", "market", "cross", "junction",
    "square", "flyover", "bridge"
}

COMMON_DEITY_WORDS = {
    "shiv", "shankar", "mahadev", "hanuman", "bajrang", "ram", "krishna", "ganesh", "balaji",
    "durga", "kali", "mata", "devi", "sai", "baba", "bhairav", "shani", "ambe", "ambaji",
}

AREA_ONLY_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "sublocality_level_3", "neighborhood", "political", "postal_code", "colloquial_area",
    "country", "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "administrative_area_level_4",
}
AREA_PLACE_TYPES = AREA_ONLY_TYPES | {"route"}

LANDMARK_INTRO_WORDS = {
    "near", "nr", "nearby", "opp", "opposite", "behind", "bh", "beside",
    "front", "infront", "samne", "pass", "paas", "adjacent", "adj", "next"
}
POSTPOSITION_WORDS = {
    "samne", "same", "paas", "pass", "pase", "baju", "bajuma", "bagal",
    "wali", "wale", "wala"
}
LANDMARK_WORDS = LANDMARK_INTRO_WORDS | POSTPOSITION_WORDS

SYNONYM_DICT = {
    # Religious / Landmarks
    "mandir": "temple", "mandiram": "temple", "mandeer": "temple", "mandiir": "temple",
    "devasthan": "temple", "devalaya": "temple", "shivalaya": "temple", "thakurbari": "temple",
    "kovil": "temple", "koil": "temple", "masjid": "mosque", "majid": "mosque",
    "masjeed": "mosque", "girja": "church", "gurudwara": "gurdwara", "gurudawara": "gurdwara",
    # Road / Thoroughfare
    "rd": "road", "st": "street", "strt": "street", "ave": "avenue", "av": "avenue",
    "mrg": "marg", "ln": "lane", "blvd": "boulevard", "rasta": "road", "salai": "road",
    "sarani": "road", "byepass": "bypass",
    # Buildings / Housing
    "bldg": "building", "bldng": "building", "apt": "apartment", "apts": "apartments",
    "appartments": "apartments", "appartment": "apartment", "soc": "society", "cmplx": "complex",
    "cplx": "complex", "hsg": "housing", "clny": "colony", "col": "colony", "twr": "tower",
    "twrs": "towers", "ht": "heights", "hts": "heights", "encl": "enclave", "vil": "village",
    "vill": "village",
    # Institutions / Commerce
    "hosp": "hospital", "haspatal": "hospital", "aspatal": "hospital", "disp": "dispensary",
    "univ": "university", "coll": "college", "inst": "institute", "acad": "academy",
    "sch": "school", "vid": "vidyalaya", "vidy": "vidyalaya", "stn": "station", "rly": "railway",
    "po": "post office", "ps": "police station", "mkt": "market", "bazar": "bazaar",
    "shwrm": "showroom", "ltd": "limited", "pvt": "private",
    # Administrative
    "govt": "government", "dist": "district", "dt": "district", "tal": "taluka", "tk": "taluk",
    "teh": "tehsil", "sec": "sector", "blk": "block", "flr": "floor",
    # Orientations / Directions
    "opp": "opposite", "nr": "near", "adj": "adjacent", "bh": "behind",
}

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

_REJECT_TEXT = {
    "AREA_TYPE": "it is an area or road, not a specific place",
    "NAME_MISMATCH": "its name does not match the address",
    "BARE_NAME": "its name is only a bare area/junction name",
    "GENERIC_OR_AREA_NAME": "its name is generic or just the area name",
    "WRONG_LOCALITY": "it is in a different locality",
    "FAR_FROM_GEOCODE": "it is too far from the geocoded point",
}

# --------------------------------------------------------------------------
# Data Classes
# --------------------------------------------------------------------------
class PlaceEval:
    """Verdict on one Places result."""
    def __init__(self, score, category, ratio=None, role=None, reason="", pin_match=None,
                 usable_area=False, flags=None, typos=None):
        self.typos = list(typos or [])
        self.score = score
        self.category = category
        self.ratio = ratio
        self.role = role
        self.reason = reason
        self.pin_match = pin_match
        self.usable_area = usable_area
        self.flags = list(flags or [])


class AddrCtx:
    """Pre-computed views of one address, shared by every comparison."""
    def __init__(self, address: str):
        # Local import to prevent circular dependency
        from .text_utils import _expand, _normalize, _pincodes
        self.text = _expand(address)
        self.tokens = self.text.split()
        self.pins = _pincodes(address)
        self.locality_words = [t for t in self.tokens
                               if len(t) >= 4 and not t.isdigit()
                               and t not in POI_GENERIC_WORDS and t not in LANDMARK_WORDS]
        self.meaningful = [t for t in self.tokens
                           if len(t) >= 3 and any(c.isalpha() for c in t)
                           and t not in POI_GENERIC_WORDS and t not in LANDMARK_WORDS]
        self.area_words: set = set()


class RunTally:
    """Counters for one run; only ever touched by the main thread."""
    def __init__(self):
        self.saved = 0
        self.done = 0
        self.skipped_api = 0
        self.skipped_cache = 0
        self.review = 0
        self.categories = Counter()
