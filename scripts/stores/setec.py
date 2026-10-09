#!/usr/bin/env python3
"""Setec (setec.mk) store client.

Implements references/client-contract.md on top of the two public JSON
backends the storefront itself uses (see references/setec.md):

  Meilisearch  POST https://search.sp.solslab.dev/indexes/products/search
               (+ /multi-search)   catalogue, prices, stock totals, facets
  Detail       GET  https://setec.mk/api/medusa/products-with-details-web?handle=
               warranty months, per-location stock
  Category tree GET https://setec.mk/api/strapi/category?locale=mk-MK&withSubcategories=true
  Web config   GET  https://setec.mk/api/medusa/web-config   (low-stock threshold)

CLI (contract):
  setec.py info
  setec.py search "<query>" [--limit N] [--in-stock] [--json PATH]
  setec.py categories [--grep REGEX] [--json PATH]
  setec.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
  setec.py detail <url-or-id> [...] [--json PATH]
  setec.py facets <category> [--json PATH]
Setec-only extras:
  setec.py gaps <category> --attr NAME [--in-stock] [--limit N] [--json PATH]
  setec.py brands [<category>] [--json PATH]
  setec.py stores <url-or-id> [...] [--json PATH]
"""

import argparse
import datetime as _dt
import json
import re
import sys
import time
from urllib.parse import unquote, urlparse

import requests

STORE = "setec"
NAME = "Setec"
BASE = "https://setec.mk"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Public client-side search key, shipped in setec.mk's JS bundle to every visitor.
# _rediscover_key re-reads host and key from that bundle if the key is rotated.
SEARCH_BASE = "https://search.sp.solslab.dev"
SEARCH_KEY = "c0424dab588b8cbbbe0a4809fc10b5f1c0c7d183b5b28ebe799f3fbf583ab358"
TREE_URL = BASE + "/api/strapi/category?locale=mk-MK&withSubcategories=true"
CONFIG_URL = BASE + "/api/medusa/web-config"
DETAIL_URL = BASE + "/api/medusa/products-with-details-web"

# Meilisearch returns at most this many hits for one query, whatever the limit
# (observed 2026-10-09). Facet counts are not capped, so totals come from facets
# and listings past the ceiling are fetched in brand/price windows.
CAP = 1000
PAGE = 500                     # hits per request inside one query window
PACE_S = 0.35                  # min gap between requests to the same host
# Both backends normally answer in well under a second; the generous timeout only
# matters when a host is cold or struggling.
TIMEOUT = 45
MAX_TRIES = 4
DEFAULT_ORDER_THRESHOLD = 3    # web-config order_threshold at time of writing

PRICE = "variants.calculated_price.calculated_amount"
BASE_FILTERS = ["status = 'published'", "is_web_active = 'true'"]  # what the site adds
IN_STOCK = "total_web_quantity > 0"                                 # the site's "Достапно"
# The central warehouse ships online orders but cannot be visited; it never counts
# as a store, so "N stores" always means places a buyer can walk into.
WAREHOUSE = "Главен Магацин"
NON_WALK_IN = {WAREHOUSE, "СЕТЕК Web"}
# Outlet store: its units are walk-in only and are NOT counted in total_web_quantity
# (per-location sum minus total_web_quantity == the outlet's quantity, checked 2026-10-03).
NOT_IN_WEB_TOTAL = {"СЕТЕК OUTLET"}
BRAND_ATTR = "Бренд"

LIST_FIELDS = ["id", "title", "handle", "brand_name", "external_id",
               PRICE, "variants.calculated_price.original_amount",
               "variants.catalogue_number", "total_web_quantity",
               "is_web_available", "product_categories", "attribute_pairs",
               "price_lists.price_list.role", "price_lists.price_list.title",
               "price_lists.price_list.id", "price_lists.price_list.starts_at",
               "price_lists.price_list.ends_at", "price_lists.prices.amount",
               "variants.calculated_price.calculated_price.price_list_id"]

CHALLENGE_RE = re.compile(
    r"Just a moment|cf-chl|challenge-platform|turnstile|captcha|Attention Required|"
    r"Sorry, you have been blocked|Access denied|cf-error-details|error code: 1\d{3}", re.I)


class Blocked(RuntimeError):
    pass


class UsageError(RuntimeError):
    pass


class StoreError(RuntimeError):
    pass


QUIET = False
VERBOSE = False


def log(msg):
    """Progress: indented, so mkshop.py treats it as progress, never as a result note."""
    if not QUIET:
        print(f"  {msg}", file=sys.stderr)


def warn(msg):
    """A line that changes how the result must be read (truncation, fallback, partial data);
    mkshop.py copies unindented WARNING lines into the shop's status note."""
    print(f"WARNING: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- HTTP

_session = None
_last = {}
_key_rediscovered = [False]
# The search client's constructor call in the site's JS: ("https://<host>/","<64 hex key>").
_KEY_RE = re.compile(r'\("(https://[a-z0-9.-]+)/?","([0-9a-f]{64})"\)')


def session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "mk,en;q=0.8",
                          "Accept": "application/json, text/plain, */*"})
        _session = s
    return _session


