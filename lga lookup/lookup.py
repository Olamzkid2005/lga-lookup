# =================================================================
# Nigeria LGA Geocoder  v6
# - Deduplicates: 14k rows resolved as ~4.6k unique addresses
# - State inference from address text (road-name false-positive guard)
# - Pre-compiled state inference patterns (no per-call recompile)
# - Smarter address splitting: digit/letter AND known-suffix boundaries
# - Orphan digit/noise stripping after phase/unit removal
# - Inferred state used as primary hint (overrides wrong STATE col)
# - Expanded keyword table: 250+ entries, all 36 states + FCT
# - PH abbreviation → Port Harcourt
# - Expanded stop-word list for token extraction
# - NaN guard in keyword_lga_lookup
# - 429 backoff, deduplication, periodic cache save
# =================================================================
#
# USAGE:
#   python lookup.py --input your_file.xlsx
#   python lookup.py --input data.csv --output results.xlsx
#
# DEPS:  pip install pandas requests tqdm openpyxl

import argparse
import json
import logging
import os
import re
import sys
import time

import pandas as pd
import requests
from tqdm import tqdm

# ── CLI ───────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Nigeria LGA Geocoder v6")
parser.add_argument("--input",  required=True, help="Input CSV or XLSX file")
parser.add_argument("--output", default=None,  help="Output file (default: <input>_LGA.<ext>)")
parser.add_argument("--sheet",  default=0,     help="Sheet name/index for XLSX (default: 0)")
args = parser.parse_args()

INPUT_FILE = args.input
_ext = os.path.splitext(INPUT_FILE)[1].lower()
if _ext not in (".csv", ".xlsx", ".xls"):
    print(f"Unsupported file type '{_ext}'. Use .csv or .xlsx")
    sys.exit(1)

OUTPUT_FILE = args.output or os.path.splitext(INPUT_FILE)[0] + "_LGA" + _ext

# ── Config ────────────────────────────────────────────────────────────

CACHE_FILE  = "lga_cache.json"
DELAY       = 1.1
MAX_RETRIES = 3
USER_AGENT  = "NigeriaLGAGeocoder/6.0 (research project)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("geocoder.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── State normalisation ───────────────────────────────────────────────

_STATE_MAP: dict[str, str] = {
    "fct": "Federal Capital Territory",
    "abuja": "Federal Capital Territory",
    "federal capital territory": "Federal Capital Territory",
    "lagos": "Lagos",         "lag": "Lagos",
    "ogun": "Ogun",           "ondo": "Ondo",
    "oyo": "Oyo",             "osun": "Osun",       "oshun": "Osun",
    "ekiti": "Ekiti",         "kwara": "Kwara",      "kogi": "Kogi",
    "rivers": "Rivers",       "river": "Rivers",
    "delta": "Delta",         "edo": "Edo",
    "bayelsa": "Bayelsa",
    "akwa ibom": "Akwa Ibom", "akwaibom": "Akwa Ibom", "akwa-ibom": "Akwa Ibom",
    "cross river": "Cross River", "crossriver": "Cross River",
    "anambra": "Anambra",     "enugu": "Enugu",
    "imo": "Imo",             "abia": "Abia",        "ebonyi": "Ebonyi",
    "kano": "Kano",           "kaduna": "Kaduna",    "katsina": "Katsina",
    "kebbi": "Kebbi",         "sokoto": "Sokoto",    "zamfara": "Zamfara",
    "jigawa": "Jigawa",
    "niger": "Niger",         "plateau": "Plateau",
    "nasarawa": "Nasarawa",   "nassarawa": "Nasarawa", "nassarrawa": "Nasarawa",
    "benue": "Benue",         "taraba": "Taraba",
    "adamawa": "Adamawa",     "gombe": "Gombe",      "bauchi": "Bauchi",
    "yobe": "Yobe",           "borno": "Borno",
    "yola": "Adamawa",
}

_ALL_CANONICAL_STATES = set(_STATE_MAP.values())
_STATE_SUFFIX_RE = re.compile(r"\s+state\s*$", re.IGNORECASE)

# Road/street phrases that contain state words but are NOT state references
_ROAD_FALSE_POSITIVES = re.compile(
    r"\b(new lagos road|lagos road|old lagos|kano road|imo road|"
    r"delta road|edo road|oyo road|rivers road|enugu road|abia road|"
    r"niger road|niger bridge|cross river road|aminu kano crescent|"
    r"aminu kano|ahmadu bello way|yakubu gowon way)\b",
    re.IGNORECASE,
)

# Pre-compiled state inference patterns (longest first, avoids per-call recompile)
_STATE_INFER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + re.escape(s.lower()) + r"\b", re.IGNORECASE), s)
    for s in sorted(_ALL_CANONICAL_STATES, key=len, reverse=True)
] + [
    (re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE), canonical)
    for alias, canonical in sorted(_STATE_MAP.items(), key=lambda x: len(x[0]), reverse=True)
    if len(alias) >= 3
]


def normalise_state(raw: str) -> str:
    """Canonical state name from raw input, tolerating typos/abbreviations."""
    if not raw or str(raw).strip().lower() in ("nan", "", "none"):
        return ""
    s = _STATE_SUFFIX_RE.sub("", str(raw).strip())
    key = s.lower()
    if key in _STATE_MAP:
        return _STATE_MAP[key]
    for alias, canonical in _STATE_MAP.items():
        if alias in key:
            return canonical
    return s.title()


def infer_state_from_address(address: str) -> str:
    """
    Detect a Nigerian state name embedded in the address text.
    Uses pre-compiled patterns. Guards against road-name false positives.
    Returns canonical state name or empty string.
    """
    scrubbed = _ROAD_FALSE_POSITIVES.sub(" ", address)
    for pattern, state in _STATE_INFER_PATTERNS:
        if pattern.search(scrubbed):
            return state
    return ""


# ── Keyword → LGA table ───────────────────────────────────────────────

