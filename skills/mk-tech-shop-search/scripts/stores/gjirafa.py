#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.28", "beautifulsoup4>=4.11"]
# ///
"""Gjirafa50 (https://gjirafa50.mk) and ZirafaMall (https://zirafamall.mk) catalogue client.
See references/client-contract.md and references/gjirafa.md.

Both shops run the same Gjirafa storefront (a customised nopCommerce on ASP.NET Core behind
Cloudflare), so one client serves both. --site picks the shop and becomes record["store"].

Data sources (the storefront's own XHR endpoints, JSON whose "html" member is the card grid):
    GET /category/products?categoryId=<id>&pagenumber=<n>&orderby=16[&pagesize=48]
        [&is=true][&hls=true][&hd=true][&ms=<id,..>][&specs=<spec>,<opt>,..;..][&price=a-b]
    GET /product/search?q=<query>&pagenumber=<n>[&is=true]
    GET /Catalog/GetManufacturerFilter?entityId=<categoryId>&entityType=Category
Category tree: the home page mega-menu (departments, groups, first children) plus each truncated
group's page (its sub-category grid); cached on disk for a day. Product detail and category
filters are read from the server-rendered pages (/p/<id> redirects to the canonical slug URL).

    gjirafa.py --site {gjirafa50,zirafamall} info
    gjirafa.py --site ... search "<query>" [--limit N] [--in-stock] [--json PATH]
    gjirafa.py --site ... categories [--grep REGEX] [--deep] [--counts] [--refresh] [--json PATH]
    gjirafa.py --site ... list <id|slug|url> [--in-stock] [--limit N] [--filter TOKEN ...]
                              [--no-brands] [--json PATH]
    gjirafa.py --site ... detail <url|slug|id> [...] [--json PATH]
    gjirafa.py --site ... facets <slug|url|id> [--counts] [--json PATH]
    (--site may also follow the sub-command; -v logs every request)

Exit codes: 0 ok (incl. a genuine zero-hit search), 1 unexpected error, 2 bad usage / unknown
category or product, 3 blocked (one stderr line starting "BLOCKED:").
"""

import argparse
import datetime as dt
import html as htmlmod
import json
import math
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CAPABILITIES_COMMON = ["search", "categories", "list", "detail", "facets", "filter",
                       "ean_in_detail", "stock_qty", "warranty", "delivery_estimate"]

SITES = {
    "gjirafa50": {
        "name": "Gjirafa50",
        "base": "https://gjirafa50.mk",
        # categories honour pagesize=12/24/36/48 (anything else -> 24); search ignores it (24)
        "page_size": 48,
        "has_totals": True,          # category XHR shows "вкупно N"
        "flags": {"in-stock": "is", "local-stock": "hls"},
        "capabilities": CAPABILITIES_COMMON,
        "sells": ("Large online electronics shop (department totals sum to ~104,000 listings, ~69,000 "
                  "orderable; some products are filed in two trees): laptops, "
                  "desktops, monitors, servers/NAS, phones, tablets, e-readers, GPS, TVs, projectors, "
                  "audio/hi-fi, cameras, dashcams, consoles and games, VR, gaming chairs, smart home, "
                  "smartwatches, e-scooters, drones, small home appliances (vacuums, air purifiers, "
                  "personal care), printers and ink, keyboards/mice/headsets, speakers, networking, "
                  "CCTV, office equipment, memory cards, external drives, cables, UPS, solar (PV), "
                  "PC components (GPU, CPU, storage, motherboards, RAM, PSU, cooling, cases). "
                  "No large kitchen/laundry appliances."),
        "notes": ("Most orderable items are supplier stock delivered in ~3-4 weeks; only '48h'-badge "
                  "items (list --filter local-stock) are local. in_stock = orderable. price_mkd = "
                  "card price incl. VAT; almost every card shows a struck-through 'was' price. "
                  "Search is fuzzy AND-matching (typo tolerant) and silently drops words that match "
                  "nothing; EAN, Код (sku) and model codes are searchable. Listing brand is inferred "
                  "from the title against the category's manufacturer filter (null in search). "
                  "Two parallel monitor trees exist (Додатоци > Монитор is the full one). Listings "
                  "over 10,000 (the search engine's window) are walked as price slices. "
                  "Warranty: 1 year store-wide. ~95% of SKUs are mirrored on ZirafaMall at the same "
                  "price (join on sku)."),
    },
    "zirafamall": {
        "name": "ZirafaMall",
        "base": "https://zirafamall.mk",
        "page_size": None,           # fixed 16 per page; pagesize is ignored
        "has_totals": False,         # category XHR shows page numbers only
        "flags": {"in-stock": "is", "local-stock": "hls", "discount": "hd"},
        "capabilities": CAPABILITIES_COMMON + ["seller"],
        "sells": ("Gjirafa's multi-vendor marketplace: technology (mostly mirrored from Gjirafa50: "
                  "computers, phones, TVs, audio, gaming, smart devices, accessories, PC parts), "
                  "large and small home appliances (бела техника), home and decor, garden, pools, "
                  "kitchen, lighting, heating, cleaning, pets, furniture, textiles, cosmetics and "
                  "personal care, clothing and fashion accessories, sport and fitness, baby and "
                  "toys, books and office/school supplies, work tools, car and moto parts, health, "
                  "food and drink."),
        "notes": ("Marketplace: seller = vendor ('Basics from GjirafaMall' carries the Gjirafa50 "
                  "mirror). international_supplier = the site's own turtle badge ('меѓународен "
                  "добавувач', 3-4 week delivery). in_stock = orderable; the site's own in-stock filter is "
                  "stale, so list --in-stock walks the whole listing and keeps unbadged cards. Category "
                  "listings have no product total and render fewer cards than indexed. Search is "
                  "fuzzy AND-matching that drops words matching nothing; EAN, Код and model codes "
                  "are searchable. Listing brand is inferred from the title against the category's "
                  "manufacturer filter (null in search). Listings over 10,000 (the search engine's "
                  "window) are walked as price slices. Warranty only when the product page states it."),
    },
}

ORDER_NEWEST = 16       # 0 relevance, 10 price asc, 11 price desc, 16 newest, 17 biggest discount
ORDER_RELEVANCE = 0
PACE_SECONDS = 0.5
MAX_RETRIES = 4
ZM_PAGE = 16            # ZirafaMall's fixed page size (cards per page before server-side hiding)
SEARCH_PAGE = 24        # Gjirafa50 search page size
SEARCH_CAP = 1000       # default cap for `search` without --limit (results are relevance-ordered)
ES_WINDOW = 10000       # Elasticsearch result window: deeper pages of any listing come back empty
SLICE_TARGET = 9000     # price slices are split until each holds at most this many listings
PRICE_CEILING = 20_000_000
MAX_WORD_CHECKS = 5     # per-word hit checks for multi-word searches
INTL_MIN_DAYS = 22      # Gjirafa50: delivery quotes this far out are the international-supplier tier
CAT_TTL_S = 24 * 3600   # category-index cache lifetime
CAT_CACHE_VERSION = 2
MAX_DEEP_PAGES = 40     # page fetches per `categories --deep` run
MAX_COUNTS = 40         # count requests per `categories --counts` / `facets --counts` run

CHALLENGE_MARKERS = (
    "<title>just a moment", "cf-chl", "challenge-platform", "cf-turnstile", "turnstile.js",
    "attention required! | cloudflare", "checking your browser", "g-recaptcha", "h-captcha",
)

MK_MONTHS = {"јануари": 1, "февруари": 2, "март": 3, "април": 4, "мај": 5, "јуни": 6, "јули": 7,
             "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12}

# Cyrillic spellings used in titles -> the manufacturer name as the site lists it (used only when
# that manufacturer is in the category's own manufacturer filter).
BRAND_ALIASES = {
    "самсунг": "Samsung", "епл": "Apple", "ејпл": "Apple", "сони": "Sony", "шаоми": "Xiaomi",
    "ксиаоми": "Xiaomi", "хуавеј": "Huawei", "леново": "Lenovo", "асус": "ASUS", "ејсус": "ASUS",
    "ејсер": "Acer", "acer": "Acer", "дел": "Dell", "филипс": "Philips", "бош": "Bosch",
    "логитек": "Logitech", "лоџитек": "Logitech", "моторола": "Motorola", "нокиа": "Nokia",
    "панасоник": "Panasonic", "тошиба": "Toshiba", "кингстон": "Kingston", "сандиск": "SanDisk",
    "вестерн дигитал": "Western Digital", "сигејт": "Seagate", "гигабајт": "Gigabyte",
    "разер": "Razer", "редрагон": "Redragon", "генесис": "Genesis", "трaст": "Trust",
    "траст": "Trust", "тефал": "Tefal", "браун": "Braun", "ровента": "Rowenta", "сенкор": "Sencor",
    "гоpење": "Gorenje", "горење": "Gorenje", "беко": "Beko", "хаер": "Haier", "тцл": "TCL",
}
# Manufacturer names that are generic words or placeholders: never used for title -> brand.
BRAND_SKIP = {"no name", "noname", "sourcing", "logo", "blow", "rebel", "planet", "mountain",
              "fury", "art", "ca", "ms", "ltc", "roger", "hiro", "spacer", "krom", "act", "action",
              "access", "accura", "basic", "basics", "smart", "pro", "max", "home", "go", "one",
              "plus", "eco", "tech", "power", "line", "light", "star", "sport", "set", "box",
              "gaming", "game", "air", "blue", "black", "white", "red", "green", "gold", "silver",
              "classic", "style", "life", "kids", "baby", "us", "eu", "uk", "it", "hd", "tv", "pc",
              "usb", "led", "rgb", "mini", "micro", "nano", "ultra", "super", "mega", "next",
              "vision", "sound", "audio", "media", "digital", "electronics", "global", "world",
              "city", "urban", "nature", "natural", "original", "premium", "royal", "master"}

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}

