#!/usr/bin/env python3
"""Setra (https://setra.mk) catalogue client. See references/client-contract.md and
references/setra.md.

Setra is WordPress + WooCommerce (Porto theme) on a bare Apache origin: no CDN, Wordfence
installed. Product data comes from the anonymous WooCommerce Store API:

    GET /wp-json/wc/store/v1/products?category=<ids>&search=<s>&include=<ids>
                                      &stock_status=instock&per_page=100&page=N&orderby=id&order=asc
    GET /wp-json/wc/store/v1/products/<id|slug>
    GET /wp-json/wc/store/v1/products/categories?per_page=100
    GET /wp-json/wc/store/v1/products/collection-data?category=<id>&calculate_price_range=true
                                      &calculate_stock_status_counts=true
    GET /wp-json/wp/v2/pages?slug=pravila-i-uslovi-na-prodazhba    (delivery terms, once per detail run)

The Store API has no brand, attribute or EAN fields. Structured filters live in the HUSKY/WOOF
filter widget on the server-rendered category pages (custom taxonomies such as Brend =
manufacturer, Display, Osvezuvanje = refresh rate, cpu, ram, Memory), so:
  * facets  = the widget's terms and counts on /product-category/<path>/ (one HTML request)
  * --filter <tax>=<slug> = the ids on /product-category/<path>/?swoof=1&<tax>=<slug>&count=N
              (HTML, paged), then the records via the Store API (include=<ids>)
  * listing `brand` = the earliest brand name from the shop-wide Brend vocabulary (/shop/ widget)
              found in the title (the shop files brands only in that HTML-only taxonomy)
  * listing `ean`  = a checksum-valid EAN/GTIN printed after an "EAN"/"UPC"/"GS1" label in the
              description (~6% of products, e.g. NZXT, Targus, nJoy); a variant list under one label
              is resolved by the colour the title names, otherwise null

The store's search matches the WHOLE query as one case-insensitive substring of the product
title (no word AND, no descriptions, no EANs): "gaming monitor" 54, "monitor gaming" 0. For a
multi-word query this client searches the rarest word and keeps titles containing every word
(--phrase sends it verbatim). Titles are English, so a Cyrillic word that finds nothing is
retried as a Latin transliteration, and a short result prints matching categories.

Every Store API call costs 2.5-6.5 s server-side (uncached PHP); keep everything sequential.

    setra.py info
    setra.py search "<query>" [--limit N] [--in-stock] [--category REF] [--phrase] [--json PATH]
    setra.py categories [--grep REGEX] [--json PATH]
    setra.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    setra.py detail <url|id|slug> [...] [--json PATH]
    setra.py facets <category> [--json PATH]
    common options: --quiet, -v/--verbose (log every request)

Category refs: a term id ("252"), a slug ("monitors"), a /product-category/.../<slug>/ URL (a
/page/N/ suffix and an /en/ prefix are ignored), an exact category name, or a comma list of
ids/slugs (union). A parent category includes its subcategories.
Filter tokens (from `facets`): brand=<slug|name>[,...] (or Brend=...), <tax>=<slug>[,...] for the
other widget taxonomies (Display=27-2, Osvezuvanje=144,165, cpu=i5, ram=16gb ...),
price=MIN-MAX (MKD, either end optional), stock=in|out|backorder, cat=<id|slug>.
Commas OR inside a token; several --filter flags AND.

Exit codes: 0 ok (incl. a genuine zero-hit search), 1 unexpected error,
2 bad usage / unknown category or product, 3 blocked (stderr line starting "BLOCKED:").
`detail` emits one record per input ({"input", "error"} for failures) and exits 0 when at least
one input resolved, 2 when none did.
"""

import argparse
import html as htmlmod
import json
import re
import sys
import time
import unicodedata
from urllib.parse import unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "setra"
NAME = "Setra"
BASE = "https://setra.mk"
API = BASE + "/wp-json/wc/store/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PER_PAGE = 100           # Store API maximum (101 -> HTTP 400 rest_invalid_param)
HTML_PAGE_SIZE = 100     # ?count=N on category pages (the theme honours values beyond its 12/24/36 menu)
PACE_S = 0.5             # pause between consecutive requests; each call already costs 2.5-6.5 s
MAX_RETRIES = 4
TIMEOUT_S = 90           # uncached PHP under load can take 40-60 s before answering
COUNT_WORDS_MAX = 4      # multi-word search: count at most this many words to pick the anchor
HINT_BELOW = 5           # search: print matching categories when fewer hits than this
# Trimmed listing objects (half the bytes; same server time). The description stays in because
# it is where the few EANs live.
LIST_FIELDS = ("id,name,slug,sku,permalink,prices,is_in_stock,is_on_backorder,is_purchasable,"
               "low_stock_remaining,stock_availability,categories,short_description,description,"
               "add_to_cart")
TERMS_SLUG = "pravila-i-uslovi-na-prodazhba"
SHOP_PATH = "/shop/"     # its filter widget lists every Brend (manufacturer) term with counts

SELLS = ("IT, gaming and small-electronics shop (~1,630 products, mostly English titles): "
         "peripherals (keyboards, mice, headsets, speakers, webcams, microphones, mouse pads, "
         "controllers, bags), gaming gear (gaming monitors, chairs, desks), PowerCube prebuilt "
         "gaming/office PCs and desktops, PC components (cases, air and water coolers, PSUs, "
         "fans; very few GPUs, CPUs or motherboards), be quiet! and NZXT ranges, monitors, "
         "laptops (Dell, Lenovo, HP, Gigabyte), tablets, printers with toners and cartridges, "
         "networking (routers, switches, extenders, adapters), UPS and stabilisers, video "
         "surveillance (IMOU/Xiaomi cameras), inverter air conditioners, small home appliances "
         "and air purifiers (mostly Xiaomi), e-scooters, smartwatches, storage (SSD, USB, "
         "memory cards), projectors, solar inverters, figurines. Essentially no phones (1 "
         "feature phone), no TVs (TV boxes only), no large appliances.")
NOTES = ("price_mkd = the current WooCommerce price (Store API minor units / 100), incl. VAT, the "
         "same for every payment method; strike-through prices are almost never used. Stock is "
         "one yes/no web flag (no quantities, no per-store split); 'Достапно по нарачка' "
         "(backorder: orderable, supplier order, no lead time) is in_stock=null. Search = the "
         "whole query as ONE substring of the title on the server; this client emulates "
         "word-AND (rarest word + client-side check) and retries Cyrillic words as Latin, but "
         "titles rarely name the product type (laptops are not called 'laptop'): use categories "
         "+ list for departments. brand is derived from the title with the shop's own brand "
         "list; ean only where the description prints one (~6%). Facets/--filter come from the "
         "category page filter widget (brand, display size, refresh rate, CPU, RAM, storage). "
         "Warranty from the short description (83% of products; 6-60 months, inconsistent). "
         "Delivery: max 4 working days after order confirmation, from 150 MKD by courier "
         "(terms page). Every request costs 2.5-6.5 s: sequential only; Wordfence installed.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing",
                "warranty", "delivery_estimate"]

# Markers that only appear on block / challenge pages. Plain "captcha"/"turnstile" are NOT used:
# the shop's normal HTML carries form-plugin strings such as "Cloudflare Turnstile verification".
BLOCK_MARKERS = (
    "generated by wordfence", "your access to this site has been limited",
    "access to this service has been temporarily limited", "wordfence-blocked",
    "just a moment...", "cf-chl", "challenge-platform", "checking your browser",
    "attention required! | cloudflare", "ddos-guard", "are you a robot",
)
# Extra markers that count only when a JSON endpoint answered with HTML.
JSON_HTML_MARKERS = ("captcha", "turnstile", "access denied", "forbidden")

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh",
           # Serbian / Russian / Bulgarian letters people may type
           "ђ": "dj", "ћ": "c", "й": "j", "я": "ja", "ю": "ju", "ы": "y", "э": "e", "щ": "sht",
           "ъ": "", "ь": "", "ё": "jo", "і": "i", "ї": "ji", "є": "je"}