def _pace(host):
    wait = PACE_S - (time.time() - _last.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def _evidence(resp):
    title = re.search(r"<title[^>]*>(.*?)</title>", resp.text[:5000], re.I | re.S)
    bits = [f"HTTP {resp.status_code}", resp.request.method + " " + resp.url[:120]]
    if title:
        bits.append("title=" + re.sub(r"\s+", " ", title.group(1)).strip()[:80])
    for h in ("server", "cf-ray", "cf-mitigated"):
        if resp.headers.get(h):
            bits.append(f"{h}={resp.headers[h]}")
    return ", ".join(bits)


def _request(method, url, *, auth=False, expect_json=True, allow_404=False, **kw):
    host = urlparse(url).netloc
    delay = 2.0
    for attempt in range(1, MAX_TRIES + 1):
        _pace(host)
        headers = dict(kw.pop("headers", {}) or {})
        if auth:
            headers["Authorization"] = "Bearer " + SEARCH_KEY
        if VERBOSE:
            log(f"{method} {url[:150]}")
        try:
            r = session().request(method, url, timeout=TIMEOUT, headers=headers, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == MAX_TRIES:
                raise StoreError(f"{method} {url}: network error after {attempt} tries: {e}")
            time.sleep(delay)
            delay *= 2
            kw["headers"] = headers
            continue
        ctype = r.headers.get("content-type", "")
        # Real storefront pages are 200-400 KB Next.js documents; challenge
        # interstitials are small HTML pages or 403/429/503 HTML answers.
        if r.headers.get("cf-mitigated") or (
                "html" in ctype and CHALLENGE_RE.search(r.text[:20000] or "")
                and (r.status_code in (403, 429, 503) or len(r.content) < 60000)):
            raise Blocked(_evidence(r))
        if r.status_code in (429, 500, 502, 503, 504) and attempt < MAX_TRIES:
            ra = r.headers.get("retry-after")
            try:
                sleep_for = max(float(ra), delay) if ra else delay
            except ValueError:
                sleep_for = delay
            log(f"HTTP {r.status_code} from {host}; retrying in {sleep_for:.0f}s")
            time.sleep(sleep_for)
            delay *= 2
            kw["headers"] = headers
            continue
        if r.status_code == 404 and allow_404:
            return None
        if auth and r.status_code in (401, 403) and "json" in ctype:
            raise PermissionError(r.text[:300])
        if r.status_code == 400 and "json" in ctype:
            try:
                msg = r.json().get("message", r.text[:300])
            except ValueError:
                msg = r.text[:300]
            raise UsageError(f"search backend rejected the request: {msg}")
        if r.status_code == 403:
            raise Blocked(_evidence(r))
        if r.status_code != 200:
            raise StoreError(f"{method} {url[:120]}: HTTP {r.status_code}: {r.text[:200]!r}")
        if expect_json:
            if "json" not in ctype:
                raise Blocked("expected JSON, got " + repr(ctype) + "; " + _evidence(r))
            return r.json()
        return r.text
    raise StoreError("unreachable")


def _rediscover_key():
    """The search key ships in the storefront's JS bundle. If the hard-coded key
    is rejected, scan the bundle once for ("https://<search host>/","<64 hex>")."""
    global SEARCH_BASE, SEARCH_KEY
    if _key_rediscovered[0]:
        return False
    _key_rediscovered[0] = True
    log("search key rejected - re-reading it from setec.mk's JS bundle")
    # The search client is bundled with the category listing page, so read a
    # category page's chunk list first, then the homepage's.
    home = _request("GET", BASE + "/", expect_json=False, headers={"Accept": "text/html"})
    slugs = re.findall(r"/category/([a-z0-9-]+)", home)
    if not slugs:
        slugs = [n["slug"] for n in load_tree().values()][:1]
    htmls = []
    if slugs:
        htmls.append(_request("GET", f"{BASE}/category/{slugs[0]}", expect_json=False,
                              headers={"Accept": "text/html"}))
    htmls.append(home)
    chunks = []
    for html in htmls:
        for c in re.findall(r'/_next/static/chunks/[^"\\ ]+\.js', html):
            if c not in chunks:
                chunks.append(c)
    for c in chunks[:60]:
        js = _request("GET", BASE + c, expect_json=False)
        hit = _KEY_RE.search(js)
        if hit:
            SEARCH_BASE, SEARCH_KEY = hit.group(1), hit.group(2)
            warn(f"using search host {SEARCH_BASE} with key {SEARCH_KEY[:8]}... "
                 "(update SEARCH_BASE/SEARCH_KEY in setec.py)")
            return True
    return False


def meili(path, body):
    url = SEARCH_BASE + path
    try:
        return _request("POST", url, auth=True, json=body)
    except PermissionError as e:
        if _rediscover_key():
            return _request("POST", SEARCH_BASE + path, auth=True, json=body)
        raise StoreError(f"search key rejected and could not be re-discovered: {e}")


def search_body(body):
    return meili("/indexes/products/search", body)


def multi(queries, batch=150):
    out = []
    for i in range(0, len(queries), batch):
        chunk = [dict(q, indexUid="products") for q in queries[i:i + batch]]
        out += meili("/multi-search", {"queries": chunk})["results"]
    return out


def q(v):
    """Quote a value for a Meilisearch filter expression."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def count(filters, query=""):
    """Exact match count: facet counts are not capped at maxTotalHits."""
    d = search_body({"q": query, "limit": 0, "filter": filters, "facets": ["status"],
                     "matchingStrategy": "all"})
    return sum((d.get("facetDistribution") or {}).get("status", {}).values())


_threshold = [None]


def order_threshold():
    if _threshold[0] is None:
        try:
            _threshold[0] = int(_request("GET", CONFIG_URL).get("order_threshold") or 0)
        except (StoreError, ValueError, TypeError, AttributeError):
            _threshold[0] = DEFAULT_ORDER_THRESHOLD
    return _threshold[0]


# --------------------------------------------------------------------------- transliteration

CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e",
           "ж": "zh", "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l",
           "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r",
           "с": "s", "т": "t", "ќ": "kj", "у": "u", "ф": "f", "х": "h", "ц": "c",
           "ч": "ch", "џ": "dzh", "ш": "sh", "ђ": "dj", "ћ": "c", "й": "j", "ы": "y",
           "э": "e", "ю": "ju", "я": "ja", "щ": "sht", "ъ": "", "ь": "", "ё": "e"}
LAT2CYR = [("dzh", "џ"), ("dz", "ѕ"), ("gj", "ѓ"), ("kj", "ќ"), ("lj", "љ"), ("nj", "њ"),
           ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("kh", "х"), ("č", "ч"), ("š", "ш"),
           ("ž", "ж"), ("ć", "ќ"), ("đ", "ѓ"), ("a", "а"), ("b", "б"), ("c", "ц"),
           ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
           ("j", "ј"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
           ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
           ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "ј"), ("z", "з")]
_LOOSE = [("dzh", "dz"), ("zh", "z"), ("sh", "s"), ("ch", "c"), ("kh", "h"), ("ts", "c"),
          ("kj", "k"), ("gj", "g"), ("lj", "l"), ("nj", "n"), ("č", "c"), ("š", "s"),
          ("ž", "z"), ("ć", "c"), ("đ", "d")]


def to_latin(s):
    return "".join(CYR2LAT.get(ch, ch) for ch in s.lower())


def to_cyrillic(s):
    out, s, i = [], s.lower(), 0
    while i < len(s):
        for a, b in LAT2CYR:
            if s.startswith(a, i):
                out.append(b)
                i += len(a)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def loose(s):
    s = to_latin(s)
    for a, b in _LOOSE:
        s = s.replace(a, b)
    return s


# Category handles encode non-ASCII and punctuation as -<hex> escapes, so
# "Напојувања" becomes "napo-d1-98uvanja-29" (-d1-98 is ј, trailing -29 is the
# category id). Decoding is lossy but good enough to name an off-menu category.
_HANDLE_SUBS = [("-d1-98", "j"), ("-d1-9f", "dz"), ("-d1-9c", "kj"), ("-d1-9b", "gj"),
                ("-20", " "), ("-2f", "/"), ("-26", "&"), ("-2c", ","), ("-2b", "+")]


def decode_handle(h):
    for a, b in _HANDLE_SUBS:
        h = h.replace(a, b)
    return h


# Words added to a category's --grep haystack when its name contains the stem, so
# `categories --grep laptop` finds Преносни Компјутери and `--grep 'перални|washing'`
# finds Машини за перење на алишта. Stems are compared after loose(), so mixed-script
# names ("Kлиматизери" starts with a Latin K) still match. Values may hold English
# words and colloquial Macedonian synonyms the menu does not use.
_EN_TAGS = {
    "компјутер": "computer computers pc", "преносни компјутери": "laptop laptops notebook лаптоп лаптопи",
    "конфигурации": "desktop desktops prebuilt pc десктоп", "матични": "motherboard mainboard",
    "куќишта": "pc case chassis", "процесори": "cpu processor", "графички карти": "gpu graphics card video card",
    "тврди дискови": "hdd ssd hard drive disk storage", "рам меморија": "ram memory dimm",
    "кулери": "cooler cooling cpu cooler fan", "напојувања": "psu power supply",
    "звучни карти": "sound card tv tuner", "галантерија": "accessories peripherals",
    "глувци": "mouse mice mousepad", "подлог": "mousepad mouse pad", "тастатури": "keyboard keyboards",
    "слушалки": "headphones headset earphones earbuds", "звучници": "speaker speakers",
    "микрофони": "microphone mic", "веб камери": "webcam web camera", "монитори": "monitor monitors display screen",
    "држачи за монитори": "monitor arm mount", "заштитни очила": "glasses blue light",
    "проектори": "projector projectors", "мрежн": "network networking lan ethernet",
    "рутери": "router routers wifi mesh access point", "адаптери": "adapter adapters",
    "преносна меморија": "storage portable", "usb мемории": "usb flash drive stick pendrive",
    "мемориски картички": "memory card sd microsd card reader", "екстерни дискови": "external hard drive ssd hdd",
    "преносни кутии": "enclosure hdd case", "печатари": "printer printers", "скенери": "scanner scanners",
    "потрошен материјал": "toner ink cartridge paper", "баркод": "barcode scanner",
    "продолжни кабли": "extension cord power strip surge", "кабли": "cable cables",
    "конектори": "connector connectors", "виртуелна реалност": "vr virtual reality",
    "софтвер": "software", "столици": "chair chairs", "бироа": "desk desks", "масажа": "massage",
    "таблети": "tablet tablets ipad", "ташни": "bag bags laptop bag", "ранци": "backpack backpacks",
    "адаптери за струја": "charger power adapter", "ладилник": "laptop cooler cooling pad",
    "пенкала": "stylus pen", "конзоли": "console consoles playstation xbox nintendo gaming",
    "игри": "game games", "контролери": "controller gamepad joystick", "волани": "steering wheel racing wheel",
    "играчки": "toy toys", "телевизори": "tv tvs television televisions",
    "држачи за телевизори": "tv mount bracket wall mount", "домашни кина": "home cinema home theater",
    "портабл звучници": "portable speaker bluetooth speaker", "аудио": "audio", "автомобил": "car",
    "антени": "antenna set-top box receiver", "диктафони": "voice recorder dictaphone",
    "далечински": "remote control", "телефони": "phone phones",
    "мобилни телефони": "smartphone smartphones mobile phone cell phone смартфон смартфони паметни телефони",
    "smartwatch": "smart watch паметен часовник паметни часовници", "навигаци": "gps navigation sat nav",
    "фото рамки": "photo frame digital frame", "камери": "camera cameras", "дронови": "drone drones",
    "фотоапарати": "camera cameras photo", "акциони камери": "action camera gopro",
    "заштитни фолии": "screen protector", "футроли": "case cases cover",
    "бела техника": "white goods large appliances major appliances",
    "перење": "washing machine washer laundry перална перални",
    "сушење": "dryer tumble dryer сушара", "миење": "dishwasher dishwashers садомијалка",
    "фрижидери": "fridge fridges refrigerator refrigerators", "вински": "wine cooler",
    "замрзнувачи": "freezer freezers", "вградн": "built-in integrated", "плотни": "hob hobs cooktop",
    "фурни": "oven ovens", "микробранов": "microwave", "аспиратори": "cooker hood range hood extractor",
    "шпорети": "cooker stove range", "садопери": "sink sinks", "бојлери": "boiler water heater",
    "чешми": "faucet tap", "автомати за вода": "water dispenser", "мали апарати": "small appliances",
    "кујнски апарати": "kitchen appliances", "airfyers": "air fryer airfryer",
    "фритези": "fryer deep fryer air fryer", "капсули": "capsules coffee pods",
    "правење леб": "bread maker", "тостери": "toaster sandwich maker", "скари": "grill",
    "миксери": "mixer", "рендање": "slicer grater", "кујнски роботи": "food processor stand mixer",
    "мелница": "grinder coffee grinder", "ваги": "scale scales", "решоа": "hot plate",
    "пареа": "steam steamer", "сецкалници": "chopper", "блендери": "blender blenders",
    "бокали": "kettle electric kettle", "мелење месо": "meat grinder mincer",
    "кафемати": "coffee machine espresso coffee maker", "соковници": "juicer",
    "лична нега": "personal care beauty", "подни ваги": "bathroom scale",
    "обликувачи за коса": "hair straightener curler styler", "чистач за облека": "lint remover fabric shaver",
    "маникир": "manicure pedicure", "депилатори": "epilator", "бричење": "shaver razor",
    "масажери": "massager", "фенови": "hair dryer", "четки за заби": "electric toothbrush",
    "потстрижување": "hair clipper trimmer", "домаќинство": "household",
    "филтрирање вода": "water filter", "под притисок": "pressure washer", "шиење": "sewing machine",
    "правосмукалки": "vacuum vacuums vacuum cleaner hoover robot vacuum",
    "дезинфекција": "cleaning disinfection", "чистач со пареа": "steam cleaner", "пегли": "iron steam iron",
    "кујнски прибор": "kitchenware cookware utensils", "тави": "pan pans pots cookware",
    "бебиња": "baby", "ладење": "cooling", "греење": "heating heater",
    "клима": "air conditioner air conditioning ac aircon",
    "сплит": "split", "вентилатори": "fan fans", "топлински пумпи": "heat pump heat pumps",
    "греалк": "heater heaters", "конвектор": "convector heater", "камин": "fireplace stove",
    "калорифер": "fan heater", "радијатори": "radiator", "цврсто гориво": "wood stove solid fuel",
    "пелети": "pellet stove", "прочистувачи": "air purifier", "овлажнувачи": "humidifier",
    "одвлажнувачи": "dehumidifier", "пепел": "ash vacuum", "спорт": "sport sports",
    "скутери": "e-scooter scooter scooters", "велосипеди": "bike bikes bicycle e-bike",
    "фитнес": "fitness gym", "статични велосипеди": "exercise bike", "траки за трчање": "treadmill",
    "тежини": "weights dumbbells barbell", "тротинети": "scooter kids",
    "кровни": "roof box bike rack", "градина": "garden home", "алат": "tools tool",
    "smart home": "smart home паметен дом", "видео надзор": "cctv security camera surveillance",
    "приклучоци": "smart plug", "сијалици": "bulb bulbs light bulb", "сензори": "sensor sensors",
    "електричен алат": "power tools", "рачен алат": "hand tools", "градинарски": "garden tools",
    "батериски алат": "cordless tools", "пумпи за вода": "water pump", "метли": "mop broom",
    "корпи за отпадоци": "trash bin", "куфери": "suitcase luggage", "декорации": "decoration decor",
    "сервирање": "serving tableware", "готвење": "cooking cookware", "мебел за двор": "garden furniture outdoor",
    "складирање храна": "food storage containers", "чинии": "plates dishes", "чаши": "glasses cups",
    "прибор за јадење": "cutlery", "рамки": "frame frames picture frame", "дифузери": "aroma diffuser",
    "осветлување": "lighting light", "светилки": "lamp lamps light", "батерии": "battery batteries",
    "полначи": "charger chargers", "рефлектори": "floodlight", "лед ленти": "led strip",
    "панели": "panel panels", "зелена енергија": "solar green energy",
    "фотоволтаи": "solar pv photovoltaic", "инвертер": "inverter",
    "двоглед": "binoculars", "телескоп": "telescope", "микроскоп": "microscope",
    "лупа": "magnifier magnifying glass", "дурбин": "spyglass monocular",
    "бонови": "voucher gift card", "огледала": "mirror mirrors", "шах": "chess",
    "ламби": "lamp lamps", "будилници": "alarm clock", "термометри": "thermometer",
}
_EN_TAGS_LOOSE = None


def en_tags(name):
    global _EN_TAGS_LOOSE
    if _EN_TAGS_LOOSE is None:
        _EN_TAGS_LOOSE = [(re.compile(r"(?<![a-z])" + re.escape(loose(k))), v)
                          for k, v in _EN_TAGS.items()]
    ln = loose(name)
    return " ".join(v for k, v in _EN_TAGS_LOOSE if k.search(ln))


def _word_variant(query, fn, keep=frozenset()):
    # Only transliterate purely alphabetic words; model numbers, EANs and
    # codes (anything with a digit) and brand names stay as typed. A brand
    # spelt in the other script only adds typo noise ('Bosch' -> 'босч'
    # matched 11 Gorenje BOS... ovens).
    return " ".join(w if re.search(r"\d", w) or w.lower() in keep else fn(w)
                    for w in query.split())


_brands = [None]
# Brand words that are also everyday Macedonian words typed in Latin; keep
# transliterating these ('dom' -> 'дом').
_BRAND_WORDS_TRANSLITERATED = {"dom", "plamen", "bratstvo", "krom", "lav", "nava", "svim", "doli"}


def brand_words(query):
    """Words of `query` that name a catalogue brand: a whole single-word brand
    ('bosch', 'samsung') or part of a multi-word brand phrase present in the
    query ('cooler master'). Words that merely occur inside a longer brand
    ('fitness' from Orion Fitness, 'master') are still transliterated, since
    'фитнес' finds 90 more genuine products. One facet call, cached."""
    if _brands[0] is None:
        d = search_body({"q": "", "limit": 0, "filter": BASE_FILTERS, "facets": ["brand_name"]})
        names = [b.strip().lower() for b in (d.get("facetDistribution") or {}).get("brand_name", {})]
        _brands[0] = (frozenset(b for b in names if " " not in b and len(b) >= 3
                                and not re.search(r"\d", b) and b.strip("?")
                                and b not in _BRAND_WORDS_TRANSLITERATED),
                      [b for b in names if " " in b])
    single, phrases = _brands[0]
    ql = " " + " ".join(query.lower().split()) + " "
    keep = {w for w in ql.split() if w in single}
    for ph in phrases:
        if f" {ph} " in ql:
            keep.update(ph.split())
    return frozenset(keep)


def query_variants(query, keep=frozenset()):
    """The query as typed, plus its Cyrillic and Latin transliterations.

    Setec's index matches Cyrillic product text far better than Latin
    transliterations of it ('slusalki' finds 13, 'слушалки' 559), and some
    titles are Latin-only, so the client searches both scripts and unions.
    Words in `keep` (brand names) are not transliterated."""
    out = [query]
    if re.search(r"[A-Za-zčšžćđ]", query):
        out.append(_word_variant(query, to_cyrillic, keep))
    if re.search(r"[Ѐ-ӿ]", query):
        out.append(_word_variant(query, to_latin, keep))
    seen, res = set(), []
    for v in out:
        k = v.lower().strip()
        if k and k not in seen:
            seen.add(k)
            res.append(v)
    return res


# --------------------------------------------------------------------------- categories

_tree = [None]


def load_tree():
    """{id: node} from the storefront's own menu tree (Strapi); {} if unavailable."""
    if _tree[0] is not None:
        return _tree[0]
    nodes = {}
    try:
        data = _request("GET", TREE_URL)
    except (StoreError, UsageError) as e:
        warn(f"category tree unavailable ({e}); parents/paths fall back to the index")
        _tree[0] = nodes
        return nodes

    def walk(n, parent, trail):
        cid, slug = n.get("medusaID"), n.get("slug")
        if not cid or not slug:
            return
        name = (n.get("name") or "").strip()
        path = trail + [name]
        if cid not in nodes:   # a node can be listed under two parents; first wins
            nodes[cid] = {"id": cid, "slug": slug, "name": name, "parent": parent,
                          "path": " > ".join(path), "depth": len(trail), "children": []}
            if parent:
                nodes[parent]["children"].append(cid)
        for c in n.get("categories") or []:
            walk(c, cid, path)

    for root in data.get("categories") or []:
        walk(root, None, [])
    _tree[0] = nodes
    return nodes


def descendants(tree, cid):
    out, stack = [], [cid]
    while stack:
        c = stack.pop()
        if c in out:
            continue
        out.append(c)
        stack.extend(tree.get(c, {}).get("children", []))
    return out


def handle_counts():
    d = search_body({"q": "", "limit": 0, "filter": BASE_FILTERS,
                     "facets": ["product_categories.handle"]})
    return (d.get("facetDistribution") or {}).get("product_categories.handle", {})


def cat_url(slug):
    return f"{BASE}/category/{slug}"


def all_categories():
    tree = load_tree()
    fd = handle_counts()
    by_slug = {n["slug"]: n for n in tree.values()}
    parents = [n for n in tree.values() if n["children"]]
    off = [h for h in fd if h not in by_slug]
    queries = [{"q": "", "limit": 0, "facets": ["status"],
                "filter": BASE_FILTERS + ["product_categories.id IN [%s]" %
                                          ", ".join(q(i) for i in descendants(tree, n["id"]))]}
               for n in parents]
    queries += [{"q": "", "limit": 1, "attributesToRetrieve": ["product_categories"],
                 "filter": BASE_FILTERS + [f"product_categories.handle = {q(h)}"],
                 "facets": ["product_categories.handle"]} for h in off]
    res = multi(queries) if queries else []
    union = {n["id"]: sum((r.get("facetDistribution") or {}).get("status", {}).values())
             for n, r in zip(parents, res)}

    recs = []
    for n in tree.values():
        recs.append({"id": n["id"], "slug": n["slug"], "name": n["name"], "path": n["path"],
                     "url": cat_url(n["slug"]), "parent": n["parent"],
                     "count": union.get(n["id"], fd.get(n["slug"], 0))})
    # Categories that hold products but are missing from the site menu: name from
    # a member product, parent inferred from co-membership (smallest category
    # holding >=50% of its products, preferring one that holds all of them).
    info = {}
    for h, r in zip(off, res[len(parents):]):
        name, cid = decode_handle(h), None
        for hit in r.get("hits") or []:
            for pc in hit.get("product_categories") or []:
                if pc.get("handle") == h:
                    name, cid = pc.get("name") or name, pc.get("id")
        co = (r.get("facetDistribution") or {}).get("product_categories.handle", {})
        n = fd[h]
        cand = [a for a, k in co.items() if a != h and k >= 0.5 * n and
                (fd.get(a, 0) > n or (fd.get(a, 0) == n and a < h))]
        parent = min(cand, key=lambda a: (co[a] < n, fd.get(a, 0), a)) if cand else None
        info[h] = {"id": cid or h, "name": name.strip(), "parent_slug": parent}
    for h, i in info.items():
        trail, cur, seen = [i["name"]], i["parent_slug"], {h}
        while cur and cur not in seen:
            seen.add(cur)
            if cur in by_slug:
                trail.insert(0, by_slug[cur]["path"])
                break
            trail.insert(0, info.get(cur, {}).get("name", decode_handle(cur)))
            cur = info.get(cur, {}).get("parent_slug")
        ps = i["parent_slug"]
        parent_id = by_slug[ps]["id"] if ps in by_slug else (info[ps]["id"] if ps in info else None)
        recs.append({"id": i["id"], "slug": h, "name": i["name"], "path": " > ".join(trail),
                     "url": cat_url(h), "parent": parent_id, "count": fd[h],
                     "in_site_menu": False})
    return recs


def resolve_category(arg):
    """-> (label, filter expression, tree-node-or-None). Raises UsageError if unknown."""
    a = arg.strip()
    if a.startswith("http"):
        u = urlparse(a)
        if "setec.mk" not in u.netloc or "/category/" not in u.path:
            raise UsageError(f"not a setec.mk category URL: {arg}")
        a = unquote(u.path.split("/category/", 1)[1].strip("/").split("/")[0])
    tree = load_tree()
    node = tree.get(a) or next((n for n in tree.values() if n["slug"] == a), None)
    if node is None and not a.startswith("pcat_"):
        named = [n for n in tree.values() if n["name"].lower() == a.lower()]
        if len(named) == 1:
            node = named[0]
    if node:
        ids = descendants(tree, node["id"])
        expr = "product_categories.id IN [%s]" % ", ".join(q(i) for i in ids)
        return node["path"], expr, node
    fd = handle_counts()
    if a in fd:
        return decode_handle(a), f"product_categories.handle = {q(a)}", None
    if a.startswith("pcat_"):
        expr = f"product_categories.id = {q(a)}"
        if count(BASE_FILTERS + [expr]):
            return a, expr, None
    raise UsageError(f"unknown category {arg!r} - run `setec.py categories --grep ...` "
                     "and pass an id, slug or url it prints")


def category_path(pcs):
    tree = load_tree()
    best = None
    for pc in pcs or []:
        n = tree.get(pc.get("id"))
        if n and (best is None or n["depth"] > best["depth"]):
            best = n
    if best:
        return best["path"]
    names = [pc.get("name") for pc in pcs or [] if pc.get("name")]
    return " > ".join(names) or None


# --------------------------------------------------------------------------- records

def _int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _gtin_ok(code):
    if not re.fullmatch(r"\d{8}|\d{12,14}", code):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def split_codes(raw):
    """catalogue_number may hold several comma-separated codes (supplier batches)
    and sometimes internal non-GTIN codes; report the first checksum-valid GTIN."""
    codes = [re.sub(r"\s+", "", c) for c in (raw or "").split(",") if c.strip()]
    best = next((c for c in codes if _gtin_ok(c)), None)
    return best, codes


def stock_state(qty, available, threshold):
    if qty is None:
        return (bool(available) if available is not None else None), None
    qty = int(qty)
    if qty <= 0:
        return False, "0 units - out of stock (site offers 'Извести ме' / notify me)"
    if threshold and qty <= threshold:
        return True, (f"{qty} unit{'s' if qty > 1 else ''} across Setec locations - low stock: "
                      "site shows 'Нарачај' (order request, Setec calls back to confirm) "
                      "instead of add-to-cart")
    return True, f"{qty} units across Setec locations"


def attributes_of(pairs):
    out = {}
    for p in pairs or []:
        if "::" not in p:
            continue
        k, v = p.split("::", 1)
        if not k.strip() or not v.strip():
            continue
        out[k] = v if k not in out else (out[k] + " | " + v if v not in out[k].split(" | ") else out[k])
    return out


def club_list_price(price_lists):
    """The 'Клуб цена' the site prints: the club ('Web Prices') price-list amount."""
    pls = price_lists or []
    pl = (next((x for x in pls if ((x or {}).get("price_list") or {}).get("role") == "club"), None)
          or next((x for x in pls if ((x or {}).get("price_list") or {}).get("title") == "Web Prices"), None))
    return _int(((pl or {}).get("prices") or {}).get("amount"))


# ---- price validity windows (contract fields price_valid_until / member_price_valid_until)
def _skopje_offset(utc):
    """UTC offset of Europe/Skopje at an aware instant: CET, CEST from the last Sunday of March
    01:00 UTC to the last Sunday of October 01:00 UTC (EU rules; zoneinfo when available)."""
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("Europe/Skopje")).utcoffset()
    except Exception:  # noqa: BLE001  (no tzdata on this machine)
        def last_sunday(month):
            d = _dt.date(utc.year, month, 31)
            return _dt.datetime(d.year, month, d.day - (d.weekday() + 1) % 7, 1, tzinfo=_dt.timezone.utc)
        summer = last_sunday(3) <= utc < last_sunday(10)
        return _dt.timedelta(hours=2 if summer else 1)


def skopje_iso(value, naive_is_utc=False):
    """A shop's date/time -> ISO 8601 with the Europe/Skopje offset ('2026-10-04T23:59:59+02:00'),
    or None for empty / placeholder values (year < 2000, .NET's 0001-01-01).
    Accepts datetime, epoch seconds or ms, '/Date(ms)/', ISO 8601 with or without offset or fraction,
    and 'DD.MM.YYYY[ HH:MM[:SS]]'. A naive value is the shop's wall-clock time in Skopje unless
    naive_is_utc; a bare date means the end of that day (23:59:59)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    d, date_only = None, False
    if isinstance(value, _dt.datetime):
        d = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\s*\d{9,13}\s*", value)):
        n = float(value)
        d = _dt.datetime.fromtimestamp(n / 1000 if n > 1e11 else n, _dt.timezone.utc)
    elif isinstance(value, str):
        s = value.strip()
        m = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", s)
        if m:
            ms = int(m.group(1))
            if ms <= 0:
                return None
            d = _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)
        else:
            m = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
            if m:
                dd, mo, y, hh, mi, ss = m.groups()
                date_only = hh is None
                try:
                    d = _dt.datetime(int(y), int(mo), int(dd), int(hh or 0), int(mi or 0), int(ss or 0))
                except ValueError:
                    return None
            else:
                m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?", s)
                if not m:
                    return None
                date_only = m.group(2) is None
                tz = m.group(3)
                if tz and tz != "Z" and ":" not in tz:
                    tz = tz[:3] + ":" + tz[3:]
                try:
                    d = _dt.datetime.fromisoformat(m.group(1) + "T" + (m.group(2) or "00:00:00")
                                                   + ("+00:00" if tz == "Z" else (tz or "")))
                except ValueError:
                    return None
    if d is None or d.year < 2000:
        return None
    if date_only:
        d = d.replace(hour=23, minute=59, second=59)
    if d.tzinfo is None:
        if naive_is_utc:
            d = d.replace(tzinfo=_dt.timezone.utc)
        else:   # wall-clock Skopje time: find the offset that maps back to the same wall time
            guess = d.replace(tzinfo=_dt.timezone.utc) - _skopje_offset(d.replace(tzinfo=_dt.timezone.utc))
            d = d.replace(tzinfo=_dt.timezone(_skopje_offset(guess)))
    utc = d.astimezone(_dt.timezone.utc).replace(microsecond=0)
    return utc.astimezone(_dt.timezone(_skopje_offset(utc))).isoformat()



NO_WINDOW = object()   # the source cannot tell when price_mkd ends: the record leaves the key out


def _list_end(ends_at):
    """A price list's ends_at -> ISO (ends then), None (no end: standing) or NO_WINDOW (unreadable)."""
    if ends_at is None or ends_at == "":
        return None
    return skopje_iso(ends_at, naive_is_utc=True) or NO_WINDOW


def price_list_window(price_lists, price, list_id=None, id_known=False):
    """When price_mkd ends: the price_list.ends_at (an ISO instant) of the Medusa price list that
    sets it, None for a standing price, NO_WINDOW when the record cannot tell.

    Index hits name the list (id_known): calculated_price.price_list_id, and a null id means
    Medusa charged the variant's base price, which has no end (the stale-club iMac: list 114,659
    above base 99,999 -> base charged) -> None. The detail API gives no id, so the list is the
    one whose amount == price_mkd; a product with no price list at all (price_lists == []) is on
    its base price -> None. Standing on 2026-10-03: the 'Web Prices' club list, ends_at null.
    NO_WINDOW: no price, the price_lists are missing, no list matches (id or amount), the
    matches disagree on ends_at, or ends_at is unreadable."""
    if price is None:
        return NO_WINDOW
    if id_known and not list_id:
        return None
    if price_lists is None:
        return NO_WINDOW
    pls = [x or {} for x in price_lists]
    if list_id:
        pick = [x for x in pls if (x.get("price_list") or {}).get("id") == list_id]
    elif not pls:
        return None
    else:
        pick = [x for x in pls if _int((x.get("prices") or {}).get("amount")) == price]
    ends = {(x.get("price_list") or {}).get("ends_at") for x in pick}
    return _list_end(ends.pop()) if len(ends) == 1 else NO_WINDOW


def record_from_hit(h, threshold):
    v = (h.get("variants") or [{}])[0] or {}
    cp = v.get("calculated_price") or {}
    price, orig = _int(cp.get("calculated_amount")), _int(cp.get("original_amount"))
    in_stock, note = stock_state(h.get("total_web_quantity"), h.get("is_web_available"), threshold)
    ean, codes = split_codes(v.get("catalogue_number"))
    brand = (h.get("brand_name") or "").strip()
    if not brand.strip("?"):   # a few brands are stored as '?????' (lost Cyrillic)
        brand = None
    rec = {
        "store": STORE,
        "id": h.get("id"),
        "sku": h.get("external_id") or None,
        "title": (h.get("title") or "").strip(),
        "url": f"{BASE}/products/{h.get('handle')}",
        "brand": brand,
        "price_mkd": price,
        "regular_price_mkd": orig if (orig and price and orig > price) else None,
        "price_valid_until": None,   # set below (or left out when the record cannot tell)
        "in_stock": in_stock,
        "stock_note": note,
        "category": category_path(h.get("product_categories")),
        "ean": ean,
        "attributes": attributes_of(h.get("attribute_pairs")),
    }
    calc = cp.get("calculated_price")
    window = price_list_window(h.get("price_lists"), price,
                               calc.get("price_list_id") if isinstance(calc, dict) else None,
                               id_known=isinstance(calc, dict) and "price_list_id" in calc)
    if window is NO_WINDOW:
        del rec["price_valid_until"]
    else:
        rec["price_valid_until"] = window
    extra = {}
    if len(codes) > 1 or (codes and not ean):
        extra["catalogue_numbers"] = codes
    shown = club_list_price(h.get("price_lists"))
    if shown is not None and price is not None and shown != price:
        # ~14 products carry a stale club price list above the price Medusa actually
        # charges (calculated_amount); the site still prints the stale figure as
        # 'Клуб цена'. price_mkd stays the charged price.
        extra["site_shown_club_price_mkd"] = shown
    if extra:
        rec["extra"] = extra
    return rec


# --------------------------------------------------------------------------- fetching

def fetch_window(filters, query="", limit=None, sort=True, total_out=None):
    """All hits of one query window (<= CAP), paging PAGE at a time."""
    want = CAP if limit is None else min(limit, CAP)
    hits, offset = [], 0
    while offset < want:
        n = min(PAGE, want - offset)
        body = {"q": query, "filter": filters, "limit": n, "offset": offset,
                "attributesToRetrieve": LIST_FIELDS, "matchingStrategy": "all"}
        if sort:
            body["sort"] = [PRICE + ":asc"]
        if offset == 0 and total_out is not None:
            body["facets"] = ["status"]
        d = search_body(body)
        if offset == 0 and total_out is not None:
            total_out.append(sum((d.get("facetDistribution") or {}).get("status", {}).values()))
        page = d.get("hits") or []
        hits += page
        if len(page) < n:
            break
        offset += n
    return hits


def plan_chunks(filters, total, by_brand=True, depth=0):
    """Split a filter whose match count exceeds CAP into windows <= CAP:
    brand bins first, then price bisection for anything still too large."""
    if total <= CAP:
        return [(filters, total)]
    if by_brand:
        d = search_body({"q": "", "limit": 0, "filter": filters, "facets": ["brand_name"]})
        brands = (d.get("facetDistribution") or {}).get("brand_name", {})
        chunks, bins = [], []
        for b, n in sorted(brands.items(), key=lambda x: -x[1]):
            if n > CAP:
                chunks += plan_chunks(filters + [f"brand_name = {q(b)}"], n, False, depth + 1)
                continue
            for bn in bins:
                if bn[1] + n <= CAP:
                    bn[0].append(b)
                    bn[1] += n
                    break
            else:
                bins.append([[b], n])
        for names, n in bins:
            chunks.append((filters + ["brand_name IN [%s]" % ", ".join(q(b) for b in names)], n))
        rest = total - sum(brands.values())
        if rest > 0 or not brands:
            rf = filters + (["NOT brand_name IN [%s]" % ", ".join(q(b) for b in brands)]
                            if brands else [])
            chunks += plan_chunks(rf, count(rf), False, depth + 1)
        return chunks
    d = search_body({"q": "", "limit": 0, "filter": filters, "facets": [PRICE]})
    st = (d.get("facetStats") or {}).get(PRICE) or {}
    lo, hi = st.get("min"), st.get("max")
    if lo is None or hi is None or lo >= hi or depth > 14:
        warn(f"cannot split a window of {total} products further; only {CAP} will come back")
        return [(filters, total)]
    mid = (lo + hi) / 2
    a, b = filters + [f"{PRICE} < {mid}"], filters + [f"{PRICE} >= {mid}"]
    na, nb = count(a), count(b)
    out = plan_chunks(a, na, False, depth + 1) + plan_chunks(b, nb, False, depth + 1)
    if na + nb < total:
        rf = filters + [f"NOT ({PRICE} < {mid} OR {PRICE} >= {mid})"]
        out.append((rf, total - na - nb))
    return out


def fetch_all(filters, limit=None):
    """Every product matching filters (chunked past the 1000-hit ceiling), price asc."""
    total = count(filters)
    if limit is not None and limit <= CAP:
        hits = fetch_window(filters, limit=limit)
        return hits, total
    chunks = plan_chunks(filters, total)
    if len(chunks) > 1:
        log(f"{total} products exceed the index's {CAP}-hit ceiling; "
             f"fetching in {len(chunks)} brand/price windows")
    seen, hits = set(), []
    for f, _ in chunks:
        for h in fetch_window(f):
            if h["id"] not in seen:
                seen.add(h["id"])
                hits.append(h)
    if len(hits) != total:
        warn(f"expected {total} products, collected {len(hits)} (index changed mid-run?)")
    hits.sort(key=lambda h: (_int(((h.get("variants") or [{}])[0].get("calculated_price") or {})
                                  .get("calculated_amount")) is None,
                             _int(((h.get("variants") or [{}])[0].get("calculated_price") or {})
                                  .get("calculated_amount")) or 0))
    if limit is not None:
        hits = hits[:limit]
    return hits, total


def filter_expr(token):
    """--filter TOKEN: 'Name::Value' as printed by facets, or a raw Meilisearch
    filter expression (e.g. 'variants.calculated_price.calculated_amount < 20000')."""
    t = token.strip()
    if "::" in t and not re.search(r"\s(=|!=|>=?|<=?|IN|TO|EXISTS|IS|AND|OR)\s|^NOT\s", t):
        name, value = t.split("::", 1)
        if name == BRAND_ATTR:   # the site's own brand filter ORs both fields
            return f"(brand_name = {q(value)} OR attribute_pairs = {q(t)})"
        return f"attribute_pairs = {q(t)}"
    return f"({t})"


# --------------------------------------------------------------------------- output

def emit(records, path, kind="products"):
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=1)
        print(len(records))
        return
    if kind == "products":
        for r in records:
            if r.get("error"):
                print(f"!! {r['input']}: {r['error']}")
                continue
            p = f"{r['price_mkd']:,}" if r.get("price_mkd") is not None else "n/a"
            was = f" (was {r['regular_price_mkd']:,})" if r.get("regular_price_mkd") else ""
            st = {True: "in", False: "out", None: "?"}[r.get("in_stock")]
            qty = re.match(r"(\d+) unit", r.get("stock_note") or "")
            st = f"{st}:{qty.group(1)}" if qty and r.get("in_stock") else st
            print(f"{p:>9} ден{was:<16} {st:<7} {r['title'][:70]:<70}  {r['url']}")
    else:
        for r in records:
            print(r)