KEYWORD_TO_LGA: dict[str, str] = {
    # ── Lagos ──────────────────────────────────────────────────────────
    "victoria island": "Eti-Osa",   "lagos island": "Lagos Island",
    "lagos mainland": "Mainland",   "ebute metta": "Mainland",
    "satellite town": "Amuwo-Odofin","tin can": "Apapa",
    "mile 12": "Kosofe",            "mile 2": "Amuwo-Odofin",
    "ago palace": "Oshodi-Isolo",   "abraham adesanya": "Eti-Osa",
    "pen cinema": "Agege",          "oko awo": "Eti-Osa",
    "oko-awo": "Eti-Osa",           "iyana ipaja": "Alimosho",
    "abule egba": "Agege",          "oshodi isolo": "Oshodi-Isolo",
    "ikeja": "Ikeja",               "maryland": "Ikeja",
    "oregun": "Ikeja",              "ojodu": "Ikeja",
    "magodo": "Kosofe",             "gbagada": "Kosofe",
    "ojota": "Kosofe",              "ketu": "Kosofe",
    "alapere": "Kosofe",            "omole": "Kosofe",
    "palmgroove": "Shomolu",        "bariga": "Shomolu",
    "shomolu": "Shomolu",           "pedro": "Shomolu",
    "surulere": "Surulere",         "aguda": "Surulere",
    "itire": "Surulere",            "yaba": "Mainland",
    "oyingbo": "Mainland",          "mushin": "Mushin",
    "oshodi": "Oshodi-Isolo",       "isolo": "Oshodi-Isolo",
    "ejigbo": "Oshodi-Isolo",       "festac": "Amuwo-Odofin",
    "amuwo": "Amuwo-Odofin",        "apapa": "Apapa",
    "ikorodu": "Ikorodu",           "agege": "Agege",
    "ifako": "Agege",               "ijaiye": "Agege",
    "dopemu": "Agege",              "ojokoro": "Ifako-Ijaiye",
    "alimosho": "Alimosho",         "ipaja": "Alimosho",
    "egbe": "Alimosho",             "idimu": "Alimosho",
    "igando": "Alimosho",           "ikotun": "Alimosho",
    "isheri": "Alimosho",           "egbeda": "Alimosho",
    "akowonjo": "Alimosho",         "shasha": "Alimosho",
    "badagry": "Badagry",           "ojo": "Ojo",
    "ibeju": "Ibeju-Lekki",         "epe": "Epe",
    "lekki": "Eti-Osa",             "ajah": "Eti-Osa",
    "sangotedo": "Eti-Osa",         "chevron": "Eti-Osa",
    "eti osa": "Eti-Osa",           "ikoyi": "Lagos Island",
    "onikan": "Lagos Island",       "marina": "Lagos Island",
    "obalende": "Lagos Island",     "ogba": "Ikeja",
    "ajuwon": "Ifo",                "akute": "Ifo",
    "ayobo": "Alimosho",            "ipakodo": "Ikorodu",
    "oriwu": "Ikorodu",             "adetola": "Eti-Osa",
    "eastline": "Eti-Osa",
    # ── Abuja FCT ──────────────────────────────────────────────────────
    "maitama": "Municipal Area Council",
    "asokoro": "Municipal Area Council",
    "garki": "Municipal Area Council",
    "wuse": "Municipal Area Council",
    "central area": "Municipal Area Council",
    "jabi": "Municipal Area Council",
    "utako": "Municipal Area Council",
    "wuye": "Municipal Area Council",
    "area 11": "Municipal Area Council",
    "area 1": "Municipal Area Council",
    "mabushi": "Municipal Area Council",
    "cadastral zone": "Municipal Area Council",
    "gudu": "Abuja Municipal",      "apo": "Abuja Municipal",
    "gwarinpa": "Abuja Municipal",  "lugbe": "Abuja Municipal",
    "galadimawa": "Abuja Municipal","lokogoma": "Abuja Municipal",
    "nyanya": "Abuja Municipal",    "karu": "Abuja Municipal",
    "gaduwa": "Abuja Municipal",    "fort royal": "Abuja Municipal",
    "kubwa": "Bwari",               "ushafa": "Bwari",
    "kuje": "Kuje",                 "gwagwalada": "Gwagwalada",
    "learnofme": "Abuja Municipal", "sani daura": "Municipal Area Council",
    "wole soyinka": "Municipal Area Council",
    # ── Rivers ─────────────────────────────────────────────────────────
    "port harcourt": "Port Harcourt",
    "trans amadi": "Obio-Akpor",    "rumuola": "Obio-Akpor",
    "rumuokoro": "Obio-Akpor",      "rumuigbo": "Obio-Akpor",
    "rumuodara": "Obio-Akpor",      "rumuokuta": "Obio-Akpor",
    "rumuadaolu": "Obio-Akpor",     "obio": "Obio-Akpor",
    "akpor": "Obio-Akpor",          "woji": "Obio-Akpor",
    "ada george": "Obio-Akpor",     "diobu": "Port Harcourt",
    "borokiri": "Port Harcourt",    "gra phase": "Port Harcourt",
    "tombia": "Port Harcourt",      "abuloma": "Port Harcourt",
    "golf estate": "Port Harcourt", "eleme": "Eleme",
    "oyigbo": "Oyigbo",             "okrika": "Okrika",
    "bonny": "Bonny",               "eke street": "Obio-Akpor",
    "igwuruta": "Ikwerre",          "omueke": "Ikwerre",
    # ── Kano ───────────────────────────────────────────────────────────
    "sabon gari": "Sabon Gari",     "fagge": "Fagge",
    "dala": "Dala",                 "gwale": "Gwale",
    "tarauni": "Tarauni",           "ungogo": "Ungogo",
    "kumbotso": "Kumbotso",         "abdu bako": "Kano Municipal",
    "kirs": "Kano Municipal",       "gidan idris": "Kano Municipal",
    "bata kano": "Kano Municipal",  "bello road kano": "Kano Municipal",
    "church road kano": "Kano Municipal",
    "kano state": "Kano Municipal",
    # ── Oyo ────────────────────────────────────────────────────────────
    "ibadan": "Ibadan North",       "bodija": "Ibadan North",
    "mokola": "Ibadan North",       "agodi": "Ibadan North",
    "dugbe": "Ibadan South West",   "ring road": "Ibadan South West",
    "challenge": "Ibadan South West",
    "iyaganku": "Ibadan South East","oluyole": "Oluyole",
    "iwo road": "Ibadan North West","ojoo": "Akinyele",
    "ogbomoso": "Ogbomosho North",  "orita": "Ibadan South West",
    "adelabu": "Ibadan South West", "cocoa road": "Ibadan South West",
    "heritage mall": "Ibadan South West",
    "oyo town": "Oyo East",         "tobacco road": "Oyo East",
    "olivet heights": "Oyo East",
    # ── Delta ──────────────────────────────────────────────────────────
    "warri sapele road": "Uvwie",   "ajamimogha": "Warri South",
    "warri": "Warri South",         "effurun": "Uvwie",
    "aladja": "Udu",                "sapele": "Sapele",
    "asaba": "Oshimili South",      "ughelli": "Ughelli North",
    "agbor": "Ika South",           "abraka": "Ethiope East",
    "ozoro": "Isoko North",         "oleh": "Isoko South",
    "kwale": "Ndokwa West",         "jakpa": "Warri South",
    "ovonomo": "Warri South",       "escravos": "Warri South West",
    "udu": "Udu",                   "ovire": "Udu",
    "owhelogbo": "Isoko South",     "orhuwhor": "Udu",
    "ekregware": "Warri South",     "malumfashi": "Warri South",
    "college road aladja": "Udu",   "okumagba": "Warri South",
    "uromi": "Esan North East",
    # ── Anambra ────────────────────────────────────────────────────────
    "onitsha": "Onitsha North",     "awka": "Awka South",
    "nnewi": "Nnewi North",         "ekwulobia": "Aguata",
    "ogidi": "Idemili North",       "nkpor": "Idemili North",
    "obosi": "Idemili North",       "fegge": "Onitsha South",
    "ogbaru": "Ogbaru",             "awkuzu": "Oyi",
    "otuocha": "Oyi",
    # ── Enugu ──────────────────────────────────────────────────────────
    "independence layout": "Enugu South",
    "new haven": "Enugu South",     "trans ekulu": "Enugu East",
    "agbani road": "Enugu South",   "rangers": "Enugu North",
    "enugu": "Enugu North",         "nsukka": "Nsukka",
    "emene": "Enugu East",          "abakpa": "Enugu East",
    "corridor layout": "Enugu North",
    # ── Imo ────────────────────────────────────────────────────────────
    "okigwe road": "Owerri Municipal",
    "mcc road": "Owerri Municipal",
    "owerri": "Owerri Municipal",   "orji": "Owerri North",
    "nekede": "Owerri West",        "okigwe": "Okigwe",
    "orlu": "Orlu",                 "mbaise": "Aboh Mbaise",
    "aba owerri road": "Owerri Municipal",
    "shell camp": "Owerri Municipal","maranatha": "Owerri Municipal",
    "disney avenue": "Owerri Municipal",
    # ── Edo ────────────────────────────────────────────────────────────
    "benin city": "Oredo",          "uselu": "Egor",
    "ugbowo": "Egor",               "ikpoba": "Ikpoba-Okha",
    "oredo": "Oredo",               "new benin": "Oredo",
    "upper sakponba": "Oredo",      "idunwina": "Oredo",
    "osagiede": "Oredo",            "oka benin": "Oredo",
    "isihor": "Egor",               "ubiaja": "Esan South East",
    "ekiadoro": "Oredo",            "tony kabaka": "Oredo",
    # ── Abia ───────────────────────────────────────────────────────────
    "aba owerri": "Aba North",      "ariaria": "Aba South",
    "aba": "Aba North",             "umuahia": "Umuahia North",
    "asa road": "Aba North",        "tenant road": "Aba North",
    "eziukwu": "Aba South",         "umuehilegbu": "Aba South",
    "bakassi freezone": "Aba South",
    # ── Akwa Ibom ──────────────────────────────────────────────────────
    "uyo": "Uyo",                   "eket": "Eket",
    "ikot ekpene": "Ikot Ekpene",   "ikot odoro": "Uyo",
    "ikot oyoro": "Uyo",            "udoette": "Uyo",
    "urua epka": "Uyo",             "michael udofia": "Uyo",
    # ── Kaduna ─────────────────────────────────────────────────────────
    "kaduna": "Kaduna North",       "kakuri": "Kaduna South",
    "barnawa": "Kaduna South",      "malali": "Kaduna North",
    "zaria": "Zaria",               "ungwan rimi": "Kaduna North",
    "mando": "Kaduna North",        "bourmedine": "Kaduna North",
    # ── Cross River ────────────────────────────────────────────────────
    "calabar": "Calabar Municipal", "ikom": "Ikom",
    "technical layout calabar": "Calabar Municipal",
    # ── Ogun ───────────────────────────────────────────────────────────
    "abeokuta": "Abeokuta South",   "sagamu": "Sagamu",
    "ijebu ode": "Ijebu Ode",       "sango ota": "Ado-Odo/Ota",
    "mowe": "Obafemi Owode",        "ifo": "Ifo",
    "magboro": "Obafemi Owode",     "sanni olomore": "Abeokuta South",
    "singer bus stop": "Ado-Odo/Ota","gasline road": "Obafemi Owode",
    "ketere": "Abeokuta South",
    # ── Bayelsa ────────────────────────────────────────────────────────
    "yenagoa": "Yenagoa",           "yenegoa": "Yenagoa",
    "yenogoa": "Yenagoa",           "ogbia": "Ogbia",
    "amarata": "Yenagoa",           "azikoro": "Yenagoa",
    "opoloyenyen": "Yenagoa",       "okaka": "Yenagoa",
    "agudama": "Yenagoa",
    # ── Plateau ────────────────────────────────────────────────────────
    "jos": "Jos North",             "bukuru": "Jos South",
    "anglo jos": "Jos North",       "bank road jos": "Jos North",
    # ── Nasarawa ───────────────────────────────────────────────────────
    "lafia": "Lafia",               "keffi": "Keffi",
    "mararaba": "Karu",             "koroduma": "Karu",
    "kabayi": "Karu",               "jidna": "Karu",
    "adamu plaza": "Karu",          "jakada plaza": "Karu",
    # ── Kwara ──────────────────────────────────────────────────────────
    "ilorin": "Ilorin West",        "offa": "Offa",
    "ilemon": "Ilorin West",        "olofa way": "Offa",
    # ── Kogi ───────────────────────────────────────────────────────────
    "lokoja": "Lokoja",             "kabba": "Kabba/Bunu",
    "ugwolawo": "Igalamela-Odolu",  "idah": "Idah",
    "ibrahim taiwo road": "Lokoja",
    # ── Benue ──────────────────────────────────────────────────────────
    "makurdi": "Makurdi",           "otukpo": "Otukpo",
    "tarka way": "Makurdi",         "lafia road makurdi": "Makurdi",
    # ── Niger ──────────────────────────────────────────────────────────
    "minna": "Chanchaga",           "suleja": "Suleja",
    "bosso": "Bosso",               "kontagora": "Kontagora",
    "rafinsanyi": "Chanchaga",      "western bypass": "Chanchaga",
    # ── Sokoto ─────────────────────────────────────────────────────────
    "sokoto": "Sokoto North",       "sani abacha way": "Municipal Area Council",
    "mabera": "Sokoto North",       "kantin sani": "Sokoto North",
    "sultan sadik": "Sokoto North",
    # ── Adamawa ────────────────────────────────────────────────────────
    "yola": "Yola North",           "mubi": "Mubi North",
    "ahmadu bello way yola": "Yola North",
    # ── Gombe ──────────────────────────────────────────────────────────
    "gombe": "Gombe",               "nayinawa": "Gombe",
    "bauchi road gombe": "Gombe",
    # ── Bauchi ─────────────────────────────────────────────────────────
    "bauchi": "Bauchi",
    # ── Borno ──────────────────────────────────────────────────────────
    "maiduguri": "Maiduguri",       "north east": "Maiduguri",
    # ── Yobe ───────────────────────────────────────────────────────────
    "damaturu": "Damaturu",         "potiskum": "Potiskum",
    # ── Taraba ─────────────────────────────────────────────────────────
    "jalingo": "Jalingo",           "gembu": "Sardauna",
    "lassandi": "Jalingo",
    # ── Zamfara ────────────────────────────────────────────────────────
    "gusau": "Gusau",               "shinkafi": "Shinkafi",
    "yaraduwa": "Shinkafi",
    # ── Kebbi ──────────────────────────────────────────────────────────
    "birnin kebbi": "Birnin Kebbi", "argungu": "Argungu",
    "ambursa": "Birnin Kebbi",      "emir usman road": "Birnin Kebbi",
    # ── Jigawa ─────────────────────────────────────────────────────────
    "hadejia": "Hadejia",           "dutse jigawa": "Dutse",
    "makwalla": "Hadejia",          "kofar arewa": "Hadejia",
    "bakin kasuwa": "Hadejia",
    # ── Katsina ────────────────────────────────────────────────────────
    "katsina": "Katsina",           "jibia": "Jibia",
    "dorawar kanikawa": "Katsina",  "unguwar sarki": "Katsina",
    # ── Ebonyi ─────────────────────────────────────────────────────────
    "abakaliki": "Abakaliki",       "amechi road": "Abakaliki",
    "ogoja": "Ogoja",
    # ── Ondo ───────────────────────────────────────────────────────────
    "akure": "Akure South",         "ondo town": "Ondo West",
    "alagbaka": "Akure South",      "igbatoro": "Akure North",
    "ipe akoko": "Akoko South East",
    # ── Osun ───────────────────────────────────────────────────────────
    "osogbo": "Osogbo",             "ile ife": "Ife Central",
    "ilesha": "Ilesha West",        "ede": "Ede North",
    # ── Ekiti ──────────────────────────────────────────────────────────
    "ado ekiti": "Ado-Ekiti",       "iworoko": "Ado-Ekiti",
    "ekute": "Ado-Ekiti",
    # ── Additional Keywords (Improvements) ─────────────────────────────
    # Delta State - Missing areas (High priority - 123 failures)
    "ugievwen": "Warri South",
    "ogbe ogonogo": "Warri South",
    "ogbeogonogo": "Warri South",
    "dsc": "Warri South",
    "delta steel": "Warri South",
    "ekpan": "Uvwie",
    "enerhen": "Uvwie",
    "pti road": "Warri South",
    "refinery road": "Warri South",
    "alegbo": "Warri South",
    "alegbo road": "Warri South",
    "otokutu": "Warri South",
    "otokutu market": "Warri South",
    "kotokoto": "Warri South",
    "kotokoto junction": "Warri South",
    "ejemudiarho": "Warri South",
    "mofor": "Warri South",
    "agbasol": "Warri South",
    "ugbomoro": "Warri South",
    "onvonomo": "Warri South",
    "ovonomo": "Warri South",
    "sedicoo": "Warri South",
    "elarhie": "Warri South",
    "ejovwo": "Warri South",
    "umuedofe": "Warri South",
    "antarex": "Warri South",
    "oduke": "Warri South",
    "nnebuisi": "Warri South",
    "polokor": "Warri South",
    "polokor market": "Warri South",
    "metsharun": "Warri South",
    "osorhu": "Udu",
    "anwai": "Oshimili South",
    "anwai road": "Oshimili South",
    "irrioleh": "Isoko South",
    "oviore": "Isoko South",
    # Rivers State - Missing areas
    "mgbuoba": "Obio-Akpor",
    "nta road": "Port Harcourt",
    "rumuorosi": "Obio-Akpor",
    "rumuokwuta": "Obio-Akpor",
    "rumueme": "Obio-Akpor",
    "rumuepirikom": "Obio-Akpor",
    "eliozu": "Obio-Akpor",
    "choba": "Obio-Akpor",
    "aluu": "Ikwerre",
    "ozuoba": "Obio-Akpor",
    "rukpokwu": "Obio-Akpor",
    "nkpolu": "Port Harcourt",
    "oroworukwo": "Port Harcourt",
    "mile 3": "Port Harcourt",
    "mile 4": "Port Harcourt",
    "rumuokwurusi": "Obio-Akpor",
    "rumuokoro": "Obio-Akpor",
    # Abia State - Missing areas
    "ekeoha": "Aba South",
    "bonsaac": "Aba South",
    "bonsac": "Aba South",
    "shopping centre": "Aba South",
    "cemetery road": "Aba South",
    "faulks road": "Aba South",
    "ngwa road": "Aba South",
    "port harcourt road aba": "Aba North",
    "osisioma": "Osisioma",
    "osisioma junction": "Osisioma",
    "aba owerri road": "Aba North",
    # Kano State - Missing areas
    "mallam kato": "Kano Municipal",
    "niger street kano": "Kano Municipal",
    "france road": "Kano Municipal",
    "murtala mohammed way kano": "Kano Municipal",
    # Oyo State - Missing areas
    "oke ebo": "Ibadan South West",
    "oke-ebo": "Ibadan South West",
    "iseoluwa": "Ibadan North",
    "apata": "Ibadan South West",
    "gate": "Ibadan North",
    "oluyole": "Oluyole",
    "oluyole estate": "Oluyole",
    "iwo road": "Ibadan North West",
    # FCT - Missing areas
    "apodu": "Abuja Municipal",
    "apo district": "Abuja Municipal",
    "apo resettlement": "Abuja Municipal",
    "sector centre": "Municipal Area Council",
    "gadon asko": "Municipal Area Council",
    "gadon nasko": "Municipal Area Council",
    "ignobis": "Municipal Area Council",
    "ahmadu bello way abuja": "Municipal Area Council",
    "sani daura way": "Municipal Area Council",
    "life camp": "Abuja Municipal",
    "jikwoyi": "Abuja Municipal",
    "karshi": "Abuja Municipal",
    "kuje road": "Kuje",
    # Imo State - Missing areas
    "johncelia": "Owerri Municipal",
    "john celia": "Owerri Municipal",
    "wazobia market": "Owerri Municipal",
    "relief market": "Owerri Municipal",
    "douglas road": "Owerri Municipal",
    "tetlow road": "Owerri Municipal",
    "world bank": "Owerri Municipal",
    "world bank road": "Owerri Municipal",
    "ikenegbu": "Owerri Municipal",
    "new owerri": "Owerri Municipal",
    # Edo State - Missing areas (74 failures)
    "ekpoma": "Esan West",
    "auchi": "Etsako West",
    "uromi": "Esan North East",
    "irrua": "Esan Central",
    "ubiaja": "Esan South East",
    "ekiadoro": "Oredo",
    "dagnote": "Oredo",
    # Bayelsa State - Missing areas (55 failures)
    "opolo": "Yenagoa",
    "imgbi": "Yenagoa",
    "kpansia": "Yenagoa",
    "swali": "Yenagoa",
    "edepie": "Yenagoa",
    "ovom": "Yenagoa",
    "biogbolo": "Yenagoa",
    # Anambra State - Missing areas
    "okpuno": "Awka South",
    "amawbia": "Awka South",
    "ifite": "Awka South",
    "unizik": "Awka South",
    # Osun State - Missing areas
    "july 7": "Osogbo",
    "tb complex": "Osogbo",
    # Nasarawa State - Missing areas
    "jakada plaza": "Karu",
    "adamu plaza": "Karu",
    "new nyanya": "Karu",
    "masaka": "Karu",
    "ado": "Karu",
    # Additional improvements based on failed addresses
    # Ebonyi State
    "onugolu": "Abakaliki",
    "izzi street": "Abakaliki",
    # Rivers State
    "nzimiro": "Port Harcourt",
    "old gra": "Port Harcourt",
    "phalga": "Port Harcourt",
    # Niger State
    "und st": "Chanchaga",
    "mina niger": "Chanchaga",
    # Kebbi State
    "dan massalaci": "Birnin Kebbi",
    "massalaci": "Birnin Kebbi",
    # Cross River State
    "uwanse": "Calabar Municipal",
    "yahen": "Yala",
    "yahe": "Yala",
    "oba road": "Yala",
    "ugep": "Yakurr",
    "ijiman": "Yakurr",
    # Katsina State
    "goruba": "Katsina",
    "road j": "Katsina",
    # Edo State
    "erhuvbi": "Oredo",
    "iduowina": "Oredo",
    "idunmowina": "Oredo",
    "jemila road": "Oredo",
    "aziegbemhin": "Oredo",
    "iyaro": "Egor",
    "iyobosa": "Egor",
    # Abia State
    "umuariama": "Umuahia North",
    "alaoma": "Umuahia North",
    "eze akomas": "Aba North",
    "octopus hotel": "Aba North",
    # Benue State
    "tobacco warehouse": "Gboko",
    "yandev": "Gboko",
    "zomo": "Gboko",
    "adeyongo": "Vandeikya",
    "lijam": "Vandeikya",
    "tsambe": "Vandeikya",
    "kungwa": "Gboko",
    "buruku": "Buruku",
    "ikyese": "Makurdi",
    "adekaa": "Makurdi",
    "igbitta": "Gboko",
    "adi-agor": "Gboko",
    # Adamawa State
    "lakare": "Yola North",
    "bole ward": "Yola North",
    # Taraba State
    "atc kofai": "Jalingo",
    "kofai": "Jalingo",
    # Sokoto State
    "nakasari": "Sokoto North",
    # Oyo State
    "agodongbo": "Oyo East",
    "ogunwa": "Iseyin",
    "barack": "Iseyin",
    # Imo State
    "umuomeniho": "Mbaitoli",
    # Gombe State
    "labwini": "Gombe",
    # Kogi State
    "zango daji": "Lokoja",
    "lana plaza": "Lokoja",
}