# The shop's slugs use the simple forms (Гејминг маси -> gejming-masi, Видео Надзор -> video-nadzor).
_MK_LAT_SIMPLE = dict(_MK_LAT, **{"ж": "z", "ч": "c", "ш": "s", "ќ": "k", "ѓ": "g", "џ": "dz"})

# English words added to a category's --grep haystack when its (Macedonian) path contains the stem,
# so `categories --grep laptop` finds Лаптопи. Lowercase stems.
_EN_TAGS = {
    "галантерија": "peripherals accessories", "wifi камери": "wifi camera ip camera security",
    "веб камери": "webcam web camera", "глувч": "mouse mice", "звучниц": "speakers audio",
    "контролер": "controller gamepad joystick", "ладилници": "cooler cooling fan fans",
    "микрофон": "microphone mic", "подлога за глушец": "mouse pad mousepad",
    "подлоги за глушец": "mouse pad mousepad", "полначи за лаптоп": "laptop charger power adapter",
    "преносни полначи": "power bank powerbank", "слушалки": "headphones headset earphones audio",
    "тастатури": "keyboard keyboards", "тастатури + глувчиња": "keyboard mouse combo set",
    "торби": "bag bags backpack", "гејминг": "gaming", "компјутери": "computer pc desktop",
    "куќишта": "case cases chassis pc case", "лаптопи": "laptop laptops notebook",
    "маси": "desk desks table", "монитори": "monitor monitors display screen",
    "столици": "chair chairs seat", "скутери": "e-scooter scooter electric",
    "кабли": "cable cables adapter", "канцелариска": "office equipment",
    # the shop misspells its components department "Комјутерски": let `--grep компјутер` find it
    "комјутерски": "компјутерски компоненти kompjuterski",
    "калкулатори": "calculator", "клима": "air conditioner ac aircon climate split",
    "инвертери": "inverter air conditioner", "компјутерски системи": "computer pc desktop system prebuilt",
    "десктоп": "desktop computer pc", "компоненти": "pc components computer parts hardware",
    "водени ладилници": "water cooling aio liquid cooler", "графички": "graphics card gpu video card",
    "матични": "motherboard mainboard", "напојувања": "power supply psu",
    "процесори": "cpu processor", "мали апарати": "small appliances home appliances kitchen",
    "мемориски уреди": "storage memory", "ssd": "ssd solid state drive storage",
    "екстерни дискови": "external hard drive hdd portable storage",
    "кутии и адаптери за дискови": "drive enclosure adapter rack",
    "мемориски картички": "memory card sd microsd", "усб драјвови": "usb flash drive stick pendrive",
    "мобилни телефони": "mobile phone smartphone cell phone", "мрежна опрема": "network networking",
    "bluetooth": "bluetooth", "екстендери": "range extender wifi repeater",
    "мрежни адаптери": "network adapter wifi adapter lan card", "рутери": "router wifi mesh",
    "свичеви": "switch network switch", "навигации": "gps navigation sat nav",
    "оптички уреди": "optical drive dvd", "паметни часовници": "smartwatch smart watch wearable",
    "печатачи": "printer printers", "проектори": "projector projectors beamer",
    "прочистувачи на воздух": "air purifier", "соларни": "solar inverter photovoltaic pv",
    "софтвер": "software", "тв бокс": "tv box stick streaming", "таблети": "tablet tablets",
    "тонери": "toner ink cartridge consumables", "фигури": "figure figurine collectible",
    "фиксни телефони": "landline phone telephone", "ups": "ups power backup stabilizer avr",
    "видео надзор": "video surveillance cctv security camera nvr dvr",
    "powercube": "prebuilt pc gaming computer desktop",
}

# A barcode label ("EAN", "EAN-13", "GTIN", "UPC", nJoy's "GS1", ...). The codes after it are read
# as a list, because vendor tables print one label for several variants:
#   "EAN: 5060301699902 (White) / 5060301699889 (Black)", "EAN Code Black: 4718...143Black/Cyan: 4718...150",
#   "EAN 5056547207971 5056547207988 5056547203393 5056547203409" (4 SKUs, no tags).
EAN_LABEL_RX = re.compile(r"(?i)(?<![\w-])(EAN(?:-?13)?|GTIN(?:-?13)?|UPC(?:-?A)?|GS1|barcode|баркод)(?![\w-])")
_EAN_HEAD_RX = re.compile(r"(?i)\s*(?:code\b)?\s*(?:\(\s*([^()]{1,20}?)\s*\))?\s*[:#]?")
_EAN_ITEM_RX = re.compile(
    r"(?i)\s*(?:[/,;|&]|\band\b)?\s*"
    r"(?:(?!(?:EAN|GTIN|UPC|GS1|barcode|PN|MPN|SKU|model)\b)([A-Za-z][A-Za-z ./-]{0,24}?)\s*:\s*)?"   # "Black:" tag
    r"(\d{8,14})(?!\d)"
    r"(?:\s*\(\s*([^()]{1,20}?)\s*\))?")                                                          # "(White)" tag
# Spec tables on monitors, laptops, PCs and UPSs: "Бренд: Dell Модел: P2723DE PN: P2723DE Тип на производ: ..."
MPN_RX = re.compile(r"(?i)(?<![\w/])(?:PN|P/N|MPN|Part\s*(?:Number|No\.?))\s*[:#]?\s*(\S{0,41})")
SPEC_BRAND_RX = re.compile(r"Бренд\s*:\s*(.{1,40}?)\s+(?:Модел|PN|Тип)\b|\bBrand\s*:?\s*([A-Z][\w!.&-]+)")
WARRANTY_RX = re.compile(r"(?i)(?:гаран[а-шѓќљњџѕј]*|garanc\w*)\s*:?\s*([^\n]{1,80})")


class Blocked(RuntimeError):
    """Response is a bot challenge / WAF block / login wall rather than data (exit 3)."""


class NotFound(RuntimeError):
    """Unknown category or product (exit 2)."""


class Usage(RuntimeError):
    """Bad usage (exit 2)."""


QUIET = False


def log(*a):
    if not QUIET:
        print(*a, file=sys.stderr)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def unescape(text):
    return collapse(htmlmod.unescape(text or ""))


def html_text(fragment, block_sep=" "):
    """HTML fragment -> plain text. Block-level tags become `block_sep`, inline tags vanish;
    WPBakery [vc_*] shortcodes that leak into descriptions are dropped."""
    if not fragment:
        return ""
    t = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d|/tr|/td|/ul|/ol|p|li|tr|h\d)\b[^>]*>", block_sep, fragment)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\[/?vc_[^\]]*\]", " ", t)
    return htmlmod.unescape(t).replace("\xa0", " ")


def translit(text, simple=False):
    table = _MK_LAT_SIMPLE if simple else _MK_LAT
    return "".join(table.get(ch, ch) for ch in (text or "").lower())


def has_cyrillic(text):
    return bool(re.search(r"[Ѐ-ӿ]", text or ""))


def fold(text):
    """Case- and accent-insensitive form for client-side substring matching (the server's MySQL
    _ci collation behaves the same way)."""
    t = unicodedata.normalize("NFKD", htmlmod.unescape(text or "").casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return (t.replace("″", '"').replace("“", '"').replace("”", '"').replace("’", "'")
            .replace("–", "-").replace("—", "-"))


def en_tags(text):
    low = (text or "").lower()
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def minor_to_mkd(amount, minor_unit):
    """Store API money is a string in minor units: '279000' with minor_unit 2 -> 2790."""
    if amount in (None, ""):
        return None
    return int(round(int(amount) / (10 ** int(minor_unit or 0))))


def gtin_ok(code):
    d = [int(c) for c in code]
    if len(d) not in (8, 12, 13, 14):
        return False
    body = d[:-1][::-1]
    return (10 - sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(body)) % 10) % 10 == d[-1]