# --------------------------------------------------------------------------- commands

def cmd_info(a):
    print(json.dumps({
        "store": STORE, "name": NAME, "base_url": BASE,
        "capabilities": ["search", "categories", "list", "detail", "facets", "filter",
                         "ean_in_listing", "ean_in_detail", "stock_qty",
                         "per_location_stock", "warranty"],
        "sells": ("Large consumer-electronics and home chain (~14k products): computers and "
                  "PC parts, laptops, monitors, phones and accessories, TVs/audio, large and "
                  "small home appliances, air conditioning/heating, kitchenware, tools, "
                  "garden, sport/bicycles, lighting, consoles, smart home."),
        "notes": ("Meilisearch catalogue + Medusa detail API, no bot protection. price_mkd = "
                  "club/web price (valid for web orders); regular = struck 'Редовна цена'; "
                  "price_valid_until = ends_at of the Medusa price list behind price_mkd "
                  "(null: the standing 'Web Prices' list or the base price has none; left out "
                  "when the list behind the price cannot be identified). "
                  "Stock = units summed over ~40 stores + central warehouse; 1-3 units => site "
                  "shows 'Нарачај' (order request). Search unions Cyrillic+Latin "
                  "transliterations; each query window caps at 1000 hits, list chunks past it. "
                  "Extra commands: gaps, brands, stores."),
    }, ensure_ascii=False))
    return 0