# Compile longest-first for greedy matching
_KW_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE), lga)
    for kw, lga in sorted(KEYWORD_TO_LGA.items(), key=lambda x: len(x[0]), reverse=True)
]

# Common abbreviations to expand before keyword matching
_ABBREV_MAP = {
    r"\bPHC\b": "Port Harcourt",
    r"\bPH\b":  "Port Harcourt",
    r"\bVI\b":  "Victoria Island",
    r"\bFCT\b": "Abuja",
    r"\bGRA\b": "Government Reserved Area",
    r"\bDSC\b": "Delta Steel Company",
    r"\bNTA\b": "Nigerian Television Authority",
}
_ABBREV_PATTERNS = [(re.compile(p, re.IGNORECASE), v) for p, v in _ABBREV_MAP.items()]


def expand_abbreviations(address: str) -> str:
    """Expand common Nigerian address abbreviations before keyword matching."""
    for pattern, replacement in _ABBREV_PATTERNS:
        address = pattern.sub(replacement, address)
    return address


def keyword_lga_lookup(address: str) -> str:
    """
    Word-boundary keyword scan, longest match first. NaN-safe.
    Tries original address first, then abbreviation-expanded version,
    so specific area names beat generic expansions like 'Port Harcourt'.
    """
    if not address or not isinstance(address, str):
        return ""
    # Pass 1: original address (specific area names win)
    for pattern, lga in _KW_PATTERNS:
        if pattern.search(address):
            return lga
    # Pass 2: abbreviation-expanded (catches PH, PHC, VI etc.)
    expanded = expand_abbreviations(address)
    if expanded != address:
        for pattern, lga in _KW_PATTERNS:
            if pattern.search(expanded):
                return lga
    return ""