def _barcodes(text):
    """-> [(kind 'EAN'|'UPC', code, variant tag or None)] for every checksum-valid code listed
    after a barcode label."""
    out = []
    for m in EAN_LABEL_RX.finditer(text):
        kind = "UPC" if m.group(1).upper().startswith("UPC") else "EAN"
        h = _EAN_HEAD_RX.match(text, m.end())
        label_tag, i, style = h.group(1), h.end(), None
        while True:
            it = _EAN_ITEM_RX.match(text, i)
            if not it:
                break
            # every code of one list is tagged the same way ("Black: X" vs "X (Black)" vs bare), so a
            # following "Weight: 12345678" is not read as another variant
            st = "pre" if it.group(1) else ("post" if it.group(3) else "bare")
            if style is not None and st != style and not (style == "post" and st == "bare"):
                break
            style = style or st
            tag = it.group(1) or it.group(3) or label_tag
            if gtin_ok(it.group(2)):
                out.append((kind, it.group(2), collapse(tag) if tag else None))
            i = it.end()
    return out


def eans_in(p):
    """-> (ean or None, all candidates). EANs are only printed in some vendor descriptions
    ("EAN: 5056547202341 (White)", "UPC 8100... EAN 5056...", nJoy "GS1 5949..."). One distinct
    checksum-valid EAN wins; a UPC is used only when no EAN is printed. A variant list under one
    label ("EAN: X (White) / Y (Black)") is resolved by the variant the title names (longest tag
    wins, so "Black/Cyan" beats "Black"); untagged or unresolvable lists -> None. The shop copies
    descriptions between variants, so the FIRST code is often another colour's."""
    # every tag becomes a space: spec tables put the label and the number in adjacent <span>s
    found = _barcodes(_plain(p))
    eans = list(dict.fromkeys(v for k, v, _ in found if k == "EAN"))
    upcs = list(dict.fromkeys(v for k, v, _ in found if k == "UPC"))
    kind = "EAN" if eans else "UPC"
    pick = eans if eans else upcs
    if len(pick) == 1:
        return pick[0], eans + upcs
    if pick:
        title = fold(p.get("name"))
        named = {}
        for k, v, tag in found:
            if k == kind and tag and re.search(r"(?<![^\W_])" + re.escape(fold(tag)) + r"(?![^\W_])", title):
                named.setdefault(v, len(tag))
        if named:
            best = max(named.values())
            top = [v for v, n in named.items() if n == best]
            if len(top) == 1:
                return top[0], eans + upcs
    return None, eans + upcs


def _plain(p):
    raw = (p.get("description") or "") + " " + (p.get("short_description") or "")
    return collapse(htmlmod.unescape(re.sub(r"<[^>]+>", " ", raw)).replace("\xa0", " "))


def mpn_of(p):
    """Manufacturer part number from the FIRST 'PN:' / 'Part Number' label of the spec table, or None.
    Only the first label counts: PC bundles leave their own PN empty and then embed the gift
    monitor's spec table, whose PN would be wrong."""
    m = MPN_RX.search(_plain(p))
    if not m:
        return None
    v = m.group(1).rstrip(".,;")
    return v if re.fullmatch(r"[A-Za-z0-9][\w./+-]{2,40}", v) and re.search(r"\d", v) else None


def spec_brand(p):
    m = SPEC_BRAND_RX.search(_plain(p))
    return collapse(m.group(1) or m.group(2)) if m else None


def warranty_of(p):
    """-> (raw text or None, months or None). The shop types it into the short description as
    'Гаранција: 24 месеци' (also 'Гаранација', '360 дена', '5 години', '23M', '24месеци')."""
    for field in ("short_description", "description"):
        text = html_text(p.get(field), "\n")
        m = WARRANTY_RX.search(text)
        if m:
            raw = collapse(m.group(1)).rstrip(".")
            if not re.search(r"\d", raw):
                continue
            months = None
            mm = re.match(r"(\d+)\s*(месец\w*|мес\.?|m\b|months?|дена|ден|days?|години|година|год\.?|years?)",
                          raw, re.I)
            if mm:
                n, unit = int(mm.group(1)), mm.group(2).lower()
                if unit.startswith(("д", "day")):
                    months = round(n / 30)
                elif unit.startswith(("г", "year")):
                    months = n * 12
                else:
                    months = n
            return raw, months
    return None, None


def _title_of(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.S | re.I)
    return collapse(htmlmod.unescape(m.group(1)))[:80] if m else None


# --------------------------------------------------------------------------- client