def cmd_search(a):
    query = (a.query or "").strip()
    if not query:
        raise UsageError("empty query")
    if a.limit is not None and a.limit <= 0:
        raise UsageError("--limit must be positive")
    filters = BASE_FILTERS + ([IN_STOCK] if a.in_stock else [])
    thr = order_threshold()
    seen, out = set(), []
    latin_words = [w for w in query.split() if re.search(r"[A-Za-zčšžćđ]", w) and not re.search(r"\d", w)]
    keep = brand_words(query) if latin_words else frozenset()
    for i, v in enumerate(query_variants(query, keep)):
        room = (a.limit - len(out)) if a.limit else None
        if room is not None and room <= 0:
            break
        tot = []
        hits = fetch_window(filters, query=v, limit=a.limit, sort=False, total_out=tot)
        total = tot[0] if tot else len(hits)
        new = [h for h in hits if h["id"] not in seen]
        for h in new:
            seen.add(h["id"])
        added = new[:room] if room is not None else new
        out += added
        label = "query" if i == 0 else "transliteration"
        log(f"{label} {v!r}: {total} match(es), {len(added)} new")
        if total > CAP and (a.limit is None or a.limit > CAP):
            warn(f"{v!r} matched {total} products but the index returns at most {CAP} "
                 "(relevance-ranked); narrow the query or use `list <category>`")
    recs = [record_from_hit(h, thr) for h in out]
    if not recs:
        log("0 products found (genuine zero from the store's search)")
    emit(recs, a.json)
    return 0