# ── Cache ─────────────────────────────────────────────────────────────

def load_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ── Address cleaning & parsing ────────────────────────────────────────

_NOISE = re.compile(
    r"\b(plot|no\.?|block|flat|floor|suite|unit|phase\s*\d*|off|beside|behind|"
    r"opposite|near|close to|junction|b/?stop|bus.?stop|p\.?o\.?\s*box|"
    r"\d+(st|nd|rd|th)|line\s+\d+|shop\s+[\w/&]+|str\b|pob\b|pmb\s*\d*|"
    r"opp\.?|c/o|by\b|n/a|house\s+\d+|compound|after|before|"
    r"adjacent|along|inside|within|back\s+of|front\s+of)\b",
    re.IGNORECASE,
)
_ORPHAN_TOKENS = re.compile(r"\b([a-z]{1,2}|\d{1,3})\b", re.IGNORECASE)
_EXTRA_SPACES  = re.compile(r"\s{2,}")

# Split digit↔letter boundaries for run-together strings
_DIGIT_LETTER = re.compile(r"(\d)([A-Za-z])|([A-Za-z])(\d)")

# Known Nigerian address suffix words — insert space before them when they
# are embedded inside a longer word (preceded AND followed by a letter).
# This avoids splitting "Broad" → "B road" since "road" in "Broad" is
# preceded by "B" but NOT followed by another letter.
_ADDR_SUFFIXES = re.compile(
    r"(?<=[A-Za-z])(road|street|avenue|close|drive|way|lane|crescent|"
    r"estate|layout|market|plaza|house|building|complex|area|"
    r"quarters|quarter|district|junction|extension|bypass|expressway)"
    r"(?=[A-Za-z]|$)",
    re.IGNORECASE,
)

