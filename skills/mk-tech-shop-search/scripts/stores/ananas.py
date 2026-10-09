#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.28", "beautifulsoup4>=4.11"]
# ///
"""Ananas.mk marketplace (https://ananas.mk) catalogue client.

Ananas is a multi-seller marketplace (Next.js behind CloudFront). Every listing is one *merchant
inventory*: one seller's offer of one product. The same product sold by two sellers is two
listings with two ids, two prices and two `seller`s; compare offers by `ean`.

Data sources (all public, no cookies or tokens):
  * Search / list / facets: the site's own Algolia index, queried directly with the public
    search-only key the frontend ships in its JS bundle:
        POST https://Y1BSBVJ7AC-dsn.algolia.net/1/indexes/prod_merchant_inventories_mk_mk/query
    Exact per-filter totals, up to 1000 hits per page, server-side filters and facets over
    every structured attribute. Pagination stops at 30,000 hits per query; bigger walks are
    split by price range automatically.
  * Category tree: the public GraphQL gateway the site uses (one request, ~2,250 nodes):
        POST https://api.ananas.rs/graphql-gateway/public   query Categories(rootOnly:false)
    Counts come from Algolia's hierarchical facets (product.categories.lvl0..lvl3).
  * Detail: the server-rendered product page /proizvod/<slug>/<id> (React flight data holds the
    merchant-inventory object: seller SKU, badges incl. warranty, spec table, sale window,
    shipping), plus the guest delivery promise from the GraphQL gateway.

Usage:
    ananas.py info
    ananas.py search "<query>" [--limit N] [--in-stock] [--strict] [--group-variants] [--json PATH]
    ananas.py categories [--grep REGEX] [--json PATH]
    ananas.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--check-site]
                              [--group-variants] [--json PATH]
    ananas.py detail <url-or-id-or-Шифра> [...] [--json PATH]
    ananas.py facets <category> [--json PATH]
    (common options: --quiet, -v/--verbose to log every request)

The index folds a seller's size/colour variants into one hit (Algolia `distinct`), as the site's
product cards do. search/list switch that off and return every orderable listing; --group-variants
gives the site's view. Fashion and toys have up to 2x more listings than cards; electronics ~1x.

<category> is any id, slug path, URL or "A > B > C" path that `categories` prints, or an exact
category name when it is unique. Filter tokens come from `facets`; repeated --filter flags AND,
and `||` inside one token ORs values (e.g. --filter 'select.DisplayDiagonal=55"||65"').

Exit codes: 0 ok (incl. a genuine zero-hit search); 1 unexpected error; 2 bad usage or unknown
category/product (or an empty category); 3 blocked (WAF / challenge / login wall / rate limit),
with a 'BLOCKED:' line on stderr.
"""

import argparse
import datetime as _dt
import datetime as dt
import html as htmlmod
import json
import math
import re
import sys
import time
from urllib.parse import unquote, urlparse

import requests

STORE = "ananas"
NAME = "Ananas"
BASE = "https://ananas.mk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Public search-only credentials from the site's JS bundle (getAlgoliaConfig in pages/_app-*.js).
# If they stop working the client re-reads them from the live bundle (_discover_algolia_config).
ALGOLIA_APP_ID = "Y1BSBVJ7AC"
ALGOLIA_SEARCH_KEY = "78d3f4f3befb3c4a68f4ebbf8c38fd81"
ALGOLIA_INDEX = "prod_merchant_inventories_mk_mk"
GRAPHQL_PUBLIC = "https://api.ananas.rs/graphql-gateway/public"
OWN_SHOP_MERCHANT_ID = 776   # "Ананас Шоп" (stored as "AНАНАС ШОП" with a Latin A)

HITS_PER_PAGE = 1000      # Algolia maximum; ~1.1 MB per page with the listing attributes
PAGINATION_CAP = 30000    # index setting paginationLimitedTo: no hit beyond #30,000 per query
SPLIT_TARGET = 20000      # walks above the cap are split into price ranges of at most this many
FACET_VALUES_MAX = 1000   # Algolia maxValuesPerFacet ceiling
ALGOLIA_PACE = 0.3        # pause between consecutive Algolia requests
PAGE_PACE = 0.7           # pause between consecutive HTML page / GraphQL requests
MAX_RETRIES = 4

SELLS = ("Multi-seller marketplace (~285,000 listings from ~650 sellers, 24 departments): home and "
         "garden (furniture, kitchenware, lighting, decor), fashion, car and moto (tyres), DIY tools, "
         "toys and LEGO, beauty and personal-care appliances, baby gear, IT (laptops, PCs, components, "
         "peripherals, office devices), gaming, phones, tablets, smartwatches and cameras, TV/audio/"
         "video, large appliances (ACs, fridges, washers, cookers, boilers), small kitchen and home "
         "appliances, pet shop, sport, food and drinks, books, office/school supplies, music "
         "instruments, gift cards. Sellers include Ananas' own shop, Нексио, ТЕХНОМАРКЕТ, Нептун, "
         "Сетек, Setra, PC MARKET (Anhoch), Бител, King Soft and hundreds of small shops.")
NOTES = ("Each listing is one seller's offer (seller field); the same product from two sellers is two "
         "records, so compare by ean. price_mkd = the price every buyer pays (VAT incl.); "
         "regular_price_mkd = the struck-through basePrice during a seller's SALE; price_valid_until = "
         "when that SALE ends (detail from the page's priceV2; on listings only for the ~3% of sale hits whose index "
         "entry carries it, the key is absent on other sale hits). shipping_mkd = the "
         "per-listing delivery fee for a guest / non-Ananas+ buyer (0 or 190 for most; 250-1,200+ for "
         "bulky items). search/list return every size/colour variant listing (the site folds them into "
         "one card; --group-variants gives that view). "
         "in_stock = onStock (out-of-stock listings are rare, ~1%); stock_note carries the seller's "
         "unit count (200 is a common default, not a real count). Search is Algolia: typo-tolerant, "
         "prefix, matches EAN, Шифра (apId) and model codes. Titles are mostly Cyrillic and only some "
         "Latin spellings are mapped (televizor = телевизор, but igracka 3 vs играчка 18,688, blender "
         "303 vs блендер 680): search product words in Cyrillic, brands/models in Latin. When no "
         "listing matches every word Algolia drops words (warned on stderr; --strict disables). "
         "Facets: every structured attribute Algolia indexes for the category (sparse, seller-entered). "
         "detail adds warranty (only when a badge or the description states it), specs, seller SKU, "
         "sale window and the guest delivery-date promise. Many listings mirror other covered shops "
         "(Нексио, ТЕХНОМАРКЕТ, Нептун, Сетек, Setra, PC MARKET).")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing",
                "ean_in_detail", "stock_qty", "warranty", "seller", "delivery_estimate",
                "search_ean", "search_codes"]

LIST_ATTRS = ["objectID", "apId", "price", "basePrice", "regularDiscountPrice", "onSale", "onStock",
              "available", "fba", "ananasGlobal", "discountPercentage", "discountType",
              "discountStartTime", "discountEndTime",
              "shippingCostPriceNonAplus", "freeShippingNonAplus", "shippingCostPriceAplus",
              "freeShippingAplus", "merchant", "product.name", "product.brand", "product.slug",
              "product.ean", "product.categories", "product.adultContent",
              "product.textAttributes.Model", "product.selectAttributes",
              "product.booleanAttributes", "product.measurementAttributes",
              "product.colorAttributes"]
DETAIL_ATTRS = LIST_ATTRS + ["product.description"]

ATTR_GROUPS = {"select": "selectAttributes", "bool": "booleanAttributes",
               "measure": "measurementAttributes", "color": "colorAttributes",
               "text": "textAttributes"}           # also the preference order for bare keys
GROUP_SHORT = {v: k for k, v in ATTR_GROUPS.items()}
FILTER_ALIASES = {"brand": "product.brand", "seller": "merchant.displayName",
                  "seller_id": "merchant.id", "in_stock": "onStock", "on_sale": "onSale",
                  "fulfilled_by_ananas": "fba", "fba": "fba", "free_shipping": "freeShippingNonAplus"}
BOOL_FLAGS = {"onStock", "onSale", "fba", "freeShippingNonAplus"}
# Facet attributes that are bookkeeping rather than product properties.
FACET_SKIP = {"_tags", "_collections", "metaDescription", "product.thumbnailUrl", "product.ean",
              "available", "recentlySoldCount", "product.measurements", "product.adultContent",
              "merchant.id", "freeShippingAplus", "discountPercentage", "ananasGlobal", "price"}
SITE_STANDARD_FACETS = ["fba", "freeShippingNonAplus", "onSale", "product.brand", "price"]
# Free-text attributes whose values are sentences, not filter values.
FACET_TEXT_SKIP = {"OtherFeatures", "Instructions", "ShortDescription", "PackageContent",
                   "Description", "Ingredients", "Composition"}