def cmd_categories(a):
    recs = all_categories()
    if a.grep:
        try:
            pat = re.compile(a.grep, re.I)
            # loose() lowercases, which would turn escapes such as \D or \S into \d or
            # \s, so the transliterated pattern is skipped for those.
            lpat = re.compile(loose(a.grep), re.I) if not re.search(r"\\[A-Z]", a.grep) else None
        except re.error as e:
            raise UsageError(f"bad --grep regex: {e}")
        def hit(r):
            fields = [r["name"], r["path"], r["slug"], decode_handle(r["slug"]), en_tags(r["name"])]
            return any(pat.search(f) or (lpat and lpat.search(loose(f))) for f in fields if f)
        recs = [r for r in recs if hit(r)]
        if not recs:
            log(f"no category matches {a.grep!r}; try a Macedonian stem (e.g. 'телевиз|televiz')")
    if a.json:
        emit(recs, a.json)
    else:
        for r in recs:
            print(f"{r['count']:>6}  {r['path']}  [{r['slug']}]")
        log(f"{len(recs)} categories")
    return 0


def cmd_list(a):
    if a.limit is not None and a.limit <= 0:
        raise UsageError("--limit must be positive")
    label, expr, _ = resolve_category(a.category)
    cat_filters = BASE_FILTERS + [expr]
    filters = cat_filters + ([IN_STOCK] if a.in_stock else []) + [filter_expr(t) for t in a.filter or []]
    hits, total = fetch_all(filters, a.limit)
    thr = order_threshold()
    recs = [record_from_hit(h, thr) for h in hits]
    log(f"{label}: {total} product(s) match, {len(recs)} returned")
    if not recs:
        if count(cat_filters) == 0:
            warn(f"category {a.category!r} exists but holds no products")
            emit(recs, a.json)
            return 2
        tokens = [t.strip() for t in a.filter or [] if filter_expr(t).startswith(("attribute_pairs", "(brand_name"))]
        if tokens:
            d = search_body({"q": "", "limit": 0, "filter": cat_filters,
                             "facets": ["attribute_pairs", "brand_name"]})
            fd = d.get("facetDistribution") or {}
            # Meilisearch compares filter strings case-insensitively; so does this check.
            known = {k.lower() for k in fd.get("attribute_pairs", {})}
            known |= {f"{BRAND_ATTR}::{b}".lower() for b in fd.get("brand_name", {})}
            bad = [t for t in tokens if t.lower() not in known]
            if bad:
                emit(recs, a.json)
                raise UsageError(f"filter token(s) {bad} never occur in this category - "
                                 "copy tokens verbatim from `setec.py facets <category>`")
        log("category has products, but none pass --in-stock/--filter together "
             "(genuine zero)")
    emit(recs, a.json)
    return 0