# Trailing state/country noise (handles "IMO STATE", "LAGOS", "NIGERIA" etc.)
_TRAILING_STATE = re.compile(
    r",?\s*(federal capital territory|fct|abuja|lagos|ogun|oyo|osun|ondo|ekiti|"
    r"rivers|delta|edo|bayelsa|akwa ibom|cross river|anambra|enugu|imo|abia|ebonyi|"
    r"kano|kaduna|katsina|kebbi|sokoto|zamfara|jigawa|niger|plateau|nasarawa|benue|"
    r"kwara|kogi|taraba|adamawa|gombe|bauchi|yobe|borno|nigeria)"
    r"(\s+state)?\s*\.?\s*$",
    re.IGNORECASE,
)

# Expanded stop-word list for token extraction
_TOKEN_STOP = frozenset({
    "road", "street", "avenue", "close", "drive", "way", "lane",
    "crescent", "estate", "house", "building", "complex", "plaza",
    "market", "office", "ministry", "secretariat", "revenue",
    "state", "federal", "government", "national", "international",
    "shop", "suite", "block", "plot", "number", "extension",
    "phase", "layout", "junction", "before", "after", "beside",
    "finance", "general", "accountant", "opposite", "behind",
    "near", "along", "beside", "between", "within", "inside",
    "hospital", "school", "church", "mosque", "bank", "hotel",
    "filling", "station", "petrol", "service", "centre", "center",
    "university", "college", "institute", "polytechnic",
    "secretaria", "secretariat", "headquarters", "district",
    "quarters", "quarter", "area", "zone", "sector", "ward",
    "floor", "flat", "unit", "room", "wing", "annex",
    "expressway", "bypass", "highway", "freeway",
})