# English words and colloquial Macedonian synonyms added to a category's --grep haystack when its
# name or path contains the stem (matched at a word start, case-insensitively), so
# `categories --grep 'перални|washing'` finds Машини за алишта and `--grep phone` finds Мобилни.
_EN_TAGS = {
    "компјутер": "computer computers pc desktop", "лаптоп": "laptop laptops notebook",
    "монитор": "monitor monitors display screen", "сервер": "server servers",
    "mini pc": "desktop small form factor", "all in one": "desktop aio",
    "мобилни телефони": "smartphone smartphones mobile phone cell phone паметни телефони смартфон",
    "мобилни": "mobile phone phones smartphone", "touchscreen": "smartphone smartphones паметни телефони смартфон",
    "телефон": "phone telephone", "телекомуникаци": "telecom telephony landline",
    "таблет": "tablet tablets ipad", "графички таблет": "drawing tablet pen tablet",
    "е-book": "ebook e-reader kindle reader", "e-book": "ebook e-reader kindle reader",
    "навигаци": "gps navigation sat nav", "тв": "tv tvs television televisions телевизор телевизори",
    "проектор": "projector projectors beamer", "аудио": "audio sound", "звучни": "speaker speakers",
    "звучник": "speaker speakers bluetooth speaker", "soundbar": "sound bar", "сабвуфер": "subwoofer",
    "амплифаер": "amplifier amp", "hifi": "hi-fi stereo", "грамофон": "turntable record player vinyl",
    "радио": "radio", "приемник": "receiver set-top box tuner",
    "слушалки": "headphones headset headsets earphones earbuds", "микрофон": "microphone mic",
    "фотоапарат": "camera cameras photo", "камер": "camera cameras", "веб-камера": "webcam web camera",
    "објектив": "lens lenses", "дрон": "drone drones", "статив": "tripod stand",
    "конзол": "console consoles playstation xbox nintendo", "видео игри": "video games game games",
    "игри": "game games", "контролер": "controller controllers gamepad joystick",
    "контролор": "controller controllers gamepad joystick",
    "волан": "steering wheel racing wheel", "гејминг": "gaming", "vr опрема": "virtual reality headset",
    "виртуелна реалност": "vr virtual reality headset",
    "смарт часовник": "smartwatch smart watch паметен часовник", "часовници": "watch watches smartwatch",
    "фитнес нараквици": "fitness tracker band", "smart home": "паметен дом home automation",
    "скутер": "scooter scooters e-scooter", "велосипед": "bike bikes bicycle e-bike cycling",
    "печатач": "printer printers", "скенер": "scanner scanners", "инкџет": "inkjet printer",
    "полнење и тонери": "toner ink cartridge", "тастатур": "keyboard keyboards",
    "глувче": "mouse mice", "подлог": "pad mousepad mat", "рутер": "router routers wifi",
    "мрежн": "network networking lan ethernet", "мрежа": "network networking",
    "мрежен": "network networking", "access point": "wifi wireless", "switches": "network switch",
    "системи за камери": "cctv security camera surveillance", "dvr": "cctv recorder surveillance",
    "кабел": "cable cables cord", "кабли": "cable cables cord", "полнач": "charger chargers charging",
    "батерии": "battery batteries", "ups": "uninterruptible power supply",
    "заштита од струја": "surge protector power strip", "заштита при напојување": "surge protector power strip",
    "соларн": "solar pv photovoltaic", "фотоволтаи": "solar pv photovoltaic",
    "диск": "disk disks drive drives hdd ssd storage hard drive", "екстерн": "external portable",
    "надворешни дискови": "external drive portable drive", "мемориск": "memory card sd microsd",
    "читач на картички": "card reader", "usb": "flash drive usb stick pendrive",
    "ram": "memory dimm", "оперативн": "ram memory dimm",
    "графичка картичка": "gpu graphics card video card", "графички картички": "gpu graphics card video card",
    "процесор": "cpu processor processors", "матичн": "motherboard motherboards mainboard",
    "извори за напојување": "psu power supply", "куќишт": "pc case chassis",
    "ладилни": "cooler cooling fan", "звучна картичка": "sound card", "оптички погон": "optical drive dvd",
    "бела текника": "white goods large appliances major appliances бела техника",
    "фрижидер": "fridge fridges refrigerator refrigerators ладилник",
    "замрзнувач": "freezer freezers",
    "машини за алишта": "washing machine washing machines washer laundry перална перални "
                        "peralna peralni машина за перење",
    "машини за сушење": "dryer tumble dryer clothes dryer сушара", "tharëse": "dryer tumble dryer сушара",
    "машини за миење садови": "dishwasher dishwashers садомијалка", "рерни": "oven ovens",
    "шпорет": "cooker cookers stove range", "плотни": "hob hobs cooktop",
    "микробранов": "microwave microwaves", "аспиратор": "cooker hood range hood extractor",
    "бојлер": "boiler water heater", "болјер": "boiler water heater",
    "клима": "air conditioner air conditioning ac aircon", "климатизер": "air conditioner air conditioning ac",
    "вентилатор": "fan fans", "правосмукалк": "vacuum vacuums vacuum cleaner hoover",
    "пегли": "iron irons steam iron", "прочистувач": "air purifier",
    "прочистување на воздухот": "air purifier", "диспензер за вода": "water dispenser",
    "апарати за мраз": "ice maker", "мали електрични апарати": "small appliances",
    "апарати за домаќинството": "small appliances household appliances",
    "уреди за домаќинство": "small appliances household appliances", "кујн": "kitchen",
    "греење": "heating heater heaters", "грејачи": "heater heaters", "радијатори": "radiator radiators",
    "ваги": "scale scales", "вага": "scale scales", "бричење": "shaver razor epilator",
    "нега за коса": "hair care", "козметика": "cosmetics beauty", "шминк": "makeup make-up",
    "мириси": "perfume fragrance", "облека": "clothing clothes apparel", "чевли": "shoes footwear",
    "чанти": "bag bags handbag", "торби": "bag bags", "куфери": "suitcase luggage",
    "накит": "jewelry jewellery", "очила": "glasses sunglasses", "спорт": "sport sports",
    "фитнес": "fitness gym", "машини за вежбање": "exercise machine home gym",
    "лента за трчање": "treadmill", "тегови": "weights dumbbells", "деца": "kids children",
    "бебе": "baby", "бебињ": "baby", "играчк": "toy toys", "кукли": "doll dolls",
    "сложувалки": "puzzle puzzles jigsaw", "друштвени игри": "board games",
    "колички": "stroller pram", "пелени": "diapers nappies", "книги": "books book",
    "библиотека": "books stationery", "канцелар": "office", "училиш": "school",
    "алат": "tools tool", "косилки": "lawn mower mowers", "градин": "garden gardening",
    "базени": "pool pools swimming pool", "мебел": "furniture", "осветлување": "lighting light lights",
    "ламби": "lamp lamps", "сијалиц": "bulb bulbs light bulb", "авто": "car auto automotive",
    "гуми и фелни": "tyres tires wheels rims", "мото": "motorcycle moto", "здравје": "health",
    "медицин": "medical", "витамини": "vitamins", "суплементи": "supplements", "храна": "food",
    "пијалоци": "drinks beverages", "кафе": "coffee", "животни": "pets pet", "кучиња": "dog dogs",
    "мачки": "cat cats", "текстил": "textiles bedding", "пешкири": "towels", "прекривки": "blankets",
    "декор": "decor decoration", "санитариј": "bathroom sanitary", "кади": "bathtub bath shower",
    "мијалници": "sink sinks", "чешми": "faucet tap", "чистење": "cleaning",
    "интерфон": "intercom doorbell", "аларм": "alarm security", "сеф": "safe safes",
    "столови": "chair chairs", "седишта": "seat seats chair", "karrige": "chair chairs gaming chair",
    "биро": "desk", "калкулатор": "calculator", "уништувач на хартија": "paper shredder",
    "телескоп": "telescope binoculars microscope", "термометар": "thermometer", "термометри": "thermometer",
    "outlet": "clearance", "што има ново": "new arrivals",
}
# keys of three letters or fewer ("тв", "ram", "ups") must be whole words
_EN_TAGS_RX = [(re.compile(r"(?<!\w)" + re.escape(k) + (r"(?!\w)" if len(k) <= 3 else ""), re.I), v)
               for k, v in _EN_TAGS.items()]


def en_tags(text):
    return " ".join(v for rx, v in _EN_TAGS_RX if rx.search(text or ""))


class StoreError(RuntimeError):
    """Unexpected response / HTTP failure (exit 1)."""


class Blocked(StoreError):
    """Challenge / block page / WAF instead of data (exit 3)."""


class NotFound(StoreError):
    """Unknown category or product (exit 2)."""


class Usage(StoreError):
    """Bad usage (exit 2)."""