def cmd_facets(a):
    label, expr, _ = resolve_category(a.category)
    d = search_body({"q": "", "limit": 0, "filter": BASE_FILTERS + [expr],
                     "facets": ["attribute_pairs", "status"]})
    fdist = d.get("facetDistribution") or {}
    pairs = fdist.get("attribute_pairs", {})
    total = sum(fdist.get("status", {}).values())
    rows = []
    for tok, n in pairs.items():
        name, _, value = tok.partition("::")
        if name.strip() and value.strip():
            rows.append({"name": name, "value": value, "count": n, "token": tok})
    rows.sort(key=lambda r: (r["name"], -r["count"], r["value"]))
    if not total:
        raise UsageError(f"{label} holds no products")
    if len(pairs) >= 4000:
        warn("facet list hit maxValuesPerFacet (4000) and may be truncated")
    log(f"{label}: {total} products, {len(rows)} attribute values "
         "(values are often blank on part of a category - see `gaps`)")
    if a.json:
        emit(rows, a.json)
    else:
        for r in rows:
            print(f"{r['count']:>6}  {r['token']}")
    return 0


def resolve_products(inputs):
    """-> list of (input, handle or None, error or None), in input order."""
    out, ids, codes = [], {}, {}
    for raw in inputs:
        s = raw.strip()
        if s.startswith("http"):
            u = urlparse(s)
            if "setec.mk" not in u.netloc:
                out.append([raw, None, "not a setec.mk URL"])
            elif "/products/" in u.path:
                out.append([raw, unquote(u.path.split("/products/", 1)[1].strip("/").split("/")[0]), None])
            elif "/category/" in u.path:
                out.append([raw, None, "that is a category URL - use `list`"])
            else:
                out.append([raw, None, "unrecognised setec.mk URL (expected /products/<handle>)"])
        elif s.startswith("prod_"):
            ids[s] = None
            out.append([raw, None, None])
        elif re.fullmatch(r"\d{3,14}", s):   # on-site Шифра or an EAN
            codes[s] = None
            out.append([raw, None, None])
        elif s:
            out.append([raw, s, None])
        else:
            out.append([raw, None, "empty input"])
    if ids:
        d = search_body({"q": "", "limit": len(ids), "attributesToRetrieve": ["id", "handle"],
                         "filter": ["id IN [%s]" % ", ".join(q(i) for i in ids)]})
        for h in d.get("hits") or []:
            ids[h["id"]] = h["handle"]
    for c in codes:
        d = search_body({"q": c, "limit": 20, "filter": BASE_FILTERS,
                         "attributesToRetrieve": ["handle", "external_id", "variants.catalogue_number"]})
        for h in d.get("hits") or []:
            cats = split_codes(((h.get("variants") or [{}])[0] or {}).get("catalogue_number"))[1]
            if h.get("external_id") == c or c in cats:
                codes[c] = h["handle"]
                break
    for row in out:
        s = row[0].strip()
        if s in ids:
            row[1] = ids[s]
            row[2] = None if ids[s] else "no product with that id"
        elif s in codes:
            row[1] = codes[s]
            row[2] = None if codes[s] else "no product with that Шифра/EAN"
    return out