def split_concatenated(address: str) -> str:
    """
    Ultra-aggressive: Insert spaces into run-together addresses.
    Handles: digit↔letter, lowercase↔uppercase, embedded words, AND known suffix boundaries.
    e.g. BLOCK60COLLEGEROADALADJADELTA → BLOCK 60 COLLEGE ROAD ALADJA DELTA
    """
    # Step 1: digit↔letter splits
    address = _DIGIT_LETTER.sub(lambda m: " ".join(filter(None, m.groups())), address)
    
    # Step 2: lowercase↔uppercase splits (catches roadAladja → road Aladja)
    address = re.sub(r'([a-z])([A-Z])', r'\1 \2', address)
    
    # Step 3: Split before common prefixes when concatenated
    prefixes = ['BY', 'OPP', 'OFF', 'SHOP', 'PLOT', 'BLOCK', 'UNIT', 'FLAT', 'NO', 'NUMBER']
    for prefix in prefixes:
        # Insert space after prefix if followed by uppercase (BYPALIMA → BY PALIMA)
        pattern = r'\b(' + prefix + r')([A-Z])'
        address = re.sub(pattern, r'\1 \2', address, flags=re.IGNORECASE)
    
    # Step 4: Split before "OPPOSITE" when embedded (OPPOSITEWHITE → OPPOSITE WHITE)
    address = re.sub(r'([A-Z])(OPPOSITE)', r'\1 \2', address, flags=re.IGNORECASE)
    address = re.sub(r'(OPPOSITE)([A-Z])', r'\1 \2', address, flags=re.IGNORECASE)
    
    # Step 5: insert space before known suffix words (e.g. "COLLEGEROAD" → "COLLEGE ROAD")
    address = _ADDR_SUFFIXES.sub(r" \1", address)
    
    # Step 6: Split before common Nigerian location words when embedded
    locations = ['WARRI', 'DELTA', 'LAGOS', 'ALADJA', 'OZORO', 'ALEGBO', 'MARKET', 'LAYOUT', 'AREA']
    for loc in locations:
        # Insert space before location if preceded by letter
        pattern = r'([A-Z])(' + loc + r')'
        address = re.sub(pattern, r'\1 \2', address, flags=re.IGNORECASE)
    
    # Step 7: Clean up multiple spaces
    address = re.sub(r'\s+', ' ', address)
    
    return address.strip()


def clean_address(address: str) -> str:
    """Normalise, split concatenated tokens, strip noise and trailing state."""
    address = split_concatenated(address)
    address = _NOISE.sub(" ", address)
    address = _ORPHAN_TOKENS.sub(" ", address)
    address = _EXTRA_SPACES.sub(" ", address)
    address = _TRAILING_STATE.sub("", address)
    return address.strip(" ,.")


def extract_meaningful_tokens(address: str) -> list[str]:
    """
    For comma-free addresses, extract tokens that look like place names.
    Returns tokens in reverse order (last = most likely area name).
    """
    tokens = re.findall(r"[A-Za-z]{4,}", address)
    return [t for t in reversed(tokens) if t.lower() not in _TOKEN_STOP]


def build_query_variants(address: str, state: str) -> list[str]:
    """
    Build a cascade of Nominatim queries, most → least specific.
    Uses inferred state as primary hint when available.
    Includes no-state variants so a wrong STATE column doesn't block geocoding.
    """
    state_norm  = state.strip() if state else ""
    cleaned     = clean_address(address)
    comma_parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    has_commas  = len(comma_parts) > 1

    variants: list[str] = []

    def add(q: str) -> None:
        variants.append(q.strip(" ,"))

    if cleaned:
        if state_norm:
            add(f"{cleaned}, {state_norm}, Nigeria")
        add(f"{cleaned}, Nigeria")

    if has_commas and len(comma_parts) >= 2:
        tail = ", ".join(comma_parts[-2:])
        if state_norm:
            add(f"{tail}, {state_norm}, Nigeria")
        add(f"{tail}, Nigeria")

    if has_commas and comma_parts:
        last = comma_parts[-1]
        if state_norm:
            add(f"{last}, {state_norm}, Nigeria")
        add(f"{last}, Nigeria")

    if not has_commas:
        tokens = extract_meaningful_tokens(cleaned)
        for tok in tokens[:4]:
            if state_norm:
                add(f"{tok}, {state_norm}, Nigeria")
            add(f"{tok}, Nigeria")

    if state_norm:
        add(f"{state_norm}, Nigeria")

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def preprocess_address_enhanced(address: str) -> tuple[str, str]:
    """
    Enhanced pre-processing: extract state, expand abbreviations, clean noise.
    Returns: (cleaned_address, extracted_state)
    """
    if not address or not isinstance(address, str):
        return "", ""
    
    # Step 1: Extract state from end of address
    state_pattern = re.compile(
        r'\b(DELTA|LAGOS|RIVERS|KANO|KADUNA|ENUGU|ABIA|IMO|ANAMBRA|'
        r'OYO|OGUN|EDO|BAYELSA|KATSINA|NASARAWA|PLATEAU|KWARA|OSUN|'
        r'ONDO|EKITI|ZAMFARA|SOKOTO|KEBBI|NIGER|BENUE|TARABA|ADAMAWA|'
        r'GOMBE|BAUCHI|YOBE|BORNO|JIGAWA|EBONYI|CROSS RIVER|AKWA IBOM|'
        r'FCT|ABUJA|FEDERAL CAPITAL TERRITORY)(\s+STATE)?\s*$',
        re.IGNORECASE
    )
    
    state_match = state_pattern.search(address)
    extracted_state = ""
    if state_match:
        extracted_state = state_match.group(1)
        # Remove state from address
        address = state_pattern.sub('', address).strip()
    
    # Step 2: Expand abbreviations before cleaning
    for pattern, replacement in _ABBREV_PATTERNS:
        address = pattern.sub(replacement, address)
    
    return address, extracted_state