class Setra:
    def __init__(self, pace=PACE_S, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "mk,en;q=0.8",
        })
        self.pace = pace
        self.verbose = verbose
        self.requests_made = 0
        self._last = 0.0
        self._cats = None
        self._brands = None        # [(name, slug)] from the shop-wide Brend widget
        self._brand_rx = None
        self._widget_blocks = []
        self._terms = False        # False = not fetched yet; None = fetched, nothing found

    # ------------------------------------------------------------------ http
    def _get(self, url, params=None, allow_404=False, html=False):
        """GET -> (parsed JSON or HTML text, response). Retries 429/5xx with backoff; HTTP 500 is
        retried too, because the PHP backend answers 500 after 40-60 s when it is overloaded."""
        if not url.startswith("http"):
            url = API + url
        qs = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], safe=",")
        full = url + ("?" + qs if qs else "")
        r = None
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(full, timeout=TIMEOUT_S,
                               headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"} if html else None)
            except requests.RequestException as e:
                self._last = time.monotonic()
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"GET {full} failed: {e}") from e
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                print(f"[setra] GET {r.url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s "
                      f"{len(r.content)} B", file=sys.stderr)
            self._check_block(r, html)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    if r.status_code == 429:
                        raise Blocked(f"HTTP 429 (rate limited) {r.url} after {MAX_RETRIES} attempts, "
                                      f"retry-after={r.headers.get('Retry-After')!r}")
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** (attempt + 1)
                log(f"  HTTP {r.status_code}, retrying in {min(delay, 60)}s")
                time.sleep(min(delay, 60))
                continue
            if r.status_code == 404 and allow_404:
                return None, r
            ctype = r.headers.get("content-type", "")
            if html:
                if r.status_code != 200 or "html" not in ctype:
                    raise RuntimeError(f"GET {r.url}: expected an HTML page, got HTTP {r.status_code} "
                                       f"({ctype})")
                return r.text, r
            if r.status_code != 200 or "json" not in ctype:
                raise RuntimeError(f"GET {r.url}: expected JSON, got HTTP {r.status_code} ({ctype}); "
                                   f"body starts {r.text[:160]!r}")
            try:
                return r.json(), r
            except ValueError as e:
                raise RuntimeError(f"GET {r.url}: invalid JSON: {r.text[:160]!r}") from e
        raise RuntimeError(f"GET {full}: still HTTP {r.status_code if r is not None else '?'} "
                           f"after {MAX_RETRIES} attempts (server overloaded?)")

    @staticmethod
    def _check_block(r, want_html):
        ctype = r.headers.get("content-type", "")
        is_html = "html" in ctype
        body = r.text[:30000].lower() if (is_html or r.status_code in (401, 403, 429, 503)) else ""
        marker = next((m for m in BLOCK_MARKERS if m in body), None)
        if not marker and is_html and not want_html:
            marker = next((m for m in JSON_HTML_MARKERS if m in body), None)
        url = r.url if len(r.url) <= 160 else r.url[:157] + "..."
        evidence = (f"HTTP {r.status_code} {url} server={r.headers.get('Server')!r} "
                    f"title={_title_of(r.text[:30000]) if body else None!r}"
                    + (f" marker={marker!r}" if marker else "")
                    + (f" retry-after={r.headers.get('Retry-After')!r}" if r.headers.get("Retry-After") else ""))
        if r.status_code in (401, 403):
            raise Blocked(evidence)
        if marker:
            # A Wordfence lockout escalates if hammered: never retry it.
            raise Blocked(evidence)

    # ------------------------------------------------------------- listing
    def walk(self, limit=None, fields=LIST_FIELDS, label="", **params):
        """Walk every page of /products -> (products, meta). Verifies against X-WP-Total."""
        base = {k: v for k, v in params.items() if v is not None}
        base.setdefault("orderby", "id")
        base.setdefault("order", "asc")
        base["per_page"] = min(PER_PAGE, limit) if limit else PER_PAGE
        if fields:
            base["_fields"] = fields
        out, seen, page, meta = [], set(), 1, {}
        while True:
            data, r = self._get("/products", params=dict(base, page=page))
            if not isinstance(data, list):
                raise RuntimeError(f"/products returned {type(data).__name__}, expected a list")
            if page == 1:
                meta = {"total": int(r.headers.get("X-WP-Total", len(data)) or 0),
                        "pages": int(r.headers.get("X-WP-TotalPages", 1) or 0)}
                if limit and meta["total"] > len(data):
                    log(f"  {label or 'products'}: {meta['total']} match; fetching the first {limit}")
                elif meta["pages"] > 1:
                    log(f"  {label or 'products'}: {meta['total']} over {meta['pages']} page(s) "
                        f"(~5 s each)")
            for p in data:
                if p.get("id") not in seen:
                    seen.add(p.get("id"))
                    out.append(p)
            if limit and len(out) >= limit:
                meta["pagesFetched"] = page
                return out[:limit], meta
            if page >= meta["pages"] or not data:
                break
            page += 1
        meta["pagesFetched"] = page
        if len(out) != meta["total"]:
            warn(f"collected {len(out)} unique products but the site reports X-WP-Total={meta['total']}")
        return out, meta

    def count(self, **params):
        """Cheap hit count (X-WP-Total of a 1-row page)."""
        _, r = self._get("/products", params=dict({k: v for k, v in params.items() if v is not None},
                                                  _fields="id", per_page=1))
        return int(r.headers.get("X-WP-Total", 0) or 0)

    def by_ids(self, ids, fields=LIST_FIELDS, label="products"):
        """Products for a list of ids (include=..., 100 per request) -> {id: product}."""
        found, ids = {}, list(dict.fromkeys(ids))
        for i in range(0, len(ids), PER_PAGE):
            chunk = ids[i:i + PER_PAGE]
            got, _ = self.walk(fields=fields, label=label, include=",".join(map(str, chunk)))
            found.update({p["id"]: p for p in got})
        return found

    # ------------------------------------------------------------------ brands
    def brands(self):
        """Shop-wide manufacturer vocabulary [(name, slug)] from the Brend filter widget on /shop/.
        Failure (other than a block) only costs the brand field."""
        if self._brands is None:
            self._brands = []
            try:
                text, _ = self._get(BASE + SHOP_PATH, html=True)
                for blk in parse_widget(text):
                    if blk["tax"].lower() == "brend":
                        self._brands = [(t["name"], t["slug"]) for t in blk["terms"] if t["name"]]
                if not self._brands:
                    log("  note: no brand list on /shop/ (layout change?); brand will be null")
            except (RuntimeError, requests.RequestException) as e:
                if isinstance(e, Blocked):
                    raise
                log(f"  note: brand list unavailable ({e}); brand will be null")
            pats = []
            for name, _ in sorted(self._brands, key=lambda b: -len(b[0])):
                pat = re.escape(name).replace(r"\ ", r"[\s-]*").replace(r"\-", r"[\s-]?")
                pats.append(pat)
            self._brand_rx = (re.compile(r"(?<![\w])(" + "|".join(pats) + r")(?![\w])", re.I)
                              if pats else None)
        return self._brands

    def brand_of(self, title):
        """Earliest brand from the shop's own brand list that the title names, as the shop spells it."""
        self.brands()
        if not self._brand_rx:
            return None
        title = title or ""
        genuine = re.search(r"(?i)\b(genuine|original|оригинал)", title)
        m = None
        for hit in self._brand_rx.finditer(title):
            # "Toner CF217A Compatible for HP 17A MS": HP is the printer it fits, not the maker
            if not genuine and re.search(r"(?i)(?:\bcompatible\s+(?:for|with)|\bfor|\bза)\s*$",
                                         title[:hit.start()]):
                continue
            m = hit
            break
        if not m:
            return None
        hit = fold(re.sub(r"[\s-]+", "", m.group(1)))
        for name, _ in self._brands:
            if fold(re.sub(r"[\s-]+", "", name)) == hit:
                return name
        return m.group(1)

    # -------------------------------------------------------------- records
    def to_record(self, p):
        pr = p.get("prices") or {}
        if pr.get("currency_code") not in (None, "MKD"):
            # WOOCS (currency switcher) is installed; never label EUR as MKD.
            raise RuntimeError(f"product {p.get('id')}: price in {pr.get('currency_code')!r}, expected MKD")
        minor = pr.get("currency_minor_unit", 2)
        price = minor_to_mkd(pr.get("price"), minor)
        regular = minor_to_mkd(pr.get("regular_price"), minor)
        if regular is None or price is None or regular <= price:
            regular = None   # only a real strike-through price is reported
        avail = collapse(html_text((p.get("stock_availability") or {}).get("text")))
        if p.get("is_on_backorder"):
            in_stock = None   # "Достапно по нарачка": orderable, but a supplier order with no lead time
            note = avail or "Достапно по нарачка"
            note += " (backorder: orderable, supplier order, no lead time given)"
        elif p.get("is_in_stock") is True:
            in_stock, note = True, avail or "in stock (no quantity shown)"
        elif p.get("is_in_stock") is False:
            in_stock, note = False, avail or "Нема на залиха"
        else:
            in_stock, note = None, avail or None
        if p.get("low_stock_remaining") is not None:
            note = f"{note}; only {p['low_stock_remaining']} left"
        if p.get("is_purchasable") is False:
            note = f"{note}; not purchasable online"
        title = unescape(p.get("name"))
        ean, _ = eans_in(p)
        rec = {
            "store": STORE,
            "id": str(p["id"]),
            "sku": collapse(p.get("sku")) or None,
            "title": title,
            "url": p.get("permalink") or f"{BASE}/?p={p['id']}",
            "brand": self.brand_of(title) or spec_brand(p),
            "price_mkd": price,
            "regular_price_mkd": regular,
            "in_stock": in_stock,
            "stock_note": note,
            "category": self.category_path(p),
            "ean": ean,
        }
        mpn = mpn_of(p)
        if mpn:
            rec["mpn"] = mpn
        return rec

    # ------------------------------------------------------------ categories
    def _categories(self):
        if self._cats is None:
            cats, page = [], 1
            while True:
                data, r = self._get("/products/categories", params={"per_page": 100, "page": page})
                if not isinstance(data, list) or any(not isinstance(c, dict) or "id" not in c for c in data):
                    raise RuntimeError(f"unexpected /products/categories payload: {str(data)[:160]}")
                cats += data
                if page >= int(r.headers.get("X-WP-TotalPages", 1) or 1) or not data:
                    break
                page += 1
            if not cats:
                raise RuntimeError("/products/categories returned no categories (soft block or change?)")
            byid = {c["id"]: c for c in cats}
            for c in cats:
                chain, cur, guard = [], c, 0
                while cur is not None and guard < 10:
                    chain.append(unescape(cur["name"]))
                    cur = byid.get(cur.get("parent") or 0)
                    guard += 1
                c["_path"] = " > ".join(reversed(chain))
                c["_name"] = unescape(c["name"])
            self._cats = byid
        return self._cats

    def category_path(self, p):
        """Leaf category paths of a product, joined with ' | '."""
        try:
            cats = self._categories()
        except (RuntimeError, requests.RequestException) as e:
            if isinstance(e, Blocked):
                raise
            cats = {}
        ids = [c["id"] for c in p.get("categories") or []]
        parents = {cats[i].get("parent") for i in ids if i in cats}
        leaves = [i for i in ids if i not in parents] or ids
        names = {c["id"]: unescape(c.get("name")) for c in p.get("categories") or []}
        paths = [cats[i]["_path"] if i in cats else names.get(i, str(i)) for i in leaves]
        return " | ".join(dict.fromkeys(paths)) or None

    @staticmethod
    def _cat_record(c):
        return {"id": str(c["id"]), "slug": unquote(c["slug"]), "name": c["_name"], "path": c["_path"],
                "url": c.get("permalink") or f"{BASE}/product-category/{c['slug']}/",
                "parent": str(c["parent"]) if c.get("parent") else None, "count": c.get("count")}

    @staticmethod
    def _cat_haystack(c):
        name, path = c["_name"], c["_path"]
        return (name, path, unquote(c["slug"]), translit(name), translit(name, simple=True),
                translit(path), en_tags(path))

    def categories(self, grep=None):
        cats = sorted(self._categories().values(), key=lambda c: c["_path"].lower())
        if grep:
            rx = re.compile(grep, re.I)
            cats = [c for c in cats if any(rx.search(f) for f in self._cat_haystack(c) if f)]
        return [self._cat_record(c) for c in cats]

    def resolve_category(self, ref):
        """-> list of category ids (a comma list is a union)."""
        ref = unquote((ref or "").strip())
        if not ref:
            raise Usage("empty category")
        cats = self._categories()
        if re.match(r"^(https?://|/|(www\.)?setra\.mk/)", ref, re.I):
            u = urlparse(ref if "://" in ref else ("https://" + ref if ref.lower().startswith(("setra", "www"))
                                                   else BASE + ref))
            if u.netloc and "setra.mk" not in u.netloc:
                raise Usage(f"not a setra.mk URL: {ref}")
            path = re.sub(r"/page/\d+/?$", "/", u.path)
            m = re.search(r"/product-category/(?:[^/]+/)*?([^/]+)/?$", path)
            if not m:
                raise NotFound(f"not a /product-category/.../<slug>/ URL: {ref}")
            parts = [m.group(1)]
        else:
            parts = [x.strip() for x in ref.split(",") if x.strip()]
        by_slug = {unquote(c["slug"]).lower(): c for c in cats.values()}
        by_name = {}
        for c in cats.values():
            by_name.setdefault(c["_name"].casefold(), []).append(c)
        whole = collapse(ref).casefold()
        if len(by_name.get(whole, [])) == 1:      # a name that itself contains a comma
            return [by_name[whole][0]["id"]]
        ids = []
        for x in parts:
            if x.isdigit() and int(x) in cats:
                ids.append(int(x))
            elif x.lower() in by_slug:
                ids.append(by_slug[x.lower()]["id"])
            elif x.casefold() in by_name and len(by_name[x.casefold()]) == 1:
                ids.append(by_name[x.casefold()][0]["id"])
            else:
                rx = re.compile(re.escape(x), re.I)
                near = [c for c in cats.values() if any(rx.search(f) for f in self._cat_haystack(c) if f)]
                hint = ("; did you mean: " + ", ".join(f"{c['id']} {unquote(c['slug'])} ({c['_path']})"
                                                         for c in near[:8])) if near else ""
                raise NotFound(f"unknown category {x!r} (the API would silently return 0 products)"
                               f"{hint}. Run `categories --grep ...`")
        return list(dict.fromkeys(ids))

    def _cat_label(self, ids):
        cats = self._categories()
        return ", ".join(f"{i} {cats[i]['_path']}" for i in ids)

    def _category_hint(self, words):
        """Categories whose name/slug/translit/English tags contain a query word's stem."""
        try:
            cats = self._categories()
        except (RuntimeError, Usage):
            return []
        hits = []
        for c in cats.values():
            hay = " ".join(f for f in self._cat_haystack(c) if f).lower()
            for w in words:
                w = w.lower()
                stem = w[:max(4, len(w) - 2)] if len(w) > 4 else w
                if len(stem) >= 3 and (stem in hay or translit(stem) in hay):
                    hits.append(c)
                    break
        return sorted(hits, key=lambda c: -(c.get("count") or 0))

    # ------------------------------------------------------------------ search
    def search(self, query, limit=None, in_stock=False, category=None, phrase=False):
        q = collapse(query)
        if not q:
            raise Usage("empty search query")
        base = {}
        if category:
            ids = self.resolve_category(category)
            base["category"] = ",".join(map(str, ids))
            log(f"  restricted to category {self._cat_label(ids)}")
        if in_stock:
            base["stock_status"] = "instock"
        if re.fullmatch(r"\d{8,14}", q):
            log("  note: the shop's search matches titles only; EANs (printed in ~6% of descriptions) "
                "cannot be searched here")
        words = [q] if phrase else list(dict.fromkeys(q.split(" ")))
        short = [w for w in words if len(w) <= 2]
        if short and not phrase and len(words) > 1:
            log(f"  note: {', '.join(map(repr, short))}: words of 1-2 characters are matched "
                f"client-side as whole tokens")
        subst = {}
        if len(words) == 1:
            w = words[0]
            prods, meta = self.walk(limit=limit, label=f"search {w!r}", search=w, **base)
            if not prods and has_cyrillic(w) and translit(w) != w.lower():
                log(f"  {w!r}: 0 hits (titles are English); retrying the transliteration {translit(w)!r}")
                subst[w] = translit(w)
                prods, meta = self.walk(limit=limit, label=f"search {translit(w)!r}", search=translit(w), **base)
            log(f"  search {subst.get(w, w)!r}: site reports {meta.get('total')} hit(s) "
                f"(one substring of the title)")
            if not prods and re.search(r"\d", w) and not re.fullmatch(r"\d{8,14}", w):
                log("  note: only titles are searched; a part number printed only in the description "
                    "(the `mpn` field, e.g. Logitech 943-000094) cannot be found. Search the model name "
                    "or `list` the category")
        else:
            counts = {}
            long_words = [w for w in words if len(w) > 2] or words
            to_count = sorted(long_words, key=len, reverse=True)[:COUNT_WORDS_MAX]
            for w in to_count:
                n = self.count(search=w, **base)
                if n == 0 and has_cyrillic(w) and translit(w) != w.lower():
                    n2 = self.count(search=translit(w), **base)
                    log(f"  {w!r}: 0 hits (titles are English); transliteration {translit(w)!r}: {n2}")
                    if n2:
                        subst[w], n = translit(w), n2
                counts[w] = n
                if n == 0:
                    break   # AND of anything with an absent word is empty
            words = [subst.get(w, w) for w in words]
            counts = {subst.get(w, w): n for w, n in counts.items()}
            if min(counts.values()) == 0:
                zero = [w for w, n in counts.items() if n == 0]
                log(f"  no title contains {', '.join(map(repr, zero))}; 0 results")
                prods, meta = [], {"total": 0}
            else:
                anchor = min(counts, key=lambda w: (counts[w], -len(w)))
                got, meta = self.walk(label=f"search {anchor!r}", search=anchor, **base)
                checks = []
                for w in words:
                    if w == anchor:
                        continue
                    if len(w) <= 2 and len(long_words) < len(words):
                        rx = re.compile(r"(?<![^\W_])" + re.escape(fold(w)) + r"(?![^\W_])")
                        checks.append(lambda hay, rx=rx: bool(rx.search(hay)))
                    else:
                        checks.append(lambda hay, o=fold(w): o in hay)
                prods = [p for p in got if all(chk(fold(p.get("name"))) for chk in checks)]
                log(f"  the store matches the whole query as ONE substring of the title; searched the "
                    f"rarest word {anchor!r} ({meta.get('total')} hits; counts "
                    f"{', '.join(f'{w}={n}' for w, n in counts.items())}) and kept {len(prods)} whose "
                    f"title contains every word")
                meta = dict(meta, total=len(prods))   # matches before --limit, for the hint below
                if limit:
                    prods = prods[:limit]
        recs = [self.to_record(p) for p in prods]
        n_hits = max(len(recs), int(meta.get("total") or 0))
        # Costs nothing when records were built (the tree is already loaded for `category`).
        hint = self._category_hint(q.split(" "))
        if hint:
            log(("  few hits: " if n_hits < HINT_BELOW else "  ")
                + "titles often omit the product type; categories matching the query words "
                  "(`list <id>` for the whole department): "
                + "; ".join(f"{c['id']} {unquote(c['slug'])} {c['_path']} ({c.get('count')})"
                            for c in hint[:6]))
        elif n_hits < HINT_BELOW:
            if not recs and has_cyrillic(q):
                log("  titles are English: search with English words or model codes, or use "
                    "`categories --grep`")
        return recs

    # -------------------------------------------------------- filter widget
    def widget(self, cat):
        """Filter-widget blocks of a category page (cached HTML, ~0.5 s)."""
        text, _ = self._get(cat.get("permalink") or f"{BASE}/product-category/{cat['slug']}/", html=True)
        blocks = parse_widget(text)
        if "woof_container" not in text and 'class="products' not in text:
            raise RuntimeError(f"category page {cat.get('permalink')} has neither products nor the filter "
                               f"widget (layout change?)")
        return blocks

    def widget_ids(self, cat, query):
        """Product ids the category page lists for a widget query {tax: [slugs]} (all pages)."""
        link = cat.get("permalink") or f"{BASE}/product-category/{cat['slug']}/"
        params = {"swoof": 1}
        for tax, slugs in query.items():
            params[tax] = ",".join(slugs)
        params["count"] = HTML_PAGE_SIZE
        ids, page, last = [], 1, 1
        while True:
            url = link if page == 1 else f"{link.rstrip('/')}/page/{page}/"
            text, _ = self._get(url, params=params, html=True)
            soup = BeautifulSoup(text, "html.parser")
            # The main loop only; "Препорачани производи" (recommended) is an is-shortcode list.
            lists = [ul for ul in soup.select("ul.products") if "is-shortcode" not in (ul.get("class") or [])]
            if page == 1 and not lists and not soup.select(".woocommerce-info, .woocommerce-no-products-found"):
                raise RuntimeError(f"{url}: neither a product list nor a 'no products' notice on the "
                                   f"filtered page (layout change?)")
            for ul in lists:
                for li in ul.select("li.product"):
                    m = re.search(r"\bpost-(\d+)\b", " ".join(li.get("class") or []))
                    if m:
                        ids.append(int(m.group(1)))
            if page == 1:
                nums = [int(a.get_text(strip=True)) for a in soup.select("a.page-numbers, span.page-numbers")
                        if a.get_text(strip=True).isdigit()]
                last = max(nums) if nums else 1
                if last > 1:
                    log(f"  filtered listing: {last} HTML page(s) of {HTML_PAGE_SIZE}")
            if page >= last:
                break
            page += 1
        return list(dict.fromkeys(ids))

    def parse_filters(self, tokens, cat_ids):
        """-> (widget query {tax: [slugs]}, [(token, predicate(product, record) or
        ("widget", tax, slugs) for a second token on an already-used taxonomy)])."""
        cats = self._categories()
        blocks = None
        query, preds = {}, []
        for tok in tokens:
            if "=" not in tok:
                raise Usage(f"bad --filter {tok!r}: expected KEY=VALUE (see `facets <category>`)")
            k, v = tok.split("=", 1)
            key, vals = fold(k.strip()), [x.strip() for x in v.split(",") if x.strip()]
            if not vals:
                raise Usage(f"bad --filter {tok!r}: empty value")
            if key == "price":
                m = re.fullmatch(r"\s*(\d[\d.,]*)?\s*-\s*(\d[\d.,]*)?\s*", v)
                if not m or not (m.group(1) or m.group(2)):
                    raise Usage(f"bad --filter {tok!r}: expected price=MIN-MAX, price=-MAX or price=MIN-")
                lo = int(re.sub(r"\D", "", m.group(1))) if m.group(1) else None
                hi = int(re.sub(r"\D", "", m.group(2))) if m.group(2) else None
                preds.append((tok, lambda p, r, lo=lo, hi=hi: r["price_mkd"] is not None
                              and (lo is None or r["price_mkd"] >= lo) and (hi is None or r["price_mkd"] <= hi)))
            elif key == "stock":
                want = {x.lower() for x in vals}
                if not want <= {"in", "out", "backorder"}:
                    raise Usage(f"bad --filter {tok!r}: stock=in, stock=out or stock=backorder")
                state = {True: "in", False: "out", None: "backorder"}
                preds.append((tok, lambda p, r, want=want: state[r["in_stock"]] in want))
            elif key in ("cat", "category"):
                ids = set()
                for x in vals:
                    ids.update(self.resolve_category(x))
                # products often carry only their leaf term (all 90 PowerCube PCs lack 15 PowerCube
                # itself), so a parent matches through its descendants, as on the shop's own page
                grew = True
                while grew:
                    more = {c["id"] for c in cats.values() if c.get("parent") in ids} - ids
                    ids |= more
                    grew = bool(more)
                preds.append((tok, lambda p, r, ids=ids: any(c["id"] in ids for c in p.get("categories") or [])))
            else:
                if len(cat_ids) != 1:
                    raise Usage(f"--filter {tok!r}: widget filters work on one category at a time "
                                f"(the shop's filter is per category page); give a single category")
                if blocks is None:
                    blocks = self.widget(cats[cat_ids[0]])
                    self._widget_blocks = blocks
                aliases = {}
                for b in blocks:
                    for al in (b["tax"], b["label"], translit(b["label"]), translit(b["label"], True)):
                        aliases[fold(al)] = b
                    if b["tax"].lower() == "brend":
                        for al in ("brand", "brend", "manufacturer", "proizvoditel", "производител"):
                            aliases[al] = b
                b = aliases.get(key)
                if b is None:
                    raise Usage(f"--filter {tok!r}: unknown key {k!r}; keys on this category page: "
                                + ", ".join(sorted({"brand" if x["tax"].lower() == "brend" else x["tax"]
                                                    for x in blocks} | {"price", "stock", "cat"})))
                ok = []
                for x in vals:
                    fx = fold(x)
                    t = next((t for t in b["terms"] if fx in (fold(t["slug"]), str(t["id"]), fold(t["name"]),
                                                              fold(t["name"]).replace('"', ""),
                                                              fold(translit(t["name"])))), None)
                    if t is None:
                        warn(f"--filter {tok!r}: unknown value {x!r} ignored")
                    else:
                        ok.append(t["slug"])
                if not ok:
                    raise Usage(f"--filter {tok!r}: unknown value; values of {b['label']} here: "
                                + ", ".join(f"{t['slug']} ({t['name']}, {t['count']})" for t in b["terms"]))
                if b["tax"] not in query:
                    query[b["tax"]] = ok
                else:
                    # a second token on the same taxonomy ANDs: intersect with its own id set
                    preds.append((tok, ("widget", b["tax"], ok)))
        return query, preds

    # ------------------------------------------------------------------ list
    def list_category(self, ref, in_stock=False, limit=None, filters=None):
        ids = self.resolve_category(ref)
        cats = self._categories()
        expected = sum(cats[i].get("count") or 0 for i in ids)
        query, preds = self.parse_filters(filters or [], ids) if filters else ({}, [])
        extra_widget = [p for p in preds if isinstance(p[1], tuple)]
        preds = [p for p in preds if not isinstance(p[1], tuple)]
        if query:
            cat = cats[ids[0]]
            wid = self.widget_ids(cat, query)
            shown = "&".join(t + "=" + ",".join(s) for t, s in query.items())
            log(f"  category {self._cat_label(ids)} filtered by the shop's widget {shown}: "
                f"{len(wid)} product id(s)")
            if not wid and len(query) == 1:
                tax, slugs = next(iter(query.items()))
                want = sum(t["count"] or 0 for b in self._widget_blocks if b["tax"] == tax
                           for t in b["terms"] if t["slug"] in slugs)
                if want:
                    warn(f"the filter widget promised {want} product(s) for {shown} but the filtered page "
                         f"listed none (soft block or layout change?)")
                    raise NotFound(f"category {ref!r} with {shown}: the store returned no products")
            for tok, (_, tax, slugs) in extra_widget:
                more = set(self.widget_ids(cat, {tax: slugs}))
                wid = [i for i in wid if i in more]
                log(f"  AND {tok}: {len(wid)} left")
            if limit and not preds and not in_stock:
                wid = sorted(wid)[:limit]   # nothing left to filter client-side: fetch only what is shown
            found = self.by_ids(wid, label="filtered products") if wid else {}
            missing = [i for i in wid if i not in found]
            if missing:
                warn(f"{len(missing)} id(s) listed on the category page are not served by the Store API: "
                     f"{missing[:10]}")
            prods = [found[i] for i in sorted(found)]
        else:
            params = {"category": ",".join(map(str, ids))}
            server_stock = in_stock and not preds
            if server_stock:
                params["stock_status"] = "instock"
            prods, meta = self.walk(limit=None if preds else limit, label=f"category {self._cat_label(ids)}",
                                    **params)
            kids = any(c.get("parent") in ids for c in cats.values())
            log(f"  category {self._cat_label(ids)}: X-WP-Total {meta.get('total')} (category count "
                f"{'+'.join(str(cats[i].get('count')) for i in ids)}"
                f"{'; includes its subcategories' if kids else ''}), "
                f"fetched {len(prods)} over {meta.get('pagesFetched')} page(s)"
                f"{' [in stock, server-side]' if server_stock else ''}")
            if not prods:
                if server_stock and expected:
                    log(f"  none of the {expected} product(s) in this category is in stock")
                    return []
                warn(f"category {self._cat_label(ids)} returned 0 products (category count {expected}); "
                     f"soft block or catalogue change?")
                raise NotFound(f"category {ref!r}: the store returned no products")
        recs = [self.to_record(p) for p in prods]
        brand_tax = next((t for t in query if t.lower() == "brend"), None)
        if brand_tax and len(query[brand_tax]) == 1:
            # the shop filed these under that manufacturer even when the title does not name it
            name = next((t["name"] for b in self._widget_blocks if b["tax"] == brand_tax
                         for t in b["terms"] if t["slug"] == query[brand_tax][0]), None)
            for r in recs:
                if name and not r["brand"]:
                    r["brand"] = name
        if preds or query:
            keep = [(p, r) for p, r in zip(prods, recs) if all(f(p, r) for _, f in preds)]
            if in_stock:
                keep = [(p, r) for p, r in keep if r["in_stock"]]
            log(f"  filters {' AND '.join(filters)}{' + in stock' if in_stock else ''}: {len(keep)} product(s)"
                f"{f' (output capped at --limit {limit})' if limit and len(keep) >= limit else ''}")
            recs = [r for _, r in keep]
            if limit:
                recs = recs[:limit]
        elif in_stock:
            recs = [r for r in recs if r["in_stock"]]
        return recs

    # ---------------------------------------------------------------- facets
    def facets(self, ref):
        ids = self.resolve_category(ref)
        if len(ids) != 1:
            raise Usage("facets takes one category (the shop's filter widget is per category page)")
        cats = self._categories()
        cat = cats[ids[0]]
        out = []
        blocks = self.widget(cat)
        for b in blocks:
            brand = b["tax"].lower() == "brend"
            for t in b["terms"]:
                out.append({"name": ("brand (Производител)" if brand else f"{b['label']} ({b['tax']})"),
                            "value": t["name"], "count": t["count"],
                            "token": f"{'brand' if brand else b['tax']}={t['slug']}"})
        if not blocks:
            log("  note: this category page shows no filter widget values (only price/stock/subcategory)")
        for c in sorted((c for c in cats.values() if c.get("parent") == cat["id"]),
                        key=lambda c: -(c.get("count") or 0)):
            out.append({"name": "subcategory", "value": c["_name"], "count": c.get("count"),
                        "token": f"cat={unquote(c['slug'])}"})
        data, _ = self._get("/products/collection-data",
                            params={"category": cat["id"], "calculate_price_range": "true",
                                    "calculate_stock_status_counts": "true"})
        pr = (data or {}).get("price_range") or {}
        minor = pr.get("currency_minor_unit", 2)
        lo, hi = minor_to_mkd(pr.get("min_price"), minor), minor_to_mkd(pr.get("max_price"), minor)
        if lo is not None and hi is not None:
            out.append({"name": "price (MKD)", "value": f"{lo}-{hi}", "count": cat.get("count"),
                        "token": f"price={lo}-{hi}"})
        names = {"instock": ("in stock", "stock=in"), "outofstock": ("out of stock", "stock=out"),
                 "onbackorder": ("on backorder (supplier order)", "stock=backorder")}
        for s in (data or {}).get("stock_status_counts") or []:
            n = int(s.get("count") or 0)
            if s.get("status") in names and n:
                out.append({"name": "stock", "value": names[s["status"]][0], "count": n,
                            "token": names[s["status"]][1]})
        log(f"  {cat['_path']}: {len(out)} facet values ({len(blocks)} widget filter(s); widget counts "
            f"are the shop's own)")
        return out

    # ---------------------------------------------------------------- detail
    @staticmethod
    def parse_product_ref(ref):
        """-> ('id', int) | ('slug', str)"""
        s = unquote((ref or "").strip())
        if not s:
            raise Usage("empty product reference")
        m = re.match(r"(?i)^id:\s*(\d+)$", s)
        if m:
            return "id", int(m.group(1))
        if re.match(r"^(https?://|/|(www\.)?setra\.mk)", s, re.I):
            u = urlparse(s if "://" in s else ("https://" + s.lstrip("/") if "setra.mk" in s.lower() else BASE + s))
            if u.netloc and "setra.mk" not in u.netloc.lower():
                raise Usage(f"not a setra.mk URL: {ref}")
            m = re.search(r"[?&](?:p|post|product_id|add-to-cart)=(\d+)", "?" + u.query)
            if m:
                return "id", int(m.group(1))
            m = re.search(r"/product/(?:[^/]+/)*?([^/]+)/?$", u.path)
            if m:
                return "slug", m.group(1)
            raise Usage(f"not a product URL (expected /product/<slug>/ or ?p=<id>): {ref}")
        if re.fullmatch(r"\d+", s):
            return "id", int(s)
        if re.fullmatch(r"[\w%.-]+", s):
            return "slug", s
        raise Usage(f"cannot parse {ref!r} (use a product URL, a numeric id or a slug)")

    def delivery_terms(self):
        """Store-wide delivery wording from the live terms-of-sale page (fetched once)."""
        if self._terms is False:
            self._terms = None
            try:
                data, _ = self._get(BASE + "/wp-json/wp/v2/pages", params={"slug": TERMS_SLUG, "_fields": "content"})
                body = collapse(html_text(data[0]["content"]["rendered"])) if data else ""
                parts = []
                m = re.search(r"доставена во рок од (?:максимум )?\d+(?:\s*[-–]\s*\d+)? работни дена", body)
                if m:
                    parts.append(m.group(0) + " од потврдата на нарачката")
                m = re.search(r"изнесува од \d+ денари", body)
                if m:
                    parts.append("испорака " + m.group(0) + " (курирска служба, според големина и маса)")
                if parts:
                    self._terms = "; ".join(parts) + f" [store-wide terms, {BASE}/{TERMS_SLUG}/]"
            except (RuntimeError, KeyError, IndexError, TypeError) as e:
                if isinstance(e, Blocked):
                    raise
                log(f"  note: terms page unavailable: {e}")
        return self._terms

    def detail_record(self, p, ref):
        rec = self.to_record(p)
        raw, months = warranty_of(p)
        ean, cands = eans_in(p)
        short = collapse(html_text(p.get("short_description")))
        desc = collapse(html_text(p.get("description")))
        pr = p.get("prices") or {}
        minor = pr.get("currency_minor_unit", 2)
        sale = minor_to_mkd(pr.get("sale_price"), minor)
        cart_max = (p.get("add_to_cart") or {}).get("maximum")
        if rec["in_stock"] is True:
            delivery = self.delivery_terms()
        elif rec["in_stock"] is None:
            delivery = "supplier order (Достапно по нарачка); the shop publishes no lead time"
        else:
            delivery = None
        extra = {k: v for k, v in {
            "warranty_raw": raw,
            "warranty_months": months,
            "ean_candidates": cands if len(cands) > 1 else None,
            "stale_sale_price_mkd": (sale if sale is not None and rec["price_mkd"] is not None
                                     and sale != rec["price_mkd"] else None),
            "max_per_order": cart_max if cart_max and cart_max < 9999 else None,
            "purchasable": p.get("is_purchasable"),
            "slug": p.get("slug"),
            "category_ids": [c["id"] for c in p.get("categories") or []],
            "delivery_fee_note": ("courier delivery from 150 MKD depending on size and weight "
                                  "(terms of sale); no free-delivery threshold is published"),
        }.items() if v is not None}
        rec.update({
            "input": ref,
            "ean": ean,
            "warranty": raw if raw and months != 0 else None,   # "0 месеци" = not entered
            "specs": " | ".join(x for x in (short, desc) if x) or None,
            "per_location_stock": None,   # one web stock flag only; no per-shop breakdown exposed
            "delivery_estimate": delivery,
            "extra": extra,
        })
        return rec

    def detail(self, refs):
        """-> (records in input order, exit code). Failures become {"input","error"} rows.
        Exit 0 if at least one product resolved, else 2 (all not found / bad input) or 1."""
        parsed = []
        for ref in refs:
            try:
                parsed.append((ref, self.parse_product_ref(ref), None))
            except Usage as e:
                parsed.append((ref, None, f"bad input: {e}"))
        out, ok, hard = [], 0, False
        try:
            ids = [k[1] for _, k, _ in parsed if k and k[0] == "id"]
            ids_error = None
            try:
                found = self.by_ids(ids, label="detail") if ids else {}
            except (RuntimeError, requests.RequestException, ValueError) as e:
                if isinstance(e, Blocked):
                    raise
                # the id lookup is one batched request: its failure must not sink the slug/URL inputs
                found, ids_error = {}, str(e)
            for ref, k, err in parsed:
                if err:
                    log(f"  {err}")
                    out.append({"input": ref, "error": err})
                    continue
                try:
                    kind, val = k
                    if kind == "id" and ids_error:
                        raise RuntimeError(f"id lookup failed: {ids_error}")
                    if kind == "id":
                        p = found.get(val)
                    else:
                        p, _ = self._get(f"/products/{val}", allow_404=True)
                        if p is not None and not (isinstance(p, dict) and p.get("id")):
                            raise RuntimeError(f"unexpected payload for slug {val!r}: {str(p)[:120]}")
                    if p is None:
                        raise NotFound(f"no product for {kind} {val!r} (unknown, unpublished or removed)")
                    out.append(self.detail_record(p, ref))
                    ok += 1
                except NotFound as e:
                    log(f"  not found: {ref} ({e})")
                    out.append({"input": ref, "error": f"not found: {e}"})
                except (RuntimeError, requests.RequestException, ValueError) as e:
                    if isinstance(e, Blocked):
                        raise
                    log(f"  error: {ref} ({e})")
                    out.append({"input": ref, "error": str(e)})
                    hard = True
        except Blocked as e:
            rest = [ref for ref, _, _ in parsed[len(out):]]
            if rest:
                out.append({"input": rest[0], "error": f"blocked: {e}"})
                out += [{"input": ref, "error": "skipped: store blocked the client"} for ref in rest[1:]]
            raise _BatchBlocked(out, e)
        if len(refs) > 1 and ok < len(refs):
            log(f"  {len(refs) - ok} of {len(refs)} input(s) did not resolve")
        return out, (0 if ok else (1 if hard else 2))