def per_location(product):
    rows = {}
    for v in product.get("variants") or []:
        for inv in v.get("inventory") or []:
            for lvl in inv.get("location_levels") or []:
                loc = (lvl.get("stock_locations") or [{}])[0] or {}
                name = ((loc.get("sales_channels") or [{}])[0] or {}).get("name")
                if name:
                    rows[name] = rows.get(name, 0) + int(lvl.get("available_quantity") or 0)
    return [{"location": n, "in_stock": qn > 0, "quantity": qn, "walk_in": n not in NON_WALK_IN,
             "in_web_total": n not in NOT_IN_WEB_TOTAL}
            for n, qn in sorted(rows.items(), key=lambda x: (-x[1], x[0]))]


def cmd_detail(a):
    resolved = resolve_products(a.inputs)
    handles = [h for _, h, e in resolved if h and not e]
    index = {}
    for i in range(0, len(handles), 100):
        part = handles[i:i + 100]
        d = search_body({"q": "", "limit": len(part), "attributesToRetrieve": LIST_FIELDS,
                         "filter": ["handle IN [%s]" % ", ".join(q(h) for h in part)]})
        for h in d.get("hits") or []:
            index[h["handle"]] = h
    thr = order_threshold()
    recs, ok = [], 0
    for raw, handle, err in resolved:
        if err or not handle:
            recs.append({"input": raw, "error": err or "unresolved"})
            continue
        try:
            d = _request("GET", DETAIL_URL, params={"handle": handle}, allow_404=True)
        except (StoreError, UsageError) as e:
            d, derr = None, str(e)
        else:
            derr = None
        p = (d or {}).get("product") if isinstance(d, dict) else None
        hit = index.get(handle)
        if not p and not hit:
            recs.append({"input": raw, "error": derr or f"no product with handle {handle!r}"})
            continue
        if hit:
            rec = record_from_hit(hit, thr)
        else:   # in the detail API but not the index (should not happen)
            rec = record_from_hit({"id": p.get("id"), "title": p.get("title"),
                                   "handle": handle, "external_id": p.get("external_id"),
                                   "brand_name": ((p.get("product_extra_details") or {})
                                                  .get("manufacturer") or {}).get("name"),
                                   "variants": p.get("variants"),
                                   "total_web_quantity": (p.get("variants") or [{}])[0].get("total_web_quantity"),
                                   "is_web_available": (p.get("variants") or [{}])[0].get("is_web_available"),
                                   "product_categories": p.get("categories"),
                                   "price_lists": p.get("price_lists")}, thr)
        extra = rec.pop("extra", {}) or {}
        rec["input"] = raw
        if p:
            v = (p.get("variants") or [{}])[0] or {}
            cp = v.get("calculated_price") or {}
            price, orig = _int(cp.get("calculated_amount")), _int(cp.get("original_amount"))
            if price is not None:
                # the index window names the list by id; the detail API can only match amounts,
                # so it is used only when the price moved or the index could not tell
                if price != rec.get("price_mkd") or "price_valid_until" not in rec:
                    window = price_list_window(p.get("price_lists"), price)
                    if window is NO_WINDOW:
                        rec.pop("price_valid_until", None)
                    else:
                        rec["price_valid_until"] = window
                rec["price_mkd"] = price
                rec["regular_price_mkd"] = orig if orig and orig > price else None
                shown = club_list_price(p.get("price_lists"))
                extra.pop("site_shown_club_price_mkd", None)
                if shown is not None and shown != price:
                    extra["site_shown_club_price_mkd"] = shown
            if v.get("total_web_quantity") is not None:
                rec["in_stock"], rec["stock_note"] = stock_state(
                    v.get("total_web_quantity"), v.get("is_web_available"), thr)
            ed = p.get("product_extra_details") or {}
            months = ed.get("output_warranty")
            rec["warranty"] = (f"{months} months" if isinstance(months, (int, float)) and months > 0
                               else ("none listed (0 months)" if months == 0 else None))
            rec["specs"] = re.sub(r"\s+", " ", p.get("description") or "").strip() or None
            rec["per_location_stock"] = per_location(p)
            floors = {k: ed.get(k) for k in ("recommended_retail_price", "min_web_price_with_vat",
                                             "min_retail_price_with_vat",
                                             "min_wholesale_price_with_vat") if ed.get(k) is not None}
            extra.update({
                "warranty_months": months,
                "price_floors": floors or None,
                "discount_percentage": v.get("discount_percentage"),
                "price_list": [pl.get("price_list") for pl in p.get("price_lists") or []] or None,
            })
            if not rec.get("ean") and ed.get("catalogue_number"):
                rec["ean"], codes = split_codes(ed.get("catalogue_number"))
                if len(codes) > 1 or (codes and not rec["ean"]):
                    extra["catalogue_numbers"] = codes
        else:
            rec.update({"warranty": None, "specs": None, "per_location_stock": None})
            extra["detail_error"] = derr or "detail API returned no product"
            warn(f"{handle}: detail API failed ({extra['detail_error']}); index data only")
        rec["extra"] = extra
        recs.append(rec)
        ok += 1
    if a.json:
        emit(recs, a.json)
    else:
        for r in recs:
            if r.get("error"):
                print(f"!! {r['input']}: {r['error']}")
                continue
            was = f" (was {r['regular_price_mkd']:,})" if r.get("regular_price_mkd") else ""
            print(f"\n{r['title']}\n  {r['url']}")
            print(f"  {r['price_mkd']:,} ден{was} | {r['stock_note']} | warranty {r['warranty']}")
            print(f"  Шифра {r['sku']} | EAN {r['ean']} | brand {r['brand']} | {r['category']}")
            for s in r.get("per_location_stock") or []:
                if s["quantity"] > 0:
                    tag = "" if s["walk_in"] else "  (no walk-in)"
                    print(f"      {s['quantity']:>4} x {s['location']}{tag}")
    return 0 if ok else 2