CURATED_MAX = 30   # a longer facetOrdering is the site's generic default, not a per-category rule

CHALLENGE_MARKERS = (
    "just a moment", "cf-chl", "challenge-platform", "turnstile", "captcha", "awswaf",
    "aws-waf-token", "challenge.js", "attention required", "are you a robot", "ddos-guard",
    "request blocked", "checking your browser",
)

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dz", "ш": "sh", "ђ": "dj",
           "ћ": "c", "è": "e"}
_LAT2CYR = str.maketrans("aAbBcCeEhHkKmMoOpPtTxXyYiIjJsS", "аАвВсСеЕнНкКмМоОрРтТхХуУіІјЈѕЅ")

# English words added to a category's grep haystack when its (Cyrillic) path contains the stem,
# so `categories --grep fridge` finds "Фрижидери". Slugs already give a Latin transliteration.
_EN_TAGS = {
    "телевизор": "tv television", "звучници": "speakers speaker", "саундбар": "soundbar",
    "слушалки": "headphones headset earphones earbuds", "проектор": "projector",
    "мобилни телефони": "phone phones mobile", "паметни телефони": "smartphone smartphones phone",
    "таблет": "tablet tablets", "смарт уреди": "smart devices wearables smartwatch",
    "паметни часовници": "smartwatch smart watch", "фотоапарат": "camera cameras photo",
    "камер": "camera", "лаптоп": "laptop laptops notebook", "компјутер": "computer pc",
    "монитор": "monitor monitors display", "тастатур": "keyboard keyboards", "глувч": "mouse mice",
    "печатач": "printer printers", "мрежна": "network networking router wifi",
    "компоненти": "components pc parts", "процесор": "cpu processor", "графичк": "gpu graphics card",
    "мемориј": "memory ram", "хард диск": "hdd hard drive storage", "ssd": "ssd storage",
    "напојув": "psu power supply", "конзол": "console consoles playstation xbox nintendo",
    "игриц": "games video games", "гејминг": "gaming", "клима": "air conditioner ac aircon",
    "фрижидер": "fridge refrigerator", "замрзнувач": "freezer",
    "машини за алишта": "washing machine washer dryer laundry",
    # colloquial Macedonian names the store does not use ("перални" for washing machines)
    "перење алишта": "перални перална машина за перење washing machine washer",
    "сушење алишта": "сушари сушара машина за сушење dryer tumble dryer",
    "машини за миење садови": "dishwasher dishwashers машина за садови судомашина",
    "air fryer": "фритеза фритези", "шпорет": "cooker stove oven hob",
    "фурн": "oven", "плотн": "hob cooktop", "микробранов": "microwave", "аспиратор": "cooker hood",
    "бојлер": "boiler water heater", "грејни": "heater heating radiator",
    "прочистувач": "air purifier", "правосмукал": "vacuum cleaner hoover",
    "кафемат": "coffee machine espresso", "кафе": "coffee", "чај": "tea",
    "мали кујнски апарати": "small kitchen appliances blender mixer toaster kettle",
    "апарати за готвење": "cooking appliances", "фритез": "fryer deep fryer",
    "тостер": "toaster", "скари": "grill", "пеглање": "iron ironing steam",
    "шиење": "sewing", "апарати за коса": "hair dryer straightener", "бричење": "shaver razor shaving",
    "нега": "care", "убавина": "beauty", "парфем": "perfume fragrance", "шминка": "makeup cosmetics",
    "козмети": "cosmetics", "играчк": "toys toy", "лего": "lego", "бебе": "baby", "бебиња": "baby",
    "пелени": "diapers nappies", "колички": "stroller pram", "авто седишта": "car seat",
    "мебел": "furniture", "градина": "garden", "осветлување": "lighting lamps", "кујна": "kitchen",
    "постелнина": "bedding", "бања": "bathroom", "алат": "tools", "гуми": "tyres tires",
    "возила": "car vehicle", "авто": "car auto", "мото": "motorcycle moto",
    "велосипед": "bike bicycle", "тротинет": "scooter e-scooter", "фитнес": "fitness gym",
    "тегови": "weights dumbbells", "кампување": "camping", "риболов": "fishing",
    "облека": "clothes clothing apparel", "обувки": "shoes footwear", "чанти": "bags backpacks",
    "куфери": "suitcase luggage", "накит": "jewelry jewellery", "часовници": "watches",
    "очила": "glasses sunglasses", "мачки": "cat cats", "кучиња": "dog dogs", "книги": "books",
    "канцелариски": "office", "училиш": "school", "пијалоци": "drinks beverages", "храна": "food",
    "суплемент": "supplements vitamins", "музички": "music musical instruments",
    "подарок": "gift", "чистење": "cleaning", "перење": "laundry washing detergent",
}


class StoreError(RuntimeError):
    """Unexpected failure (exit 1)."""


class Blocked(StoreError):
    """A block page / challenge / login wall / persistent rate limit instead of data (exit 3)."""


class NotFound(StoreError):
    """Unknown or empty category, unknown product (exit 2)."""


class Usage(StoreError):
    """Bad input (exit 2)."""


QUIET = False


def log(msg):
    if not QUIET:
        print(f"[ananas] {msg}", file=sys.stderr)


def warn(msg):
    print(f"[ananas] WARNING: {msg}", file=sys.stderr)


def collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def html_text(s):
    s = re.sub(r"<\s*(br|/p|/li|/h\d|/tr)\b[^>]*>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return htmlmod.unescape(s)


def to_int(v):
    """7990.0 -> 7990; '8.810' -> 8810; '5990,00' -> 5990; None/'' -> None"""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"[^\d.,]", "", str(v))
    s = re.sub(r"[.,]\d{2}$", "", s) if re.search(r"[.,]\d{2}$", s) else s
    s = s.replace(".", "").replace(",", "")
    return int(s) if s.isdigit() else None


def has_cyr(s):
    return any("Ѐ" <= ch <= "ӿ" for ch in s or "")


def mixed_script(s):
    return has_cyr(s) and any(ch.isascii() and ch.isalpha() for ch in s or "")


def fold_cyr(text):
    """Inside words that mix scripts, map Latin look-alikes to Cyrillic ('Mото' -> 'Мото')."""
    return "".join(w.translate(_LAT2CYR) if mixed_script(w) else w
                   for w in re.split(r"(\W+)", text or ""))


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def norm_key(text):
    """Case-, whitespace- and homoglyph-insensitive key for comparing names and paths."""
    return collapse(fold_cyr(text)).casefold()


def norm_path(text):
    return " > ".join(norm_key(p) for p in re.split(r"\s*>\s*", text or "") if p.strip())