class _BatchBlocked(Exception):
    def __init__(self, recs, err):
        super().__init__(str(err))
        self.recs, self.err = recs, err


def parse_widget(text):
    """HUSKY/WOOF filter widget -> [{tax, label, terms: [{id, slug, name, count}]}] (empty blocks
    dropped). Radio blocks carry name=<tax> data-slug=<slug>; checkbox blocks data-tax=<tax>
    name=<slug>; both carry a hidden data-anchor="woof_n_<tax>_<slug>" whose value is the label."""
    soup = BeautifulSoup(text, "html.parser")
    blocks, seen = [], set()
    for c in soup.select("div.woof_container[data-css-class]"):
        tax = c["data-css-class"].replace("woof_container_", "", 1)
        if not tax or tax in seen:
            continue
        h = c.find("h4")
        label = collapse(h.get_text(" ")) if h else tax
        terms = []
        for li in c.select("li"):
            inp = li.find("input", attrs={"data-term-id": True}, recursive=False)
            hid = li.find("input", attrs={"data-anchor": True}, recursive=False)
            if not inp:
                continue
            anchor = (hid.get("data-anchor") if hid else "") or ""
            prefix = f"woof_n_{tax}_"
            slug = inp.get("data-slug") or (anchor[len(prefix):] if anchor.startswith(prefix) else inp.get("name"))
            cnt = li.find(class_=re.compile(r"_count$"))
            digits = re.sub(r"\D", "", cnt.get_text()) if cnt else ""
            name = collapse(hid.get("value")) if hid and hid.get("value") else None
            if not name:
                lab = li.find("label", recursive=False)
                name = collapse(re.sub(r"\(\d+\)\s*$", "", lab.get_text(" "))) if lab else slug
            terms.append({"id": inp.get("data-term-id"), "slug": unquote(slug or ""), "name": unescape(name),
                          "count": int(digits) if digits else None})
        if terms:
            seen.add(tax)
            blocks.append({"tax": tax, "label": label, "terms": terms})
    return blocks