def _collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def _mkd(text):
    """Displayed price '5,990 MKD.' / '11,190 MKD.' -> int (comma = thousands separator)."""
    if text is None:
        return None
    s = re.sub(r"MKD\.?|ден\.?|Denar", "", str(text), flags=re.I).strip()
    m = re.fullmatch(r"([\d.,\s]*?)(?:[.,](\d{1,2}))?", s)
    if m and m.group(2) is not None and re.search(r"[.,]\d{3}", m.group(1) or ""):
        s = m.group(1)                 # '5.990,00' -> drop the minor units
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def _mkd_attr(value):
    """data-discountedprice '5990,00' / '2790,0000' -> int (comma = decimal separator)."""
    if not value:
        return None
    try:
        return int(round(float(str(value).strip().replace(",", "."))))
    except ValueError:
        return None


def _looks_like_challenge(text):
    low = (text or "")[:30000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def _mk_date(text):
    m = re.fullmatch(r"(\d{1,2})\s+(\S+)\s+(\d{4})", text.strip())
    if not m or m.group(2).lower() not in MK_MONTHS:
        return None
    return dt.date(int(m.group(3)), MK_MONTHS[m.group(2).lower()], int(m.group(1)))


def _cache_dir():
    d = os.environ.get("MKSHOP_CACHE_DIR")
    if not d:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        d = os.path.join(base, "mk-tech-shop-search")
    return d


class Gjirafa:
    def __init__(self, site, pace=PACE_SECONDS, verbose=False):
        if site not in SITES:
            raise Usage(f"unknown site {site!r}; use one of {', '.join(SITES)}")
        self.site = site
        self.cfg = SITES[site]
        self.base = self.cfg["base"]
        self.host = urlparse(self.base).hostname
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "mk,en;q=0.8",
        })
        self.pace = pace
        self.verbose = verbose
        self._last = 0.0
        self._brands = None
        self.requests_made = 0
        self._index = None

    def log(self, msg):
        print(f"[{self.site}] {msg}", file=sys.stderr)

    # ------------------------------------------------------------------ http
    def _request(self, path, params=None, xhr=False):
        url = path if path.startswith("http") else self.base + path
        headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, */*"} if xhr else {}
        r = None
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=60)
            except requests.RequestException as e:
                self._last = time.monotonic()
                if attempt == MAX_RETRIES:
                    raise StoreError(f"GET {url} failed: {e}") from e
                self.log(f"network error ({e.__class__.__name__}), retrying")
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                self.log(f"GET {r.url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s")
            if r.headers.get("cf-mitigated") or (
                    r.status_code in (403, 503) and _looks_like_challenge(r.text)):
                raise Blocked(
                    f"HTTP {r.status_code} on {r.url}, cf-mitigated={r.headers.get('cf-mitigated')!r}, "
                    f"cf-ray={r.headers.get('cf-ray')}: Cloudflare challenge/block page, not data "
                    f"({_collapse(r.text[:160])!r})")
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** attempt
                self.log(f"HTTP {r.status_code}, retrying in {delay}s")
                time.sleep(min(delay, 60))
                continue
            if r.status_code in (401, 403):
                raise Blocked(f"HTTP {r.status_code} on {r.url}, cf-ray={r.headers.get('cf-ray')} "
                              f"({_collapse(r.text[:160])!r})")
            return r
        if r is not None and r.status_code == 429:
            raise Blocked(f"HTTP 429 (rate limited) on {r.url} after {MAX_RETRIES} attempts with backoff, "
                          f"cf-ray={r.headers.get('cf-ray')}, retry-after={r.headers.get('Retry-After')!r}")
        raise StoreError(f"GET {url}: still HTTP {r.status_code if r is not None else '?'} "
                         f"after {MAX_RETRIES} attempts")

    def _json(self, path, params):
        r = self._request(path, params=params, xhr=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 404 or "page-not-found" in r.url:
            what = f"category id {params.get('categoryId')}" if "categoryId" in params else path
            raise NotFound(f"{what}: the site answers page-not-found (unknown id)")
        if r.status_code != 200 or "json" not in ctype:
            if _looks_like_challenge(r.text):
                raise Blocked(f"HTTP {r.status_code} on {r.url}: challenge page instead of JSON, "
                              f"cf-ray={r.headers.get('cf-ray')}")
            raise StoreError(f"{r.url}: expected JSON, got HTTP {r.status_code} ({ctype}); "
                             f"body starts {r.text[:120]!r}")
        data = r.json()
        if not isinstance(data, dict) or "html" not in data or "totalpages" not in data:
            raise StoreError(f"{r.url}: unexpected payload keys "
                             f"{list(data)[:10] if isinstance(data, dict) else type(data)}")
        return data

    def _page(self, path):
        """GET a storefront HTML page; -> (response, text). Unknown pages -> NotFound."""
        r = self._request(path)
        final = urlparse(r.url).path
        if r.status_code == 404 or "page-not-found" in r.url:
            raise NotFound(f"{path}: not found (HTTP {r.status_code}, landed on {r.url})")
        if r.status_code != 200:
            raise StoreError(f"{path}: HTTP {r.status_code}")
        if _looks_like_challenge(r.text) and "categoryId=" not in r.text and "price-value-" not in r.text:
            raise Blocked(f"HTTP {r.status_code} on {r.url}: challenge page, cf-ray={r.headers.get('cf-ray')}")
        lang = re.search(r'<html[^>]*\blang="([^"]+)"', r.text[:3000])
        if lang and lang.group(1).lower().startswith("sq"):
            # The storefront picks the language from the User-Agent (browser UA -> mk-MK, some
            # bot-like UAs -> sq-MK Albanian); names and stock parsing expect Macedonian. (Some
            # Macedonian pages carry a stray lang="en", so only Albanian is rejected.)
            raise StoreError(f"{r.url}: page served in Albanian ({lang.group(1)!r}); the site "
                             f"may have stopped honouring the browser User-Agent")
        return r, final, r.text

    def path_of(self, arg, kind="page"):
        """URL / slug / path -> site path; rejects the other shop's URLs."""
        arg = arg.strip()
        if arg.startswith("http"):
            u = urlparse(arg)
            host = (u.hostname or "").removeprefix("www.")
            if host and host != self.host:
                raise Usage(f"{arg} is not a {self.site} URL (expected {self.base})")
            return u.path or "/"
        return "/" + arg.strip("/")

    # ---------------------------------------------------------------- cards
    def parse_cards(self, fragment):
        soup = BeautifulSoup(fragment or "", "html.parser")
        cards = []
        for box in soup.select("div.item-box"):
            item = box.select_one("[data-productid]")
            a = box.select_one("h3.product-title a") or box.select_one("a.product-title-lines")
            if not item or not a:
                continue
            pid = item["data-productid"]
            sku = None
            for el in box.select("[onclick*=sendAddToCartEvent]"):
                m = re.search(r"sendAddToCartEvent\(\s*'(\d+)'\s*,\s*'([^']*)'", el.get("onclick", ""))
                if m and m.group(1) == pid:
                    sku = m.group(2) or None
                    break
            prices = box.select_one("section.prices")
            now_el = old_el = None
            if prices:
                old_el = prices.select_one(".old-price")
                now_el = next((sp for sp in prices.select("span.price")
                               if "old-price" not in (sp.get("class") or [])), None)
            top = box.select_one("section.absolute") or box
            top_txt = _collapse(top.get_text(" "))
            turtle = bool(top.select_one("img[src*=breshka]")) or "меѓународен добавувач" in top_txt
            vendor = box.select_one(".productbox-vendor span")
            cards.append({
                "id": pid,
                "sku": sku,
                "title": _collapse(a.get_text()),
                "href": a.get("href"),
                "price": _mkd(now_el.get_text()) if now_el else None,
                "data_price": _mkd_attr(item.get("data-discountedprice")),
                "old_price": _mkd(old_el.get_text()) if old_el and _collapse(old_el.get_text()) else None,
                "sold_out": bool(top.select_one("i.icon-sold-out")) or "Продадено" in top_txt or "E shitur" in top_txt,
                "local_48h": bool(re.search(r"\b48h\b", top_txt)),
                "turtle": turtle,
                "vendor": _collapse(vendor.get_text()) if vendor else None,
            })
        return cards

    def card_to_record(self, c, category=None):
        price = c["price"] if c["price"] is not None else c["data_price"]
        if c["price"] is not None and c["data_price"] is not None and abs(c["price"] - c["data_price"]) > 1:
            self.log(f"note: {c['id']} card price {c['price']} != data-discountedprice {c['data_price']}")
        if c["sold_out"]:
            in_stock, note = False, "Продадено (sold out)"
        else:
            in_stock = True
            if c["local_48h"]:
                note = "48h: local stock, fast delivery"
            elif c["turtle"]:
                note = ("orderable; 'Овој производ доаѓа од меѓународен добавувач' "
                        "(international supplier, ~3-4 weeks)")
            else:
                note = "orderable; no 48h local-stock badge (supplier stock, delivery date on the product page)"
        if c["turtle"]:
            intl = True
        elif c["local_48h"]:
            intl = False
        elif self.site == "zirafamall" and not c["sold_out"]:
            intl = False      # ZirafaMall puts the turtle badge on every orderable card it applies to
        else:
            intl = None       # sold-out cards never carry the badge; Gjirafa50 has no badge at all
        href = c["href"] or ""
        return {
            "store": self.site,
            "id": c["id"],
            "sku": c["sku"],
            "title": c["title"],
            "url": self.base + href if href.startswith("/") else href,
            "brand": self.brand_from_title(c["title"]),
            "price_mkd": price,
            "regular_price_mkd": c["old_price"] if c["old_price"] and price and c["old_price"] > price else None,
            "in_stock": in_stock,
            "stock_note": note,
            "category": category,
            "ean": None,
            "seller": c["vendor"],
            "international_supplier": intl,
            "delivery_estimate": "48h" if c["local_48h"] else None,
        }

    # ------------------------------------------------------------- brands
    def manufacturers(self, cid):
        """[(id, name)] from the site's manufacturer filter for a category (memoised)."""
        memo = self.__dict__.setdefault("_manu_memo", {})
        if cid not in memo:
            memo[cid] = self._manufacturers(cid)
        return memo[cid]

    def _manufacturers(self, cid):
        r = self._request("/Catalog/GetManufacturerFilter",
                          params={"entityId": cid, "entityType": "Category"}, xhr=True)
        if r.status_code != 200:
            raise StoreError(f"{r.url}: HTTP {r.status_code}")
        if _looks_like_challenge(r.text):
            raise Blocked(f"HTTP {r.status_code} on {r.url}: challenge page instead of the manufacturer "
                          f"list, cf-ray={r.headers.get('cf-ray')}")
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for inp in soup.select("input[data-manufacturer-id]"):
            lab = soup.select_one(f'label[for="{inp.get("id")}"]')
            name = _collapse(lab.get_text()) if lab else None
            if name:
                out.append((str(inp["data-manufacturer-id"]), name))
        return out

    def load_brands(self, cid):
        names = {n for _, n in self.manufacturers(cid)}
        names = {n for n in names if n and n.lower() not in BRAND_SKIP and len(n) >= 2}
        self._brands = sorted(names, key=len, reverse=True)
        return self._brands

    def brand_from_title(self, title):
        if not self._brands:
            return None
        best = None
        norm_title = title.replace("-", " ")
        for name in self._brands:
            base = name.rstrip("!").strip()
            for v in {base, base.replace("-", " "), base.replace(" ", "")}:
                if len(v) < 2:
                    continue
                flags = 0 if len(v) <= 3 else re.I
                m = re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", norm_title, flags)
                if m and (best is None or m.start() < best[0] or (m.start() == best[0] and len(name) > len(best[1]))):
                    best = (m.start(), name)
        if best is None:
            low = title.lower()
            lower_brands = {b.lower(): b for b in self._brands}
            hits = [(low.find(a), lower_brands[name.lower()]) for a, name in BRAND_ALIASES.items()
                    if name.lower() in lower_brands
                    and re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", low)]
            if hits:
                best = min(hits)
        return best[1] if best else None

    # ---------------------------------------------------------------- walks
    def _page_size(self, path):
        if path == "/product/search":
            return SEARCH_PAGE if self.site == "gjirafa50" else ZM_PAGE
        return self.cfg["page_size"] or ZM_PAGE

    def _first(self, path, params, order):
        p = dict(params)
        if order is not None:
            p["orderby"] = order
        return self._json(path, dict(p, pagenumber=1))

    @staticmethod
    def _meta(data):
        tot = re.search(r"вкупно\s+([\d.,]+)", data.get("paginationHtml") or "")
        return {"totalpages": data.get("totalpages") or 0,
                "totalHits": data.get("totalHits"),
                "site_total": int(re.sub(r"\D", "", tot.group(1))) if tot else None}

    @staticmethod
    def _estimate(meta, size):
        """Listings matched (exact on Gjirafa50 and in search; pages x size, an upper bound, on
        ZirafaMall categories)."""
        if meta["site_total"] is not None:
            return meta["site_total"]
        if meta["totalHits"] is not None:
            return meta["totalHits"]
        return meta["totalpages"] * size

    def _walk_once(self, path, params, limit=None, order=None, first=None, quiet=False, keep=None):
        """keep: optional card predicate; only kept cards are returned and count towards limit
        (meta["listed"] counts every unique card seen)."""
        params = dict(params)
        if order is not None:
            params["orderby"] = order
        cards, seen, dups, page, meta = [], set(), 0, 1, {}
        while True:
            data = first if (page == 1 and first is not None) else self._json(path, dict(params, pagenumber=page))
            batch = self.parse_cards(data["html"])
            if page == 1:
                meta = self._meta(data)
                need = meta["totalpages"]
                if limit and batch:
                    need = min(need, -(-limit // len(batch)), -(-ES_WINDOW // len(batch)))
                if need > 20 and not quiet:
                    self.log(f"{need} pages to walk (~{need * (self.pace + 0.7) / 60:.0f} min at the polite "
                             f"pace)" + ("" if limit else "; pass --limit to stop early"))
            elif not data.get("totalpages") and not batch:
                # Elasticsearch serves at most ES_WINDOW results per query: deeper pages come back empty
                meta["window_capped"] = True
                page -= 1
                break
            elif page % 50 == 0 and not quiet:
                self.log(f"... page {page}/{meta['totalpages']}, {len(cards)} products so far")
            n_raw = len(re.findall(r'class="item-box"', data["html"] or ""))
            if n_raw != len(batch):
                self.log(f"WARNING: page {page}: {n_raw} item boxes but {len(batch)} parsed cards "
                         f"(layout change?)")
            for c in batch:
                if c["id"] in seen:
                    dups += 1
                    continue
                seen.add(c["id"])
                if keep is None or keep(c):
                    cards.append(c)
            if limit and len(cards) >= limit:
                meta.update(pages=page, dups=dups, truncated=True, listed=len(seen))
                return cards[:limit], meta
            if page >= meta["totalpages"]:
                break
            page += 1
        meta.update(pages=page, dups=dups, listed=len(seen))
        return cards, meta

    @staticmethod
    def _expected(meta):
        return meta.get("site_total") if meta.get("site_total") is not None else meta.get("totalHits")

    def walk(self, path, params, limit=None, order=None, fallback=None, label="", slice_prices=False,
             keep=None):
        """Walk every page (up to limit).

        Drift: if the walk shows duplicate rows across page boundaries (a product was listed or
        delisted mid-walk), walk again in `fallback` order and merge. A shortfall against the site
        total without duplicates is not drift: both shops render fewer cards than they index.
        Window: the search engine serves at most ES_WINDOW (10,000) results per query. With
        slice_prices, a larger listing is walked as price slices (the site's own price=a-b filter,
        inclusive) that each fit in the window, and merged by product id."""
        size = self._page_size(path)
        first = self._first(path, params, order)
        est = self._estimate(self._meta(first), size)
        if slice_prices and est > ES_WINDOW and not (limit and limit <= ES_WINDOW - size):
            return self._walk_sliced(path, params, limit, order, label, est, size, keep)
        cards, meta = self._walk_once(path, params, limit, order, first=first, keep=keep)
        if meta.get("truncated"):
            return cards, meta
        exp = self._expected(meta)
        if fallback is not None and meta["dups"]:
            self.log(f"{label}: walk shows drift ({meta['dups']} duplicate rows, {len(cards)} unique "
                     f"vs site total {exp}); re-walking with orderby={fallback} and merging")
            more, m2 = self._walk_once(path, params, limit, fallback, keep=keep)
            ids = {c["id"] for c in cards}
            cards += [c for c in more if c["id"] not in ids]
            meta["pages"] += m2["pages"]
            if limit:
                cards = cards[:limit]
        if meta.get("window_capped"):
            self.log(f"WARNING: {label}: results truncated at the store's {ES_WINDOW:,}-result search window "
                     f"({len(cards)} of ~{est:,}); narrow the query, filter, or list a subcategory")
        meta["unique"] = len(cards)
        return cards, meta

    def _walk_sliced(self, path, params, limit, order, label, est, size, keep=None):
        lo, hi = 0, PRICE_CEILING
        m = re.fullmatch(r"(\d+)-(\d+)", str(params.get("price", "")))
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
        self.log(f"{label}: ~{est:,} listings exceed the store's {ES_WINDOW:,}-result search window; "
                 f"walking price slices (price=a-b, inclusive) and merging by id")
        todo = [(lo, hi, None)]
        cards, seen, pages, slices, capped, listed = [], set(), 0, 0, 0, set()
        while todo:
            a, b, first = todo.pop(0)
            p = dict(params, price=f"{a}-{b}")
            if first is None:
                first = self._first(path, p, order)
            e = self._estimate(self._meta(first), size)
            if e > SLICE_TARGET and b - a >= 2:
                base = max(a, 50)
                mid = int(math.sqrt(base * b)) if b > 4 * base else (a + b) // 2
                mid = min(max(mid, a + 1), b - 1)
                # overlapping bounds (inclusive filter): a product priced exactly `mid` lands in both
                # slices and is deduplicated; nothing between two integers can fall through
                todo[0:0] = [(a, mid, None), (mid, b, None)]
                continue
            got, meta = self._walk_once(path, p, None, order, first=first, quiet=True)
            listed.update(c["id"] for c in got)
            if keep is not None:
                got = [c for c in got if keep(c)]
            slices += 1
            pages += meta["pages"]
            capped += bool(meta.get("window_capped"))
            new = [c for c in got if c["id"] not in seen]
            seen.update(c["id"] for c in new)
            cards += new
            if e:
                self.log(f"... price {a}-{b}: {len(got)} listings ({len(cards)} so far)")
            if limit and len(cards) >= limit:
                cards = cards[:limit]
                break
        if capped:
            self.log(f"WARNING: {label}: {capped} price slice(s) still hit the {ES_WINDOW:,}-result window")
        meta = {"totalpages": None, "totalHits": None, "site_total": est if self.cfg["has_totals"] else None,
                "pages": pages, "dups": 0, "slices": slices, "unique": len(cards), "listed": len(listed),
                "truncated": bool(limit and len(cards) >= limit)}
        return cards, meta

    # ---------------------------------------------------------- categories
    def _cache_path(self):
        return os.path.join(_cache_dir(), f"gjirafa-{self.site}-categories.json")

    def _load_cache(self):
        try:
            with open(self._cache_path(), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == CAT_CACHE_VERSION and time.time() - data.get("built", 0) < CAT_TTL_S:
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save_cache(self, idx):
        path = self._cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            if self.verbose:
                self.log(f"note: could not write category cache {path}: {e}")

    def _parse_menu(self, text):
        """Home-page mega-menu -> nodes {slug: node}. Departments carry their id in the icon
        class (icon-<categoryId>); groups list at most 3 children plus 'Погледни повеќе'."""
        soup = BeautifulSoup(text, "html.parser")
        wrap = soup.select_one(".categories-wrapper")
        if wrap is None:
            raise StoreError("home page has no .categories-wrapper mega-menu (layout change?)")
        nodes = {}

        def add(slug, name, parent, level, cid=None, truncated=False):
            slug = unquote(slug).rstrip("/") or "/"
            if slug in nodes or not slug.startswith("/") or slug == "/":
                return slug
            nodes[slug] = {"slug": slug.lstrip("/"), "name": _collapse(name), "id": cid,
                           "parent": parent, "level": level, "expanded": False,
                           "truncated": truncated}
            return slug

        for cw in wrap.select(":scope > .category-wrapper"):
            a = cw.select_one(".main-cat a")
            if not a or not a.get("href"):
                continue
            icon = a.select_one("i[class*=icon-]")
            m = re.search(r"\bicon-(\d+)\b", " ".join(icon.get("class", []))) if icon else None
            d = add(a["href"], a.get_text(" "), None, 1, m.group(1) if m else None)
            nodes[d]["expanded"] = True          # its groups are all in the menu
            sub = cw.select_one(".sub-cat")
            if not sub:
                continue
            for g in sub.select("div.flex.w-full.flex-col"):
                ga = g.select_one(":scope > a")
                if not ga or not ga.get("href"):
                    continue
                kids = g.select(":scope > div > a")
                more = any("Погледни" in k.get_text() for k in kids)
                gs = add(ga["href"], ga.get_text(" "), d, 2, truncated=more)
                nodes[gs]["expanded"] = not more     # an untruncated group lists all its children
                for k in kids:
                    if "Погледни" in k.get_text() or not k.get("href"):
                        continue
                    add(k["href"], k.get_text(" "), gs, 3)
        # top-bar shortcuts that are categories of their own (Gjirafa50: Outlet, Што има ново?)
        for a in soup.select(".top-bar nav a[href^='/']"):
            add(a["href"], a.get_text(" "), None, 1, truncated=True)
        if not nodes:
            raise StoreError("mega-menu parsed to zero categories (layout change?)")
        return nodes

    def _expand(self, idx, key):
        """Fetch a category page: its id and its full sub-category grid."""
        node = idx["nodes"][key]
        r, final, text = self._page("/" + quote(node["slug"]))
        ids = set(re.findall(r"/category/products\?categoryId=(\d+)", text))
        if len(ids) == 1:
            node["id"] = ids.pop()
        added = 0
        for slug, name in self._subcategories(text):
            slug = "/" + slug
            if slug not in idx["nodes"]:
                idx["nodes"][slug] = {"slug": slug.lstrip("/"), "name": name, "id": None, "parent": key,
                                      "level": node["level"] + 1, "expanded": False, "truncated": False}
                added += 1
        node["expanded"] = True
        node["truncated"] = False
        return added

    def index(self, refresh=False):
        if self._index is not None and not refresh:
            return self._index
        idx = None if refresh else self._load_cache()
        if idx is None:
            t0 = time.monotonic()
            r, final, text = self._page("/")
            nodes = self._parse_menu(text)
            idx = {"version": CAT_CACHE_VERSION, "site": self.site, "built": time.time(), "nodes": nodes}
            todo = [k for k, n in nodes.items() if n["truncated"]]
            self.log(f"building the category index: menu has {len(nodes)} categories; fetching "
                     f"{len(todo)} truncated group pages (cached for {CAT_TTL_S // 3600} h in {_cache_dir()})")
            for k in todo:
                try:
                    self._expand(idx, k)
                except NotFound:
                    self.log(f"WARNING: menu category {k} answers page-not-found; dropped")
                    idx["nodes"].pop(k, None)
            self.log(f"category index: {len(idx['nodes'])} categories "
                     f"({time.monotonic() - t0:.0f}s, {self.requests_made} requests)")
            self._save_cache(idx)
        self._index = idx
        return idx

    def _path(self, idx, key):
        names, seen = [], set()
        while key and key in idx["nodes"] and key not in seen:
            seen.add(key)
            names.append(idx["nodes"][key]["name"])
            key = idx["nodes"][key]["parent"]
        return " > ".join(reversed(names))

    def _records(self, idx):
        out = []
        for k, n in idx["nodes"].items():
            parent = idx["nodes"].get(n["parent"]) if n["parent"] else None
            out.append({
                "id": n["id"], "slug": n["slug"], "name": n["name"], "path": self._path(idx, k),
                "url": self.base + "/" + quote(n["slug"]),
                "parent": (parent["id"] or parent["slug"]) if parent else None,
                "count": n.get("count"), "level": n["level"],
                "subcategories_listed": bool(n["expanded"]),
            })
        return out

    @staticmethod
    def _match(rec, rx, rx_lat):
        fields = [x for x in (rec["name"], rec["path"], rec["slug"]) if x]
        fields += [translit(rec["name"]), translit(rec["path"]), en_tags(rec["path"])]
        return any(rx.search(x) or (rx_lat and rx_lat.search(x)) for x in fields)

    def categories(self, grep=None, deep=False, refresh=False, counts=False):
        idx = self.index(refresh=refresh)
        rx = re.compile(grep, re.I) if grep else None
        # transliterate only the Cyrillic letters of the pattern (lower-casing it all would turn
        # regex escapes such as \D or \S into \d / \s)
        lat = "".join(_MK_LAT.get(ch.lower(), ch) for ch in grep) if grep else None
        rx_lat = re.compile(lat, re.I) if grep and lat != grep else None
        if deep:
            budget, changed = MAX_DEEP_PAGES, False
            while budget > 0:
                recs = self._records(idx)
                want = [r for r in recs if (rx is None or self._match(r, rx, rx_lat))
                        and not idx["nodes"]["/" + r["slug"]]["expanded"]]
                if not want:
                    break
                for r in want[:budget]:
                    budget -= 1
                    try:
                        self._expand(idx, "/" + r["slug"])
                    except NotFound:
                        idx["nodes"].pop("/" + r["slug"], None)
                    changed = True
            left = [r for r in self._records(idx) if (rx is None or self._match(r, rx, rx_lat))
                    and not idx["nodes"]["/" + r["slug"]]["expanded"]]
            if left:
                self.log(f"WARNING: --deep stopped after {MAX_DEEP_PAGES} page fetches; {len(left)} matching "
                         f"categories still have unlisted subcategories (run again: results are cached)")
            if changed:
                self._save_cache(idx)
        recs = self._records(idx)
        if rx is not None:
            recs = [r for r in recs if self._match(r, rx, rx_lat)]
        if counts:
            self._fill_counts(idx, recs)
        return recs

    def _fill_counts(self, idx, recs):
        if not self.cfg["has_totals"]:
            self.log("note: ZirafaMall category listings report no product total; count stays null "
                     "(`list` reports pages x 16 slots)")
            return
        todo = recs[:MAX_COUNTS]
        if len(recs) > MAX_COUNTS:
            self.log(f"WARNING: --counts capped at {MAX_COUNTS} categories ({len(recs)} matched); narrow --grep")
        changed = False
        for rec in todo:
            node = idx["nodes"]["/" + rec["slug"]]
            if not node["id"]:
                try:
                    self._expand(idx, "/" + rec["slug"])
                    changed = True
                except NotFound:
                    continue
                rec["id"] = node["id"]
            if node["id"]:
                data = self._json("/category/products", {"categoryId": node["id"], "pagenumber": 1, "pagesize": 12})
                tot = re.search(r"вкупно\s+([\d.,]+)", data.get("paginationHtml") or "")
                rec["count"] = int(re.sub(r"\D", "", tot.group(1))) if tot else (0 if not data.get("totalpages") else None)
        if changed:
            self._save_cache(idx)

    def resolve_category(self, arg):
        """-> (category_id, label, page_text or None). Accepts an id, a URL or a slug."""
        arg = arg.strip()
        if re.fullmatch(r"\d+/?", arg):
            cid = arg.rstrip("/")
            idx = self._load_cache() or {"nodes": {}}
            label = next((self._path(idx, k) for k, n in idx["nodes"].items() if n.get("id") == cid), None)
            return cid, label, None
        if arg.startswith("http"):
            q = parse_qs(urlparse(arg).query)
            if q.get("categoryId", [""])[0].isdigit():
                return self.resolve_category(q["categoryId"][0])
        path = self.path_of(arg)
        try:
            r, final, text = self._page(quote(unquote(path)))
        except NotFound as e:
            raise NotFound(f"unknown category {arg!r}: {e}") from None
        ids = set(re.findall(r"/category/products\?categoryId=(\d+)", text))
        if len(ids) != 1:
            raise NotFound(f"{r.url}: not a category page (expected one categoryId, found {sorted(ids)}); "
                           f"use `categories --grep` to find the category")
        crumbs = [_collapse(htmlmod.unescape(x)) for x in re.findall(r'itemprop="name"[^>]*>([^<]*)<', text)]
        crumbs = [c for c in crumbs if c and c.lower() not in ("почетна", "home")]
        cid = ids.pop()
        self._remember_id(unquote(urlparse(r.url).path), cid)
        return cid, " > ".join(crumbs) or None, text

    def _remember_id(self, path, cid):
        """Record a category id learned from its page in the cached index (ids below the
        departments are null until a page fetch learns them), so `facets <id>` / `list <id>`
        can find the page and label later."""
        idx = self._index or self._load_cache()
        key = "/" + path.strip("/")
        if idx and key in idx.get("nodes", {}) and not idx["nodes"][key].get("id"):
            idx["nodes"][key]["id"] = cid
            self._save_cache(idx)

    @staticmethod
    def _subcategories(text):
        """Sub-category tiles (grid on some pages, swiper slider on others) -> [(slug, name)]."""
        soup = BeautifulSoup(text or "", "html.parser")
        out, seen = [], set()
        for a in soup.select("a[href]:has(.sub-category-item)"):
            h = a.select_one("h2, h3")
            img = a.select_one("img[alt]")
            name = _collapse(h.get_text(" ")) if h else ""
            if not name and img is not None:
                name = _collapse(img.get("alt"))
            slug = unquote(urlparse(a["href"]).path).strip("/")
            if name and slug and slug not in seen:
                seen.add(slug)
                out.append((slug, name))
        return out

    # ----------------------------------------------------------- facets
    def _spec_filters(self, text):
        """Category page filter panel -> [{spec_id, name, options: [(option_id, value)]}]."""
        soup = BeautifulSoup(text, "html.parser")
        groups = []
        for ul in soup.select("ul.product-spec-group"):
            nm = ul.select_one(".sub-spec-name")
            opts = []
            spec_id = None
            for inp in ul.select("input[data-option-id][data-spec-id]"):
                spec_id = inp["data-spec-id"]
                lab = inp.find_next_sibling("span")
                opts.append((inp["data-option-id"], _collapse(lab.get_text()) if lab else ""))
            if nm and opts:
                groups.append({"spec_id": spec_id, "name": _collapse(nm.get_text()), "options": opts})
        price = None
        lo = soup.select_one("input.from-price[min]")
        hi = soup.select_one("input.to-price[max]")
        if lo is not None and hi is not None:
            price = (lo.get("min"), hi.get("max"))
        return groups, price

    def _category_page(self, arg):
        cid, label, text = self.resolve_category(arg)
        if text is None:
            # the filters live on the category page, which is addressed by slug only
            idx = self.index()
            slug = next((n["slug"] for n in idx["nodes"].values() if n.get("id") == cid), None)
            if slug is None:
                raise Usage(f"category id {cid} is not in the category index (ids below the departments "
                            f"are learned when a page is fetched), so its page (which holds the filters) "
                            f"cannot be found; pass the category slug or URL instead")
            cid, label, text = self.resolve_category(slug)
        return cid, label, text

    def facets(self, arg, counts=False):
        cid, label, text = self._category_page(arg)
        groups, price = self._spec_filters(text)
        out = []
        for tok, key in self.cfg["flags"].items():
            desc = {"in-stock": "orderable (not sold out)", "local-stock": "48h local stock",
                    "discount": "has a discount"}[tok]
            out.append({"name": "availability", "value": desc, "count": None, "token": tok, "param": f"{key}=true"})
        if price:
            out.append({"name": "price", "value": f"{price[0]}-{price[1]} MKD (range on this category)",
                        "count": None, "token": f"price={price[0]}-{price[1]}", "param": "price"})
        for mid, name in self.manufacturers(cid):
            out.append({"name": "brand", "value": name, "count": None, "token": f"brand={mid}",
                        "param": f"ms={mid}"})
        for g in groups:
            for oid, val in g["options"]:
                out.append({"name": g["name"], "value": val, "count": None,
                            "token": f"spec={g['spec_id']}:{oid}", "param": f"specs={g['spec_id']},{oid};"})
        self.log(f"category {cid} ({label or '?'}): {len(groups)} attribute filters, "
                 f"{sum(1 for x in out if x['name'] == 'brand')} brands; the site shows no per-value counts"
                 + (" (use --counts for exact counts)" if self.cfg["has_totals"] and not counts else ""))
        if counts:
            if not self.cfg["has_totals"]:
                self.log("note: ZirafaMall listings report no totals; counts stay null")
            else:
                todo = [x for x in out if x["name"] != "price"][:MAX_COUNTS * 2]
                if len(out) > len(todo):
                    self.log(f"WARNING: --counts capped at {len(todo)} values of {len(out)}")
                for x in todo:
                    params = {"categoryId": cid, "pagenumber": 1, "pagesize": 12}
                    params.update(self._filter_params([x["token"]], cid))
                    data = self._json("/category/products", params)
                    tot = re.search(r"вкупно\s+([\d.,]+)", data.get("paginationHtml") or "")
                    x["count"] = int(re.sub(r"\D", "", tot.group(1))) if tot else (0 if not data.get("totalpages") else None)
        for x in out:
            x.pop("param", None)
        return out

    def _filter_params(self, tokens, cid):
        params, specs, brands_seen = {}, {}, False
        names = None
        for tok in tokens or []:
            tok = tok.strip()
            low = tok.lower()
            if low in self.cfg["flags"]:
                params[self.cfg["flags"][low]] = "true"
                continue
            if low in ("in-stock", "local-stock", "discount"):
                raise Usage(f"filter {tok!r} is not supported on {self.site} "
                            f"(supported flags: {', '.join(self.cfg['flags'])})")
            m = re.fullmatch(r"price=(\d+)?-(\d+)?", low)
            if m:
                params["price"] = f"{m.group(1) or 0}-{m.group(2) or 99999999}"
                continue
            m = re.fullmatch(r"(?:brand|ms)=(.+)", tok, re.I)
            if m:
                if brands_seen:
                    raise Usage("two brand= filters cannot both hold (a product has one manufacturer); "
                                "use one token with a comma list for either: brand=ID1,ID2")
                brands_seen = True
                ids = []
                for part in [p.strip() for p in m.group(1).split(",") if p.strip()]:
                    if part.isdigit():
                        ids.append(part)
                        continue
                    if names is None:
                        names = {n.lower(): i for i, n in self.manufacturers(cid)}
                    if part.lower() not in names:
                        raise Usage(f"brand {part!r} is not in this category's manufacturer filter; "
                                    f"see `facets`")
                    ids.append(names[part.lower()])
                params["ms"] = ",".join(ids)
                continue
            m = re.fullmatch(r"spec=(\d+):(\d+(?:,\d+)*)", low)
            if m:
                if m.group(1) in specs:
                    raise Usage(f"two spec={m.group(1)}:... filters cannot both hold; put the values in one "
                                f"token (spec={m.group(1)}:A,B means either)")
                specs[m.group(1)] = m.group(2).split(",")
                continue
            raise Usage(f"unknown filter token {tok!r}; use the tokens `facets` prints "
                        f"(spec=S:O[,O], brand=ID|NAME[,..], price=MIN-MAX, {', '.join(self.cfg['flags'])})")
        if specs:
            params["specs"] = "".join(f"{s},{','.join(o)};" for s, o in specs.items())
        return params

    # ----------------------------------------------------------- list
    def list_category(self, arg, in_stock=False, limit=None, filters=None, brands=True):
        filters = list(filters or [])
        if any(t.strip().lower() == "in-stock" for t in filters):   # same as --in-stock
            in_stock = True
            filters = [t for t in filters if t.strip().lower() != "in-stock"]
        cid, label, text = self.resolve_category(arg)
        if text:
            subs = self._subcategories(text)
            if subs:
                self.log(f"subcategories (included in this listing): "
                         + "; ".join(f"{n} [{s}]" for s, n in subs[:40]) + (" ..." if len(subs) > 40 else ""))
        params = {"categoryId": cid}
        if self.cfg["page_size"]:
            params["pagesize"] = self.cfg["page_size"]
        params.update(self._filter_params(filters, cid))
        if in_stock and self.site == "gjirafa50":
            params[self.cfg["flags"]["in-stock"]] = "true"     # matches the sold-out badge exactly
        elif in_stock:
            # ZirafaMall's in-stock index (is=true) is stale both ways: it returns ~1.5-5% sold-out
            # cards and omits ~0.3-0.7% orderable ones (Oct 2026: 2 of 276 fridges, 3 of 1,218
            # disks, each confirmed orderable on its page). Walk everything and keep the cards
            # without the sold-out badge (a strict superset of the is=true walk, ~1.5x the pages).
            self.log("note: ZirafaMall --in-stock walks the whole listing and keeps cards without the "
                     "sold-out badge (the site's own in-stock filter omits some orderable products)")
        if brands:
            try:
                self.load_brands(cid)
            except (StoreError, ValueError) as e:
                if isinstance(e, Blocked):
                    raise
                self.log(f"WARNING: manufacturer filter unavailable ({e}); listing brands will be null")
        keep = (lambda c: not c["sold_out"]) if in_stock else None
        cards, meta = self.walk("/category/products", params, limit=limit, order=ORDER_NEWEST,
                                fallback=ORDER_RELEVANCE, label=f"category {cid}", slice_prices=True,
                                keep=keep)
        recs = [self.card_to_record(c, label) for c in cards]
        if in_stock:
            recs = [r for r in recs if r["in_stock"]]
        total = meta.get("site_total")
        how = (f"{meta['slices']} price slices, {meta['pages']} pages" if meta.get("slices")
               else f"{meta['pages']} page(s)")
        if self.cfg["has_totals"]:
            extra = f"site total 'вкупно {total}'" if total is not None else "site shows no total"
        elif meta.get("totalpages") is not None:
            extra = (f"site shows no total; {meta['totalpages']} page(s) x {ZM_PAGE} = "
                     f"{meta['totalpages'] * ZM_PAGE} index slots (ZirafaMall leaves some slots empty)")
        else:
            extra = "site shows no total"
        listed = meta.get("listed", len(cards)) if in_stock else len(cards)
        self.log(f"category {cid} ({label or 'label unknown: pass the slug/URL for a breadcrumb'}): {extra}; "
                 f"fetched {listed} unique over {how}"
                 + (f", {len(cards)} of them orderable" if in_stock else "")
                 + (f" (stopped at --limit {limit})" if meta.get("truncated") else ""))
        if total is not None and not meta.get("truncated") and listed != total:
            self.log(f"note: collected {listed} unique products, the site reports {total}"
                     + ("" if listed > total else
                        " (the site's own grid renders fewer products than its count, up to ~25% in "
                        "expensive laptop/PC ranges; other orderings find no more, shoppers do not see "
                        "them either)"))
        if not recs:
            if listed:
                raise NotFound(f"category {cid}: all {listed} listed products are sold out (none orderable)")
            hint = (" (the filters match nothing)" if (filters or in_stock) else " (empty category)")
            raise NotFound(f"category {cid} returned no products.{hint}")
        return recs

    # --------------------------------------------------------------- search
    def _word_checks(self, query, params):
        words = [w for w in re.split(r"\s+", query) if w]
        if len(words) < 2:
            return
        for w in [w for w in words if len(w) >= 3][:MAX_WORD_CHECKS]:   # the site's 3-char minimum
            data = self._json("/product/search", dict(params, q=w, pagenumber=1))
            if not data.get("totalHits"):
                self.log(f"WARNING: word {w!r} matches no product on its own; the store drops such words "
                          f"silently (OR-like fallback), so the results are for the other words only")

    def search(self, query, limit=None, in_stock=False, check_words=True):
        query = _collapse(query)
        if len(query.replace(" ", "")) < 3:
            raise Usage("search needs at least 3 non-space characters (the site's own rule)")
        params = {"q": query}
        if in_stock:
            params["is"] = "true"
        cap = limit or SEARCH_CAP
        cards, meta = self.walk("/product/search", params, limit=cap, order=None,
                                fallback=ORDER_NEWEST, label=f"search {query!r}")
        hits = meta.get("totalHits")
        self.log(f"search {query!r}: site reports totalHits={hits}, fetched {len(cards)} over "
                 f"{meta['pages']} page(s)")
        if hits and not cards:
            self.log(f"WARNING: the site reports {hits} hit(s) but rendered no product cards (hidden "
                     f"listings, or a layout change the parser does not recognise)")
        if not limit and hits and hits > cap:
            self.log(f"WARNING: results capped at {cap} of {hits} (relevance order; the store's fuzzy search "
                     f"returns a long unrelated tail). Pass --limit, narrow the query, or `list` a category")
        if check_words and len(query.split()) > 1:
            self._word_checks(query, {k: v for k, v in params.items() if k != "q"})
        recs = [self.card_to_record(c) for c in cards]
        if in_stock:   # ZirafaMall's is=true index lags: it still returns ~1.5% sold-out cards
            recs = [r for r in recs if r["in_stock"] is not False]
        return recs

    # --------------------------------------------------------------- detail
    def product_path(self, arg):
        """id -> /p/<id> (the site 301s it to the slug URL); URL or slug -> its path."""
        arg = arg.strip()
        if re.fullmatch(r"\d+/?", arg):
            return f"/p/{arg.strip('/')}"
        return quote(unquote(self.path_of(arg)))

    def detail(self, arg):
        path = self.product_path(arg)
        r = self._request(path)
        final = urlparse(r.url).path
        if r.status_code == 404 or final in ("/", "/page-not-found") or "page-not-found" in r.url:
            raise NotFound(f"product {arg!r}: not found (HTTP {r.status_code}, landed on {r.url}; "
                           f"unknown ids redirect to the home page, unknown slugs to /page-not-found)")
        if r.status_code != 200:
            raise StoreError(f"product {arg!r}: HTTP {r.status_code}")
        lang = re.search(r'<html[^>]*\blang="([^"]+)"', r.text[:3000])
        if lang and lang.group(1).lower().startswith("sq"):
            raise StoreError(f"{r.url}: page served in Albanian ({lang.group(1)!r}); "
                             f"the site may have stopped honouring the browser User-Agent")
        soup = BeautifulSoup(r.text, "html.parser")
        ov = soup.select_one("div.overview.product-details") or soup.select_one("div.overview")
        if ov is None:
            if _looks_like_challenge(r.text):
                raise Blocked(f"HTTP {r.status_code} on {r.url}: challenge page instead of product page, "
                              f"cf-ray={r.headers.get('cf-ray')}")
            if "categoryId=" in r.text:
                raise NotFound(f"{r.url} is a category page, not a product")
            raise StoreError(f"{r.url}: product block not found (not a product page, or layout change)")
        pe = ov.select_one("span[id^=price-value-]") or soup.select_one("span[id^=price-value-]")
        if not pe:
            raise StoreError(f"{r.url}: no price-value-<id> element; cannot determine product id")
        pid = pe["id"].rsplit("-", 1)[1]

        h1 = ov.select_one(".product-name h1")
        title = _collapse(h1.get_text()) if h1 else None
        manu = [_collapse(a.get_text()) for a in ov.select(".manufacturers a")]
        brand = next((m for m in manu if not m.startswith("Продавница")), None) or None
        seller = next((m.removeprefix("Продавница").strip() for m in manu if m.startswith("Продавница")), None)
        code = ov.select_one("#product-code-copy")
        sku = ((code.get("value") or "").strip() or None) if code else None

        now = old = None
        for sp in ov.select(f".prices span#price-value-{pid}, .prices span.price-value-{pid}"):
            txt = _collapse(sp.get_text())
            if not txt:
                continue
            if "line-through" in (sp.get("class") or []):
                old = _mkd(txt)
            elif now is None:
                now = _mkd(txt)

        av = ov.select_one(f"#stock-availability-value-{pid}")
        avail = _collapse(av.get_text()) if av else None
        low = (avail or "").lower()
        if "нема на залиха" in low or "продаден" in low:
            in_stock = False
        elif avail:
            in_stock = True
        else:
            in_stock = None
        qty = None
        m = re.search(r"само уште\s+(\d+)", low)
        if m:
            qty = int(m.group(1))
        elif "повеќе од" in low:
            m = re.search(r"повеќе од\s+(\d+)", low)
            qty = f">{m.group(1)}" if m else None
        elif in_stock is False:
            qty = 0

        deliv_el = ov.select_one(f"#free-shipping-{pid}") or ov.select_one("div.delivery")
        deliv_txt = _collapse(deliv_el.get_text(" ")) if deliv_el else ""
        local_now = "Земи веднаш" in deliv_txt
        delivery, min_days = self._delivery(deliv_txt) if deliv_el else (None, None)
        turtle = soup.select_one("#turtle-badge-product-details") is not None

        specs_pairs = []
        for h in soup.select("#product-specifications-split-page .spec-name"):
            val = h.find_next(class_="spec-value")
            specs_pairs.append((_collapse(h.get_text()).rstrip(":"), _collapse(val.get_text()) if val else ""))
        spec = dict(specs_pairs)
        origin = None
        og = re.search(r"<!--origin-->(.*?)<!--product code-->", r.text, re.S)
        if og:
            m = re.search(r'<img[^>]*title="([^"]*)"', og.group(1))
            origin = htmlmod.unescape(m.group(1)) if m else None
        desc_el = soup.select_one("#product-desc")
        desc = _collapse(desc_el.get_text(" ")) if desc_el else ""
        specs = desc
        if specs_pairs:
            specs += (" | " if specs else "") + " ; ".join(f"{k}: {v}" for k, v in specs_pairs)
        if origin:
            specs += f" | Потекло (origin flag): {origin}"

        crumbs = [_collapse(x.get_text()) for x in soup.select('[itemtype*="BreadcrumbList"] [itemprop="name"]')]
        crumbs = [c for c in crumbs if c and c.lower() not in ("почетна", "home")]
        if crumbs and title and crumbs[-1] == title:
            crumbs = crumbs[:-1]

        # The spec is machine-translated: 'Гаранција за производителот: 2 Брзо' means 2 years.
        wparts = [f"{k}: {v}" for k, v in spec.items() if "гаранц" in k.lower() and v]
        if self.site == "gjirafa50":
            wparts.append("store-wide 1 година ('Сите наши производи имаат гаранција од 1 година', "
                          "gjirafa50.mk/koga-pristignuva-proizvodot)")
        warranty = "; ".join(wparts) or None

        inferred = False
        if turtle:
            intl = True
        elif local_now:
            intl = False
        elif in_stock is False or min_days is None:
            intl = None
        elif self.site == "zirafamall":
            intl = False      # the site shows the turtle badge whenever it applies
        else:
            # Gjirafa50 never shows the badge. Its delivery quotes fall in two tiers that matched
            # ZirafaMall's badge for the same SKUs 1:1 (125/125, Oct 2026): ~19-21 days = other
            # supplier stock, ~24-29 days = international supplier. The tiers drift with the weekday.
            intl = min_days >= INTL_MIN_DAYS
            inferred = True
        parts = [avail and f"Количина: {avail}"]
        if in_stock is False:
            parts.append("нема на залиха (sold out)")
        elif local_now:
            parts.append("Земи веднаш: local stock")
        elif turtle:
            parts.append("Овој производ доаѓа од меѓународен добавувач (international supplier)")
        elif inferred and intl:
            parts.append(f"international-supplier tier, inferred from the {min_days}-day delivery quote "
                         f"(Gjirafa50 shows no badge; ZirafaMall flags these SKUs 'меѓународен добавувач')")
        elif delivery:
            parts.append("no local stock: ships from supplier (not flagged as international)")
        if delivery:
            parts.append(f"delivery {delivery}")
        mpn = next((v for k, v in spec.items()
                    if re.search(r"шифра на производ|part ?number|\bmpn\b|модел број", k, re.I) and v), None)
        return {
            "store": self.site,
            "id": pid,
            "sku": sku,
            "title": title,
            "url": r.url,
            "brand": brand or spec.get("Брендови") or None,
            "price_mkd": now,
            "regular_price_mkd": old if old and now and old > now else None,
            "in_stock": in_stock,
            "stock_note": "; ".join(p for p in parts if p) or None,
            "category": " > ".join(crumbs) or None,
            "ean": (spec.get("EAN") or "").strip() or None,
            "mpn": mpn,
            "seller": seller,
            "international_supplier": intl,
            "delivery_estimate": delivery,
            "warranty": warranty,
            "specs": specs,
            "per_location_stock": None,  # one web stock figure; no per-store breakdown exposed
            "extra": {"availability_text": avail, "quantity": qty, "local_stock_now": local_now,
                      "delivery_min_days": min_days, "international_inferred": inferred,
                      "origin_flag": origin},
        }

    @staticmethod
    def _delivery(text):
        """'... СКОПЈЕ 30 октомври 2026 - 31 октомври 2026 Македонија, други 31 октомври 2026 -
        02 ноември 2026 ...' -> 'СКОПЈЕ 2026-10-30..2026-10-31 (29-30 days); ...'"""
        date = r"(\d{1,2}\s+[^\W\d_]+\s+\d{4})"
        out = []
        min_days = None
        today = dt.date.today()
        for label, pat in (("СКОПЈЕ", r"СКОПЈЕ"), ("Македонија, други", r"Македонија,\s*други")):
            m = re.search(pat + r"\s+" + date + r"\s*-\s*" + date, text)
            if not m:
                continue
            d1, d2 = _mk_date(m.group(1)), _mk_date(m.group(2))
            if d1 and d2:
                out.append(f"{label} {d1.isoformat()}..{d2.isoformat()} "
                           f"({(d1 - today).days}-{(d2 - today).days} days)")
                if label == "СКОПЈЕ":
                    min_days = (d1 - today).days
            else:
                out.append(f"{label} {m.group(1)} - {m.group(2)}")
        if not out:
            return None, None
        return ("Земи веднаш; " if "Земи веднаш" in text else "") + "; ".join(out), min_days

    def detail_batch(self, refs):
        """-> (records, exit_code). One record per input, in order; failures become
        {"input", "error"} rows. A block aborts the rest (they would all fail)."""
        out, code = [], 0
        for i, ref in enumerate(refs):
            try:
                out.append(self.detail(ref))
            except Blocked as e:
                out.append({"input": ref, "error": f"blocked: {e}"})
                out.extend({"input": x, "error": "skipped: store blocked the client"} for x in refs[i + 1:])
                raise _BatchBlocked(out, e)
            except (NotFound, Usage) as e:
                self.log(f"not found: {ref} ({e})")
                out.append({"input": ref, "error": f"not found: {e}"})
                code = code or 2
            except (StoreError, requests.RequestException, ValueError) as e:
                self.log(f"error: {ref} ({e})")
                out.append({"input": ref, "error": str(e)})
                code = 1
        return out, code


class _BatchBlocked(Exception):
    def __init__(self, recs, err):
        super().__init__(str(err))
        self.recs, self.err = recs, err


# ------------------------------------------------------------------- CLI
def _print_products(recs):
    for r in recs:
        if "error" in r:
            print(f"      !  ERROR  {r['input']}: {r['error']}")
            continue
        price = f"{r['price_mkd']:>7,}" if r.get("price_mkd") is not None else "      ?"
        if r.get("regular_price_mkd"):
            price += f" (was {r['regular_price_mkd']:,})"
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        flag = ("INTL" if r.get("international_supplier") else
                "48h " if (r.get("delivery_estimate") or "").startswith(("48h", "Земи")) else "    ")
        title = (r.get("title") or "")[:90]
        print(f"{price}  {stock} {flag}  {title}  {r['url']}")


def _print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{(r['id'] or '-'):>6} {cnt}  {r['path']}  [{r['slug']}]")


def _print_facets(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{cnt}  {r['name']}: {r['value']}   --filter '{r['token']}'")


def _emit(recs, path, printer=_print_products, what="records", log=None):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        print(len(recs))
    else:
        printer(recs)
        sys.stdout.flush()
    print(f"-- {len(recs)} {what}" + (f" -> {path}" if path else ""), file=sys.stderr)


def info(site):
    c = SITES[site]
    return {"store": site, "name": c["name"], "base_url": c["base"], "capabilities": c["capabilities"],
            "sells": c["sells"], "notes": c["notes"]}


def main(argv=None):
    for _stream in (sys.stdout, sys.stderr):   # UTF-8 output on every platform, even when piped
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--site", choices=sorted(SITES), default=argparse.SUPPRESS,
                        help="which shop (also record['store'])")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH, print the count")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="log every request to stderr")
    ap = argparse.ArgumentParser(description="Gjirafa50 / ZirafaMall catalogue client")
    ap.add_argument("--site", choices=sorted(SITES), help="which shop (also record['store']); required")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--no-word-check", action="store_true",
                   help="skip the per-word hit checks for multi-word queries")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p.add_argument("--deep", action="store_true",
                   help=f"also fetch matching categories' pages to list their subcategories "
                        f"(max {MAX_DEEP_PAGES} pages per run; cached)")
    p.add_argument("--counts", action="store_true",
                   help=f"Gjirafa50 only: exact product counts (one request per category, max {MAX_COUNTS})")
    p.add_argument("--refresh", action="store_true", help="rebuild the cached category index")
    p = sub.add_parser("list", parents=[common])
    p.add_argument("category")
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", action="append", default=[], metavar="TOKEN")
    p.add_argument("--no-brands", action="store_true",
                   help="skip the manufacturer-filter request used to fill brand from titles")
    p = sub.add_parser("detail", parents=[common])
    p.add_argument("products", nargs="+")
    p = sub.add_parser("facets", parents=[common])
    p.add_argument("category")
    p.add_argument("--counts", action="store_true",
                   help="Gjirafa50 only: exact count per value (one request each)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not a.site:
        print("ERROR: --site {gjirafa50,zirafamall} is required", file=sys.stderr)
        return 2
    if a.cmd == "info":
        print(json.dumps(info(a.site), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    g = Gjirafa(a.site, verbose=a.verbose)
    code = 0
    try:
        if a.cmd == "search":
            _emit(g.search(a.query, limit=a.limit, in_stock=a.in_stock, check_words=not a.no_word_check),
                  a.json)
        elif a.cmd == "categories":
            if a.grep:
                try:
                    re.compile(a.grep)
                except re.error as e:
                    raise Usage(f"bad --grep regex: {e}")
            recs = g.categories(grep=a.grep, deep=a.deep, refresh=a.refresh, counts=a.counts)
            if a.grep and not recs:
                g.log(f"WARNING: no category matches {a.grep!r} (matched against name, path, slug, a Latin "
                      f"transliteration and English tags; try a Macedonian stem, or --deep to list deeper "
                      f"subcategories)")
            _emit(recs, a.json, _print_categories, "categories")
        elif a.cmd == "list":
            _emit(g.list_category(a.category, in_stock=a.in_stock, limit=a.limit, filters=a.filter,
                                  brands=not a.no_brands), a.json)
        elif a.cmd == "facets":
            _emit(g.facets(a.category, counts=a.counts), a.json, _print_facets, "facet values")
        elif a.cmd == "detail":
            try:
                recs, code = g.detail_batch(a.products)
            except _BatchBlocked as b:
                _emit(b.recs, a.json)
                raise b.err
            _emit(recs, a.json)
        g.log(f"({g.requests_made} requests)")
        return code
    except Blocked as e:
        print(f"BLOCKED: [{a.site}] {e}", file=sys.stderr)
        return 3
    except (NotFound, Usage) as e:
        print(f"ERROR: [{a.site}] {e}", file=sys.stderr)
        return 2
    except (StoreError, requests.RequestException, ValueError) as e:
        print(f"ERROR: [{a.site}] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