def en_tags(text):
    low = fold_cyr(text or "").lower()
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def aq(value):
    """Quote a value for an Algolia `filters` expression."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def and_filters(*parts):
    parts = [p for p in parts if p]
    return " AND ".join(f"({p})" if " OR " in p else p for p in parts)


def evidence(r):
    bits = [f"HTTP {r.status_code}", f"server={r.headers.get('server')}"]
    for h in ("x-cache", "x-amz-cf-pop", "x-amz-cf-id", "cf-ray"):
        if r.headers.get(h):
            bits.append(f"{h}={r.headers[h]}")
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text[:5000], re.S | re.I)
    bits.append(f"title={collapse(m.group(1))[:80]!r}" if m else f"body={collapse(r.text[:120])!r}")
    return ", ".join(bits)


def looks_like_challenge(text):
    low = (text or "")[:30000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


# ---- price validity windows (contract fields price_valid_until / member_price_valid_until)
NO_WINDOW = object()   # a sale price whose end the source does not give: the key is left out
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



# ------------------------------------------------------------------------------------- client
class Ananas:
    def __init__(self, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "mk,en;q=0.8"})
        self.verbose = verbose
        self.app_id, self.api_key, self.index = ALGOLIA_APP_ID, ALGOLIA_SEARCH_KEY, ALGOLIA_INDEX
        self._rediscovered = False
        self._last = {"algolia": 0.0, "page": 0.0}
        self.requests_made = 0
        self.delivery_failures = 0
        self._nodes = None          # flat category tree
        self._tree_source = None
        self._tree_error = None

    # ---------------------------------------------------------------------------- http
    def _send(self, kind, method, url, **kw):
        """One request with pacing and backoff on 429/5xx. Returns the final Response."""
        kw.setdefault("timeout", 60)
        gap = ALGOLIA_PACE if kind == "algolia" else PAGE_PACE
        r = None
        for attempt in range(1, MAX_RETRIES + 1):
            wait = gap - (time.monotonic() - self._last[kind])
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                self._last[kind] = time.monotonic()
                if attempt == MAX_RETRIES:
                    raise StoreError(f"{method} {url} failed: {e}") from e
                time.sleep(2 ** attempt)
                continue
            finally:
                self.requests_made += 1
            self._last[kind] = time.monotonic()
            if self.verbose:
                print(f"[ananas] {method} {url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s",
                      file=sys.stderr)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** attempt
                log(f"HTTP {r.status_code} from {urlparse(url).netloc}, retrying in {delay}s")
                time.sleep(min(delay, 60))
                continue
            return r
        if r is not None and r.status_code == 429:
            raise Blocked(f"rate limited by {urlparse(url).netloc} after {MAX_RETRIES} attempts: "
                          f"{evidence(r)}")
        raise StoreError(f"{method} {url}: still HTTP {r.status_code if r is not None else '?'} "
                         f"after {MAX_RETRIES} attempts")

    def get_page(self, url, allow_404=False):
        r = self._send("page", "GET", url)
        if r.status_code == 404 and allow_404:
            return r
        if r.status_code in (401, 403, 405) or (r.status_code != 200 and looks_like_challenge(r.text)):
            raise Blocked(f"GET {url}: {evidence(r)}")
        if r.status_code != 200:
            raise StoreError(f"GET {url}: HTTP {r.status_code}")
        if ("self.__next_f" not in r.text and "__NEXT_DATA__" not in r.text
                and looks_like_challenge(r.text)):
            raise Blocked(f"GET {url}: challenge page instead of the Next.js page ({evidence(r)})")
        return r

    # -------------------------------------------------------------------------- algolia
    def algolia(self, params):
        """One Algolia query. params: search parameters (query, filters, page, hitsPerPage, ...)."""
        url = f"https://{self.app_id}-dsn.algolia.net/1/indexes/{self.index}/query"
        for _ in range(2):
            headers = {"x-algolia-application-id": self.app_id, "x-algolia-api-key": self.api_key,
                       "Content-Type": "application/json"}
            r = self._send("algolia", "POST", url, json=params, headers=headers)
            ctype = r.headers.get("content-type", "")
            if r.status_code in (401, 403) and "json" in ctype and not self._rediscovered:
                log(f"Algolia rejected the search key ({r.json().get('message')}); re-reading it "
                    f"from the site's JS bundle")
                self._discover_algolia_config()
                url = f"https://{self.app_id}-dsn.algolia.net/1/indexes/{self.index}/query"
                continue
            if r.status_code == 400 and "json" in ctype:
                raise Usage(f"Algolia rejected the query: {r.json().get('message')}")
            if r.status_code != 200 or "json" not in ctype:
                if r.status_code in (401, 403) or looks_like_challenge(r.text):
                    raise Blocked(f"Algolia: {evidence(r)}")
                raise StoreError(f"Algolia: HTTP {r.status_code} ({ctype}): {r.text[:200]!r}")
            data = r.json()
            if "hits" not in data or "nbHits" not in data:
                raise StoreError(f"Algolia: unexpected payload keys {list(data)[:10]}")
            return data
        raise Blocked("Algolia still rejects the search key after re-reading it from the bundle")

    def _discover_algolia_config(self):
        """Find applicationID / searchOnlyAPIKey / productSearchIndexName in the live JS bundle."""
        self._rediscovered = True
        home = self.get_page(BASE + "/")
        chunks = sorted(set(re.findall(r'(?:https://[^"\s]+)?/_next/static/chunks/[\w./~-]+\.js',
                                       home.text)), key=lambda u: ("_app" not in u, u))
        for u in chunks[:60]:
            js = self._send("page", "GET", u if u.startswith("http") else BASE + u).text
            m = re.search(r'applicationID:"(\w+)",searchOnlyAPIKey:"(\w+)",'
                          r'productSearchIndexName:`(\w+)\$\{', js)
            if m:
                self.app_id, self.api_key, self.index = m.group(1), m.group(2), m.group(3) + "mk"
                log(f"using Algolia app {self.app_id}, index {self.index}")
                return
        raise StoreError("could not find the Algolia config in the site's JS bundle")

    def _pages(self, params, first, limit, seen, out):
        """Collect hits from `first` and the following pages of the same query."""
        data, page = first, 0
        while True:
            for h in data["hits"]:
                if h["objectID"] not in seen:
                    seen.add(h["objectID"])
                    out.append(h)
                    if limit and len(out) >= limit:
                        return
            page += 1
            if page >= (data.get("nbPages") or 0) or not data["hits"]:
                return
            data = self.algolia({**params, "page": page})

    def walk(self, filters="", query="", limit=None, attrs=None, extra=None, group_variants=False):
        """Every hit for filters/query (all pages, split by price above the 30k cap).
        The index groups a seller's size/colour variants into one hit (Algolia `distinct`); unless
        group_variants is set, that grouping is switched off so every orderable listing comes back.
        Returns (hits, meta) with meta = nbHits / queryAfterRemoval / exhaustive of the first page."""
        per = min(limit, HITS_PER_PAGE) if limit else HITS_PER_PAGE
        params = {"query": query, "filters": filters, "hitsPerPage": per,
                  "attributesToRetrieve": attrs or LIST_ATTRS, "attributesToHighlight": [],
                  "attributesToSnippet": [], "analytics": False, "clickAnalytics": False,
                  **({} if group_variants else {"distinct": False}), **(extra or {})}
        first = self.algolia({**params, "page": 0})
        nb = first["nbHits"]
        meta = {"nbHits": nb, "queryAfterRemoval": first.get("queryAfterRemoval"),
                "exhaustive": (first.get("exhaustive") or {}).get("nbHits", first.get("exhaustiveNbHits"))}
        hits, seen = [], set()
        if nb > PAGINATION_CAP and (not limit or limit > PAGINATION_CAP):
            self._walk_split(params, limit, seen, hits, nb)
        else:
            self._pages(params, first, limit, seen, hits)
        if not limit and len(hits) < min(nb, PAGINATION_CAP) and meta["exhaustive"] is not False:
            warn(f"collected {len(hits)} unique listings, Algolia reports nbHits={nb}")
        return hits, meta

    def _walk_split(self, params, limit, seen, hits, nb):
        """Walk a result set above the pagination cap as a union of price ranges."""
        stats = self.algolia({**params, "hitsPerPage": 0, "page": 0, "facets": ["price"]})
        ps = (stats.get("facets_stats") or {}).get("price") or {}
        lo, hi = math.floor(ps.get("min", 0)), math.floor(ps.get("max", 10 ** 8)) + 1
        log(f"{nb} hits exceed Algolia's {PAGINATION_CAP}-hit pagination cap; walking price ranges "
            f"{lo}..{hi} MKD separately")
        ranges, parts = [(lo, hi)], 0
        while ranges:
            a, b = ranges.pop(0)
            p = {**params, "filters": and_filters(params["filters"], f"price >= {a} AND price < {b}")}
            n = self.algolia({**p, "hitsPerPage": 0, "page": 0})["nbHits"]
            if n > SPLIT_TARGET and b - a > 1:
                mid = (a + b) // 2
                ranges[:0] = [(a, mid), (mid, b)]
                continue
            if n == 0:
                continue
            if n > PAGINATION_CAP:
                warn(f"{n} listings priced exactly {a} MKD; only the first {PAGINATION_CAP} are reachable")
            parts += 1
            first = self.algolia({**p, "page": 0})
            self._pages(p, first, limit, seen, hits)
            if limit and len(hits) >= limit:
                return
        log(f"price-split walk: {parts} ranges, {len(hits)} unique listings (first-page estimate {nb})")

    # -------------------------------------------------------------------------- records
    @staticmethod
    def seller_name(m):
        name = collapse((m or {}).get("displayName"))
        cyr = collapse((m or {}).get("displayNameCyr"))
        # Ananas' own shop is "AНАНАС ШОП" with a Latin A. displayNameCyr is machine-transliterated
        # nonsense for Latin names ("Оффице Плус"), so it is used only when displayName mixes scripts.
        if mixed_script(name):
            return cyr if cyr and not mixed_script(cyr) else fold_cyr(name)
        return name or None

    @staticmethod
    def is_own_shop(m):
        if not m:
            return None
        if str(m.get("id")) == str(OWN_SHOP_MERCHANT_ID):
            return True
        return norm_key(m.get("displayName")) in ("ананас шоп", "ananas shop", "ananas sop")

    @staticmethod
    def category_path(p):
        cats = p.get("categories") or {}
        for lvl in ("lvl3", "lvl2", "lvl1", "lvl0"):
            paths = [collapse(c) for c in (cats.get(lvl) or []) if c]
            if paths:
                return " | ".join(dict.fromkeys(paths))
        return None

    @staticmethod
    def attributes(p):
        out = {}
        for grp in ("textAttributes", "colorAttributes", "measurementAttributes",
                    "booleanAttributes", "selectAttributes"):       # later groups win on clashes
            for k, v in (p.get(grp) or {}).items():
                if k == "Model" and grp == "textAttributes":
                    continue
                if isinstance(v, dict):
                    v = v.get("name") or v.get("content") or v.get("value")
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                if isinstance(v, str) and grp == "measurementAttributes" and "|" in v:
                    v = " ".join(x for x in v.split("|") if x)
                if isinstance(v, str) and grp == "colorAttributes" and "|" in v:
                    v = v.split("|", 1)[0]
                if v not in (None, ""):
                    out[k] = v
        return out or None

    @staticmethod
    def gtin(v):
        v = re.sub(r"\s", "", str(v)) if v not in (None, "") else ""
        return v if re.fullmatch(r"\d{8,14}", v) and v.strip("0") else None

    @staticmethod
    def mpn_of(model):
        model = collapse(model if isinstance(model, str) else "")
        if 3 <= len(model) <= 40 and re.search(r"\d", model) and re.search(r"[A-Za-z]", model) \
                and model.count(" ") <= 2:
            return model
        return None

    def hit_to_record(self, h):
        p = h.get("product") or {}
        m = h.get("merchant") or {}
        price, base = to_int(h.get("price")), to_int(h.get("basePrice"))
        avail = h.get("available")
        on = h.get("onStock")
        in_stock = bool(on) if on is not None else (avail > 0 if isinstance(avail, (int, float)) else None)
        note = None
        if in_stock is not None:
            note = f"{'има' if in_stock else 'нема'} на залиха" + (
                f" (available {avail})" if isinstance(avail, (int, float)) else "")
        if h.get("freeShippingNonAplus"):
            ship = 0
        else:
            ship = to_int(h.get("shippingCostPriceNonAplus"))
        extra = {"seller_id": m.get("id"), "ananas_own_shop": self.is_own_shop(m),
                 "fulfilled_by_ananas": h.get("fba"),
                 "available_units": avail if isinstance(avail, (int, float)) else None}
        rdp = to_int(h.get("regularDiscountPrice"))
        if rdp and price and rdp > price:
            extra["regular_discount_price_mkd"] = rdp      # 2nd struck-through figure on the page
        # The index carries the SALE window (seller wall-clock time, no offset) only for ~3% of sale
        # listings: those whose SALE stacks on a standing regularDiscountPrice. A hit with
        # onSale false can keep a stale window after its sale ended (938098), so only trust it
        # while the hit is on sale. A sale hit without one cannot tell, so the key is left out
        # (detail always has the window, priceV2.dateTo); a price not on sale is standing (null).
        on_sale = bool(h.get("onSale")) and bool(base and price and base > price)
        until = skopje_iso(h.get("discountEndTime")) if on_sale else None
        window = {} if h.get("onSale") and not until else {"price_valid_until": until}   # on sale, no end: cannot tell
        if h.get("freeShippingAplus") is not None:
            extra["shipping_mkd_ananas_plus"] = 0 if h.get("freeShippingAplus") else \
                to_int(h.get("shippingCostPriceAplus"))
        if p.get("adultContent"):
            extra["adult_content"] = True
        return {
            "store": STORE,
            "id": str(h["objectID"]),
            "sku": h.get("apId") or None,
            "title": collapse(p.get("name")),
            "url": f"{BASE}/proizvod/{p.get('slug') or 'x'}/{h['objectID']}",
            "brand": collapse(p.get("brand")) or None,
            "price_mkd": price,
            "regular_price_mkd": base if (base and price and base > price) else None,
            **window,
            "in_stock": in_stock,
            "stock_note": note,
            "category": self.category_path(p),
            "ean": self.gtin(p.get("ean")),
            "mpn": self.mpn_of((p.get("textAttributes") or {}).get("Model")),
            "seller": self.seller_name(m),
            "shipping_mkd": ship,
            "international_supplier": h.get("ananasGlobal"),
            "attributes": self.attributes(p),
            "extra": extra,
        }

    # ------------------------------------------------------------------------ categories
    def _graphql(self, op, query, variables):
        r = self._send("page", "POST", GRAPHQL_PUBLIC, json={
            "query": query, "variables": variables, "operationName": op},
            headers={"Content-Type": "application/json", "x-ananas-market": "MK",
                     "accept-language": "mk", "Origin": BASE, "Referer": BASE + "/"})
        if r.status_code in (401, 403):
            raise Blocked(f"GraphQL {op}: {evidence(r)}")
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            raise StoreError(f"GraphQL {op}: HTTP {r.status_code}: {r.text[:200]!r}")
        d = r.json()
        if d.get("errors") and not d.get("data"):
            raise StoreError(f"GraphQL {op}: {d['errors'][0].get('message')}")
        return d.get("data") or {}

    def nodes(self):
        """The category tree as flat nodes (GraphQL; Algolia facets as a fallback)."""
        if self._nodes is not None:
            return self._nodes
        level = "...CategoryFields\n"
        for _ in range(6):
            level = "...CategoryFields\nchildren {\n" + level + "}\n"
        q = ("query Categories($rootOnly: Boolean = true) {\n  response: getCategories(rootOnly: "
             "$rootOnly) {\n" + level + "}\n}\nfragment CategoryFields on Category {\n  id\n  name\n"
             "  slug\n}\n")
        try:
            roots = self._graphql("Categories", q, {"rootOnly": False}).get("response") or []
            if not roots:
                raise StoreError("empty category tree")
        except StoreError as e:
            warn(f"category tree from the GraphQL gateway failed ({e}); rebuilding names and paths "
                 f"from Algolia facets (no ids or slugs)")
            self._tree_error = e
            self._nodes = self._nodes_from_facets()
            self._tree_source = "algolia"
            return self._nodes
        out = []

        def walk(n, names, parent, depth):
            raw = names + [n.get("name") or ""]
            node = {"id": str(n["id"]), "name": collapse(n.get("name")), "slug": n.get("slug"),
                    "path": " > ".join(collapse(x) for x in raw),
                    # Algolia's hierarchical value: raw names joined, outer whitespace stripped
                    # (a few names end in a space, which survives inside deeper paths).
                    "apath": " > ".join(raw).strip(), "depth": depth, "parent": parent}
            out.append(node)
            for c in n.get("children") or []:
                walk(c, raw, node["id"], depth + 1)

        for r in roots:
            walk(r, [], None, 0)
        self._nodes, self._tree_source = out, "graphql"
        return out

    def _nodes_from_facets(self):
        data = self.algolia({"query": "", "hitsPerPage": 0, "maxValuesPerFacet": FACET_VALUES_MAX,
                             "facets": [f"product.categories.lvl{i}" for i in range(4)],
                             "analytics": False})
        out = []
        for i in range(4):
            for path in sorted((data.get("facets") or {}).get(f"product.categories.lvl{i}", {})):
                parts = path.split(" > ")
                out.append({"id": None, "name": collapse(parts[-1]), "slug": None,
                            "path": " > ".join(collapse(x) for x in parts), "apath": path,
                            "depth": i, "parent": None})
        return out

    def counts(self, nodes):
        """apath -> listing count from Algolia's hierarchical facets; levels whose facet list hit
        the 1000-value ceiling are re-queried per group of departments."""
        attrs = [f"product.categories.lvl{i}" for i in range(4)]
        data = self.algolia({"query": "", "hitsPerPage": 0, "facets": attrs,
                             "maxValuesPerFacet": FACET_VALUES_MAX, "analytics": False})
        facets = data.get("facets") or {}
        counts, complete = {}, {}
        for i, a in enumerate(attrs):
            vals = facets.get(a) or {}
            counts.update(vals)
            complete[i] = len(vals) < FACET_VALUES_MAX
        for i, a in enumerate(attrs):
            if complete[i]:
                continue
            roots = [n for n in nodes if n["depth"] == 0]
            sizes = {r["apath"]: sum(1 for n in nodes if n["depth"] == i
                                     and n["apath"].startswith(r["apath"] + " > ")) for r in roots}
            groups, cur, size = [], [], 0
            for r, k in sorted(sizes.items(), key=lambda x: -x[1]):
                if cur and size + k > 900:
                    groups.append(cur)
                    cur, size = [], 0
                cur.append(r)
                size += k
            if cur:
                groups.append(cur)
            ok = True
            for g in groups:
                f = " OR ".join(f"product.categories.lvl0:{aq(r)}" for r in g)
                vals = (self.algolia({"query": "", "filters": f, "hitsPerPage": 0, "facets": [a],
                                      "maxValuesPerFacet": FACET_VALUES_MAX, "analytics": False})
                        .get("facets") or {}).get(a) or {}
                # A listing filed in two departments brings its other department's paths into this
                # group's facet with a partial count (e.g. 1 instead of 909): keep only own paths.
                own = tuple(r + " > " for r in g)
                counts.update({k: v for k, v in vals.items() if k.startswith(own)})
                ok = ok and len(vals) < FACET_VALUES_MAX
            complete[i] = ok
        return counts, complete

    def categories(self, grep=None):
        nodes = self.nodes()
        counts, complete = self.counts(nodes)
        sel = nodes
        if grep:
            rx = re.compile(grep, re.I)
            sel = []
            for n in nodes:
                folded = fold_cyr(n["path"])
                hay = (n["name"], n["path"], n["slug"] or "", folded, translit(folded), en_tags(n["path"]))
                if any(rx.search(x) for x in hay if x):
                    sel.append(n)
        out = []
        for n in sel:
            c = counts.get(n["apath"])
            if c is None and complete.get(n["depth"]):
                c = 0
            out.append({"id": n["id"], "slug": n["slug"], "name": n["name"], "path": n["path"],
                        "url": f"{BASE}/kategorii/{n['slug']}" if n["slug"] else None,
                        "parent": n["parent"], "count": c})
        log(f"{len(nodes)} categories in the tree ({self._tree_source}); {len(out)} selected; count = "
            f"listings incl. size/colour variants (what `list` returns; the site's cards fold variants)")
        return out

    def category_from_page(self, slug):
        """The Algolia filter and displayed total from a category page's InstantSearch state."""
        r = self.get_page(f"{BASE}/kategorii/{slug}", allow_404=True)
        if r.status_code == 404:
            raise NotFound(f"category page /kategorii/{slug} is 404")
        html = r.text
        i = html.find('window[Symbol.for("InstantSearchInitialResults")]')
        if i < 0:
            raise StoreError(f"/kategorii/{slug}: no InstantSearch state on the page")
        state, _ = json.JSONDecoder().raw_decode(html, html.find("{", i))
        entry = state.get(self.index) or next(iter(state.values()))
        st = entry.get("state") or {}
        filters = st.get("filters")
        if not filters:
            ref = st.get("hierarchicalFacetsRefinements") or {}
            path = next((v[0] for v in ref.values() if v), None)
            if not path:
                raise StoreError(f"/kategorii/{slug}: no category filter in the page state")
            filters = f"product.categories.lvl{path.count(' > ')}:{aq(path)}"
        total = ((entry.get("results") or [{}])[0]).get("nbHits")
        return filters, total

    def resolve(self, arg):
        """-> {node, filter, label}. Accepts id, slug path, URL, 'A > B' path or unique name."""
        a = (arg or "").strip()
        if not a:
            raise Usage("empty category")
        nodes = self.nodes()
        node, slug = None, None
        if re.fullmatch(r"\d+", a):
            if self._tree_source != "graphql":
                msg = ("category ids need the GraphQL category tree, which is unavailable; "
                       "pass the slug or the 'A > B > C' path instead")
                if isinstance(self._tree_error, Blocked):
                    raise Blocked(f"{self._tree_error} ({msg})")
                raise Usage(msg)
            node = next((n for n in nodes if n["id"] == a), None)
            if not node:
                raise NotFound(f"no category with id {a} (run `categories --grep ...`)")
        elif re.match(r"^(https?://|(www\.)?ananas\.mk/|/)", a, re.I):
            u = a if re.match(r"^https?://", a, re.I) else (BASE + a if a.startswith("/") else "https://" + a)
            path = unquote(urlparse(u).path)
            m = re.match(r"/(?:mk/)?(?:kategorii|kategorije|kategorija)/(.+?)/?$", path)
            if not m:
                raise Usage(f"not a category URL: {arg} (expected https://ananas.mk/kategorii/<slug path>)")
            slug = m.group(1).lower()
        elif ">" in a:
            want = norm_path(a)
            node = next((n for n in nodes if norm_path(n["path"]) == want), None)
            if not node:
                if self._tree_source == "algolia":
                    return {"node": None, "label": a,
                            "filter": f"product.categories.lvl{a.count('>')}:{aq(collapse(a))}"}
                raise NotFound(f"no category with path {a!r} (run `categories --grep ...`)")
        elif re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)*/?", a):
            slug = a.strip("/")
        else:
            want = norm_key(a)
            cands = [n for n in nodes if norm_key(n["name"]) == want]
            if len(cands) > 1:
                raise Usage(f"category name {a!r} is ambiguous: " +
                            "; ".join(f"{n['id']} = {n['path']}" for n in cands[:8]) + " (pass the id)")
            if not cands:
                raise NotFound(f"no category named {a!r} (run `categories --grep ...`)")
            node = cands[0]
        if slug is not None:
            node = next((n for n in nodes if n["slug"] == slug), None)
            if not node and "/" not in slug:
                tail = [n for n in nodes if n["slug"] and n["slug"].rsplit("/", 1)[-1] == slug]
                if len(tail) == 1:
                    node = tail[0]
                elif len(tail) > 1:
                    raise Usage(f"slug {slug!r} is ambiguous: " +
                                "; ".join(f"{n['slug']}" for n in tail[:8]))
            if not node:
                # Not in the public tree (hidden or renamed): ask the category page itself.
                filters, total = self.category_from_page(slug)
                log(f"/kategorii/{slug} is not in the category tree; using the page's own filter")
                return {"node": None, "filter": filters, "label": slug, "site_total": total}
        return {"node": node, "label": f"{node['path']} (id {node['id']})",
                "filter": f"product.categories.lvl{node['depth']}:{aq(node['apath'])}"}

    # --------------------------------------------------------------------- filter tokens
    def _bare_attr(self, key, base_filter):
        """Find which attribute group a bare key such as 'DisplayDiagonal' lives in."""
        cands = [f"product.{g}.{key}" for g in ATTR_GROUPS.values()]
        data = self.algolia({"query": "", "filters": base_filter, "hitsPerPage": 0, "facets": cands,
                             "maxValuesPerFacet": 1, "analytics": False})
        for c in cands:
            if (data.get("facets") or {}).get(c):
                return c
        raise Usage(f"no attribute {key!r} with values here; run `facets` and copy a token")

    def parse_filters(self, tokens, base_filter):
        clauses = []
        for tok in tokens or []:
            t = tok.strip()
            if "=" not in t:
                if t in FILTER_ALIASES and FILTER_ALIASES[t] in BOOL_FLAGS:
                    t += "=true"
                else:
                    raise Usage(f"bad --filter {tok!r}: expected KEY=VALUE (tokens are printed by `facets`)")
            key, val = (x.strip() for x in t.split("=", 1))
            vals = [v for v in (x.strip() for x in val.split("||")) if v]
            if not key or not vals:
                raise Usage(f"bad --filter {tok!r}: empty key or value")
            if key == "price":
                m = re.fullmatch(r"(\d+(?:\.\d+)?)?\s*-\s*(\d+(?:\.\d+)?)?", val.replace(" ", ""))
                if not m or not (m.group(1) or m.group(2)):
                    raise Usage(f"bad price filter {tok!r}: use price=MIN-MAX (either side optional)")
                if m.group(1):
                    clauses.append(f"price >= {m.group(1)}")
                if m.group(2):
                    clauses.append(f"price <= {m.group(2)}")
                continue
            if key == "category":
                ors = []
                for v in vals:
                    ors.append(self.resolve(v)["filter"])
                clauses.append(" OR ".join(ors))
                continue
            if key in FILTER_ALIASES:
                attr = FILTER_ALIASES[key]
            elif "." in key and key.split(".", 1)[0] in ATTR_GROUPS:
                g, k = key.split(".", 1)
                attr = f"product.{ATTR_GROUPS[g]}.{k}"
            elif key.startswith("product.") or key.startswith("merchant.") or key in BOOL_FLAGS:
                attr = key
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key):
                attr = self._bare_attr(key, base_filter)
            else:
                raise Usage(f"bad --filter key {key!r}")
            ors = []
            for v in vals:
                if attr in BOOL_FLAGS or attr.startswith("product.booleanAttributes."):
                    if v.lower() not in ("true", "false", "1", "0", "yes", "no"):
                        raise Usage(f"{key} takes true/false, not {v!r}")
                    yes = v.lower() in ("true", "1", "yes")
                    if attr in BOOL_FLAGS:
                        # Numeric form: the facet form `onSale:true` misses many listings whose
                        # onSale is true (kitchenware 2026-10-03: 676 vs 1,139; every out-of-stock
                        # one), while `onSale=1` matches the attribute and the facet count exactly.
                        ors.append(f"{attr}={1 if yes else 0}")
                    else:
                        ors.append(f"{attr}:{'true' if yes else 'false'}")
                elif attr == "merchant.id":
                    ors.append(f"merchant.id:{int(v)}" if v.isdigit() else f"merchant.id:{aq(v)}")
                elif attr == "merchant.displayName" and norm_key(v) in ("ананас шоп", "ananas shop",
                                                                         "ananas sop", "ananas"):
                    ors.append(f"merchant.id:{OWN_SHOP_MERCHANT_ID}")
                else:
                    ors.append(f"{attr}:{aq(v)}")
            clauses.append(" OR ".join(ors))
        return clauses

    # ------------------------------------------------------------------------- commands
    def search(self, query, limit=None, in_stock=False, strict=False, group_variants=False):
        query = collapse(query)
        if not query:
            raise Usage("empty search query")
        extra = {"removeWordsIfNoResults": "none"} if strict else None
        hits, meta = self.walk(filters="onStock:true" if in_stock else "", query=query, limit=limit,
                               extra=extra, group_variants=group_variants)
        qar = meta.get("queryAfterRemoval") or ""
        dropped = re.findall(r"<em>(.*?)</em>", qar)
        if dropped and hits:
            warn(f"no listing matches every word of {query!r}; Algolia dropped "
                 f"{', '.join(repr(d) for d in dropped)} and matched only the remaining words "
                 f"(use --strict to get no results instead)")
        nb = meta["nbHits"]
        log(f"search {query!r}{' (in stock)' if in_stock else ''}: Algolia nbHits={nb}, "
            f"returned {len(hits)}" + (f" (capped by --limit {limit})" if limit and nb > len(hits) else ""))
        if not has_cyr(query) and re.search(r"(?<![\w-])[^\W\d_]{4,}(?![\w-])", query):
            log("note: titles are mostly Cyrillic and only some Latin spellings are mapped to them "
                "(igracka 3 vs играчка 18,688; blender 303 vs блендер 680); for product words also "
                "search the Macedonian Cyrillic word. Brands and model codes are fine in Latin.")
        return [self.hit_to_record(h) for h in hits]

    def list_category(self, arg, in_stock=False, limit=None, filters=None, check_site=False,
                      group_variants=False):
        t = self.resolve(arg)
        extra = self.parse_filters(filters, t["filter"])
        f = and_filters(t["filter"], "onStock:true" if in_stock else "", *extra)
        hits, meta = self.walk(filters=f, limit=limit, group_variants=group_variants)
        if not hits and not (in_stock or extra):
            node = t.get("node")
            if node and node.get("slug"):
                # The tree name may have drifted from Algolia's value: use the page's own filter.
                pf, total = self.category_from_page(node["slug"])
                if pf != t["filter"]:
                    log(f"retrying with the category page's own filter {pf!r} (page shows {total})")
                    f = pf
                    hits, meta = self.walk(filters=pf, limit=limit, group_variants=group_variants)
            if not hits:
                raise NotFound(f"category {t['label']} has no listings")
        recs = [self.hit_to_record(h) for h in hits]
        nb = meta["nbHits"]
        # A complete walk is the exact count; nbHits is an estimate above ~30k hits.
        total = nb if limit else len(recs)
        cards = total
        if not group_variants:
            # The site's own count (variants folded into one card): one cheap count-only query.
            cards = self.algolia({"query": "", "filters": f, "hitsPerPage": 0, "analytics": False})["nbHits"]
        log(f"category {t['label']}: Algolia nbHits={nb}"
            + (" (in stock)" if in_stock else "") + (f" with {len(extra)} filter(s)" if extra else "")
            + f", fetched {len(recs)}"
            + (f"; the site shows {cards} product cards, folding {total - cards} size/colour variant "
               f"listings into them (--group-variants for that view)" if cards < total else ""))
        if not recs:
            warn("no listing in this category matches --in-stock/--filter")
        site_total = t.get("site_total")
        if check_site and site_total is None and t.get("node") and t["node"].get("slug"):
            try:
                _, site_total = self.category_from_page(t["node"]["slug"])
            except Blocked:
                raise
            except StoreError as e:
                warn(f"could not read the page's displayed total ({e}; department and some parent "
                     f"pages are landing pages of sub-category tiles)")
        if site_total is not None:
            log(f"category page shows {site_total} listings (unfiltered)")
            if not (in_stock or extra or limit) and site_total != cards:
                warn(f"page total {site_total} != Algolia's product-card count {cards}")
        return recs

    def facets(self, arg):
        t = self.resolve(arg)
        data = self.algolia({"query": "", "filters": t["filter"], "hitsPerPage": 0, "facets": ["*"],
                             "maxValuesPerFacet": FACET_VALUES_MAX, "analytics": False,
                             "distinct": False})
        nb = data["nbHits"]
        if not nb:
            raise NotFound(f"category {t['label']} has no listings")
        fac = data.get("facets") or {}
        order = (((data.get("renderingContent") or {}).get("facetOrdering") or {})
                 .get("facets") or {}).get("order") or []
        if len(order) > CURATED_MAX:      # no per-category merchandising rule for this category
            order = []
        order = [a for a in dict.fromkeys(order) if fac.get(a)]
        curated = set(order) | set(SITE_STANDARD_FACETS)
        by_path = {n["apath"]: n for n in self.nodes()}
        recs = []

        def add(attr, name, value, count, token):
            recs.append({"name": name, "value": value, "count": count, "token": token,
                         "attribute": attr, "site_filter": attr in curated})

        ps = (data.get("facets_stats") or {}).get("price")
        if ps:
            lo, hi = math.floor(ps["min"]), math.ceil(ps["max"])
            add("price", "price", f"{lo}-{hi}", nb, f"price={lo}-{hi}")
        depth = t["node"]["depth"] if t.get("node") else None
        if depth is not None and depth < 3:
            child_attr = f"product.categories.lvl{depth + 1}"
            prefix = t["node"]["apath"] + " > "
            for path, c in sorted((fac.get(child_attr) or {}).items(), key=lambda x: -x[1]):
                if not path.startswith(prefix):
                    continue
                n = by_path.get(path)
                add(child_attr, "category", collapse(path[len(prefix):]), c,
                    f"category={n['id'] if n and n.get('id') else collapse(path)}")
        rest = []
        for attr, vals in fac.items():
            if attr in FACET_SKIP or attr.startswith("product.categories.") or not vals:
                continue
            if attr.split(".")[-1] in FACET_TEXT_SKIP:
                continue
            rest.append(attr)
        fixed = ["product.brand", "merchant.displayName", "onStock", "onSale", "fba",
                 "freeShippingNonAplus"]

        def rank(a):
            if a in order:
                return (0, order.index(a), 0)
            if a in fixed:
                return (1, fixed.index(a), 0)
            return (2, 0, -sum(fac[a].values()))

        names = {"product.brand": "brand", "merchant.displayName": "seller", "onStock": "in_stock",
                 "onSale": "on_sale", "fba": "fulfilled_by_ananas",
                 "freeShippingNonAplus": "free_shipping"}
        for attr in sorted(rest, key=rank):
            m = re.match(r"product\.(\w+Attributes)\.(.+)$", attr)
            if attr in names:
                name, key = names[attr], names[attr]
            elif m and m.group(1) in GROUP_SHORT:
                name, key = m.group(2), f"{GROUP_SHORT[m.group(1)]}.{m.group(2)}"
            else:
                name, key = attr, attr
            for v, c in sorted(fac[attr].items(), key=lambda x: -x[1]):
                if len(v) > 100:
                    continue
                shown = v
                if attr == "merchant.displayName" and mixed_script(v):
                    shown = fold_cyr(v)
                elif attr.startswith("product.measurementAttributes.") and "|" in v:
                    shown = " ".join(x for x in v.split("|") if x)
                elif attr.startswith("product.colorAttributes.") and "|" in v:
                    shown = v.split("|", 1)[0]          # "Црна|#000000|false" = name|hex|multicolour
                add(attr, name, shown, c, f"{key}={v}")
        ex = (data.get("exhaustive") or {}).get("facetsCount", data.get("exhaustiveFacetsCount"))
        log(f"category {t['label']}: {nb} listings, {len(rest)} facet attributes, {len(recs)} values"
            + ("" if ex is not False else " (Algolia flags facet counts as approximate)")
            + f"; the category page's own filter widgets: "
            + (", ".join(a.rsplit(".", 1)[-1] for a in order) or "none beyond brand/price/flags"))
        return recs

    # --------------------------------------------------------------------------- detail
    def parse_product_ref(self, arg):
        """-> (listing id, product URL)."""
        a = arg.strip()
        m = re.search(r"/proizvod/(?:([^/?#]+)/)?(\d+)/?(?:[?#].*)?$", a)
        if m:
            return m.group(2), f"{BASE}/proizvod/{m.group(1) or 'x'}/{m.group(2)}"
        if re.fullmatch(r"\d{1,9}", a):
            return a, f"{BASE}/proizvod/x/{a}"
        if re.fullmatch(r"[A-Za-z0-9]{8,12}", a) and re.search(r"[A-Za-z]", a):
            # The site's "Шифра на производот" (apId), e.g. HLBDTOQ7Q9 (upper case on the site).
            a = a.upper()
            data = self.algolia({"query": a, "hitsPerPage": 5, "attributesToRetrieve": ["objectID", "apId"],
                                 "analytics": False, "removeWordsIfNoResults": "none", "distinct": False})
            hit = next((h for h in data["hits"] if h.get("apId") == a), None)
            if not hit:
                raise NotFound(f"no listing with Шифра {a}")
            return str(hit["objectID"]), f"{BASE}/proizvod/x/{hit['objectID']}"
        if re.match(r"^(https?://)?(www\.)?ananas\.mk", a, re.I):
            raise Usage(f"not a product URL: {arg} (want https://ananas.mk/proizvod/<slug>/<id>)")
        raise Usage(f"cannot read a listing id from {arg!r} (want a product URL, a listing id or a Шифра)")

    @staticmethod
    def _flight(html):
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
        return "".join(json.loads('"' + c + '"') for c in chunks)

    @staticmethod
    def _flight_text_ref(flight, ref):
        """Resolve a '$43' reference to a text row '43:T<hexlen>,<text>' (length in UTF-8 bytes)."""
        if not (isinstance(ref, str) and re.fullmatch(r"\$[0-9a-f]+", ref)):
            return ref
        b = flight.encode("utf-8")
        m = re.search(rb"(?:^|\n)" + ref[1:].encode() + rb":T([0-9a-f]+),", b)
        if not m:
            return None
        n = int(m.group(1), 16)
        return b[m.end():m.end() + n].decode("utf-8", "replace")

    @staticmethod
    def _active_tags(flight):
        """Site-wide CMS promo tags ("cmsActiveTags": tag -> label/description); a product page
        shows a tag when the listing's own `tags` contain it (e.g. v_abrzo -> code abrzo10)."""
        i = flight.find('"cmsActiveTags":')
        if i < 0:
            return {}
        try:
            arr, _ = json.JSONDecoder().raw_decode(flight, i + len('"cmsActiveTags":'))
        except ValueError:
            return {}
        return {a["tag"]: a for a in arr if isinstance(a, dict) and a.get("tag")} if isinstance(arr, list) else {}

    @staticmethod
    def _inventory_from_page(flight, oid):
        dec = json.JSONDecoder()
        i = flight.find('{"product":{"id":"%s"' % oid)
        if i >= 0:
            obj, _ = dec.raw_decode(flight, i)
            return obj["product"]
        j = flight.find('"canonicalId":"')     # fallback: back up to the enclosing object
        while j >= 0:
            k = j
            for _ in range(50):
                k = flight.rfind("{", 0, k)
                if k < 0:
                    break
                try:
                    obj, _ = dec.raw_decode(flight, k)
                except ValueError:
                    continue
                if isinstance(obj, dict) and "priceV2" in obj and str(obj.get("id")) == oid:
                    return obj
            j = flight.find('"canonicalId":"', j + 1)
        return None

    def delivery_promise(self, oid):
        """The guest delivery window the product page shows (same public GraphQL call)."""
        q = ("query GetCalculateGuestDeliveryPromise($request: CalculateGuestDeliveryPromiseRequest!) {\n"
             "  calculateGuestDeliveryPromise(request: $request) {\n    dateFrom\n    dateTo\n  }\n}")
        try:
            d = self._graphql("GetCalculateGuestDeliveryPromise", q,
                              {"request": {"merchantInventoryId": int(oid), "quantity": 1}})
        except StoreError as e:
            self.delivery_failures += 1
            warn(f"delivery promise for {oid} failed ({e}); delivery_estimate left null")
            return None
        d = d.get("calculateGuestDeliveryPromise") or {}
        f, t = d.get("dateFrom"), d.get("dateTo")
        if not f or not t:
            return None
        try:
            d1, d2 = dt.date.fromisoformat(f[:10]), dt.date.fromisoformat(t[:10])
            today = dt.date.today()
            return f"{d1.isoformat()}..{d2.isoformat()} ({(d1 - today).days}-{(d2 - today).days} days)"
        except ValueError:
            return f"{f}..{t}"

    @staticmethod
    def sale_valid_until(pv, price, base):
        """priceV2 -> when the current sellablePrice ends: dateTo of a running SALE /
        CLEARANCE_SALE (the seller's wall-clock time in Skopje, e.g. '2026-10-04T23:00' or
        '2026-10-30T23:59:59'; the page prints 'Понудата важи од .. до ..' and a countdown).
        None for a standing price (priceV2 present: no discountType, or no markdown). NO_WINDOW
        (key left out) when the page has no priceV2 or a running sale has no readable dateTo."""
        if not pv:
            return NO_WINDOW                    # a page without priceV2 cannot tell
        if not (pv.get("discountType") and price is not None and base and base > price):
            return None
        return skopje_iso(pv.get("dateTo")) or NO_WINDOW   # a running sale without a readable end

    def detail_one(self, arg):
        oid, url = self.parse_product_ref(arg)
        r = self.get_page(url, allow_404=True)
        if r.status_code == 404:
            raise NotFound(f"listing {oid}: HTTP 404 (unknown or delisted listing id)")
        flight = self._flight(r.text)
        mi = self._inventory_from_page(flight, oid)
        if not mi:
            if looks_like_challenge(r.text):
                raise Blocked(f"{url}: challenge page instead of the product page ({evidence(r)})")
            raise StoreError(f"{url}: merchant-inventory object not found in the page's flight data")
        data = self.algolia({"query": "", "filters": f"objectID:{aq(oid)}", "hitsPerPage": 1,
                             "attributesToRetrieve": DETAIL_ATTRS, "analytics": False,
                             "distinct": False})
        hit = data["hits"][0] if data["hits"] else None
        prod = mi.get("product") or {}
        m = mi.get("merchant") or {}
        brand = prod.get("brand")
        if isinstance(brand, str) and brand.startswith("{"):
            try:
                brand = json.loads(brand)
            except ValueError:
                pass
        brand = brand.get("name") if isinstance(brand, dict) else brand
        pv = mi.get("priceV2") or {}
        price = to_int(pv.get("sellablePrice")) if pv.get("sellablePrice") is not None else to_int(mi.get("price"))
        base = to_int(pv.get("basePrice"))
        avail = mi.get("available")
        if hit:
            rec = self.hit_to_record(hit)
            if price is not None and rec["price_mkd"] != price:
                log(f"note: {oid} Algolia price {rec['price_mkd']} != page price {price}; using the page price")
        else:
            crumbs = [c.get("name") for c in sorted(prod.get("breadcrumbs") or [],
                                                    key=lambda c: -(c.get("priority") or 0))]
            rec = {"store": STORE, "id": oid, "sku": None, "title": None, "url": url, "brand": None,
                   "price_mkd": None, "regular_price_mkd": None,
                   "in_stock": None, "stock_note": None,
                   "category": " > ".join(collapse(c) for c in crumbs if c) or None, "ean": None,
                   "mpn": None, "seller": self.seller_name(m), "shipping_mkd": None,
                   "international_supplier": mi.get("ananasGlobal"), "attributes": None,
                   "extra": {"seller_id": m.get("id"), "ananas_own_shop": self.is_own_shop(m),
                             "fulfilled_by_ananas": mi.get("fba"), "available_units": None}}
        rec["sku"] = mi.get("apId") or rec["sku"]
        rec["title"] = collapse(prod.get("name")) or rec["title"]
        # /proizvod/x/<id> redirects (308) to the listing's own slug URL. `canonicalId` is NOT this
        # listing: it is the product's SEO-canonical offer, often another seller's listing at another
        # price (ТЕХНОМАРКЕТ 3929700 at 5,499 -> King Soft 3409583 at 4,990), so it only goes to extra.
        final = unquote(urlparse(r.url).path)
        if re.fullmatch(r"/proizvod/[^/]+/%s/?" % re.escape(oid), final):
            rec["url"] = f"{BASE}{final.rstrip('/')}"
        canon = mi.get("canonicalId")
        if canon and not str(canon).rstrip("/").endswith("/" + oid):
            rec.setdefault("extra", {})["canonical_offer_url"] = f"{BASE}/proizvod/{canon}"
        rec["brand"] = collapse(brand) or rec["brand"]
        if price is not None:
            rec["price_mkd"] = price
            rec["regular_price_mkd"] = base if (base and base > price) else None
            rec["price_valid_until"] = self.sale_valid_until(pv, price, base)
            if rec["price_valid_until"] is NO_WINDOW:
                del rec["price_valid_until"]
        if (mi.get("shippingCost") or {}).get("price") is not None:
            rec["shipping_mkd"] = to_int(mi["shippingCost"]["price"])
        if isinstance(avail, (int, float)):
            lvl = mi.get("stockLevel")
            rec["in_stock"] = avail > 0
            rec["stock_note"] = (f"{'има' if avail > 0 else 'нема'} на залиха (available {avail}"
                                 + (f", stockLevel {lvl}" if lvl is not None else "") + ")")
        ex = rec.setdefault("extra", {})
        ex["available_units"] = avail if isinstance(avail, (int, float)) else ex.get("available_units")
        ex["seller_sku"] = mi.get("sku") or None
        rdp = to_int(pv.get("regularDiscountPrice"))
        if rdp and price and rdp > price:
            ex["regular_discount_price_mkd"] = rdp
        note = None
        if pv.get("discountType") and pv.get("discountAmount"):
            note = (f"{pv['discountType']} -{to_int(pv.get('discountPercentage'))}% "
                    f"(-{to_int(pv['discountAmount'])} MKD from {base})")
            if pv.get("dateFrom") or pv.get("dateTo"):
                note += f", valid {pv.get('dateFrom') or '?'}..{pv.get('dateTo') or '?'}"
        ex["price_note"] = note
        if pv.get("vat") is not None:
            ex["vat_percent"] = pv.get("vat")
        active = self._active_tags(flight)
        promos = []
        for tag in mi.get("tags") or []:
            a = active.get(tag)
            if a:
                label = collapse(a.get("tagLabel"))
                text = collapse(html_text(a.get("tagDescription")))
                promos.append(f"{label}: {text}" if text else label)
        ex["promo_note"] = "; ".join(dict.fromkeys(promos)) or None   # codes are never applied to price

        desc = self._flight_text_ref(flight, prod.get("description"))
        if desc is None and hit:
            desc = (hit.get("product") or {}).get("description")
        desc_txt = collapse(html_text(desc))
        warranty = None
        for b in mi.get("badges") or []:
            t = fold_cyr(collapse(html_text(b.get("text"))))     # "1 годинa" has a Latin a
            if re.search(r"гаранц|garanc|warrant", t, re.I):
                warranty = t
                break
        if not warranty:
            mm = re.search(r"гаранциј\w*\s*(?:од\s*)?[:\-–]?\s*(\d+\s*(?:\+\s*\d+\s*)?(год\w*|месец\w*|мес\.?|"
                           r"meseci|years?|months?)\.?)", desc_txt, re.I)
            if mm:
                warranty = mm.group(0).strip()
        pairs, model = [], None
        for grp in ((prod.get("specifications") or {}).get("attributes") or []):
            if grp.get("hidden"):
                continue
            for a in grp.get("attributes") or []:
                val = a.get("content", a.get("value", a.get("name")))
                if isinstance(val, bool):
                    val = "Да" if val else "Не"
                if val is None or val == "":
                    continue
                if a.get("key") == "Model":
                    model = val
                unit = a.get("unit") or ""
                pairs.append(f"{a.get('trait') or a.get('key')}: {val}{(' ' + unit) if unit else ''}")
        rec["mpn"] = rec.get("mpn") or self.mpn_of(model)
        rec.update({
            "warranty": warranty,
            "ean": self.gtin(prod.get("ean")) or rec.get("ean"),
            "specs": collapse(desc_txt + (" | Спецификации: " + "; ".join(pairs) if pairs else "")),
            "per_location_stock": None,   # a listing has one stock figure, no per-store breakdown
            # The gateway quotes dates even for a listing with 0 units, which cannot be ordered.
            "delivery_estimate": None if rec.get("in_stock") is False else self.delivery_promise(oid),
        })
        return rec

    def detail(self, refs):
        """-> (records in input order, exit code). Failures become {"input","error"} rows."""
        out, ok, hard = [], 0, False
        for i, ref in enumerate(refs):
            try:
                out.append(self.detail_one(ref))
                ok += 1
            except Blocked as e:
                out.append({"input": ref, "error": f"blocked: {e}"})
                out.extend({"input": r, "error": "skipped: store blocked the client"} for r in refs[i + 1:])
                raise _BatchBlocked(out, e)
            except (NotFound, Usage) as e:
                log(f"{ref}: {e}")
                out.append({"input": ref, "error": str(e)})
            except (StoreError, requests.RequestException, ValueError, KeyError) as e:
                log(f"{ref}: error: {e}")
                out.append({"input": ref, "error": f"{type(e).__name__}: {e}"})
                hard = True
        if ok < len(refs):
            log(f"{len(refs) - ok} of {len(refs)} input(s) failed")
        if self.delivery_failures:
            warn(f"{self.delivery_failures} delivery-promise call(s) failed; those records have "
                 f"delivery_estimate=null")
        return out, (0 if ok else (1 if hard else 2))