# --------------------------------------------------------------------------- CLI

def print_products(recs):
    for r in recs:
        if "error" in r:
            print(f"      !  ERROR  {r['input']}: {r['error']}")
            continue
        price = f"{r['price_mkd']:>7}" if r.get("price_mkd") is not None else "      ?"
        reg = f" (was {r['regular_price_mkd']})" if r.get("regular_price_mkd") else ""
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        brand = f"[{r['brand']}] " if r.get("brand") else ""
        print(f"{price} MKD{reg}  {stock}  {brand}{(r.get('title') or '')[:80]}  {r['url']}")
        if "warranty" in r:
            print(f"        id {r.get('id')} | EAN {r.get('ean')} | warranty {r.get('warranty')} | "
                  f"{r.get('category')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{r['id']:>6} {cnt}  {r['path']}  [{r['slug']}]")


def print_facets(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    ?"
        print(f"{cnt}  {r['name']}: {r['value']}   --filter '{r['token']}'")


def emit(recs, path, printer=print_products, what="records"):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        print(len(recs))
        log(f"  {len(recs)} {what} -> {path}")
    else:
        printer(recs)
        sys.stdout.flush()
        log(f"-- {len(recs)} {what}")


def info():
    return {"store": STORE, "name": NAME, "base_url": BASE, "capabilities": CAPABILITIES,
            "sells": SELLS, "notes": NOTES}


def main(argv=None):
    global QUIET
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    ap = argparse.ArgumentParser(description="Setra (setra.mk) catalogue client")
    # also accepted before the subcommand (separate dests: subparser defaults would overwrite them)
    ap.add_argument("--quiet", dest="g_quiet", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", dest="g_verbose", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--category", help="restrict to a category (id, slug, url or name; server-side)")
    p.add_argument("--phrase", action="store_true",
                   help="send the query verbatim (the store matches it as one substring of the title)")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p = sub.add_parser("list", parents=[common])
    p.add_argument("category")
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", action="append", default=[], metavar="TOKEN")
    p = sub.add_parser("detail", parents=[common])
    p.add_argument("refs", nargs="+")
    p = sub.add_parser("facets", parents=[common])
    p.add_argument("category")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    QUIET = a.quiet or a.g_quiet

    if a.cmd == "info":
        print(json.dumps(info(), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    c = Setra(verbose=a.verbose or a.g_verbose)
    code = 0
    try:
        if a.cmd == "search":
            emit(c.search(a.query, limit=a.limit, in_stock=a.in_stock, category=a.category,
                          phrase=a.phrase), a.json)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = c.categories(grep=a.grep)
            if a.grep and not recs:
                log(f"  no category matches {a.grep!r} (matched against name, path, slug, a Latin "
                    f"transliteration and English department words)")
            emit(recs, a.json, print_categories, "categories")
        elif a.cmd == "list":
            emit(c.list_category(a.category, in_stock=a.in_stock, limit=a.limit, filters=a.filter), a.json)
        elif a.cmd == "facets":
            emit(c.facets(a.category), a.json, print_facets, "facet values")
        elif a.cmd == "detail":
            try:
                recs, code = c.detail(a.refs)
            except _BatchBlocked as b:
                emit(b.recs, a.json)
                raise b.err
            emit(recs, a.json)
        log(f"  ({c.requests_made} requests)")
        return code
    except Blocked as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 3
    except (NotFound, Usage) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (RuntimeError, requests.RequestException, ValueError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