# ── Nominatim result parser ───────────────────────────────────────────

_LGA_FIELDS = [
    ("county",         3),
    ("state_district", 3),
    ("city_district",  2),
    ("municipality",   2),
    ("suburb",         1),
    ("city",           1),
    ("town",           1),
    ("village",        1),
]


def extract_lga(result: dict, state: str) -> tuple[str, int]:
    """Return (lga, confidence) from a Nominatim result dict."""
    addr       = result.get("address", {})
    state_norm = state.strip().lower() if state else ""
    bad        = {"nigeria", state_norm, ""}

    for field, conf in _LGA_FIELDS:
        val = addr.get(field, "")
        if val and val.lower() not in bad:
            return val, conf
    return "", 0


# ── Geocoder ──────────────────────────────────────────────────────────

def geocode(query: str, session: requests.Session) -> dict | None:
    """Call Nominatim with retry + 429 backoff."""
    url     = "https://nominatim.openstreetmap.org/search"
    params  = {"q": query, "format": "json", "addressdetails": 1,
               "limit": 1, "countrycodes": "ng"}
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 429:
                wait = DELAY * 6 * attempt
                log.warning(f"Rate limited (429). Waiting {wait:.0f}s ...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data[0] if data else None
        except requests.exceptions.RequestException as e:
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for '{query}': {e}")
            if attempt < MAX_RETRIES:
                time.sleep(DELAY * attempt)
    return None


# ── Core resolver ─────────────────────────────────────────────────────

# ── State-based defaults for generic addresses ───────────────────────

STATE_DEFAULTS = {
    "Delta": "Warri South",
    "Edo": "Oredo",
    "Bayelsa": "Yenagoa",
    "Rivers": "Port Harcourt",
    "Lagos": "Ikeja",
    "Abia": "Aba North",
    "Anambra": "Onitsha North",
    "Imo": "Owerri Municipal",
    "Oyo": "Ibadan North",
    "Kano": "Kano Municipal",
    "Federal Capital Territory": "Municipal Area Council",
    "Kaduna": "Kaduna North",
    "Katsina": "Katsina",
    "Plateau": "Jos North",
    "Nasarawa": "Karu",
    "Kwara": "Ilorin West",
    "Ogun": "Abeokuta South",
    "Ondo": "Akure South",
    "Osun": "Osogbo",
    "Ekiti": "Ado-Ekiti",
    "Cross River": "Calabar Municipal",
    "Akwa Ibom": "Uyo",
    "Enugu": "Enugu North",
    "Ebonyi": "Abakaliki",
    "Benue": "Makurdi",
    "Kogi": "Lokoja",
    "Niger": "Chanchaga",
    "Taraba": "Jalingo",
    "Adamawa": "Yola North",
    "Gombe": "Gombe",
    "Bauchi": "Bauchi",
    "Yobe": "Damaturu",
    "Borno": "Maiduguri",
    "Sokoto": "Sokoto North",
    "Kebbi": "Birnin Kebbi",
    "Zamfara": "Gusau",
    "Jigawa": "Dutse",
}

# Pattern-based resolution rules
PATTERN_RULES = [
    (r'\bWARRI\b', "Delta", "Warri South"),
    (r'\bSAPELE\b', "Delta", "Sapele"),
    (r'\bASABA\b', "Delta", "Oshimili South"),
    (r'\bUGHELLI\b', "Delta", "Ughelli North"),
    (r'\bAGBOR\b', "Delta", "Ika South"),
    (r'\bEFFURUN\b', "Delta", "Uvwie"),
    (r'\bUDU\b', "Delta", "Udu"),
    (r'\bOZORO\b', "Delta", "Isoko North"),
    (r'\bOLEH\b', "Delta", "Isoko South"),
    (r'\bBENIN\b', "Edo", "Oredo"),
    (r'\bEKPOMA\b', "Edo", "Esan West"),
    (r'\bAUCHI\b', "Edo", "Etsako West"),
    (r'\bURONI\b', "Edo", "Esan North East"),
    (r'\bYENAGOA\b', "Bayelsa", "Yenagoa"),
    (r'\bYENEGOA\b', "Bayelsa", "Yenagoa"),
    (r'\bYENOGOA\b', "Bayelsa", "Yenagoa"),
    (r'\bPORT\s*HARCOURT\b', "Rivers", "Port Harcourt"),
    (r'\bRUMU\w+\b', "Rivers", "Obio-Akpor"),
    (r'\bELEME\b', "Rivers", "Eleme"),
    (r'\bIBADAN\b', "Oyo", "Ibadan North"),
    (r'\bOGBOMOSO\b', "Oyo", "Ogbomosho North"),
    (r'\bKANO\b', "Kano", "Kano Municipal"),
    (r'\bSABON\s*GARI\b', "Kano", "Sabon Gari"),
    # Additional patterns for failed addresses
    (r'\bABAKALIKI\b', "Ebonyi", "Abakaliki"),
    (r'\bOLD\s*GRA\b', "Rivers", "Port Harcourt"),
    (r'\bNZIMIRO\b', "Rivers", "Port Harcourt"),
    (r'\bMINNA\b', "Niger", "Chanchaga"),
    (r'\bBIRNIN\s*KEBBI\b', "Kebbi", "Birnin Kebbi"),
    (r'\bCALABAR\b', "Cross River", "Calabar Municipal"),
    (r'\bUGEP\b', "Cross River", "Yakurr"),
    (r'\bKATSINA\b', "Katsina", "Katsina"),
    (r'\bUMUAHIA\b', "Abia", "Umuahia North"),
    (r'\bGBOKO\b', "Benue", "Gboko"),
    (r'\bMAKURDI\b', "Benue", "Makurdi"),
    (r'\bVANDEIKYA\b', "Benue", "Vandeikya"),
    (r'\bBURUKU\b', "Benue", "Buruku"),
    (r'\bYOLA\b', "Adamawa", "Yola North"),
    (r'\bJALINGO\b', "Taraba", "Jalingo"),
    (r'\bSOKOTO\b', "Sokoto", "Sokoto North"),
    (r'\bISEYIN\b', "Oyo", "Iseyin"),
    (r'\bMBAITOLI\b', "Imo", "Mbaitoli"),
    (r'\bGOMBE\b', "Gombe", "Gombe"),
    (r'\bLOKOJA\b', "Kogi", "Lokoja"),
]


def pattern_based_resolution(address: str, state: str) -> str:
    """Use pattern matching to resolve address based on known location patterns."""
    for pattern, pattern_state, lga in PATTERN_RULES:
        if state and state != pattern_state:
            continue
        if re.search(pattern, address, re.IGNORECASE):
            return lga
    return ""


def resolve_lga(address: str, state: str, session: requests.Session,
                cache: dict) -> tuple[str, str]:
    """
    Resolution pipeline:
      1. Cache  — instant, no API call
      2. Nominatim cascade — inferred state takes priority over STATE col
      3. Keyword table — abbreviation expansion + word-boundary matching
      4. Pattern-based resolution — location name patterns
      5. State defaults — for generic addresses with valid state
    Returns (lga, method).
    """
    if not address or not isinstance(address, str) or \
       address.lower() in ("nan", "none", ""):
        return "", "skipped"

    # Enhanced pre-processing: extract state and expand abbreviations
    preprocessed_addr, extracted_state = preprocess_address_enhanced(address)
    if preprocessed_addr:
        address = preprocessed_addr
    
    # Inferred state from address text overrides potentially wrong STATE col
    inferred = infer_state_from_address(address)
    # Priority: extracted state > inferred state > provided state
    if extracted_state:
        effective_state = extracted_state
    elif inferred:
        effective_state = inferred
    else:
        effective_state = state

    variants = build_query_variants(address, effective_state)

    # 1. Cache pass
    for query in variants:
        cached = cache.get(query.lower())
        if cached:
            return cached, "cache"

    # 2. Nominatim cascade
    best_lga, best_conf = "", 0

    for query in variants:
        cache_key = query.lower()
        if cache_key in cache:
            continue

        result = geocode(query, session)
        time.sleep(DELAY)

        if result:
            hint = effective_state if effective_state.lower() in query.lower() else ""
            lga, conf = extract_lga(result, hint)
            cache[cache_key] = lga

            if lga and conf > best_conf:
                best_lga, best_conf = lga, conf

            if conf >= 2:
                return lga, "nominatim"
        else:
            cache[cache_key] = ""

    if best_lga:
        return best_lga, "nominatim"

    # 3. Keyword fallback (with abbreviation expansion)
    lga = keyword_lga_lookup(address)
    if lga:
        cache[address.lower()] = lga
        return lga, "keyword"

    # 4. Pattern-based resolution (NEW)
    lga = pattern_based_resolution(address, effective_state)
    if lga:
        cache[address.lower()] = lga
        return lga, "pattern"

    # 5. State-based default for generic addresses (NEW)
    # Only use for short/generic addresses with a valid state
    if effective_state and effective_state in STATE_DEFAULTS:
        # Use default if address is very short or very generic
        if len(address) < 35 or re.search(r'^\s*(NUMBER|NO\.?|SHOP|PLOT|BLOCK)\s+\d+', address, re.IGNORECASE):
            lga = STATE_DEFAULTS[effective_state]
            cache[address.lower()] = lga
            return lga, "state_default"

    return "", "failed"


# ── Load data ─────────────────────────────────────────────────────────

log.info(f"Loading: {INPUT_FILE}")
try:
    if _ext == ".csv":
        df = pd.read_csv(INPUT_FILE, encoding="utf-8-sig")
    else:
        df = pd.read_excel(INPUT_FILE, sheet_name=args.sheet)
except Exception as e:
    log.error(f"Failed to read input file: {e}")
    sys.exit(1)

df.columns = df.columns.str.strip()

col_map: dict[str, str] = {}
for col in df.columns:
    low = col.lower()
    if "address" in low and "ADDRESS" not in col_map.values():
        col_map[col] = "ADDRESS"
    elif "state" in low and "STATE" not in col_map.values():
        col_map[col] = "STATE"

df.rename(columns=col_map, inplace=True)

if "ADDRESS" not in df.columns:
    log.error(f"No ADDRESS column found. Columns: {list(df.columns)}")
    sys.exit(1)

if "STATE" not in df.columns:
    log.warning("No STATE column — will infer state from address text only.")
    df["STATE"] = ""

df["STATE"]   = df["STATE"].apply(lambda v: normalise_state(str(v)))
df["ADDRESS"] = df["ADDRESS"].astype(str).str.strip()

log.info(f"{len(df):,} total rows, {df['ADDRESS'].nunique():,} unique addresses.")

# ── Deduplicated geocoding ────────────────────────────────────────────

cache = load_cache(CACHE_FILE)
log.info(f"{len(cache):,} cached entries loaded.")

pairs = df[["ADDRESS", "STATE"]].drop_duplicates()
log.info(f"Geocoding {len(pairs):,} unique (address, state) pairs ...")

pair_results: dict[tuple[str, str], tuple[str, str]] = {}
counters = {"cache": 0, "nominatim": 0, "keyword": 0, "skipped": 0, "failed": 0}

with requests.Session() as session:
    adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)

    for i, (_, row) in enumerate(tqdm(pairs.iterrows(), total=len(pairs), desc="Geocoding")):
        address = row["ADDRESS"]
        state   = row["STATE"]

        lga, method = resolve_lga(address, state, session, cache)
        pair_results[(address, state)] = (lga, method)
        counters[method] = counters.get(method, 0) + 1

        if (i + 1) % 100 == 0:
            save_cache(cache, CACHE_FILE)