# --- setec-only commands ------------------------------------------------------
# gaps, brands and stores: Setec-only extras on the same HTTP layer, category
# resolution and base filters as `list`.

def cmd_gaps(a):
    """Products in a category with no value for one attribute: a value filter on
    that attribute silently drops them. Each record carries the product description
    as `specs`, where the missing value is usually stated."""
    if a.limit is not None and a.limit <= 0:
        raise UsageError("--limit must be positive")
    label, expr, _ = resolve_category(a.category)
    hits, total = fetch_all(BASE_FILTERS + [expr])
    if not hits:
        raise UsageError(f"category {a.category!r} holds no products")
    attr = a.attr.strip()
    names = {}
    for h in hits:
        for k in attributes_of(h.get("attribute_pairs")):
            names[k] = names.get(k, 0) + 1
    if attr not in names:
        alt = [n for n in names if n.lower() == attr.lower()]
        if len(alt) == 1:
            attr = alt[0]
        else:
            top = ", ".join(f"{n} ({c})" for n, c in sorted(names.items(), key=lambda x: -x[1])[:25])
            raise UsageError(f"no product in {label} has attribute {a.attr!r}; names here: {top}")
    thr = order_threshold()
    blank = [h for h in hits if not attributes_of(h.get("attribute_pairs")).get(attr)]
    rows = [record_from_hit(h, thr) for h in blank]
    if a.in_stock:
        rows = [r for r in rows if r["in_stock"]]
    shown = rows[:a.limit] if a.limit else rows
    desc = {}
    ids = [r["id"] for r in shown]
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        d = search_body({"q": "", "limit": len(part), "attributesToRetrieve": ["id", "description"],
                         "filter": ["id IN [%s]" % ", ".join(q(x) for x in part)]})
        for h in d.get("hits") or []:
            desc[h["id"]] = re.sub(r"\s+", " ", h.get("description") or "").strip() or None
    for r in shown:
        r["specs"] = desc.get(r["id"])
    summary = (f"{label}: {total} products; {total - len(blank)} carry a usable {attr!r} value; "
               f"{len(blank)} have it blank or absent"
               + (f" ({len(rows)} of them in stock)" if a.in_stock else "")
               + f" - a filter on {attr!r} drops these silently")
    if a.json:
        log(summary)
        emit(shown, a.json)
    else:
        print(summary + ":")
        for r in shown:
            emit([r], None)
            if r["specs"]:
                print(f"{'':>10}{r['specs'][:160]}")
    return 0


def cmd_brands(a):
    filters = list(BASE_FILTERS)
    label = "whole catalogue"
    if a.category:
        label, expr, _ = resolve_category(a.category)
        filters.append(expr)
    d = search_body({"q": "", "limit": 0, "filter": filters, "facets": ["brand_name", "status"]})
    fd = d.get("facetDistribution") or {}
    brands = sorted(fd.get("brand_name", {}).items(), key=lambda x: (-x[1], x[0].lower()))
    total = sum(fd.get("status", {}).values())
    if not total:
        raise UsageError(f"{label} holds no products")
    unbranded = total - sum(n for _, n in brands)
    log(f"{label}: {total} products, {len(brands)} brands"
         + (f", {unbranded} without a brand_name" if unbranded > 0 else ""))
    rows = [{"brand": b, "count": n} for b, n in brands]
    if a.json:
        emit(rows, a.json, kind="rows")
    else:
        for r in rows:
            print(f"{r['count']:>6}  {r['brand']}")
    return 0


def cmd_stores(a):
    """Per-location stock rolled up across a shortlist: which shop has most of it."""
    rows = resolve_products(a.inputs)
    out, agg = [], {}
    for raw, handle, err in rows:
        p = None
        if not err and handle:
            d = _request("GET", DETAIL_URL, params={"handle": handle}, allow_404=True)
            p = (d or {}).get("product") if isinstance(d, dict) else None
            err = None if p else f"no product with handle {handle!r}"
        if not p:
            warn(f"skipping {raw!r}: {err or 'unresolved'}")
            out.append({"input": raw, "error": err or "unresolved"})
            continue
        locs = per_location(p)
        out.append({"input": raw, "title": (p.get("title") or "").strip(),
                    "url": f"{BASE}/products/{handle}", "per_location_stock": locs})
        for l in locs:
            if l["quantity"] > 0:
                m = agg.setdefault(l["location"], {"location": l["location"], "models": 0, "units": 0,
                                                   "walk_in": l["walk_in"], "titles": []})
                m["models"] += 1
                m["units"] += l["quantity"]
                m["titles"].append(out[-1]["title"])
    good = [r for r in out if not r.get("error")]
    if not good:
        raise UsageError("no resolvable products")
    if a.json:
        emit(out, a.json, kind="rows")
    else:
        for m in sorted(agg.values(), key=lambda m: (-m["models"], -m["units"], m["location"])):
            tag = "" if m["walk_in"] else "  (no walk-in)"
            print(f"{m['location']:26} {m['models']:2}/{len(good)} models, {m['units']:4} units{tag}")
    return 0


# --------------------------------------------------------------------------- main

def main(argv=None):
    global QUIET, VERBOSE
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common]).set_defaults(fn=cmd_info)

    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p.set_defaults(fn=cmd_categories)

    p = sub.add_parser("list", parents=[common])
    p.add_argument("category")
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", action="append", default=[],
                   help="'Name::Value' token from `facets`, or a raw Meilisearch filter")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("detail", parents=[common])
    p.add_argument("inputs", nargs="+", help="product URL, prod_ id, handle, Шифра or EAN")
    p.set_defaults(fn=cmd_detail)

    p = sub.add_parser("facets", parents=[common])
    p.add_argument("category")
    p.set_defaults(fn=cmd_facets)

    p = sub.add_parser("gaps", parents=[common], help="products in a category with no value for an attribute")
    p.add_argument("category")
    p.add_argument("--attr", required=True)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int, help="list at most N blank products")
    p.set_defaults(fn=cmd_gaps)

    p = sub.add_parser("brands", parents=[common], help="brand counts, optionally within a category")
    p.add_argument("category", nargs="?")
    p.set_defaults(fn=cmd_brands)

    p = sub.add_parser("stores", parents=[common], help="per-store stock rollup across products")
    p.add_argument("inputs", nargs="+")
    p.set_defaults(fn=cmd_stores)

    a = ap.parse_args(argv)
    QUIET, VERBOSE = a.quiet, a.verbose and not a.quiet
    try:
        return a.fn(a)
    except Blocked as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 3
    except UsageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except StoreError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