class _BatchBlocked(Exception):
    def __init__(self, recs, err):
        super().__init__(str(err))
        self.recs, self.err = recs, err


# ---------------------------------------------------------------------------------------- CLI
def print_products(recs):
    for r in recs:
        if "error" in r:
            print(f"      !  ERROR  {r['input']}: {r['error']}")
            continue
        price = f"{r['price_mkd']:>7}" if r.get("price_mkd") is not None else "      ?"
        was = f" (was {r['regular_price_mkd']})" if r.get("regular_price_mkd") else ""
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        own = "*" if (r.get("extra") or {}).get("ananas_own_shop") else " "
        seller = (r.get("seller") or "?")[:16]
        ship = f" +{r['shipping_mkd']}" if r.get("shipping_mkd") else ""
        print(f"{price} MKD{was}{ship}  {stock} {own}{seller:16}  {(r.get('title') or '')[:80]}  {r['url']}")
        if "warranty" in r:
            print(f"        warranty {r.get('warranty')} | Шифра {r.get('sku')} | EAN {r.get('ean')} | "
                  f"{r.get('stock_note')} | delivery {r.get('delivery_estimate')} | {r.get('category')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>6}" if r.get("count") is not None else "     -"
        print(f"{r['id'] or '-':>8} {cnt}  {r['path']}  [{r['slug'] or '-'}]")


def print_facets(recs):
    for r in recs:
        mark = "*" if r.get("site_filter") else " "
        print(f"{r['count']:>6} {mark} {r['name']}: {r['value']}   --filter '{r['token']}'")


def emit(recs, path, printer=print_products, what="listings"):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        print(len(recs))
        log(f"{len(recs)} {what} -> {path}")
    else:
        printer(recs)
        sys.stdout.flush()
        log(f"-- {len(recs)} {what}" + (" (* = Ananas' own shop)" if printer is print_products else
                                         " (* = one of the site's own filter widgets)"
                                         if printer is print_facets else ""))


def info():
    return {"store": STORE, "name": NAME, "base_url": BASE, "capabilities": CAPABILITIES,
            "sells": SELLS, "notes": NOTES}


def main(argv=None):
    global QUIET
    for _stream in (sys.stdout, sys.stderr):   # UTF-8 output on every platform, even when piped
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH, print only the count")
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    ap = argparse.ArgumentParser(description="Ananas.mk marketplace catalogue client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--strict", action="store_true",
                   help="no results rather than Algolia's drop-words fallback when nothing matches every word")
    p.add_argument("--group-variants", action="store_true",
                   help="one hit per seller product group, as the site shows (default: every variant listing)")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep", help="regex over name, path, slug, Latin transliteration, English words")
    p = sub.add_parser("list", parents=[common])
    p.add_argument("category", help="id, slug path, URL, 'A > B > C' path or unique name")
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", action="append", default=[], metavar="TOKEN",
                   help="token from `facets` (repeatable, AND; '||' ORs values inside one token)")
    p.add_argument("--check-site", action="store_true",
                   help="also fetch the category page and compare its displayed total")
    p.add_argument("--group-variants", action="store_true",
                   help="one hit per seller product group, as the site shows (default: every variant listing)")
    p = sub.add_parser("detail", parents=[common])
    p.add_argument("refs", nargs="+", metavar="URL-OR-ID")
    p = sub.add_parser("facets", parents=[common])
    p.add_argument("category")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    QUIET = a.quiet
    if a.cmd == "info":
        print(json.dumps(info(), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    c = Ananas(verbose=a.verbose)
    code = 0
    try:
        if a.cmd == "search":
            emit(c.search(a.query, limit=a.limit, in_stock=a.in_stock, strict=a.strict,
                          group_variants=a.group_variants), a.json)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = c.categories(grep=a.grep)
            if a.grep and not recs:
                warn(f"no category matches {a.grep!r} (matched against name, path, slug, a Latin "
                     f"transliteration and English department words)")
            emit(recs, a.json, print_categories, "categories")
        elif a.cmd == "list":
            emit(c.list_category(a.category, in_stock=a.in_stock, limit=a.limit, filters=a.filter,
                                 check_site=a.check_site, group_variants=a.group_variants), a.json)
        elif a.cmd == "facets":
            emit(c.facets(a.category), a.json, print_facets, "facet values")
        elif a.cmd == "detail":
            try:
                recs, code = c.detail(a.refs)
            except _BatchBlocked as b:
                emit(b.recs, a.json)
                raise b.err
            emit(recs, a.json)
        log(f"({c.requests_made} requests)")
        return code
    except Blocked as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 3
    except (NotFound, Usage) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (StoreError, requests.RequestException, ValueError, KeyError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