save_cache(cache, CACHE_FILE)

df["LGA"]     = df.apply(lambda r: pair_results.get((r["ADDRESS"], r["STATE"]), ("", "failed"))[0], axis=1)
df["_method"] = df.apply(lambda r: pair_results.get((r["ADDRESS"], r["STATE"]), ("", "failed"))[1], axis=1)

# ── Summary ───────────────────────────────────────────────────────────

filled   = df["LGA"].notna() & (df["LGA"] != "")
coverage = filled.sum() / len(df) * 100

print("-" * 55)
print(f"Done!")
print(f"  Total rows      : {len(df):,}")
print(f"  Unique pairs    : {len(pairs):,}")
print(f"  LGA found       : {filled.sum():,}  ({coverage:.1f}%)")
print(f"  via cache       : {counters['cache']:,}")
print(f"  via API         : {counters['nominatim']:,}")
print(f"  via keyword     : {counters['keyword']:,}")
print(f"  via pattern     : {counters.get('pattern', 0):,}")
print(f"  via state dflt  : {counters.get('state_default', 0):,}")
print(f"  skipped (blank) : {counters['skipped']:,}")
print(f"  failed          : {counters['failed']:,}")
print("-" * 55)

failed_df = df[df["LGA"] == ""][["ADDRESS", "STATE", "_method"]].drop_duplicates()
if not failed_df.empty:
    print(f"\n{len(failed_df)} unique unresolved addresses:")
    print(failed_df.head(30).to_string())

# ── Save output ───────────────────────────────────────────────────────

df.drop(columns=["_method"], inplace=True, errors="ignore")

out_ext = os.path.splitext(OUTPUT_FILE)[1].lower()
if out_ext in (".xlsx", ".xls"):
    df.to_excel(OUTPUT_FILE, index=False)
else:
    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

print(f"Saved -> {OUTPUT_FILE}")
