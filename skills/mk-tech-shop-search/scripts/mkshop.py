#!/usr/bin/env python3
"""
mkshop.py - search, list and compare products across North Macedonian shops.

One driver over eleven self-contained store clients in scripts/stores/ (see
references/client-contract.md). It never talks to a shop itself: every
request goes through that shop's client, run as a subprocess. Shops are
queried in parallel (one worker per shop); requests inside a shop stay
sequential, as the contract requires.

Commands
  stores      the shops, their domains, what they sell and which data they expose
  search      cross-shop full-text search, merged and sorted (price by default)
  categories  grep every shop's category tree, or print one shop's full tree
  list        every product in one shop's category (all pages), optional filters
  facets      one shop's structured attributes for a category (filter tokens)
  detail      full product records; URLs are routed to the right shop by domain
  match       find the same product in other shops (by EAN, model code, title)
  group       merge saved search/list/match/detail JSON into one row per product
              (EAN, then model codes / part numbers), offers sorted by price

Run any command with -h for its flags. mkshop.py itself uses only the
standard library; the store clients need Python 3.9+ with requests and
beautifulsoup4 (MKSHOP_PYTHON picks the interpreter that runs them).

Store keys: setec anhoch neksio ddstore neptun hivetec gjirafa50 zirafamall
setra ananas tehnomarket (alias: gjirafa = gjirafa50,zirafamall).

Per-shop status, reported for every command that touches several shops:
  ok         answered (possibly with zero results)
  partial    some of several queries failed; the results are incomplete
  not_found  exit 2 from the client: unknown category/product or bad usage
  blocked    CAPTCHA / Cloudflare challenge / WAF / login wall (client exit 3)
  error      client crashed, is missing, or printed invalid JSON
  timeout    the shop did not finish within --timeout seconds
A shop that is not "ok" has NOT been searched; never read it as "not found".

Exit codes: 0 at least one shop answered; 1 every shop failed; 2 usage error
or nothing found where something was required (unknown category/product);
3 every shop was blocked.
"""

import argparse
import atexit
import bisect
import csv
import datetime as _dt
import html
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORES_DIR = os.path.join(HERE, "stores")

DEFAULT_TIMEOUT = 240          # seconds per shop for search / match / detail
DEFAULT_LIST_TIMEOUT = 900     # list walks every page of a category
INFO_TIMEOUT = 30              # `info` is network-free; it should be instant
DEFAULT_LIMIT_PER_STORE = 120
DEFAULT_MATCH_LIMIT = 40
DEFAULT_SHOW = 100
# group prints a products table and then each product's offers: 100 x 10 rows ran to 40k
# characters on a two-shop test, past what an agent's tool output shows inline (~30k).
GROUP_SHOW = 40
GROUP_SHOW_OFFERS = 5
TITLE_WIDTH = 72
# Pause between two client runs against the same shop (each run is one
# search; the client paces its own pages). Keeps a shop's traffic sequential
# and spaced, as the client contract asks.
STORE_GAP = 0.4

# --------------------------------------------------------------------------
# Store registry
# --------------------------------------------------------------------------
# ean_search: does the shop's own search match an EAN typed as the query?
#   True  - verified (setec indexes catalogue_number, neksio/neptun match
#           barcodes, Algolia on ananas indexes product.ean, Gjirafa's
#           Elasticsearch matches GTIN on both sites; probed 2026-10-03).
#   False - verified useless: the shop has no EAN anywhere (anhoch, hivetec,
#           tehnomarket), or its search covers titles only (setra) or ignores
#           the barcode field (ddstore).
#   None  - unknown; match tries it (one cheap request) and trusts nothing
#           it returns without an EAN or title check.
# A client can override this with a capability "search_ean" or
# "search_codes" in its `info` output.
# ean_detail / ean_listing: registry defaults for the contract capabilities
# ean_in_detail / ean_in_listing, used only when `info` is unavailable.
# marketplace: listings carry a `seller`; shown in the STORE column.
# search_args: extra flags for the client's `search` (search and match).
#   anhoch: --all-words = every word in any order (title), plus genuine
#   phrase hits; without it a multi-word query whose word order differs from
#   the title silently falls back to OR-matching (hundreds of unrelated hits).
# `sells` is only a fallback: `stores` shows each client's own `info` text.
REGISTRY = {
    "setec": dict(
        name="Setec", script="setec.py", args=[],
        domains=["setec.mk"],
        sells="Large consumer-electronics and home chain: computers and PC parts, "
              "laptops, monitors, phones, TVs/audio, large and small home "
              "appliances, air conditioning, kitchenware, tools, garden, sport.",
        ean_search=True, ean_listing=True, ean_detail=True),
    "anhoch": dict(
        name="Anhoch", script="anhoch.py", args=[],
        domains=["anhoch.com", "www.anhoch.com"],
        sells="IT and consumer-electronics chain: laptops, desktops, monitors, "
              "PC components, peripherals, gaming, phones, TVs, audio, "
              "networking, printers, air conditioners; no large kitchen/laundry "
              "appliances.",
        ean_search=False, ean_listing=False, ean_detail=False,
        search_args=["--all-words"]),
    "neksio": dict(
        name="Neksio", script="neksio.py", args=[],
        domains=["g.store.neksio.mk", "neksio.mk"],
        sells="IT distributor webshop: peripherals, PC components, monitors, "
              "networking, storage, gaming gear, laptops, small home appliances; "
              "effectively no TVs and no large appliances.",
        ean_search=True, ean_listing=True, ean_detail=True),
    "ddstore": dict(
        name="DDStore", script="ddstore.py", args=[],
        domains=["ddstore.mk"],
        sells="IT and electronics shop: PC components, printer consumables, "
              "peripherals, monitors, TVs, projectors, laptops, phones, "
              "networking, small home appliances, air conditioners.",
        ean_search=False, ean_listing=True, ean_detail=True),
    "neptun": dict(
        name="Neptun", script="neptun.py", args=[],
        domains=["neptun.mk"],
        sells="Consumer-electronics and appliance chain: TVs, audio, phones, "
              "laptops, gaming, small and large home appliances, air "
              "conditioners, personal care, sport.",
        ean_search=True, ean_listing=True, ean_detail=True),
    "hivetec": dict(
        name="Hivetec", script="hivetec.py", args=[],
        domains=["hivetec.mk"],
        sells="Gaming and PC specialist: PC components, peripherals, monitors, "
              "gaming chairs, laptops, prebuilt PCs, consoles, networking; no "
              "phones, TVs or home appliances.",
        ean_search=False, ean_listing=False, ean_detail=False),
    "gjirafa50": dict(
        name="Gjirafa50", script="gjirafa.py", args=["--site", "gjirafa50"],
        domains=["gjirafa50.mk"],
        sells="Large online electronics shop: IT, phones, TVs, audio, gaming, "
              "PC components, small appliances; much stock is supplier-ordered "
              "(weeks).",
        ean_search=True, ean_listing=False, ean_detail=True),
    "zirafamall": dict(
        name="ZirafaMall", script="gjirafa.py", args=["--site", "zirafamall"],
        domains=["zirafamall.mk"],
        sells="Marketplace sister of Gjirafa50: electronics (mostly mirrored "
              "from Gjirafa50), large and small appliances, home, garden, toys, "
              "sports, beauty, fashion.",
        ean_search=True, ean_listing=False, ean_detail=True),
    "setra": dict(
        name="Setra", script="setra.py", args=[],
        domains=["setra.mk"],
        sells="IT, gaming and small-electronics shop: peripherals, PC "
              "components, prebuilt PCs, monitors, laptops, printers, "
              "networking, small appliances; no phones, TVs or large appliances.",
        ean_search=False, ean_listing=True, ean_detail=True),
    "ananas": dict(
        name="Ananas", script="ananas.py", args=[],
        domains=["ananas.mk"],
        sells="Multi-seller marketplace (many shops incl. Neksio, Neptun, "
              "Setec, Setra, Tehnomarket): electronics, appliances, home, toys, "
              "sports, fashion, almost anything.",
        ean_search=True, ean_listing=True, ean_detail=True, marketplace=True),
    "tehnomarket": dict(
        name="Tehnomarket", script="tehnomarket.py", args=[],
        domains=["tehnomarket.com.mk"],
        sells="Consumer-electronics and appliance chain: TVs, phones, IT, small "
              "and large home appliances, air conditioners.",
        ean_search=False, ean_listing=False, ean_detail=False),
}
REGISTRY["zirafamall"]["marketplace"] = True
STORE_KEYS = list(REGISTRY)
_STORE_WORDS = {re.sub(r"[^0-9a-z]", "", k) for k in REGISTRY} | {re.sub(r"[^0-9a-z]", "", v["name"].lower())
                                                                   for v in REGISTRY.values()}
ALIASES = {"gjirafa": ["gjirafa50", "zirafamall"], "all": STORE_KEYS}

# Ananas sellers that are themselves shops covered here (seller name folded to
# lowercase Latin, letters and digits only -> store key). Their Ananas
# listings mirror the shop's own catalogue, usually at the same price.
ANANAS_MIRROR_SELLERS = [
    ("neksio", "neksio"),
    ("neptun", "neptun"),
    ("setek", "setec"),           # "Сетек Се од Техника"
    ("setra", "setra"),
    ("tehnomarket", "tehnomarket"),
    ("pcmarket", "anhoch"),
]
# Shops whose `sku` is usually the manufacturer part number, so it is useful
# as a model code when matching across shops.
SKU_IS_MPN = {"hivetec"}

# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
_print_lock = threading.Lock()
QUIET = False


def die(msg, code=2):
    """Usage error: message on stderr, exit 2."""
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)
    sys.exit(code)


def log(msg):
    if QUIET:
        return
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def warn(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v))
    s = re.sub(r"[^\d.,-]", "", str(v))
    if not s:
        return None
    s = s.replace(".", "").replace(",", "") if re.search(r"[.,]\d{3}(?!\d)", s) else s.replace(",", ".")
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def now_iso():
    """Local time with offset: the envelope's generated_at (group uses it to
    keep the freshest copy of a record seen in several saved runs)."""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_price(v):
    return "" if v is None else f"{v:,}"


def trunc(s, n):
    s = "" if s is None else str(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def stock_cell(v):
    return {True: "yes", False: "no"}.get(v, "?")


def render_table(rows, cols, out):
    """cols: list of (header, func(row)->str, align 'l'|'r', max width or None)."""
    cells = []
    for r in rows:
        line = []
        for _h, fn, _a, w in cols:
            v = fn(r)
            v = "" if v is None else str(v)
            if w:
                v = trunc(v, w)
            line.append(v)
        cells.append(line)
    widths = [len(h) for h, *_ in cols]
    for line in cells:
        for i, v in enumerate(line):
            widths[i] = max(widths[i], len(v))
    widths[-1] = 0  # last column (usually the URL) is never padded

    def fmt(line):
        parts = []
        for i, v in enumerate(line):
            a = cols[i][2]
            parts.append(v.rjust(widths[i]) if a == "r" else v.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    print(fmt([h for h, *_ in cols]), file=out)
    for line in cells:
        print(fmt(line), file=out)


def write_json(obj, path):
    if path in (None, "-"):
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


CSV_FIELDS = ["effective_price_mkd", "price_condition", "price_mkd", "regular_price_mkd", "member_price_mkd",
              "store", "seller", "mirror_of",
              "in_stock", "stock_note", "title", "brand", "sku", "ean", "mpn",
              "category", "match_key", "model_key", "url", "id"]


CSV_VALID_FIELDS = ["effective_price_valid_until", "price_valid_until", "member_price_valid_until"]


def csv_fields(records, lead=()):
    """CSV_FIELDS, plus the price-validity columns when any record has one
    (absent key -> 'unknown', null -> '' = standing price)."""
    fields = list(lead) + CSV_FIELDS
    if any(f in r for r in records for f in VALID_FIELDS):
        fields += CSV_VALID_FIELDS
    return fields


def csv_value(r, k):
    if k in CSV_VALID_FIELDS:
        if k == "effective_price_valid_until":
            known, v = effective_valid_until(r)
            return "unknown" if not known else "" if v is None else v
        return "unknown" if k not in r else ("" if r[k] is None else r[k])
    v = r.get(k)
    return "" if v is None else v


def write_csv(records, path, lead=()):
    buf = io.StringIO() if path == "-" else open(path, "w", encoding="utf-8", newline="")
    try:
        fields = csv_fields(records, lead)
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow({k: csv_value(r, k) for k in fields})
        if path == "-":
            sys.stdout.write(buf.getvalue())
    finally:
        buf.close()


# --------------------------------------------------------------------------
# Text folding (Cyrillic -> Latin) and product-title analysis
# --------------------------------------------------------------------------
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e",
    "ж": "zh", "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l",
    "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "ќ": "kj", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "џ": "dz", "ш": "sh", "ђ": "dj", "ћ": "c", "й": "j", "ы": "y",
    "э": "e", "ю": "ju", "я": "ja", "щ": "sht", "ъ": "a", "ь": "", "ё": "e",
    "є": "e", "і": "i", "ї": "i", "ў": "u",
}
_CYR_RE = re.compile("[Ѐ-ӿ]")


def fold(s):
    """Lowercase, Cyrillic transliterated to Latin, accents stripped."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def alnum(s):
    return re.sub(r"[^0-9a-z]", "", fold(s))


def is_url(s):
    return bool(re.match(r"^(https?://|www\.)", s.strip(), re.I)) or bool(
        re.match(r"^[a-z0-9.-]+\.(mk|com)(/|$)", s.strip(), re.I))


def store_for_url(ref):
    s = ref.strip()
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    host = (urllib.parse.urlsplit(s).hostname or "").lower()
    if not host:
        return None
    best = None
    for key, st in REGISTRY.items():
        for d in st["domains"]:
            if host == d or host.endswith("." + d):
                if best is None or len(d) > best[1]:
                    best = (key, len(d))
    return best[0] if best else None


def norm_gtin(v):
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if not 8 <= len(d) <= 14:
        return None
    d = d.lstrip("0")
    return d.zfill(13) if len(d) <= 13 else d


def ean_query_forms(ean, raw=None):
    """The spellings of one barcode worth typing into a shop's search. norm_gtin
    pads to 13 digits, but shops index the code as printed: a UPC-A product
    (EAN-13 with a leading 0) is stored as 12 digits by Setec, Ananas and
    Gjirafa (exact-token search: the padded form finds nothing there) and as
    13 digits by Neptun. -> list, shortest standard form first."""
    forms = []
    if ean:
        sig = ean.lstrip("0")
        if len(sig) <= 8:
            forms.append(sig.zfill(8))       # EAN-8
        elif len(sig) <= 12:
            forms.append(sig.zfill(12))      # UPC-A
        forms.append(ean)
    raw = re.sub(r"\D", "", str(raw or ""))
    if 8 <= len(raw) <= 14:
        forms.append(raw)
    out = []
    for f in forms:
        if f not in out:
            out.append(f)
    return out


def gtin_check_ok(d):
    d = re.sub(r"\D", "", d or "")
    if len(d) not in (8, 12, 13, 14):
        return False
    body, chk = d[:-1], int(d[-1])
    tot = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return (10 - tot % 10) % 10 == chk


# Words that describe a product type, connectivity or marketing rather than
# identify a model (folded to lowercase Latin). Used only for matching and for
# building title queries, never to drop search results.
GENERIC = set("""
a an the and or with without for of in on to by from at as new original genuine
official edition version model type kit set combo bundle pack pcs pc
piece pieces unit item product products gb tb
keyboard keyboards mouse mice gaming gamer game games wireless wired cordless
bluetooth mechanical membrane optical laptop notebook ultrabook computer
desktop smartphone phone mobile cellphone tablet tv television smart monitor
display screen panel headset headphones headphone earbuds earphones earphone
speaker speakers soundbar cable adapter charger case cover bag backpack sleeve
stand holder mount dock docking hub router camera webcam printer scanner
console controller gamepad joystick smartwatch fitness band tracker
fridge refrigerator freezer washer washing machine dryer dishwasher oven
microwave cooker hob vacuum cleaner robot conditioner heater fan iron kettle
blender mixer toaster grill fryer coffee maker espresso hair shaver trimmer
toothbrush ssd hdd drive disk memory ram card graphics gpu cpu processor
motherboard mainboard psu power supply cooler cooling chassis tower internal
external portable rgb led lcd hd fhd uhd full wifi usb hdmi type nvme sata vga
inch inches cm mm lp low profile layout switches keycaps key
illuminated backlit backlight lighting light noise cancelling anc active
waterproof water resistant compact ergonomic silent quiet fast rapid
charging battery dual single triple multi multimedia standard home office
professional business performance premium essential classic basic
edition limited special eu us uk de intl international english packing
gaming-keyboard mechanical-keyboard pale light matte glossy metallic
so za i od na vo ili bez kon do pri me per dhe nga te
tastatura tastaturi glushec gluvche gluvcinja laptop laptopi monitor monitori
televizor televizori telefon telefoni mobilen mobilni pametni pameten
frizider ladilnik zamrznuvach peralna mashina mashini sushara sadomiyalka
rerna shporet mikrobranova pechka klima uredi ured slushalki slushalka
zvuchnik zvuchnici kabel adapter polnach futrola torba ranec kompjuter
procesor memorija napojuvanje kuler kukjishte grafichka karta disk
mehanichka mehanichki mehanichko gejmerska gejmerski gejming bezhichna
bezhichen bezhichni zhichna zhichen zhichni kopchinja kopchinja tasteri
tastatures mekanike tastiere
""".split())

# Accessory nouns (folded). A title with one of these plus "for" / "за" /
# "compatible", or starting with one, describes an accessory for the product
# it names ("Samsung Case for Galaxy A56"), not the product.
ACCESSORY_WORDS = {
    "case", "cover", "bumper", "flip", "wallet", "pouch", "sleeve", "skin", "protector",
    "glass", "tempered", "film", "foil", "charger", "cable", "adapter", "strap", "band",
    "holder", "mount", "stand", "dock", "bag", "backpack", "remote", "battery", "keycaps",
    "futrola", "maska", "maski", "kapak", "zashtitno", "zashtitna", "zashtita", "staklo",
    "folija", "polnach", "kabel", "adapter", "remen", "rementche", "drzhach", "stalak",
    "torba", "ranec", "daljinski", "baterija", "navlaka", "kalap", "mbrojtese", "karikues",
}
_FOR_WORDS = {"for", "za", "compatible", "kompatibilen", "kompatibilna", "fits", "per", "pentru", "fur"}
# Protective accessories: these nouns name the accessory wherever they stand
# ('Hishell Tempered Glass Screen Protector - Apple iPhone 17 Pro',
# 'Заштитник за леќи ... iPhone 17 Pro'); a phone's own title never has them.
# Not here: bare 'стакло' (a kettle's 'стакло' body) and 'заштита' ('заштита
# од деца' on a washer); they still count next to 'за' / at the start.
_ACC_STRONG = {
    "protector", "tempered", "bumper", "pouch", "futrola", "futroli", "maska", "maski",
    "zashtitno", "zashtitna", "zashtitni", "zashtitnik", "zashtitnici", "zakaleno",
    "zakalena", "folija", "folii", "navlaka", "kalap", "mbrojtese", "mbrojtes",
    "obvivka", "obvivki", "kllapa", "kellef",
}
# 'case' / 'cover' are also PC chassis and book covers, so they mark an
# accessory only next to a phone / tablet / wearable word ('Apple iPhone 17
# Pro Clear Case with MagSafe', '3MK Clear MagCase за Apple iPhone').
_ACC_CASE = {"case", "cover", "magcase", "etui", "kutija"}
# ...but not the device's own case: 'Apple Watch ... Aluminium Case with Sport
# Band', 'AirPods Pro 2 with MagSafe Charging Case', 'Stainless Steel case'.
_OWN_CASE_BEFORE = {"charging", "aluminium", "aluminum", "alu", "titanium", "steel",
                    "stainless", "ceramic", "polnenje", "polnach"}
# 'for' words that mark a device accessory ('за iPhone 17'); 'compatible with
# iPhone' is also how smartwatches describe themselves, so it does not count.
_FOR_DEVICE_WORDS = _FOR_WORDS - {"compatible", "kompatibilen", "kompatibilna"}
_DEVICE_WORDS = {
    "iphone", "ipad", "galaxy", "redmi", "pixel", "airpods", "macbook", "magsafe",
    "phone", "smartphone", "telefon", "telefoni", "mobilen", "mobilni", "tablet", "tableti",
    "smartwatch", "watch", "kindle",
}
# Spare parts and accessories ACCESSORY_WORDS lacks: ear pads / cushions,
# charging cases ('Кутија за полнење'), mounts, dongles, ear tips (folded).
_ACC_PARTS = {"pernichinja", "pernicinja", "pernichki", "pernici", "pernica", "jastuchinja",
              "jastuche", "jastuchinjata", "earpads", "earpad", "cushions", "cushion", "pads",
              "kutija", "kutii", "bateri", "akumulator", "nosach", "bracket", "dongle", "nastavki",
              "eartips", "tips", "kabllo", "aksesoar", "aksesoari", "accessory", "accessories", "aksesore",
              "priemnik", "prijemnik"}
# Words that make the next noun a spare part ('Резервни перничиња', 'Replacement ear pads').
_REPLACEMENT_WORDS = {"rezervni", "rezervna", "rezerven", "rezervno", "replacement", "spare",
                      "zamena", "zamenski", "zamenska", "zevendesuese", "zevendesues"}
# 'with' words: what follows names an included part ('... со велурни перничиња',
# 'w/Microphone', 'Kufje me mikrofon', 'with Charging Case'), never the product.
_INCLUDE_WORDS = {"with", "w", "so", "me", "incl", "including", "inkl", "vklucheno", "vkluchuva"}
# Words that may come before a title's subject noun without being one.
_LEAD_WORDS = {"smart", "original", "new", "official", "univerzalen", "univerzalna", "univerzalni",
               "universal", "magneten", "magnetna", "magnetic", "silikonska", "silikonski", "kozhena",
               "leather", "silicone", "protective"}


def _include_pos(words):
    """Index of the first 'with' word after the first word, else len(words)."""
    return next((i for i, w in enumerate(words) if i and w in _INCLUDE_WORDS), len(words))


def _accessory(words, bw=()):
    """The accessory / spare-part noun a title is about, or None. Decided by the
    title's subject: the first product or accessory noun outside 'for X' and
    'with X' phrases. 'Резервни перничиња за слушалки ISK HD9999', 'Батерија за
    лаптоп', 'Headphone Stand' are parts or accessories; 'Безжични слушалки ... со
    кревач ..., за фиксен телефон' are headphones. An accessory noun is the
    subject when it leads the title (after brand / generic words), is followed by
    'for' or follows 'replacement'. Without a subject noun: a protective word
    anywhere ('Tempered Glass Screen Protector - iPhone 17'), a case next to a
    phone / tablet / watch word, or 'for <device>'."""
    n = len(words)
    incl = _include_pos(words)
    for_zone = set()
    for i, w in enumerate(words):
        if w in _FOR_WORDS:
            for_zone.update(range(i + 1, min(n, i + 3)))
    nouns = ACCESSORY_WORDS | _ACC_PARTS

    def lead_ok(i):
        return all(words[j] in bw or words[j] in GENERIC or words[j] in _LEAD_WORDS
                   or words[j] in _REPLACEMENT_WORDS for j in range(i))

    def model_next(i):  # 'Smart Band 9', 'Case 4000D': a model name, not an accessory noun
        x = words[i + 1] if i + 1 < n else ""
        return bool(re.fullmatch(r"\d{1,4}[a-z]?|[a-z]{0,3}\d{1,4}[a-z]{0,2}", x)) and not _UNIT_RE.match(x)
    for i in range(incl):
        w = words[i]
        if i in for_zone or w in bw:
            continue
        if w in PRODUCT_TYPES:
            nxt = words[i + 1] if i + 1 < incl else None
            if nxt in nouns and not model_next(i + 1):   # 'Headphone Stand', 'Laptop Bag'
                return nxt
            return None
        if w in _ACC_STRONG:
            return w
        if w in nouns and not (i and words[i - 1] in _OWN_CASE_BEFORE) and not model_next(i):
            if lead_ok(i) or (i and words[i - 1] in _REPLACEMENT_WORDS) \
                    or any(words[k] in _FOR_WORDS for k in range(i + 1, min(n, i + 3))):
                return w
    scope = words[:incl]
    # no product noun: an accessory noun that ends the title names it ('Poly BT600
    # Bluetooth USB Адаптер, Црн', 'Sony WH-1000XM5 Ear Pads'; not 'Smart Band 9'
    # and not 'Case', which ends many PC chassis titles)
    for i in range(len(scope) - 1, -1, -1):
        w = scope[i]
        if i in for_zone or w in COLOURS or w in COLOUR_MODIFIERS or w in bw or re.fullmatch(r"\d+[a-z]*", w):
            continue
        if w in nouns and w not in _ACC_CASE and w not in ("staklo", "glass", "zashtita", "film", "foil", "skin") \
                and not model_next(i) and not (i and words[i - 1] in _OWN_CASE_BEFORE):   # a kettle's 'стакло'
            return w
        if w not in GENERIC:
            break
    case_acc = [w for i, w in enumerate(scope)
                if (w in _ACC_CASE or (len(w) > 5 and w.endswith(("case", "cover"))))  # MattCase, Backcover
                and not (i and scope[i - 1] in _OWN_CASE_BEFORE)]
    if case_acc and _DEVICE_WORDS & set(words):
        return case_acc[0]
    # '... за телефон', '... for iPhone 17': made for a device, so not the device
    return next((f"{w} {x}" for i, w in enumerate(scope) if w in _FOR_DEVICE_WORDS
                 for x in words[i + 1:i + 3] if x in _DEVICE_WORDS), None)

TIER_WORDS = {"pro", "max", "plus", "ultra", "mini", "lite", "air", "fe", "se",
              "slim", "neo", "edge", "xl", "xs", "xr", "ti", "super", "oc",
              "prime", "turbo", "extreme", "elite", "core", "go", "nano"}
# Phrases in which a tier word is not a tier.
_PHRASE_FIX = [
    (r"\bultra[\s-]*hd\b", "uhd"), (r"\bfull[\s-]*hd\b", "fhd"),
    (r"\bair\s+(conditioner|fryer|purifier|cooler|pump|cooling)\b", r"\1"),
    (r"\bmini[\s-]*led\b", "miniled"), (r"\bmini[\s-]*pc\b", "minipc"),
    (r"\bmini[\s-]*usb\b", "miniusb"), (r"\bmini[\s-]*hdmi\b", "minihdmi"),
    (r"\bmini[\s-]*itx\b", "miniitx"), (r"\bmini[\s-]*dp\b", "minidp"),
    (r"\bsuper[\s-]*speed\b", "superspeed"),
]

# Laptops, prebuilt PCs and configurations print their parts' codes (CPU
# 14650HX, 7800X3D, i7-14700F; GPU RTX5070): in such a title they name a
# component that many different systems share, not the product.
_CPU_CODE_RE = re.compile(r"^(i[3579]\d{4,5}[a-z]{0,2}\d?|\d{3,5}(hx3d|x3d|hx|hs|h|u|x|xt|k|kf|f|g|ks|t)|"
                          r"r[3579]\d{4}[a-z]{0,3}|c[3579]\d{3}[a-z]{0,2}|x1[ep]\d{5})$")
_GPU_CODE_RE = re.compile(r"^(rtx|gtx|rx|arc)[a-z]?\d{3,4}[a-z]{0,4}$")
SYSTEM_WORDS = {"laptop", "laptopi", "notebook", "ultrabook", "konfiguracija", "kompjuter", "desktop",
                "workstation", "minipc", "allinone", "aio", "kompjuteri"}
# Laptop / desktop lines whose name alone says 'computer' (no headset or mouse
# carries them; OMEN, Legion, ROG, TUF, Nitro... do, so they count only with a
# CPU and a memory size in the title, see _CPU_PHRASE_RE).
LAPTOP_SERIES = {"vivobook", "zenbook", "ideapad", "thinkpad", "thinkbook", "expertbook", "elitebook",
                 "probook", "inspiron", "latitude", "vostro", "aspire", "travelmate", "extensa", "macbook",
                 "chromebook", "matebook", "magicbook", "redmibook", "ideacentre", "thinkcentre", "optiplex",
                 "prodesk", "elitedesk", "omnibook", "zbook"}
# A CPU named in prose: 'Core i5', 'Core 5', 'Core Ultra 7', 'Ryzen 7', 'Рајзен 5', 'Celeron'.
_CPU_PHRASE_RE = re.compile(r" (?:core (?:i[3579]|ultra [579]|[3579])|i[3579] \d{4,5}|ryzen (?:ai )?[3579]|"
                            r"rajzen [3579]|celeron|pentium|athlon|snapdragon x) ")
_PHONE_HINTS = {"iphone", "ipad", "redmi", "poco", "pixel", "smartphone", "telefon", "mobilen", "tablet", "tableti"}
_RAM_SIZES = {1, 2, 3, 4, 6, 8, 10, 12, 16, 18, 20, 24, 32, 36, 48, 64, 96, 128}
# RAM + storage pairs: '8/256GB', '8GB+256GB', '16GB/1TB', '12/256' (unitless: phone / computer titles only)
_MEM_PAIR_RE = re.compile(r"(?<![\w.,])(\d{1,3}) ?(?:gb)? ?[/+] ?(\d{2,4}|[124]) ?(gb|tb)(?![a-z])")
_MEM_PAIR_BARE_RE = re.compile(r"(?<![\w.,/])(\d{1,2}) ?/ ?(32|64|128|256|512)(?![\w./])")
_RAM_AFTER = {"ram", "ddr3", "ddr4", "ddr4x", "ddr5", "ddr5x", "lpddr4", "lpddr4x", "lpddr5", "lpddr5x"}
_STORAGE_NEAR = {"ssd", "hdd", "nvme", "emmc", "ufs", "storage", "disk", "rom", "m.2", "m2", "pcie"}


def _gb(num, unit):
    v = float(str(num).replace(",", ".")) * (1000 if unit == "tb" else 1)
    return int(v) if v == int(v) else v


def _config(text, s, computer=False, phone=False, cpu=False):
    """The configuration a title states, as variant dimensions: s.cpu (CPU
    model codes: 13620h, 7520u, 150u), s.cpu_tier (ryzen5, corei7, core5,
    ultra7), s.ram / s.storage (GB), s.gpu (rtx4050) and s.vram (GPU memory,
    not RAM). Memory pairs ('8/256GB', '8+256GB', '16GB/1TB') are RAM + storage
    in any title; CPU, GPU, labelled sizes ('16GB RAM', '512GB SSD', '16GB
    DDR5') and unlabelled ones (the smaller is RAM, the larger storage) only in
    a computer or phone title (the CPU also in any title that names one in
    prose: a free-text 'ASUS TUF A15 FA507NU Ryzen 7')."""
    s.cpu, s.cpu_tier, s.ram, s.storage, s.gpu, s.vram = set(), set(), set(), set(), set(), set()
    t = fold(clean_title(text)).replace("гб", "gb")
    t = re.sub(r"(\d)\s*(gb|tb)\b", r"\1\2", t)
    for mt in _MEM_PAIR_RE.finditer(t):
        a, b = int(mt.group(1)), _gb(mt.group(2), mt.group(3))
        if a in _RAM_SIZES and b >= 32 and b > a:
            s.ram.add(a)
            s.storage.add(b)
    if computer or phone:
        for mt in _MEM_PAIR_BARE_RE.finditer(t):
            if int(mt.group(1)) in _RAM_SIZES and not s.ram:
                s.ram.add(int(mt.group(1)))
                s.storage.add(int(mt.group(2)))
    if not (computer or phone or cpu):
        return s
    # separators stay as tokens: '16GB/ SSD 512GB' labels only the 512GB as storage
    toks = [x if x in ",;/|" else x.strip("-.") for x in re.findall(r"[^\s,;:/|()\[\]+\"'”″“*]+|[,;/|]", t)]
    toks = [x for x in toks if x]
    n = len(toks)
    gpu_at = set()
    if computer or cpu:
        for k, x in enumerate(toks):
            nxt = toks[k + 1] if k + 1 < n else ""
            nxt2 = toks[k + 2] if k + 2 < n else ""
            g = re.fullmatch(r"(rtx|gtx|rx)(\d{3,4})(ti|s|xt)?", x) if computer else None
            if not computer:   # a GPU only in a computer's title
                pass
            elif x in ("rtx", "gtx", "rx") and re.fullmatch(r"\d{3,4}[a-z]?", nxt):
                s.gpu.add(x + nxt + (nxt2 if nxt2 in ("ti", "super", "xt") else ""))
                gpu_at.update((k, k + 1, k + 2))
            elif g:
                s.gpu.add(x + (nxt if nxt in ("ti", "super", "xt") else ""))
                gpu_at.update((k, k + 1))
            model = None
            if x in ("ryzen", "rajzen") and re.fullmatch(r"[3579]", nxt):
                s.cpu_tier.add("ryzen" + nxt)
                model = nxt2
            elif x in ("ryzen", "rajzen") and nxt == "ai" and re.fullmatch(r"[3579]", nxt2):
                s.cpu_tier.add("ryzenai" + nxt2)
                model = toks[k + 4] if k + 4 < n and toks[k + 3] == "hx" else toks[k + 3] if k + 3 < n else None
            elif re.fullmatch(r"r([3579])-(\d{4}[a-z]{0,3})", x):
                mm = re.fullmatch(r"r([3579])-(\d{4}[a-z]{0,3})", x)
                s.cpu_tier.add("ryzen" + mm.group(1))
                s.cpu.add(mm.group(2))
            elif re.fullmatch(r"i([3579])(?:-?(\d{4,5}[a-z]{0,2}\d?))?", x):
                mm = re.fullmatch(r"i([3579])(?:-?(\d{4,5}[a-z]{0,2}\d?))?", x)
                s.cpu_tier.add("corei" + mm.group(1))
                if mm.group(2):
                    s.cpu.add(mm.group(2))
                else:
                    model = nxt
            elif re.fullmatch(r"c([3579])-(\d{3}[a-z]{0,2})", x):     # 'C7-150U'
                mm = re.fullmatch(r"c([3579])-(\d{3}[a-z]{0,2})", x)
                s.cpu_tier.add("core" + mm.group(1))
                s.cpu.add(mm.group(2))
            elif x in ("core", "coretm") and re.fullmatch(r"[3579](-\d{3}[a-z]{0,2})?", nxt):
                s.cpu_tier.add("core" + nxt[0])
                if "-" in nxt:
                    s.cpu.add(nxt.split("-", 1)[1])
                else:
                    model = nxt2
            elif x == "ultra" and re.fullmatch(r"[579]", nxt):
                s.cpu_tier.add("ultra" + nxt)
                model = nxt2
            elif x in ("celeron", "pentium", "athlon", "snapdragon", "kompanio"):
                s.cpu_tier.add(x)
                model = nxt if not re.fullmatch(r"x|elite|plus|gold|silver", nxt) else nxt2
            elif re.fullmatch(r"x1[ep]-?\d{2}-?\d{3}", x):
                s.cpu.add(x.replace("-", ""))
            elif re.fullmatch(r"n\d{3,4}", x) and ("intel" in toks or "celeron" in toks or "processor" in toks):
                s.cpu.add(x)
            if model and re.fullmatch(r"\d{3,5}[a-z]{0,3}\d?|[a-z]\d{3,5}[a-z]{0,2}", model) \
                    and not re.fullmatch(r"\d+(gb|tb|mb|hz|w)", model):
                s.cpu.add(model)
    if not (computer or phone):
        return s
    loose = []
    for k, x in enumerate(toks):
        mt = re.fullmatch(r"(\d+(?:[.,]\d+)?)(gb|tb)", x)
        if not mt:
            continue
        v = _gb(mt.group(1), mt.group(2))
        a0 = toks[k + 1] if k + 1 < n else ""
        b1 = toks[k - 1] if k else ""
        if a0 in _RAM_AFTER:
            s.ram.add(v)
        elif a0 in _STORAGE_NEAR:
            s.storage.add(v)
        elif (b1 == "ram" or (b1 == "memorija" and "ram" in toks[max(0, k - 3):k])) \
                and not (k >= 2 and re.fullmatch(r"\d+(?:[.,]\d+)?(gb|tb)", toks[k - 2])):   # not '8GB RAM 512GB'
            s.ram.add(v)
        elif b1 in _STORAGE_NEAR:
            s.storage.add(v)
        elif k - 1 in gpu_at or k - 2 in gpu_at or re.match(r"g?ddr\d|gddr|vram", a0) and a0 not in _RAM_AFTER:
            s.vram.add(v)
        else:
            loose.append(v)
    loose = sorted({v for v in loose if v not in s.ram | s.storage | s.vram})
    if len(loose) >= 2:
        if not s.ram and loose[0] <= 64:
            s.ram.add(loose[0])
        if not s.storage and loose[-1] >= 64:
            s.storage.add(loose[-1])
    elif loose:
        v = loose[0]
        big = v >= 32 if phone and not computer else v > 64
        if big and not s.storage:
            s.storage.add(v)
        elif not big and not s.ram:
            s.ram.add(v)
    return s

# Neptun spells Samsung TV part numbers with spaces ('UE 55 U8072H UXXH',
# 'QE 55 Q7F AAUXXH'); glued, they are the codes every other shop prints, and
# they carry the screen size that tells UE55U8072H from UE43U8072H.
_SPACED_TV_CODE_RE = re.compile(
    r"\b(UE|QE|GQ|GU|QN|TQ|UN|QA|UA)\s+(\d{2,3})\s+([A-Z]{1,3}\d{1,4}[A-Z0-9]{0,4})"
    r"(?:\s+([A-Z]{0,3}XX[A-Z]{0,2}))?\b", re.I)

# A Samsung TV part number starts with the diagonal: QE55Q7FAAUXXH = 55".
# At least four characters must follow, so series names (QN90D, QN85F,
# QN900D) are not read as a 90" / 85" / 900" set.
_SAMSUNG_TV_CODE_RE = re.compile(r"^(?:ue|qe|gq|gu|qn|tq|un|qa|ua)(\d{2,3})[a-z][a-z0-9]{3,}$")

COLOURS = {
    "black": "black", "white": "white", "silver": "silver", "grey": "grey",
    "gray": "grey", "blue": "blue", "red": "red", "green": "green",
    "gold": "gold", "pink": "pink", "purple": "purple", "violet": "purple",
    "yellow": "yellow", "orange": "orange", "brown": "brown", "beige": "beige",
    "navy": "navy", "graphite": "graphite", "titanium": "titanium",
    "midnight": "midnight", "starlight": "starlight", "rose": "pink",
    "mint": "mint", "cream": "cream", "lavender": "purple", "ivory": "cream",
    "bronze": "bronze", "copper": "bronze", "champagne": "gold", "teal": "teal",
    "cyan": "teal", "turquoise": "teal", "lime": "green", "olive": "green",
    "charcoal": "graphite", "anthracite": "graphite", "inox": "silver",
    "stainless": "silver", "transparent": "clear", "clear": "clear", "prozirna": "clear",
    "proziren": "clear", "transparentna": "clear", "transparenten": "clear",
    "sage": "green", "sky": "blue",
}
# Macedonian colour stems (folded) with common adjective endings.
_MK_COLOUR_STEMS = {
    "crn": "black", "bel": "white", "srebren": "silver", "srebrena": "silver",
    "siv": "grey", "sin": "blue", "plav": "blue", "crven": "red",
    "zelen": "green", "zlaten": "gold", "zlatn": "gold", "rozev": "pink",
    "rozov": "pink", "roze": "pink", "violetov": "purple", "zholt": "yellow",
    "portokalov": "orange", "kafeav": "brown", "kafen": "brown", "bezh": "beige",
    "teget": "navy", "tirkiz": "teal", "grafit": "graphite", "titanium": "titanium",
}
_MK_SUFFIXES = ["", "a", "o", "i", "na", "no", "ni", "ena", "eno", "eni"]
for _stem, _c in _MK_COLOUR_STEMS.items():
    for _suf in _MK_SUFFIXES:
        COLOURS.setdefault(_stem + _suf, _c)
# Albanian (Gjirafa/ZirafaMall titles can be Albanian), folded.
for _w, _c in {"zi": "black", "zeze": "black", "bardhe": "white", "kuq": "red",
               "kuqe": "red", "kalter": "blue", "blu": "blue", "gjelber": "green",
               "jeshile": "green", "verdhe": "yellow", "hiri": "grey", "gri": "grey",
               "argjendte": "silver", "argjend": "silver", "roze": "pink",
               "portokalli": "orange", "vjollce": "purple", "ari": "gold",
               # plural / feminine forms Gjirafa50 titles use ('të zeza', 'të kaltërta')
               "zeza": "black", "bardha": "white", "kaltra": "blue", "kalterta": "blue",
               "kalterte": "blue", "hirta": "grey", "hirte": "grey", "arta": "gold", "arte": "gold",
               "verdha": "yellow", "gjelbra": "green", "gjelberta": "green", "gjelberte": "green",
               "argjendta": "silver", "vjollca": "purple"}.items():
    COLOURS.setdefault(_w, _c)
# A few that the stem rule mangles.
for _w, _c in {"crna": "black", "crno": "black", "crni": "black", "bela": "white",
               "belo": "white", "beli": "white", "sina": "blue", "sino": "blue",
               "sini": "blue", "zlatna": "gold", "zlatno": "gold"}.items():
    COLOURS[_w] = _c
# Marketing colour names (JBL 'Sand', Sony 'Smoky Pink', Samsung 'Latte'),
# shop abbreviations (Neptun 'WHT', Ananas 'BLK') and a few more MK/AL stems.
# Left out on purpose: words that are also product or brand names
# (onyx, sapphire, platinum = PSU rating, ocean = watch band, kafe = coffee).
for _w, _c in {
        "sand": "sand", "latte": "latte", "mocha": "brown", "chocolate": "brown",
        "camel": "brown", "tan": "brown", "taupe": "grey", "slate": "grey", "ash": "grey",
        "pearl": "white", "obsidian": "black", "ebony": "black", "gunmetal": "graphite",
        "chrome": "silver", "denim": "blue", "indigo": "blue", "cobalt": "blue",
        "azure": "blue", "aqua": "teal", "emerald": "green", "jade": "green",
        "khaki": "green", "coral": "orange", "terracotta": "orange", "amber": "orange",
        "peach": "pink", "salmon": "pink", "blush": "pink", "magenta": "pink",
        "fuchsia": "pink", "lilac": "purple", "lila": "purple", "mauve": "purple",
        "plum": "purple", "burgundy": "red", "bordeaux": "red", "bordo": "red",
        "wine": "red", "maroon": "red", "crimson": "red", "scarlet": "red",
        "mustard": "yellow", "lemon": "yellow", "nickel": "nickel", "nikel": "nickel",
        "wht": "white", "blk": "black", "gry": "grey", "slv": "silver", "pnk": "pink",
        "grn": "green", "ylw": "yellow", "gld": "gold",
        "krem": "cream", "krema": "cream", "kreme": "cream", "shampanj": "gold",
        "lavanda": "purple", "menta": "mint", "bezhe": "beige", "rere": "sand"}.items():
    COLOURS.setdefault(_w, _c)
for _stem, _c in {"pesochn": "sand", "maslinest": "green", "krem": "cream", "lilav": "purple",
                  "bordov": "red"}.items():
    for _suf in _MK_SUFFIXES:
        COLOURS.setdefault(_stem + _suf, _c)
# Words that only modify a colour right after them ('Forest Gray', 'Smoky
# Pink', 'Space Gray', 'темно сина'): dropped there, ordinary words elsewhere
# ('Arctic Freezer', 'Royal Kludge').
COLOUR_MODIFIERS = {
    "forest", "smoky", "smokey", "soft", "deep", "dark", "jet", "space", "phantom",
    "cosmic", "mist", "misty", "ice", "icy", "arctic", "bright", "electric", "royal",
    "baby", "hot", "powder", "ocean", "sea", "stone", "army", "pastel", "neon", "dusty",
    "storm", "stormy", "cloud", "glacier", "frost", "frosted", "moon", "lunar", "shadow",
    "midnight", "natural", "warm", "cool", "pure", "true", "sky", "rich", "light", "pale",
    "temno", "temna", "temni", "svetlo", "svetla", "svetli", "nezhno", "nezhna", "erret",
    # marketing colour names ('Platinum Silver', 'Quiet Blue', 'Luna Grey', 'Mecha Gray',
    # 'Onyx Black'); ordinary words where no colour follows ('80 Plus Platinum')
    "platinum", "quiet", "luna", "mecha", "eclipse", "abyss", "starry", "stellar", "aurora",
    "polar", "desert", "alpine", "marine", "pacific", "sierra", "snow", "night", "dusk",
    "dawn", "fog", "haze", "smoke", "smoked", "pebble", "moss", "clay", "titan", "silky",
    "satin", "dazzling", "prism", "awesome", "onyx", "sapphire", "mystic", "iceberg",
    "arktichko", "arktichka", "arktichki",
}
# Modifiers that make a different colour edition of the same base colour:
# 'Light Blue' and 'Dark Blue' are two Galaxy A57s (SM-A576BLB.. vs BDB..).
# A plain 'Blue' still fits either; only opposite shades conflict.
_SHADES = {"light": "light", "svetlo": "light", "svetla": "light", "svetli": "light", "pale": "light",
           "dark": "dark", "temno": "dark", "temna": "dark", "temni": "dark", "deep": "dark"}
# 3-letter colour abbreviations that vendors append to part numbers
# (JBL LIVE770NCBLK, T770NCWHT): the colour of that listing.
_CODE_COLOUR_SUFFIX = {"blk": "black", "wht": "white", "blu": "blue", "gry": "grey",
                       "slv": "silver", "pnk": "pink", "grn": "green", "ylw": "yellow",
                       "gld": "gold"}
# One-letter colour suffixes some vendors put right after a model number (Sony
# WH-CH520W / B / L, WH1000XM5S, WF1000XM5B): the colours each letter may mean.
# Never trusted alone: a title on one side must name that colour (see
# code_colour), since for most brands a trailing letter is part of the model
# (MDR-ZX110AP, NTH-100M, SM-A566B).
_COLOUR_LETTERS = {"b": {"black"}, "w": {"white"}, "l": {"blue", "navy"}, "s": {"silver"},
                   "p": {"pink"}, "c": {"beige", "cream"}, "y": {"yellow"}}
# A regional / packaging tail of a part number: Samsung TV AUXXH / XXH, Sony
# CE7 / CEC, LG AEU, EU editions, dual-SIM DS. Also written after a dot or a
# slash ('WH1000XM5L.CE7', 'SM-S931B/DS', 'MTJV3ZD/A'), where the base code is
# what other shops print.
# Spec / feature abbreviations that follow a model code without being part of it.
_SPEC_ABBR = {"ips", "tn", "va", "fhd", "qhd", "uhd", "hdr", "lcd", "led", "oled", "ssd", "hdd", "atx", "itx",
              "usb", "ai", "nc", "anc", "enc", "tws", "mic", "dac", "amp", "hub", "set", "kit", "oem", "ram",
              "cpu", "gpu", "psu", "rgb", "dpi", "bt", "ps", "pc", "mac", "dect", "uhf", "vhf", "fm", "am",
              "dvd", "tv", "hd", "sd", "xl", "xs", "nfc", "gps", "lte", "esim", "sim", "ds", "eu", "new"}
_PKG_TAIL_RE = re.compile(r"^(?:[a-z]{0,3}xx[a-z]{0,2}|ce\d|cec|ae[a-z]?|eu[a-z]?|ds|dsn|dseu)$")
_PKG_SPLIT_RE = re.compile(r"^(.*\d[A-Za-z]{0,4})[./]([A-Za-z]{1,4}\d?)$")
# Last words of a title that are not a colour even when no colour is named
# ('... Dual SIM', '... ANC', '... Retail'); see Sig.maybe_colour.
_NOT_COLOUR = {
    "sim", "esim", "dual", "nfc", "lte", "anc", "enc", "tws", "mic", "microphone", "edition",
    "version", "bundle", "retail", "oem", "tray", "box", "boxed", "refurbished", "renewed",
    "certified", "original", "global", "series", "model", "ips", "curved", "flat", "frameless",
    "vertical", "travel", "kids", "junior", "outdoor", "indoor", "inverter", "digital",
    "analog", "automatic", "manual", "cellular", "wifi", "android", "ios", "windows", "linux",
    "freedos", "dos", "nos", "win", "home", "pro", "plus", "free", "touch", "stylus", "pen",
    "adapter", "charger", "remote", "case", "cover", "glass", "pad", "mat", "set", "kit",
    "pack", "pcs", "duo", "trio", "max", "mini", "lite", "ultra", "slim", "gaming", "sport",
    "sports", "active", "performance", "business", "office", "studio", "creator", "studio",
    "white", "black", "type", "cable", "cord", "led", "rgb", "argb", "hub", "dock", "stand",
    "mount", "bracket", "holder", "strap", "band", "loop", "buds", "earbuds", "speaker",
    "headset", "headphones", "mouse", "keyboard", "monitor", "laptop", "notebook", "tablet",
    "phone", "smartphone", "watch", "smartwatch", "camera", "router", "printer", "console",
    "controller", "gamepad", "vacuum", "cleaner", "fan", "heater", "kettle", "toaster",
    "blender", "mixer", "iron", "dryer", "washer", "fridge", "freezer", "oven", "hob",
    "microwave", "dishwasher", "cooker", "hood", "boiler", "conditioner", "purifier",
    "humidifier", "dehumidifier", "scale", "trimmer", "shaver", "epilator", "toothbrush",
    "straightener", "curler", "styler", "projector", "soundbar", "subwoofer", "amplifier",
    "receiver", "turntable", "drone", "scooter", "bike", "ssd", "hdd", "nvme", "ram",
    "memory", "cpu", "gpu", "psu", "cooler", "tower", "chassis", "board", "motherboard",
    "wireless", "wired", "bluetooth", "ergonomic", "silent", "quiet", "mechanical",
    "optical", "magnetic", "hybrid", "smart", "cordless", "portable", "compact", "oc",
    "tkl", "full", "size", "layout", "ansi", "iso", "us", "uk", "eu", "intl",
    "linear", "tactile", "clicky", "hotswap", "swappable", "international", "usa", "nordic", "german",
    "english", "qwerty", "qwertz", "azerty", "mac", "macos", "win", "speed", "fast", "lightspeed",
    "ergonomik", "pa", "tel", "me", "dhe",
}

# Product nouns (folded; EN/MK/AL) -> product type. The first one in a title
# is its type; a record without one takes the type its category names. Used
# to drop title-only ('likely') matches of another kind of product: a party
# speaker is not a variant of headphones.
PRODUCT_TYPES = {}
for _t, _ws in {
        "headphones": "headphones headphone headset headsets earbuds earbud earphones earphone "
                      "slushalki slushalka slushalkata kufje kufjet degjuese",
        "speaker": "speaker speakers zvuchnik zvuchnici zvuchnikot altoparlant altoparlante "
                   "partybox",
        "soundbar": "soundbar soundbars",
        "mouse": "mouse mice glushec glushci gluvche gluvchinja maus",
        "keyboard": "keyboard keyboards tastatura tastaturi tastiere tastiera",
        "monitor": "monitor monitori",
        "tv": "tv televizor televizori television televizion",
        "phone": "smartphone smartphones phone telefon telefoni",
        "tablet": "tablet tableti",
        "laptop": "laptop laptopi notebook",
        "watch": "smartwatch watch chasovnik",
        "console": "console konzola",
        "controller": "controller gamepad dzhojstik kontroler",
        "camera": "camera kamera fotoaparat",
        "webcam": "webcam",
        "router": "router ruter",
        "printer": "printer printeri pechatar pechatach pechatachi mfp",
        # printer consumables: a toner 'for M111a/M111w' is not the M111w printer
        "consumable": "toner toneri tonera cartridge cartridges crtg kertridz kertridzi mastilo drum refill",
        "microphone": "microphone microphones mikrofon mikrofoni mikrofonot",
        "vacuum": "pravosmukalka pravosmukalki",
        "washer": "peralna",
        "fridge": "fridge refrigerator frizider ladilnik",
}.items():
    for _w in _ws.split():
        PRODUCT_TYPES[_w] = _t

REGIONS = {"eu", "us", "uk", "de", "intl", "int", "global", "cn", "asia",
           "ansi", "iso", "nordic", "ita", "fr", "es", "ch", "adria"}
# Keyboard layouts and layout languages, as the region they name ('QWERTZ' /
# 'германски' = de, 'AZERTY' / 'frëngjisht' = fr): G80-3000N QWERTZ and AZERTY
# are two products.
_REGION_ALIAS = {"qwertz": "de", "azerty": "fr", "germanski": "de", "germanska": "de", "gjermanisht": "de",
                 "francuski": "fr", "francuska": "fr", "frengjisht": "fr", "skandinavski": "nordic"}
REGIONS |= set(_REGION_ALIAS)

UNIT_ALIASES = {
    "gb": "gb", "tb": "gb", "mb": "mb", "w": "w", "kw": "w", "hz": "hz",
    "khz": "hz", "mhz": "mhz", "ghz": "ghz", "mah": "mah", "wh": "wh",
    "mm": "mm", "cm": "cm", "inch": "inch", "in": "inch", "l": "l", "lit": "l",
    "kg": "kg", "v": "v", "db": "db", "dpi": "dpi", "ms": "ms", "k": "k",
    "rpm": "rpm", "btu": "btu", "bar": "bar", "mp": "mp", "nm": "nm",
    "m": "m", "g": "g", "a": "a", "mbps": "mbps", "gbps": "gbps", "tops": "tops", "ml": "ml",
}
_UNIT_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)(gb|tb|mb|kw|w|khz|mhz|ghz|hz|mah|wh|mm|cm|inch|in|lit|ml|l|"
    r"kg|v|db|dpi|ms|k|rpm|btu|bar|mp|nm|m|g|a|mbps|gbps|tops)$")
_MULTI_CAP_RE = re.compile(r"^(\d+)/(\d+)(gb|tb)$")
# A spec range is no part number: '20Hz-20kHz', '560-590MHz' (raw_tokens glues
# the unit on), '100-240V', '2.4-5GHz', '1.5-3A', and '20Hz-20' when a
# thousands comma cut '20Hz-20,000Hz'. A unit on at least one end.
_RANGE_UNITS = (r"(?:gb|tb|mb|kw|w|khz|mhz|ghz|hz|mah|wh|mm|cm|inch|in|lit|ml|l|kg|v|db|dpi|ms|k|rpm|btu|"
                r"bar|mp|nm|m|g|a|mbps|gbps|tops)")
_UNIT_RANGE_RE = re.compile(
    r"^\d+(?:[.,]\d+)?" + _RANGE_UNITS + r"?[-~/]\d+(?:[.,]\d+)?" + _RANGE_UNITS + r"$"
    r"|^\d+(?:[.,]\d+)?" + _RANGE_UNITS + r"[-~/]\d+(?:[.,]\d+)?$")
# A kit (2x16GB) or a size: '10x15cm' photo paper, '5760x1440dpi', '60x40x2mm'.
_KIT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)x(\d+(?:[.,]\d+)?)(?:x\d+(?:[.,]\d+)?)?(gb|tb|w|mm|cm|m|in|inch|dpi|px)?$")
_PERCENT_RE = re.compile(r"^\d+%$")
# Alphanumeric tokens that name a technology or spec rather than a model.
_TECH_RE = re.compile(
    r"^(ddr\d|gddr\d[x]?|lpddr\d+x?|pcie?\d?(\.\d)?|pci-?e\d?|usb\d?(\.\d)*|"
    r"usb-?c|type-?c|hdmi\d?(\.\d)?|dp\d(\.\d)?|wi-?fi\d?e?|bt\d(\.\d)?|[2-6]g|"
    r"ip[x\d]\d|h\.?26[45]|m\.?2|sata\d?|rj-?45|qi\d?|\d+in1|"
    r"\d+-in-1|gen\d|\d+(st|nd|rd|th)|\d{1,2}x|hdr\d*\+?|dolby|"
    r"atmos|amoled|oled|qled|nanocell|1080p|1440p|2160p|720p|\d{3,4}p|"
    r"\d{3,4}x\d{3,4}|wxga|wuxga|qhd|uhd|fhd|e-?sports?|a\+*|\d+-?bits?|\d+-?pcs|\d+-?keys|dx\d{2}u?|"
    r"r\d{3}a|class|r32|r290|r410a|ac\d{3,4}|ax\d{3,4}|be\d{3,4}|"
    # SSD read speed ('6000R/4000W') and screen curvature ('1500R'): specs
    r"\d{3,5}r|"
    # port / zone counts: '24port', '8-Zone RGB', '2xUSB-A', '4xDDR4', '1xPCIEx16'
    r"\d{1,3}-?ports?|\d{1,2}-?zones?|"
    r"\d{1,2}x(?:usb|hdmi|ddr|pcie|pci|sata|m\.?2|dp|displayport|rj-?45|type|lan|vga|sfp|thunderbolt)[a-z0-9.\-]*)$")
_VERSION_RE = re.compile(r"^(v\d{1,2}(\.\d)?|mk\d|mkii|mkiii|ii|iii|iv|gen\d{1,2}|rev\d*)$")
_YEAR_RE = re.compile(r"^(19[89]\d|20[0-3]\d)$")
# Model years ('Audi A6 C8 2018-2023'): never a part number.
_YEAR_RANGE_RE = re.compile(r"^(19[89]\d|20[0-3]\d)[-/](19[89]\d|20[0-3]\d)$")

_SPLIT_RE = re.compile(r"[\s,;:|()\[\]{}<>!?\"'’‘“”«»+*&–—=_~#@]+")


def clean_title(title):
    """Undo storefront escaping that some shops leave in titles ('55&quot;',
    '55\\"' on Gjirafa50/ZirafaMall), so sizes and words parse normally."""
    t = str(title or "")
    if "&" in t:
        t = html.unescape(t)
    return t.replace('\\"', '"').replace("\\'", "'")


def raw_tokens(title):
    t = unicodedata.normalize("NFKC", clean_title(title))
    # a decimal comma ('15,6"', '1,5 L'; not '20,000Hz')
    t = re.sub(r"(\d),(\d)(?!\d)", r"\1.\2", t)
    # Glue inch marks and common units to their number: 55" -> 55inch.
    # "S20+", "Note 13 Pro+" -> "S20 plus": the plus is part of the model name.
    t = re.sub(r"(\b[A-Za-z]*\d+[A-Za-z]*|\b(?:pro|max|ultra|note))\+(?=[\s,;)]|$)", r"\1 plus", t, flags=re.I)
    t = re.sub(r"(\d)\s*(\"|''|”|″)", r"\1inch ", t)
    t = re.sub(r"(\d)\s*-?(inch(?:es)?|инчи|инч)\b", r"\1inch ", t, flags=re.I)
    t = re.sub(r"(\d)in\b", r"\1inch", t, flags=re.I)
    t = re.sub(r"(\d)\s+(gb|tb|mb|w|hz|mhz|ghz|mah|mm|cm|kg|ml|l|dpi)\b", r"\1\2", t, flags=re.I)
    t = re.sub(r"(\d)\s*мл(?![а-яѓќѕјљњџ])", r"\1ml", t, flags=re.I)
    # cable / range lengths: '2.5 m', '6 м', '3.5 мм' (not 'Gen 1 M.2')
    t = re.sub(r"(\d)\s*(m|м)(?![.\wа-яѓќѕјљњџ])", r"\1m", t, flags=re.I)
    t = re.sub(r"(\d)\s*мм(?![а-яѓќѕјљњџ])", r"\1mm", t, flags=re.I)
    t = re.sub(r"(\d)\s*(гб)(?![а-яѓќѕјљњџ])", r"\1gb", t, flags=re.I)
    t = re.sub(r"(\d)\s*(тб)(?![а-яѓќѕјљњџ])", r"\1tb", t, flags=re.I)
    t = re.sub(r"(\d)\s*(кг)(?![а-яѓќѕјљњџ])", r"\1kg", t, flags=re.I)
    t = re.sub(r"(\d)\s*(л|lit)(?![а-яѓќѕјљњџa-z])", r"\1l", t, flags=re.I)
    t = re.sub(r"(\d)\s*(обрт|вртежи|об/мин|rpm)\.?(?![а-яѓќѕјљњџa-z])", r"\1rpm", t, flags=re.I)
    t = re.sub(r"(\d)\s*(вати|в)(?![а-яѓќѕјљњџ])", r"\1w", t, flags=re.I)
    t = re.sub(r"(\d)\s*(инчи|инч)\b", r"\1inch", t, flags=re.I)
    out = []
    for tok in _SPLIT_RE.split(t):
        tok = tok.strip("-./\\")
        if tok:
            out.append(tok)
    return out


class Sig:
    """What identifies a product in a title, and what separates variants.
    derived: model codes a title writes with spaces ('LIVE 770 NC' -> 770nc,
    'WH 1000 XM5' -> wh1000xm5) -> (first, last) title position; they match
    only a code written in one piece elsewhere. maybe_colour: the last word(s)
    of a title that names no known colour, when they could be one ('... 770NC
    Sand'); ptype: the product noun of the title (headphones, speaker, ...), or
    of its category (tptype: the title's own). pkg_base: base code -> the code
    with its packaging tail ('wh1000xm5l' -> 'wh1000xm5lce7'). code_suffix: a
    model suffix written apart ('CVM-V01SP UC'). cpu / cpu_tier / ram / storage /
    gpu / vram: a configuration (see _config). colour_src: 'title' or 'attr'
    (a seller's colour attribute). incl_pos: title position of the first 'with'
    word; what follows is an included part. for_only: words the title names
    only right after 'for' / 'за' ('TJ ink refill for Canon Pixma': canon), the
    brand of what the item fits rather than its own."""
    __slots__ = ("brand", "ean", "strong", "weak", "numbers", "tokens", "tiers",
                 "versions", "colours", "regions", "units", "concat", "kept", "seq", "pairs",
                 "accessory", "bundle", "codes", "glued", "derived", "maybe_colour", "ptype", "system",
                 "shades", "tptype", "colour_src", "pkg_base", "incl_pos", "cpu", "cpu_tier", "ram", "storage",
                 "gpu", "vram", "code_suffix", "for_only")

    def __repr__(self):
        return "Sig(" + ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__slots__ if k != "concat") + ")"


def _is_strong_code(raw):
    """A manufacturer-style model code: SM-S931B, QE55Q60D, CT1000P3PSSD8,
    920-014207, GK-K0CC2-KM570-S10NA, RTX4070. Letters and digits mixed (at
    least two digits, or one digit plus a separator), or digits split by a
    dash; at least five alphanumerics; never a unit, spec or technology."""
    if _CYR_RE.search(raw):
        return False
    a = re.sub(r"[^0-9A-Za-z]", "", raw)
    if len(a) < 5 and not (len(a) == 4 and re.search(r"[A-Za-z0-9]-[A-Za-z0-9]", raw)):
        return False
    nd = len(re.findall(r"\d", a))
    has_l = bool(re.search(r"[A-Za-z]", a))
    if nd and has_l:
        f = raw.lower()
        if (_UNIT_RE.match(f) or _MULTI_CAP_RE.match(f) or _KIT_RE.match(f)
                or _TECH_RE.match(f) or _VERSION_RE.match(f) or _VERSION_RE.match(f.replace("-", ""))
                or _UNIT_RANGE_RE.match(f)):   # 'Gen-2' is a version
            return False
        return nd >= 2 or bool(re.search(r"[-/.]", raw))
    if nd and not has_l and re.match(r"^\d{2,6}[-/.]\d{3,8}$", raw):
        return not _YEAR_RANGE_RE.match(raw)  # Logitech-style 920-014207; a year range is none
    return False


def _is_weak_code(raw):
    """Short model token: G515, S25, 3S, A56, K295, R65, X1."""
    if _CYR_RE.search(raw):
        return False
    a = re.sub(r"[^0-9A-Za-z]", "", raw)
    if not 2 <= len(a) <= 6:
        return False
    if not (re.search(r"\d", a) and re.search(r"[A-Za-z]", a)):
        return False
    f = raw.lower()
    if (_UNIT_RE.match(f) or _MULTI_CAP_RE.match(f) or _KIT_RE.match(f)
            or _TECH_RE.match(f) or _VERSION_RE.match(f) or _PERCENT_RE.match(f)
            or _UNIT_RANGE_RE.match(f)):
        return False
    return True


def _split_compound(orig):
    """'Black/Red' -> parts; 'Pale-Gray' -> parts; 'SM-S931B/DS' stays whole
    (its head is a code)."""
    if "-" in orig and "/" not in orig:
        hparts = [x for x in orig.split("-") if x]
        if len(hparts) > 1 and all(x.isalpha() for x in hparts) and any(fold(x) in COLOURS for x in hparts):
            return hparts
        # a part number with the colour spelled after it: T520BT-BLACK,
        # DUAL-RTX5070-O12G-WHITE -> the code, then the colour
        k = len(hparts)
        while k > 1 and hparts[k - 1].isalpha() and (fold(hparts[k - 1]) in COLOURS
                                                     or fold(hparts[k - 1]) in COLOUR_MODIFIERS):
            k -= 1
        if 1 < k + 1 <= len(hparts) and k < len(hparts) and _is_strong_code("-".join(hparts[:k])) \
                and fold(hparts[-1]) in COLOURS:
            return ["-".join(hparts[:k])] + hparts[k:]
    if "/" not in orig and "\\" not in orig:
        return [orig]
    parts = [x for x in re.split(r"[/\\]", orig) if x]
    if parts and _is_strong_code(parts[0]) and all(len(re.sub(r"[^0-9A-Za-z]", "", x)) <= 4 for x in parts[1:]) \
            and not any(_UNIT_RE.match(fold(x)) for x in parts[1:]):   # 'R5-7520U/16GB': a CPU, then RAM
        return [orig]
    return parts


def _derive_piece(orig, kind, bwords):
    """Can this title token be one piece of a code written with spaces? A
    number, a short code (XM5, C4) or 2-3 letters that are no ordinary word,
    tier, colour or region ('NC' in 'LIVE 770 NC', 'WH' in 'WH 1000 XM5'); four
    capitals count too ('WNEI 84 APS')."""
    a = alnum(orig)
    if not a or _CYR_RE.search(orig):
        return None
    if kind == "number" and len(a) <= 6:
        return "digits"
    if kind == "weak":
        return "code"
    if kind in ("word", "tier?") and a.isalpha() and (2 <= len(a) <= 3 or (len(a) == 4 and orig.isupper())) \
            and a not in bwords \
            and a not in GENERIC and a not in TIER_WORDS and a not in COLOURS and a not in REGIONS \
            and a not in ACCESSORY_WORDS and a not in _FOR_WORDS \
            and a not in COLOUR_MODIFIERS and a not in ("and", "for", "the", "new", "usb", "led", "ram",
                                                         "ssd", "hdd", "cpu", "gpu", "psu", "mic", "anc",
                                                         "enc", "tws", "rgb", "dac", "amp", "hub", "set"):
        return "letters"
    return None


def _derived_codes(items, kinds, strong, bwords):
    """Concatenations of 2-3 adjacent title tokens that form a model code
    written with spaces -> {code: (first pos, last pos)}. Letters must follow
    the digits ('770 NC', 'WH 1000 XM5'): letters + digits alone ('RTX 5070',
    'RX 7800') name a chip or a series, not one product."""
    out = {}
    for i in range(len(items)):
        for n in (2, 3):
            run = items[i:i + n]
            if len(run) < n or any(f == "\x00unit" for _p, _o, f in run):
                continue
            pieces = [_derive_piece(o, kinds.get(p), bwords) for p, o, _f in run]
            if None in pieces or not ({"digits", "code"} & set(pieces)) or set(pieces) == {"digits"}:
                continue
            code = "".join(alnum(o) for _p, o, _f in run)
            if code in strong or code in out or re.fullmatch(r"[a-z]+\d+", code) or re.fullmatch(r"\d+", code):
                continue
            if _is_strong_code(code) and len(code) <= 16:
                out[code] = (run[0][0], run[-1][0])
    return out


def product_type(words):
    """First product noun among folded words -> type, or None. A noun right
    after 'for' / 'за' names what the item is for ('Боја за печатач' is ink,
    'Маска за телефон' a case), not the item; a 'Компатибилен кертриџ' is one."""
    prev = ""
    for w in words:
        a = re.sub(r"[^0-9a-z]", "", w)
        t = PRODUCT_TYPES.get(a)
        if t and prev not in _FOR_DEVICE_WORDS:
            return t
        prev = a
    return None


def category_type(category):
    """Product type a category path names, nearest segment first
    ('... > Bluetooth слушалки' -> headphones, not the 'Телефони' above it)."""
    if not category:
        return None
    for seg in reversed(re.split(r"\s*(?:>|/|\|)\s*", str(category))):
        t = product_type(fold(seg).split())
        if t:
            return t
    return None


def signature(title, brand=None, ean=None, mpn=None, extra_codes=()):
    s = Sig()
    s.brand = alnum(brand) if brand else ""
    s.ean = norm_gtin(ean)
    s.strong, s.weak, s.numbers, s.tokens = set(), set(), set(), set()
    s.tiers, s.versions, s.colours, s.regions = set(), set(), set(), set()
    s.shades = set()  # (base colour, 'light' | 'dark') from 'Light Blue', 'темно сина'
    s.units = {}
    s.kept = []  # (original token, folded token, kind, position) in title order
    s.seq = []   # (position, alnum token, kind) for kept tokens
    s.pairs = set()  # adjacent word+number joins: "gmmk 2" -> "gmmk2"
    s.codes = {}     # alnum part number -> as written ("910006582" -> "910-006582")
    text = unicodedata.normalize("NFKC", clean_title(title))
    for pat, rep in _PHRASE_FIX:
        text = re.sub(pat, rep, text, flags=re.I)
    s.concat = re.sub(r"[^0-9a-z]", "", fold(text))
    items = []  # (position, original, folded)
    pos = 0
    for raw in raw_tokens(text):
        m = _MULTI_CAP_RE.match(fold(raw))
        if m:  # 8/256GB
            mult = 1000 if m.group(3) == "tb" else 1
            s.units.setdefault("gb", set()).update({int(m.group(1)) * mult, int(m.group(2)) * mult})
            items.append((pos, raw, "\x00unit"))
            pos += 1
            continue
        for part in _split_compound(raw):
            part = part.strip("-.")
            if part:
                items.append((pos, part, fold(part)))
                pos += 1
    folded_words = [f for _i, _o, f in items if f != "\x00unit"]
    word_pos = [p for p, _o, f in items if f != "\x00unit"]
    clean_words = [re.sub(r"[^0-9a-z]", "", w) or w for w in folded_words]
    after_for = [k > 0 and clean_words[k - 1] in _FOR_WORDS for k in range(len(clean_words))]
    s.for_only = ({w for w, f in zip(clean_words, after_for) if f}
                  - {w for w, f in zip(clean_words, after_for) if not f})
    s.accessory = _accessory(clean_words, brand_words(brand) | ({alnum(brand)} if brand else set()))
    # the first 'with' word: what follows is an included part ('w/Microphone')
    ip = _include_pos(clean_words)
    s.incl_pos = word_pos[ip] if ip < len(word_pos) else None
    # A set / combo of several products ("MX Keys S + MX Master 3S") is not
    # the single product, and vice versa.
    # "set"/"kit" count only near the start ("Keyboard and Mouse Set ..."),
    # not in "+Keycap Language Set".
    fw = set(folded_words)
    s.bundle = bool(fw & {"combo", "bundle", "komplet", "kombo", "paket"}) or bool(
        {"set", "kit"} & set(folded_words[:5]))
    kinds = {}
    next_fold = {items[j][0]: items[j + 1][2] for j in range(len(items) - 1)}
    for i, orig, f in items:
        if f == "\x00unit":
            kinds[i] = "unit"
        elif _is_strong_code(orig):
            kinds[i] = "strong"
        elif f in COLOUR_MODIFIERS and next_fold.get(i) in COLOURS:
            kinds[i] = "colourmod"   # 'Forest' in 'Forest Gray'
        elif f in COLOURS:
            kinds[i] = "colour"
        elif _UNIT_RE.match(f) or (_KIT_RE.match(f) and _KIT_RE.match(f).group(3)) or _PERCENT_RE.match(f):
            kinds[i] = "unit"
        elif f in REGIONS:
            kinds[i] = "region"
        elif _VERSION_RE.match(f):
            kinds[i] = "version"
        elif f in TIER_WORDS:
            kinds[i] = "tier?"
        elif _YEAR_RE.match(f) or _YEAR_RANGE_RE.match(f):
            kinds[i] = "year"
        elif _is_weak_code(f):
            kinds[i] = "weak"
        elif f.isdigit():
            kinds[i] = "number" if len(f) <= 5 else "skip"
        else:
            kinds[i] = "word"
    # 'Core' / 'Ultra' / 'Plus' in a CPU name ('Intel Core i7', 'Core Ultra 7',
    # 'Snapdragon X Plus') are no model tiers
    for j, (i, _orig, f) in enumerate(items):
        nf = items[j + 1][2] if j + 1 < len(items) else ""
        pf = items[j - 1][2] if j else ""
        if kinds.get(i) == "tier?" and (
                (f == "core" and (re.fullmatch(r"i[3579]|ultra|[3579](-\d{3}[a-z]{0,2})?|tm", nf) or pf == "intel"))
                or (f == "ultra" and (pf == "core" or re.fullmatch(r"[579]", nf)))
                or (f in ("plus", "elite") and pf == "x" and j >= 2 and items[j - 2][2] == "snapdragon")):
            kinds[i] = "skip"
    anchors = [i for i, k in kinds.items() if k in ("strong", "weak", "number", "version")]
    latin_count = 0
    cyr_tokens = []
    for i, orig, f in items:
        k = kinds[i]
        if k == "tier?":
            if f in ("super", "ti", "oc"):
                k = "tier" if any(0 < i - j <= 2 for j in anchors) else "word"
            else:
                k = "tier" if any(abs(i - j) <= 2 for j in anchors) else "word"
        if k == "unit":
            if f == "\x00unit":
                continue
            mu = _UNIT_RE.match(f)
            if mu:
                num = float(mu.group(1).replace(",", "."))
                unit = UNIT_ALIASES[mu.group(2)]
                if mu.group(2) == "tb":
                    num *= 1000
                s.units.setdefault(unit, set()).add(int(num) if num == int(num) else num)
            elif _PERCENT_RE.match(f):
                s.units.setdefault("pct", set()).add(f)
            else:
                s.units.setdefault("kit", set()).add(f)
        elif k == "strong":
            s.strong.add(alnum(orig))
            s.codes.setdefault(alnum(orig), orig.upper())
            s.kept.append((orig, f, "strong", i))
            s.seq.append((i, alnum(orig), "strong"))
            latin_count += 1
        elif k == "colour":
            s.colours.add(COLOURS[f])
        elif k == "colourmod":
            if f in _SHADES:
                s.shades.add((COLOURS[next_fold[i]], _SHADES[f]))
            continue
        elif k == "region":
            s.regions.add(_REGION_ALIAS.get(f, f))
        elif k == "version":
            s.versions.add(f)
            s.kept.append((orig, f, "version", i))
            s.seq.append((i, alnum(f), "version"))
        elif k == "tier":
            s.tiers.add(f)
            s.kept.append((orig, f, "tier", i))
            s.seq.append((i, f, "tier"))
        elif k == "weak":
            a = alnum(f)
            s.weak.add(a)
            s.tokens.add(a)
            s.kept.append((orig, f, "weak", i))
            s.seq.append((i, a, "weak"))
            latin_count += 1
        elif k == "number":
            n = f.lstrip("0") or "0"
            s.numbers.add(n)
            s.tokens.add(n)
            s.kept.append((orig, f, "number", i))
            s.seq.append((i, n, "number"))
        elif k == "word":
            if f in GENERIC or _TECH_RE.match(f):
                continue
            a = re.sub(r"[^0-9a-z]", "", f)
            if len(a) < 2 or a in GENERIC:
                continue
            if _CYR_RE.search(orig):
                cyr_tokens.append((orig, a, i))
                continue
            s.tokens.add(a)
            s.kept.append((orig, f, "word", i))
            s.seq.append((i, a, "word"))
            latin_count += 1
    if latin_count < 2:
        for orig, a, i in cyr_tokens:
            s.tokens.add(a)
            s.kept.append((orig, a, "word", i))
    s.seq.sort()
    for (p1, t1, k1), (p2, t2, k2) in zip(s.seq, s.seq[1:]):
        if p2 == p1 + 1 and k2 in ("number", "weak") and k1 in ("word", "weak"):
            s.pairs.add(t1 + t2)
    for code in [mpn, *extra_codes]:
        if code and _is_strong_code(str(code)):
            s.strong.add(alnum(code))
            s.codes.setdefault(alnum(code), str(code).upper())
    # 'WH1000XM5L.CE7', 'SM-S931B/DS': the base code is what most shops print
    # (Anhoch lists 'WH-1000XM5L'), so it is a code of this title too.
    s.pkg_base = {}
    for c in sorted(s.strong):
        mt = _PKG_SPLIT_RE.match(s.codes.get(c, ""))
        if mt and mt.group(2).lower() not in TIER_WORDS and not _UNIT_RE.match(mt.group(2).lower()) \
                and _is_strong_code(mt.group(1)) and alnum(mt.group(1)) != c:
            b = alnum(mt.group(1))
            s.strong.add(b)
            s.codes.setdefault(b, mt.group(1).upper())
            s.pkg_base[b] = c
    # A model suffix written apart from its code: 'Comica CVM-V01SP UC' is the
    # USB-C model of CVM-V01SP (two to three capitals that are no ordinary word,
    # spec, colour, tier or region).
    s.code_suffix = {}
    for c in s.strong:
        w = s.codes.get(c)
        mc = re.search(re.escape(w), text, re.I) if w else None
        ms = re.match(r"[ \t]+([A-Z]{2,3})(?![\w-])(?![ \t]*\d)", text[mc.end():]) if mc else None  # not 'RTX 5070'
        if ms:
            x = ms.group(1).lower()
            if x not in GENERIC and x not in _NOT_COLOUR and x not in REGIONS and x not in COLOURS \
                    and x not in TIER_WORDS and x not in COLOUR_MODIFIERS and x not in _SPEC_ABBR \
                    and not _TECH_RE.match(x) and x not in brand_words(brand):
                s.code_suffix[c] = x
    s.glued = []
    for mt in _SPACED_TV_CODE_RE.finditer(text):
        code = "".join(g for g in mt.groups() if g)
        if _is_strong_code(code):  # an extra code: the spaced tokens stay as they are
            s.strong.add(alnum(code))
            s.codes.setdefault(alnum(code), code.upper())
            s.glued.append(alnum(code))
    if "inch" not in s.units and (s.brand == "samsung" or "samsung" in s.concat):
        # the size the code states, so a 43" sibling differs and the 55" does not
        sizes = {int(mt.group(1)) for mt in map(_SAMSUNG_TV_CODE_RE.match, s.strong) if mt}
        if len(sizes) == 1:
            s.units["inch"] = sizes
    # a shop's own name is no model code ('... Gaming PC GJIRAFA50')
    for c in [c for c in s.strong if c in _STORE_WORDS]:
        s.strong.discard(c)
        s.codes.pop(c, None)
    comp = {c for c in s.strong if _CPU_CODE_RE.match(c) or _GPU_CODE_RE.match(c)}
    cw = set(clean_words)
    ftext = " " + re.sub(r"[^0-9a-z]+", " ", fold(text)) + " "
    s.system = bool(cw & SYSTEM_WORDS or "pc" in cw or cw & LAPTOP_SERIES
                    or (any(_CPU_CODE_RE.match(c) for c in comp)
                        and (re.search(r"(rtx|gtx|rx)\d{4}", s.concat) or s.units.get("gb")))
                    or (_CPU_PHRASE_RE.search(ftext) and s.units.get("gb")))
    if comp and s.system:
        for c in comp:
            s.strong.discard(c)
            s.codes.pop(c, None)
    bwords = brand_words(brand) | ({s.brand} if s.brand else set())
    s.derived = _derived_codes(items, kinds, s.strong, bwords)
    if not s.colours:
        # a vendor colour suffix on a part number: LIVE770NCBLK -> black
        for c in s.strong:
            m = re.search(r"\d[a-z]*?(" + "|".join(_CODE_COLOUR_SUFFIX) + r")$", c)
            if m and len(c) >= 8:
                s.colours.add(_CODE_COLOUR_SUFFIX[m.group(1)])
    # The title's last word(s) when they name no known colour but could be
    # one: after the model code / number, Latin letters, no product, spec,
    # tier, region, edition or technology word, or packaging tail written
    # apart ('UXXH', 'QLED')
    # ('Headphones JBL Live 770NC ANC Wireless Sand' when 'sand' is not in
    # the vocabulary).
    s.maybe_colour = set()
    if not s.colours and 2 < len(items) <= 14:   # a long marketing title's last words are rarely a colour
        anchors_pos = [i for i, k in kinds.items() if k in ("strong", "weak", "number")]
        in_code = {p for a, b in s.derived.values() for p in range(a, b + 1)}
        tail = []
        for i, orig, f in reversed(items):
            k = kinds.get(i)
            if (k == "word" and i not in in_code and not _CYR_RE.search(orig) and re.fullmatch(r"[a-z]{3,12}", f)
                    and f not in GENERIC and f not in _NOT_COLOUR and f not in _FOR_WORDS
                    and f not in bwords and f not in ACCESSORY_WORDS and f not in PRODUCT_TYPES
                    and f not in COLOUR_MODIFIERS and not _PKG_TAIL_RE.match(f)   # 'UE 55 U8072H UXXH'
                    and not _TECH_RE.match(f)                                     # '..., 4K QLED'
                    and anchors_pos and i > min(anchors_pos)):
                tail.append(f)
                if len(tail) == 2:
                    break
                continue
            break
        s.maybe_colour = set(tail)
    s.colour_src = "title" if s.colours else None
    # the product noun outside 'with ...' ('... w/Microphone' names a part)
    s.ptype = s.tptype = product_type(folded_words[:_include_pos(clean_words)])
    phone = s.tptype in ("phone", "tablet") or bool(cw & _PHONE_HINTS) or (
        "galaxy" in cw and not cw & {"buds", "watch", "fit", "book", "ring"})
    _config(text, s, computer=s.system and not s.accessory, phone=phone and not s.accessory,
            cpu=bool(_CPU_PHRASE_RE.search(ftext)) and not s.accessory)
    if s.brand:
        s.tokens.add(s.brand)
    return s


def model_phrase(sig, brand_words=()):
    """The run of title tokens that names the model, without the brand: the
    first model token (weak code / version, else a number), up to two words
    directly before it, and directly following tiers / versions / numbers.
    'Logitech MX Master 3S' -> MX Master 3S; 'Apple iPhone 16 Pro Max 256GB'
    -> iPhone 16 Pro Max. Tokens are contiguous in the title, so the phrase
    also works on shops whose search matches an ordered phrase. -> list of
    kept tuples (orig, folded, kind, pos); falls back to the first three
    contiguous words ('MX Keys Mini')."""
    words = [w for w in sig.kept if w[2] in ("word", "weak", "number", "tier", "version")
             and alnum(w[1]) not in brand_words]
    trig = next((i for i, w in enumerate(words) if w[2] in ("weak", "version")), None)
    num = next((i for i, w in enumerate(words) if w[2] == "number"), None)
    if num is not None and (trig is None or num < trig):
        trig = num
    if trig is None:
        out = []
        for w in words:
            if w[2] != "word":
                continue
            if out and w[3] != out[-1][3] + 1:
                break
            out.append(w)
            if len(out) == 3:
                break
        return out
    out = [words[trig]]
    j = trig - 1
    while j >= 0 and len(out) < 3 and words[j][3] == out[0][3] - 1 and words[j][2] != "version":
        out.insert(0, words[j])
        j -= 1
    j = trig + 1
    while j < len(words) and len(out) < 6 and words[j][3] == out[-1][3] + 1:
        kj, fj = words[j][2], words[j][1]
        if kj == "tier" or (kj == "version" and not re.match(r"^(gen|rev)", fj)) \
                or (kj in ("number", "weak") and out[-1][2] == "number"):
            out.append(words[j])
            j += 1
        else:
            break
    return out


def _phrase_anchored(phrase, sig):
    """Does the phrase name a model? A code or version always does; a bare
    number only with a word next to it ('MX Master 4', 'Origins 2 1800'),
    or when the title has no part number ('61' keys next to GK-916 does not)."""
    if any(w[2] in ("weak", "version") for w in phrase):
        return True
    if any(w[2] == "number" for w in phrase):
        return not sig.strong or any(w[2] == "word" for w in phrase)
    return False


def brand_run_before(sig, pos, bwords):
    """Original text of brand tokens ending right before title position pos
    ('Royal Kludge' before 'R65'), or None."""
    by_pos = {w[3]: w for w in sig.kept if alnum(w[1]) in bwords}
    run, p = [], pos - 1
    while p in by_pos:
        run.insert(0, by_pos[p][0])
        p -= 1
    return " ".join(run) or None


def has_tok(t, sig):
    """Token t (alnum) appears in sig: as a token, a code, or (for t of 4+
    chars, or 3 letters) anywhere in the squashed title, so 'G502X' finds
    'G502 X' and 'TUF' finds 'TUF-RTX4070TIS'."""
    return t in sig.tokens or t in sig.strong or (
        (len(t) >= 4 or (len(t) == 3 and t.isalpha())) and t in sig.concat)


_COLOUR_ATTR_RE = re.compile(r"^(боја|boja|colou?r|ngjyr|farbe)", re.I)


def attr_colours(r):
    """Colours named by the record's attributes ('Боја': 'Црна' on Setec)."""
    attrs = r.get("attributes") if isinstance(r, dict) else None
    out = set()
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            if isinstance(v, str) and _COLOUR_ATTR_RE.search(str(k).strip()):
                for w in re.split(r"[^0-9a-z]+", fold(v)):
                    if w in COLOURS:
                        out.add(COLOURS[w])
    return out


def enrich_sig(s, r):
    """Fill what a title leaves out from the record: the colour its attributes
    name, the product type its category names."""
    if not s.colours:
        ac = attr_colours(r)
        if ac:
            s.colours = ac
            s.colour_src = "attr"   # a seller's free text: a vendor colour code outranks it
            s.maybe_colour = set()
    if not s.ptype:
        s.ptype = category_type(r.get("category"))
    return s


def record_sig(r):
    extra = []
    if r.get("store") in SKU_IS_MPN and r.get("sku"):
        extra.append(r["sku"])
    return enrich_sig(signature(r.get("title"), r.get("brand"), r.get("ean"), r.get("mpn"), extra), r)


def brand_words(brand):
    """alnum forms of the brand and its words ('Royal Kludge' -> royalkludge,
    royal, kludge); empty when unknown."""
    if not brand:
        return set()
    out = {alnum(brand)}
    out.update(alnum(w) for w in re.split(r"[\s.\-&]+", str(brand)) if len(alnum(w)) >= 2)
    out.discard("")
    return out


def match_key(r, sig=None):
    """EAN where the record has one, else brand + model tokens + the variant
    dimensions (capacity, size, tier, version, colour), so variants never share a key.
    'ean:0196188046074', 'model:samsung:galaxy+s25:8+128gb:navy',
    'model:logitech:mx+master+3s:graphite'."""
    e = norm_gtin(r.get("ean"))
    if e:
        return "ean:" + e
    return model_key(r, sig)


# Common brands (alnum, folded), used only to key records whose shop gives no
# brand: the first title token that names a brand keys the record, so
# 'Maus wireless Logitech MX Master 3S' and 'Disk SSD Kingston SNV3S 1TB' key
# under logitech / kingston rather than under their first word. annotate()
# adds every brand the records of the same run file.
KNOWN_BRANDS = set("""
apple samsung xiaomi redmi poco huawei honor oppo realme oneplus motorola nokia google
sony lg philips panasonic tcl hisense toshiba sharp grundig vivax tesla fox aiwa haier
bosch siemens gorenje beko whirlpool electrolux aeg candy hoover indesit midea miele
zanussi ariston hotpoint neff smeg liebherr sencor rowenta tefal braun remington delonghi
krups nespresso dyson karcher severin moulinex russellhobbs gree daikin mitsubishi
logitech razer steelseries corsair hyperx kingston crucial wd westerndigital seagate
sandisk lexar adata transcend intenso verbatim patriot teamgroup samsung msi asus acer
lenovo hp dell gigabyte asrock nvidia amd intel bequiet coolermaster nzxt fractal lianli
thermaltake deepcool arctic noctua zalman montech jbl marshall bose sennheiser jabra
anker baseus ugreen tplink mercusys dlink tenda ubiquiti netgear dji gopro canon nikon
fujifilm epson brother kyocera nintendo microsoft xbox aoc benq viewsonic iiyama trust
genius a4tech redragon whiteshark marvo fantech hama spacer canyon defender sven
playstation garmin amazfit fitbit lenovo hisense jvc thomson skyworth kiano
""".split())


def infer_brand(sig, vocab=None):
    """First title token (or adjacent pair: 'be quiet', 'Cooler Master') that
    names a brand -> (brand key, its words), or ('', set())."""
    vocab = KNOWN_BRANDS | set(vocab or ())
    words = [(alnum(w[1]), w[3]) for w in sig.kept if w[2] in ("word", "weak")]
    for i, (a, pos) in enumerate(words):
        if i + 1 < len(words) and words[i + 1][1] == pos + 1 and a + words[i + 1][0] in vocab:
            return a + words[i + 1][0], {a, words[i + 1][0], a + words[i + 1][0]}
        if a in vocab and len(a) >= 2:
            return a, {a}
    return "", set()


def model_key(r, sig=None, brand_vocab=None):
    """brand + model phrase (or part number) + variant dimensions, ignoring
    the EAN. Bridges listings with and without an EAN."""
    s = sig or record_sig(r)
    brand = _brand_key(r.get("brand")) or ""
    bw = brand_words(r.get("brand"))
    if not brand:
        brand, bw = infer_brand(s, brand_vocab)
    if not brand:
        fw = _first_word(s)
        brand = fw or ""
        bw = {fw} if fw else set()
    # The model phrase is what every shop writes the same way (Galaxy A56,
    # MX Master 3S); part numbers often carry shop-specific suffixes, so they
    # key only when the title has no phrase anchored by a model token.
    phrase = model_phrase(s, bw)
    if s.glued:  # 'UE 55 U8072H UXXH' keys as UE55U8072HUXXH does
        model = s.glued[:1]
    elif _phrase_anchored(phrase, s):
        model = [alnum(w[1]) for w in phrase if w[2] != "tier"]
    elif s.strong:  # 'WH1000XM5L.CE7' keys as its base code, as other shops print it
        model = sorted(s.strong - set((s.pkg_base or {}).values()))[:2]
    else:
        model = [alnum(w[1]) for w in phrase if w[2] != "tier"]
    if not model:
        return None
    parts = ["+".join(model)]
    if "gb" in s.units:
        parts.append("+".join(str(x) for x in sorted(s.units["gb"])) + "gb")
    # Screen size: The Frame 55" vs 65" share every other token. Skipped when
    # a part number or the model already fixes it ('QE55Q7FAAUXXH', 'QE 55
    # Q7F', 'P2425H'), so a shop that spells out 55" and one that does not
    # key the same product alike.
    inches = [] if any(t in s.strong for t in model) else sorted(
        x for x in s.units.get("inch", ()) if not any(str(x) in t for t in model if re.search(r"\d", t)))
    if inches:
        parts.append("+".join(str(x) for x in inches) + "in")
    if s.tiers:
        parts.append("+".join(sorted(s.tiers)))
    vers = sorted(v for v in s.versions if v not in model)
    if vers:
        parts.append("+".join(vers))
    if s.colours:
        parts.append("+".join(sorted(s.colours)))
    if s.accessory:
        parts.append("accessory-" + s.accessory)
    if s.bundle:
        parts.append("bundle")
    return "model:" + brand + ":" + ":".join(parts)


def _brand_key(brand):
    """Brand for keys: 'LOGITECH G' -> logitech, 'G.Skill' -> gskill."""
    if not brand:
        return ""
    words = [w for w in re.split(r"[\s]+", str(brand).strip()) if w]
    while len(words) > 1 and len(alnum(words[-1])) <= 2:
        words.pop()
    return alnum(" ".join(words))


def _first_word(sig):
    for orig, f, kind, _pos in sig.kept:
        if kind == "word":
            return alnum(f)
    return None


# --------------------------------------------------------------------------
# Running store clients
# --------------------------------------------------------------------------
_live = set()
_live_lock = threading.Lock()


# Each client runs in its own process group, so a terminal Ctrl-C reaches mkshop alone and mkshop
# stops the clients itself. POSIX has process groups and signals; Windows has neither, so there a
# client gets a new console process group (Ctrl-C does not reach it) and is stopped with
# TerminateProcess. Clients start no processes of their own, so ending the client is enough.
_PROCESS_GROUPS = hasattr(os, "killpg")
_SPAWN_GROUP = ({"start_new_session": True} if _PROCESS_GROUPS
                else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)})


def _kill(p):
    def stop(sig):
        try:
            if _PROCESS_GROUPS:
                os.killpg(p.pid, sig)
            else:
                p.kill()   # TerminateProcess on Windows
        except (ProcessLookupError, PermissionError, OSError):
            pass
    stop(signal.SIGTERM)
    try:
        p.wait(3)
    except subprocess.TimeoutExpired:
        stop(getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            pass


def _kill_all():
    with _live_lock:
        procs = list(_live)
    for p in procs:
        _kill(p)


atexit.register(_kill_all)

_MARKER_RE = re.compile(r"(?i)\bwarn(ing)?\b")


def is_warning_line(key, ln):
    """A client stderr line that affects results (truncation, OR fallback,
    partial data), as opposed to progress: unindented and marked WARNING."""
    if not ln or ln.lstrip().startswith(("Traceback", "BLOCKED")):
        return False
    return not ln[:1].isspace() and bool(_MARKER_RE.search(ln))


class Ctx:
    def __init__(self, stores_dir, python, keep_tmp=False):
        self.stores_dir = stores_dir
        self.python = python
        self.tmp = tempfile.mkdtemp(prefix="mkshop-")
        self.keep_tmp = keep_tmp
        self._n = 0
        self._n_lock = threading.Lock()
        self._info = {}
        self._info_lock = threading.Lock()
        self._site_after = set()  # stores whose client wants --site after the subcommand
        if not keep_tmp:
            atexit.register(shutil.rmtree, self.tmp, True)

    def tmpfile(self, key, suffix=".json"):
        with self._n_lock:
            self._n += 1
            n = self._n
        return os.path.join(self.tmp, f"{key}-{n}{suffix}")

    def script(self, key):
        return os.path.join(self.stores_dir, REGISTRY[key]["script"])

    def run(self, key, argv, timeout, want_json=True, site_after=False):
        """Run one client command. Returns a dict with status, data, message,
        warnings, exit_code, elapsed_s, stderr_tail."""
        st = REGISTRY[key]
        script = self.script(key)
        res = {"status": "error", "data": None, "message": None, "warnings": [],
               "exit_code": None, "elapsed_s": 0.0, "stderr_tail": []}
        if not os.path.isfile(script):
            res["message"] = f"client not found: {script}"
            return res
        if timeout is not None and timeout <= 0:
            res.update(status="timeout", message="no time left before the shop's deadline")
            return res
        out_path = self.tmpfile(key) if want_json else None
        site = list(st["args"])
        site_after = site_after or key in self._site_after
        cmd = [self.python, script]
        if site and not site_after:
            cmd += site
        cmd += list(argv)
        if site and site_after:
            cmd += site
        if want_json:
            cmd += ["--json", out_path]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   PYTHONDONTWRITEBYTECODE="1", MKSHOP="1")
        t0 = time.monotonic()
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            try:
                p = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                                     cwd=self.stores_dir, env=env, **_SPAWN_GROUP)
            except OSError as e:
                res["message"] = f"cannot start client: {e}"
                return res
            with _live_lock:
                _live.add(p)
            timed_out = False
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(p)
                rc = p.returncode
            finally:
                with _live_lock:
                    _live.discard(p)
            res["elapsed_s"] = round(time.monotonic() - t0, 2)
            fe.seek(0, os.SEEK_END)
            size = fe.tell()
            fe.seek(max(0, size - 65536))
            err = fe.read().decode("utf-8", "replace")
            fo.seek(0, os.SEEK_END)
            osize = fo.tell()
            fo.seek(0)
            stdout = fo.read(min(osize, 4 << 20)).decode("utf-8", "replace")
        lines = [ln.rstrip() for ln in err.splitlines() if ln.strip()]
        res["stderr_tail"] = lines[-15:]
        res["exit_code"] = rc
        res["warnings"] = [ln.strip() for ln in lines if is_warning_line(key, ln)][-8:]
        # gjirafa.py takes --site before the subcommand; tolerate a client
        # that only accepts it after.
        if (not timed_out and rc == 2 and site and not site_after
                and re.search(r"unrecognized arguments:.*--site", err)
                and "usage:" in err):
            retry = self.run(key, argv, None if timeout is None else max(1, timeout - res["elapsed_s"]),
                             want_json, site_after=True)
            if retry["exit_code"] != 2 or "--site" not in " ".join(retry["stderr_tail"]):
                self._site_after.add(key)
            return retry
        if timed_out:
            res["status"] = "timeout"
            res["message"] = f"no answer within {timeout:.0f}s (killed)"
            if want_json and out_path and os.path.exists(out_path):
                res["data"] = _load_json(out_path)
            return res
        if rc == 0:
            if want_json:
                data = _load_json(out_path) if os.path.exists(out_path) else None
                if data is None:
                    res["message"] = ("client exited 0 but its --json file is missing or not valid JSON"
                                      + (f" (last stderr: {lines[-1][:160]})" if lines else ""))
                    return res
                res["data"] = data
            else:
                res["data"] = stdout
            res["status"] = "ok"
            return res
        if want_json and out_path and os.path.exists(out_path):
            res["data"] = _load_json(out_path)  # e.g. detail's per-item errors on exit 2
        blocked = next((ln for ln in lines if ln.lstrip().startswith("BLOCKED")), None)
        if rc == 3:
            res["status"] = "blocked"
            res["message"] = blocked or (lines[-1] if lines else "exit 3 (blocked)")
        elif rc == 2:
            res["status"] = "not_found"
            msg = [ln for ln in lines if not ln.startswith("usage:") and not ln.startswith("  ")]
            res["message"] = (msg[-1] if msg else "exit 2 (bad usage or not found)")
        elif rc is not None and rc < 0:
            res["message"] = f"killed by signal {-rc}"
        else:
            res["status"] = "error"
            tb = [ln for ln in lines if not ln.startswith(" ")]
            res["message"] = f"exit {rc}: " + (tb[-1] if tb else "no stderr output")
        if blocked and res["status"] != "blocked":
            res["status"] = "blocked"
            res["message"] = blocked
        return res

    def info(self, key):
        with self._info_lock:
            if key in self._info:
                return self._info[key]
        r = self.run(key, ["info"], INFO_TIMEOUT, want_json=False)
        info = None
        if r["status"] == "ok":
            txt = r["data"] or ""
            try:
                info = json.loads(txt)
            except ValueError:
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    try:
                        info = json.loads(m.group(0))
                    except ValueError:
                        info = None
            if not isinstance(info, dict):
                info = None
                r["status"] = "error"
                r["message"] = "info did not print a JSON object"
        val = {"info": info, "status": r["status"], "message": r["message"]}
        with self._info_lock:
            self._info[key] = val
        return val

    def infos(self, keys):
        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as ex:
            list(ex.map(self.info, keys))
        return {k: self._info[k] for k in keys}

    def capabilities(self, key):
        i = self.info(key)["info"] or {}
        caps = i.get("capabilities")
        return set(caps) if isinstance(caps, list) else None

    def ean_searchable(self, key):
        caps = self.capabilities(key)
        if caps and ({"search_ean", "search_codes"} & caps):
            return True
        return REGISTRY[key]["ean_search"]

    def ean_in_detail(self, key):
        caps = self.capabilities(key)
        if caps is not None:
            return "ean_in_detail" in caps
        return bool(REGISTRY[key].get("ean_detail"))


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def as_records(data):
    """Clients write a JSON list; tolerate {"results": [...]} or a single object."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("results", "records", "items", "products"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
        return [data]
    return []


# Shop text is third-party data. Invisible characters can hide text from a reader but not from
# a model: C0/C1 controls (tab, newline and carriage return kept), zero-width and bidi
# controls, the BOM, the soft hyphen, and the Unicode tag block (U+E0000-E007F) that can carry
# hidden instructions. Some shops also leave HTML tags in titles ('Logitech G435, сина<br />').
# Variation selectors (U+FE0F kept for emoji) and Hangul fillers also render as nothing.
_INVISIBLE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u061c\u115f\u1160\u180e"
                           "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\u3164\ufe00-\ufe0e"
                           "\ufeff\uffa0\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
# Only real markup tags: '<IPS 144Hz>' or 'Adapter <HDMI> to <VGA>' in a title is text.
_TAG_RE = re.compile(r"</?(?:a|b|br|div|em|font|h[1-6]|hr|i|img|li|ol|p|small|span|strong|sub|sup|u|ul)"
                     r"(?:\s[^<>]*)?/?>", re.I)


def _scrub(v, depth=0):
    """Invisible characters out of every string, dict key and nested value."""
    if isinstance(v, str):
        return _INVISIBLE_RE.sub("", v) if _INVISIBLE_RE.search(v) else v
    if depth < 8 and isinstance(v, dict):
        return {_scrub(k, 8): _scrub(x, depth + 1) for k, x in v.items()}
    if depth < 8 and isinstance(v, list):
        return [_scrub(x, depth + 1) for x in v]
    return v


def clean_record(r, key):
    t = r.get("title")
    if isinstance(t, str) and ("&" in t or '\\"' in t or "\\'" in t):
        # Gjirafa titles carry '55&quot;' and '55\\"': show them as the page does. Unescape before
        # scrubbing, so '&#x202E;' or '&lt;br /&gt;' cannot bring back what the scrub removes.
        r["title"] = clean_title(t)
    for f, v in list(r.items()):
        if isinstance(v, (str, dict, list)) and f not in ("found_by", "sources"):
            r[f] = _scrub(v)
    t = r.get("title")
    if isinstance(t, str) and "<" in t and _TAG_RE.search(t):
        r["title"] = " ".join(_TAG_RE.sub(" ", t).split())
    r.setdefault("store", key)
    if r.get("store") != key and key in ("gjirafa50", "zirafamall"):
        r["store"] = key
    for f in ("price_mkd", "regular_price_mkd", "member_price_mkd", "shipping_mkd"):
        if f in r and r[f] is not None and not isinstance(r[f], int):
            r[f] = to_int(r[f])
    if r.get("id") is not None and not isinstance(r["id"], str):
        r["id"] = str(r["id"])
    if not r.get("error"):
        r["effective_price_mkd"] = effective_price(r)
        r["price_condition"] = member_condition(r) if uses_member_price(r) else None
        known, until = effective_valid_until(r)
        if known:
            r["effective_price_valid_until"] = until
        else:
            r.pop("effective_price_valid_until", None)
    return r


# --------------------------------------------------------------------------
# Member prices
# --------------------------------------------------------------------------
# A client may add member_price_mkd: a lower price that needs the shop's
# loyalty card or membership, while price_mkd stays the price without it. By
# default the lower of the two is the record's effective price: it drives
# sorting, --min-price/--max-price and the PRICE column, and is always shown
# with its condition. --no-member-prices turns this off (price_mkd only).
MEMBER_PRICES = True


def _member_lower(r):
    m, p = r.get("member_price_mkd"), r.get("price_mkd")
    return isinstance(m, int) and isinstance(p, int) and 0 < m < p


def uses_member_price(r):
    return MEMBER_PRICES and _member_lower(r)


def effective_price(r):
    """The price mkshop ranks, filters and shows first."""
    return r["member_price_mkd"] if uses_member_price(r) else r.get("price_mkd")


def member_condition(r):
    """What the member price needs, in the client's words: its
    member_price_condition, else its member_price_name (on the record or in
    extra), else a generic 'loyalty card or membership'."""
    ex = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    for f in ("member_price_condition", "member_price_name"):
        for src in (r, ex):
            v = src.get(f)
            if isinstance(v, str) and v.strip():
                v = re.sub(r"\s+", " ", v).strip()
                return v if f == "member_price_condition" else f"{v} loyalty card or membership"
    return "loyalty card or membership"


# --------------------------------------------------------------------------
# Price validity windows
# --------------------------------------------------------------------------
# A client may add price_valid_until (for price_mkd) and
# member_price_valid_until (for member_price_mkd): an ISO 8601 date-time with
# the Skopje offset at which that price ends per the shop (a past date is kept
# while the price is still charged), null = standing price, key absent = the
# record cannot tell. mkshop derives effective_price_valid_until for the
# effective price, keeping the three states: the string, null, or "unknown"
# when only the other price's window is known; the key is left out when the
# record has neither field, so such records stay exactly as before.
VALID_FIELDS = ("price_valid_until", "member_price_valid_until")
VALID_MARK_DAYS = 7     # tables mark ends this close (or past); JSON/CSV/detail always carry them


def effective_valid_until(r):
    """-> (known, value): the window of the effective price; known False when
    the record has no validity field at all."""
    if not any(f in r for f in VALID_FIELDS):
        return False, None
    src = "member_price_valid_until" if uses_member_price(r) else "price_valid_until"
    return True, (r[src] if src in r else "unknown")


def parse_until(v):
    """ISO 8601 string -> datetime (aware when it has an offset), else None."""
    if not isinstance(v, str) or not re.match(r"^\s*\d{4}-\d{2}-\d{2}", v):
        return None
    t = v.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(t)
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
        try:
            return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59)
        except ValueError:
            return None


def _now_like(dt):
    return _dt.datetime.now(dt.tzinfo) if dt.tzinfo else _dt.datetime.now()


def until_info(r):
    """-> (state, datetime) for the effective price's end: state 'past',
    'soon' (within VALID_MARK_DAYS) or 'later'; None when no end is known."""
    v = r.get("effective_price_valid_until")
    if v is None and "effective_price_valid_until" not in r:
        v = effective_valid_until(r)[1]
    dt = parse_until(v)
    if not dt:
        return None
    now = _now_like(dt)
    if dt < now:
        return "past", dt
    return ("soon" if dt - now <= _dt.timedelta(days=VALID_MARK_DAYS) else "later"), dt


def fmt_day(dt, with_year=False):
    now = _now_like(dt)
    return dt.strftime("%d.%m.%Y" if (with_year or dt.year != now.year) else "%d.%m")


def until_text(r, always=False):
    """'until 04.10' / 'ended 02.10' for the effective price, or '' (only
    within VALID_MARK_DAYS or past unless always)."""
    ui = until_info(r)
    if not ui or (ui[0] == "later" and not always):
        return ""
    return ("ended " if ui[0] == "past" else "until ") + fmt_day(ui[1])


def fmt_when(dt):
    """'04.10.2026 23:59' (the time only when it is not midnight)."""
    return dt.strftime("%d.%m.%Y" + (" %H:%M" if (dt.hour, dt.minute) != (0, 0) else ""))


def validity_line(r):
    """'price ... until 04.10.2026 23:59; member price ... standing' for detail,
    or None when no end date is known (null = standing, absent = not shown)."""
    parts = []
    for f, label, price in (("price_valid_until", "price", r.get("price_mkd")),
                            ("member_price_valid_until", "member price", r.get("member_price_mkd"))):
        if f not in r or (f == "member_price_valid_until" and not _member_lower(r)):
            continue
        v = r[f]
        dt = parse_until(v)
        if dt:
            past = dt < _now_like(dt)
            parts.append(f"{label} {fmt_price(price)} {'ended' if past else 'until'} {fmt_when(dt)}"
                         + (" (date passed, price still shown)" if past else ""))
        elif v is None:
            parts.append(f"{label} {fmt_price(price)} standing (no end date)")
    if not any(" until " in p or " ended " in p for p in parts):
        return None
    return "valid: " + "; ".join(parts)


def until_summary(r):
    """'; that price ends 04.10 (in 1 day)' for a match / group summary."""
    ui = until_info(r)
    if not ui:
        return ""
    state, dt = ui
    if state == "past":
        return f"; its end date {fmt_day(dt)} has passed (re-check the price)"
    days = (dt.date() - _now_like(dt).date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return f"; that price ends {fmt_day(dt)} ({when})"


def valid_footnote(rows):
    """The line under a table that explains 'until' / 'ended' markers."""
    if not any(until_text(r) for r in rows):
        return None
    return (f"VALID: the shop's end date for that price (marked when within {VALID_MARK_DAYS} days; 'ended' = the "
            "date has passed but the price was still shown); a '*' row's date is the member price's. JSON/CSV: "
            "effective_price_valid_until (null = standing price, 'unknown' = not exposed)")


def price_text(r, was=False):
    """Price for prose lines: the effective price first, with the member
    price's condition and the price without it when the two differ."""
    p = r.get("price_mkd")
    base = f"{fmt_price(p)} MKD" if p is not None else "price n/a"
    if was and (r.get("regular_price_mkd") or 0) > (p or 0):
        base += f" (was {fmt_price(r['regular_price_mkd'])})"
    if uses_member_price(r):
        return f"{fmt_price(r['member_price_mkd'])} MKD member price ({member_condition(r)}); without it {base}"
    if _member_lower(r):
        base += f"; member price {fmt_price(r['member_price_mkd'])} ({member_condition(r)})"
    return base


def rec_id(r):
    return str(r.get("id") or r.get("url") or r.get("title") or id(r))


# --------------------------------------------------------------------------
# Store selection and annotation
# --------------------------------------------------------------------------
def parse_stores(spec, exclude=None):
    keys = []
    for part in re.split(r"[,\s]+", (spec or "all").strip().lower()):
        if not part:
            continue
        if part in ALIASES:
            keys += ALIASES[part]
        elif part in REGISTRY:
            keys.append(part)
        else:
            die(f"mkshop: unknown store '{part}'. Valid: {', '.join(STORE_KEYS)} "
                             f"(aliases: {', '.join(ALIASES)})")
    ex = set()
    for part in re.split(r"[,\s]+", (exclude or "").strip().lower()):
        if part:
            if part in ALIASES:
                ex.update(ALIASES[part])
            elif part in REGISTRY:
                ex.add(part)
            else:
                die(f"mkshop: unknown store '{part}' in --exclude")
    out = []
    for k in keys:
        if k not in ex and k not in out:
            out.append(k)
    if not out:
        die("mkshop: no stores selected")
    return out


def mirror_target(r, g50_skus=()):
    """The covered shop whose catalogue this listing mirrors, or None.
    Ananas: the seller is a covered shop (Нексио -> neksio, PC MARKET ->
    anhoch, ...). ZirafaMall: its SKU is a Gjirafa50 SKU seen in the same
    record set. ZirafaMall's mirror vendor is 'Basics from GjirafaMall', but
    that vendor also sells white goods Gjirafa50 does not carry, so the vendor
    name alone proves nothing; only a seller literally named Gjirafa50 would."""
    st = r.get("store")
    if st == "ananas" and r.get("seller"):
        sk = alnum(r["seller"])
        for pat, target in ANANAS_MIRROR_SELLERS:
            if sk == pat or sk.startswith(pat):
                return target
    elif st == "zirafamall":
        if r.get("seller") and alnum(r["seller"]).startswith("gjirafa50"):
            return "gjirafa50"
        if r.get("sku") and str(r["sku"]).strip().lower() in g50_skus:
            return "gjirafa50"
    return None


def annotate(records, extra_g50_skus=()):
    """Add mirror_of, model_key and match_key to every record (in place).
    mirror_of is recomputed from this record set (group re-runs it on the
    union of saved runs, so a ZirafaMall SKU meets Gjirafa50's own listing)."""
    g50 = {}
    for r in records:
        if r.get("store") == "gjirafa50" and r.get("sku"):
            g50.setdefault(str(r["sku"]).strip().lower(), []).append(r)
    g50_skus = set(g50) | {s.lower() for s in extra_g50_skus}
    vocab = {_brand_key(r.get("brand")) for r in records if r.get("brand")} - {""}
    sigs = {}

    def sig_of(r):
        if id(r) not in sigs:
            sigs[id(r)] = record_sig(r)
        return sigs[id(r)]
    for r in records:
        r["mirror_of"] = mirror_target(r, g50_skus)
        try:
            sig = sig_of(r)
            twins = g50.get(str(r.get("sku") or "").strip().lower()) if r.get("store") == "zirafamall" else None
            if r["mirror_of"] == "gjirafa50" and twins and not any(_twin_ok(sig_of(t), sig)[0] for t in twins):
                r["mirror_of"] = None   # same SKU, but the titles name another product
            r["model_key"] = model_key(r, sig, vocab)
            r["match_key"] = ("ean:" + norm_gtin(r["ean"])) if norm_gtin(r.get("ean")) else r["model_key"]
            if sig.accessory:
                r["accessory"] = sig.accessory
            else:
                r.pop("accessory", None)
        except Exception as e:  # never let annotation break a run
            r.setdefault("model_key", None)
            r.setdefault("match_key", None)
            warn(f"mkshop: could not annotate {r.get('store')}:{r.get('id')}: {e}")
    return records


def sort_records(records, how):
    big = 1 << 62

    def price(r):
        p = effective_price(r)
        return big if p is None else p
    if how == "store":
        key = lambda r: (STORE_KEYS.index(r["store"]) if r.get("store") in REGISTRY else 99, price(r))
    elif how == "title":
        key = lambda r: (fold(r.get("title")), price(r))
    else:
        # on equal prices, an offer that needs no member card comes first
        key = lambda r: (price(r), uses_member_price(r), fold(r.get("title")))
    return sorted(records, key=key)


def store_cell(r):
    s = r.get("store") or "?"
    if r.get("seller") and REGISTRY.get(s, {}).get("marketplace"):
        s += "/" + str(r["seller"])
    return s


def price_columns(rows, was=True):
    """PRICE (the effective price; '*' marks a member price), then the other
    price of rows that have a lower member price: NON-MEMBER (price_mkd), or
    MEMBER under --no-member-prices; then WAS (struck-through price) unless
    was=False. Compute it per table, from that table's rows, so a member
    column appears exactly where member_footnote(rows) explains one."""
    has_member = any(_member_lower(r) for r in rows)
    marked = has_member and MEMBER_PRICES

    def cell(r):
        v = fmt_price(effective_price(r))
        return v + ("*" if uses_member_price(r) else " ") if marked and v else v
    cols = [("PRICE", cell, "r", None)]
    if any(until_text(r) for r in rows):
        cols.append(("VALID", until_text, "l", None))
    if marked:
        cols.append(("NON-MEMBER", lambda r: fmt_price(r["price_mkd"]) if _member_lower(r) else "", "r", None))
    elif has_member:
        cols.append(("MEMBER", lambda r: fmt_price(r["member_price_mkd"]) if _member_lower(r) else "", "r", None))
    if was:
        cols.append(("WAS", lambda r: fmt_price(r.get("regular_price_mkd"))
                     if (r.get("regular_price_mkd") or 0) > (r.get("price_mkd") or 0) else "", "r", None))
    return cols


def member_footnote(rows, again=False):
    """The line under a table that explains member prices and names each
    shop's condition, or None when no row has a lower member price. again=True
    gives a short explanation for a later table under an earlier footnote."""
    conds = []
    for r in rows:
        if _member_lower(r):
            c = f"{r.get('store') or '?'}: {member_condition(r)}"
            if c not in conds:
                conds.append(c)
    if not conds:
        return None
    more = f"; +{len(conds) - 4} more" if len(conds) > 4 else ""
    if again:
        head = ("* member price; NON-MEMBER is the price without it" if MEMBER_PRICES
                else "MEMBER: lower price that needs a loyalty card or membership") + " (see the note above)"
    elif MEMBER_PRICES:
        head = ("* member price (needs the shop's loyalty card or membership): used for ranking and price "
                "filters; NON-MEMBER is the price without it; --no-member-prices ignores member prices")
    else:
        head = ("MEMBER: lower price that needs the shop's loyalty card or membership; not used for ranking "
                "or price filters (--no-member-prices)")
    return f"{head}. Conditions: {'; '.join(conds[:4])}{more}"


def print_records(records, out, show, title_width):
    rows = records if not show or show <= 0 else records[:show]
    has_mirror = any(r.get("mirror_of") for r in rows)
    cols = price_columns(rows) + [("STORE", store_cell, "l", 24)]
    if has_mirror:
        cols.append(("MIRROR", lambda r: r.get("mirror_of") or "", "l", None))
    cols += [("STOCK", lambda r: stock_cell(r.get("in_stock")), "l", None),
             ("TITLE", lambda r: r.get("title"), "l", title_width),
             ("URL", lambda r: r.get("url"), "l", None)]
    if rows:
        render_table(rows, cols, out)
    n = len(records)
    if show and 0 < show < n:
        print(f"... {n - show} more rows not shown (use --show 0 for all, or --json/--csv)", file=out)
    note = member_footnote(rows)
    if note:
        print(note, file=out)
    note = valid_footnote(rows)
    if note:
        print(note, file=out)


def print_status_table(statuses, out, with_kept=True):
    cols = [("store", lambda k: k, "l", None),
            ("status", lambda k: statuses[k]["status"], "l", None),
            ("hits", lambda k: "" if statuses[k].get("count") is None else statuses[k]["count"], "r", None)]
    if with_kept:
        cols.append(("kept", lambda k: "" if statuses[k].get("kept") is None else statuses[k]["kept"], "r", None))
    if any(st.get("relevance") for st in statuses.values()):
        rel = lambda k, f: (statuses[k].get("relevance") or {}).get(f, "")  # noqa: E731
        cols += [("all-words", lambda k: rel(k, "all_words"), "r", None),
                 ("accessories", lambda k: rel(k, "accessories"), "r", None)]
    cols += [("time", lambda k: f"{statuses[k].get('elapsed_s', 0):.1f}s", "r", None),
             ("note", lambda k: _status_note(statuses[k]), "l", 150)]
    render_table(list(statuses), cols, out)


def _status_note(st):
    """Message (failure, limit hit), mkshop's own notes and the first client warning: a
    warning such as an OR fallback must stay visible next to a 'hit --limit' note."""
    parts = [st["message"]] if st.get("message") else []
    parts += st.get("notes") or []
    if st.get("warnings"):
        parts.append("warning: " + st["warnings"][0].strip())
    return "; ".join(parts)


def footer_line(statuses, n_results, noun="results"):
    ok = [k for k, v in statuses.items() if v["status"] in ("ok", "partial")]
    nf = [k for k, v in statuses.items() if v["status"] == "not_found"]
    bad = [f"{k} ({v['status']})" for k, v in statuses.items()
           if v["status"] not in ("ok", "partial", "not_found")]
    part = [k for k, v in statuses.items() if v["status"] == "partial"]
    s = f"-- {n_results} {noun} from {len(ok)}/{len(statuses)} stores"
    if part:
        s += f"; incomplete: {', '.join(part)}"
    if nf:
        s += f"; not found / rejected (exit 2): {', '.join(nf)}"
    if bad:
        s += f"; NOT searched: {', '.join(bad)}"
    answered = {k: v for k, v in statuses.items() if v["status"] in ("ok", "partial")}
    more = [k for k, v in answered.items() if any(q.get("hit_limit") for q in v.get("queries") or [])]
    if more:
        s += f"; more may exist (hit --limit-per-store): {', '.join(more)}"
    acc = [k for k, v in answered.items() if any(n.startswith("mostly accessories") for n in v.get("notes") or [])]
    if acc:
        s += f"; mostly accessories: {', '.join(acc)}"
    warned = [k for k, v in answered.items() if v.get("warnings")]
    if warned:
        s += f"; warnings (see the status table): {', '.join(warned)}"
    return s


def overall_exit(statuses):
    vals = [v["status"] for v in statuses.values()]
    if any(v in ("ok", "partial") for v in vals):
        return 0
    if vals and all(v == "blocked" for v in vals):
        return 3
    if vals and all(v == "not_found" for v in vals):
        return 2
    return 1


# --------------------------------------------------------------------------
# Command: stores
# --------------------------------------------------------------------------
def cmd_stores(ctx, a):
    keys = parse_stores(a.stores, a.exclude)
    infos = ctx.infos(keys)
    rows = []
    for k in keys:
        st = REGISTRY[k]
        inf = infos[k]["info"] or {}
        caps = inf.get("capabilities") if isinstance(inf.get("capabilities"), list) else None
        rows.append({
            "store": k, "name": inf.get("name") or st["name"],
            "client": os.path.relpath(ctx.script(k), HERE) + (" " + " ".join(st["args"]) if st["args"] else ""),
            "domains": st["domains"], "base_url": inf.get("base_url") or f"https://{st['domains'][0]}",
            "sells": inf.get("sells") or st["sells"],
            "capabilities": caps,
            "ean_search": ctx.ean_searchable(k),
            "notes": inf.get("notes"),
            "client_status": infos[k]["status"] if infos[k]["status"] != "ok" else "ok",
            "client_message": infos[k]["message"],
        })
    failed = [r for r in rows if r["client_status"] != "ok"]
    rc = 1 if rows and len(failed) == len(rows) else 0
    missing = {m.group(1) for r in failed for m in [re.search(r"No module named '([\w.]+)'", r["client_message"] or "")] if m}
    if missing:
        n = sum(1 for r in failed if "No module named" in (r["client_message"] or ""))
        warn(f"mkshop: {n} of {len(rows)} store clients cannot start: missing Python module(s) "
             f"{', '.join(sorted(missing))}. Install requests and beautifulsoup4 for {ctx.python} "
             "(a virtualenv works), or set MKSHOP_PYTHON to an interpreter that has them.")
    if a.json is not None:
        write_json(rows, a.json)
        if a.json != "-":
            print(f"wrote {len(rows)} stores to {a.json}")
        return rc
    out = sys.stdout
    for r in rows:
        caps = ", ".join(r["capabilities"]) if r["capabilities"] is not None else "(info unavailable)"
        ean = {True: "yes", False: "no", None: "unknown"}[r["ean_search"]]
        print(f"{r['store']:<12} {r['name']}  -  {', '.join(r['domains'])}", file=out)
        print(f"{'':<12} sells: {r['sells']}", file=out)
        print(f"{'':<12} capabilities: {caps}; search matches EAN: {ean}", file=out)
        if r["notes"]:
            print(f"{'':<12} notes: {trunc(r['notes'], 300)}", file=out)
        if r["client_status"] != "ok":
            print(f"{'':<12} CLIENT {r['client_status'].upper()}: {r['client_message']}", file=out)
    return rc


# --------------------------------------------------------------------------
# Command: search
# --------------------------------------------------------------------------
def _search_store(ctx, key, queries, limit, timeout):
    deadline = time.monotonic() + timeout
    merged, order = {}, []
    per_q = []
    warnings = []
    t0 = time.monotonic()
    for n, q in enumerate(queries):
        if n:
            time.sleep(STORE_GAP)
        remaining = deadline - time.monotonic()
        argv = ["search", q] + list(REGISTRY[key].get("search_args") or [])
        if limit:
            argv += ["--limit", str(limit)]
        r = ctx.run(key, argv, remaining)
        recs = as_records(r["data"]) if r["data"] is not None else []
        entry = {"query": q, "status": r["status"], "count": len(recs) if r["status"] == "ok" else None,
                 "message": r["message"], "elapsed_s": r["elapsed_s"]}
        if r["status"] == "ok" and limit and len(recs) >= limit:
            entry["hit_limit"] = True
        per_q.append(entry)
        warnings += [w for w in r["warnings"] if w not in warnings]
        if r["status"] in ("ok", "timeout"):
            for rec in recs:
                clean_record(rec, key)
                rid = rec_id(rec)
                if rid not in merged:
                    rec["found_by"] = [q]
                    merged[rid] = rec
                    order.append(rid)
                elif q not in merged[rid]["found_by"]:
                    merged[rid]["found_by"].append(q)
        shown = f"ok {len(recs)}" if r["status"] == "ok" else f"{r['status'].upper()}: {r['message']}"
        log(f"[{key}] search {q!r} -> {shown} ({r['elapsed_s']:.1f}s)")
        if r["status"] in ("blocked", "timeout") or (r["status"] == "error" and r["message"]
                                                      and r["message"].startswith(("client not found", "cannot start"))):
            # Do not keep hammering a shop that blocks us or has run out of time.
            for q2 in queries[len(per_q):]:
                per_q.append({"query": q2, "status": "skipped", "count": None,
                              "message": f"not run after {r['status']}", "elapsed_s": 0})
            break
    oks = [e for e in per_q if e["status"] == "ok"]
    fails = [e for e in per_q if e["status"] not in ("ok",)]
    if oks and not fails:
        status, msg = "ok", None
    elif oks:
        status = "partial"
        msg = "; ".join(f"{e['query']!r}: {e['status']} {e['message'] or ''}".strip() for e in fails)
    else:
        first = fails[0] if fails else {"status": "error", "message": "no queries"}
        status, msg = first["status"], first["message"]
        if status == "not_found":
            status = "error"
            msg = f"client rejected the search (exit 2): {msg}"
    hit_limit = [e["query"] for e in per_q if e.get("hit_limit")]
    if hit_limit and status in ("ok", "partial"):
        note = f"hit --limit-per-store {limit} for {', '.join(map(repr, hit_limit))}; more may exist"
        msg = f"{msg}; {note}" if msg else note
    return {"status": status, "count": len(merged) if (oks or merged) else None, "message": msg,
            "warnings": warnings[:8],
            "elapsed_s": round(time.monotonic() - t0, 2), "queries": per_q,
            "records": [merged[i] for i in order]}


def _passes_strict(rec, queries):
    hay = alnum_words(" ".join(str(rec.get(f) or "") for f in ("title", "brand", "sku", "ean", "mpn")))
    glued = hay.replace(" ", "")   # 'rtx5070' must find 'RTX 5070'
    for q in rec.get("found_by") or queries:
        words = [w for w in alnum_words(q).split() if len(w) >= 2]
        if words and all(w in hay or w in glued for w in words):
            return True
    return False


# A shop whose hits are mostly accessories (cases, glass, batteries, mounts) gets a note in
# its status: on 106 hand-labelled result sets this was right 7 times out of 7, while a
# "few hits contain the query words" rule was wrong about a third of the time (translated
# product nouns, titles that are only a model code), so that one is reported as a count only.
ACC_NOTE_MIN_HITS = 10
ACC_NOTE_SHARE = 0.5


def relevance_counts(records, statuses):
    """Per shop that answered: how many hits contain every word of a query that found them
    (exactly what --strict keeps) and how many the title analysis calls accessories (not
    counted when the query that found them asks for an accessory itself)."""
    acc_query = {}
    counts = {k: [0, 0, 0] for k, st in statuses.items() if st["status"] in ("ok", "partial")}
    for r in records:
        c = counts.get(r.get("store"))
        if c is None:
            continue
        qs = r.get("found_by") or []
        c[0] += 1
        c[1] += _passes_strict(r, qs)
        if r.get("accessory"):
            for q in qs:
                if q not in acc_query:
                    acc_query[q] = bool(signature(q).accessory)
            if not any(acc_query[q] for q in qs):
                c[2] += 1
    for k, (n, words, acc) in counts.items():
        st = statuses[k]
        st["relevance"] = {"hits": n, "all_words": words, "accessories": acc}
        if n >= ACC_NOTE_MIN_HITS and acc >= ACC_NOTE_SHARE * n:
            st.setdefault("notes", []).append(f"mostly accessories ({acc} of {n})")


def alnum_words(s):
    return re.sub(r"[^0-9a-z]+", " ", fold(s)).strip()


def _in_price_range(v, a):
    return v is not None and (a.min_price is None or v >= a.min_price) and (a.max_price is None or v <= a.max_price)


def filter_records(records, a, statuses, strict_queries=None):
    kept = []
    dropped = {}
    for r in records:
        why = None
        p = effective_price(r)
        if a.in_stock and r.get("in_stock") is False:
            why = "out of stock"
        elif a.in_stock and getattr(a, "strict_stock", False) and r.get("in_stock") is not True:
            why = "stock unknown"
        elif a.min_price is not None and (p is None or p < a.min_price):
            why = "below --min-price"
            if uses_member_price(r) and _in_price_range(r["price_mkd"], a):
                # by default: say so when only the member price falls below it
                why += " (non-member price within it)"
        elif a.max_price is not None and (p is None or p > a.max_price):
            why = "above --max-price"
            if not MEMBER_PRICES and _member_lower(r) and _in_price_range(r["member_price_mkd"], a):
                # --no-member-prices: say so when only the member price fits
                why += " (member price within it)"
        elif strict_queries and not _passes_strict(r, strict_queries):
            why = "--strict: query words missing from title/brand/codes"
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        kept.append(r)
    for k, st in statuses.items():
        st["kept"] = sum(1 for r in kept if r.get("store") == k) if st["status"] in ("ok", "partial", "timeout") else None
    return kept, dropped


def cmd_search(ctx, a):
    queries = []
    for q in ([a.query] if a.query else []) + (a.q or []):
        q = q.strip()
        if q and q not in queries:
            queries.append(q)
    if not queries and a.json not in (None, "-") and not re.search(r"[/\\]|\.json$", a.json):
        # `search --json "galaxy a56"`: the optional PATH swallowed the query.
        warn(f"mkshop search: reading {a.json!r} as the QUERY (JSON goes to stdout); put QUERY before a bare --json")
        queries, a.json = [a.json], "-"
    if not queries:
        die("mkshop search: give a QUERY and/or --q QUERY")
    keys = parse_stores(a.stores, a.exclude)
    timeout = a.timeout or DEFAULT_TIMEOUT
    log(f"searching {len(keys)} stores for {', '.join(map(repr, queries))} (timeout {timeout:.0f}s per store)")
    with ThreadPoolExecutor(max_workers=len(keys)) as ex:
        futs = {k: ex.submit(_search_store, ctx, k, queries, a.limit_per_store, timeout) for k in keys}
        results = {k: f.result() for k, f in futs.items()}
    records = []
    statuses = {}
    for k in keys:
        r = results[k]
        records += r.pop("records")
        statuses[k] = r
    annotate(records)
    relevance_counts(records, statuses)
    total = len(records)
    kept, dropped = filter_records(records, a, statuses, queries if a.strict else None)
    hidden = 0
    if a.hide_mirrors:
        okstores = {k for k, v in statuses.items() if v["status"] in ("ok", "partial")}
        before = len(kept)
        kept = [r for r in kept if not (r.get("mirror_of") in okstores)]
        hidden = before - len(kept)
        if hidden:
            dropped["mirror listing (--hide-mirrors)"] = hidden
    kept = sort_records(kept, a.sort)
    env = {"command": "search", "generated_at": now_iso(), "query": queries[0] if len(queries) == 1 else queries,
           "queries": queries, "filters": _filters_dict(a), "member_prices": MEMBER_PRICES, "dropped": dropped,
           "total_before_filters": total, "count": len(kept), "stores": statuses,
           "results": kept}
    return _emit_listing(env, a, kept, statuses, "results")


def _filters_dict(a):
    d = {}
    for f in ("in_stock", "min_price", "max_price", "strict", "hide_mirrors", "limit_per_store", "sort"):
        v = getattr(a, f, None)
        if v not in (None, False):
            d[f] = v
    return d


def _emit_listing(env, a, kept, statuses, noun):
    rc = overall_exit(statuses)
    if a.csv:
        write_csv(kept, a.csv)
    if a.json is not None:
        write_json(env, a.json)
    to_stdout_json = a.json == "-" or a.csv == "-"
    if not to_stdout_json:
        wrote = [p for p in (a.json, a.csv) if p]
        if not (wrote and a.no_table):
            print_records(kept, sys.stdout, a.show, a.title_width)
        if wrote:
            print(f"wrote {len(kept)} {noun} to {', '.join(wrote)}")
        print(footer_line(statuses, len(kept), noun) + _dropped_note(env.get("dropped")))
    sys.stdout.flush()
    with _print_lock:
        print("", file=sys.stderr)
        print_status_table(statuses, sys.stderr)
    return rc


def _dropped_note(dropped):
    if not dropped:
        return ""
    return "; filtered out: " + ", ".join(f"{v} {k}" for k, v in dropped.items())


# --------------------------------------------------------------------------
# Command: categories
# --------------------------------------------------------------------------
def cmd_categories(ctx, a):
    if a.store:
        keys = parse_stores(a.store)
    else:
        keys = parse_stores(a.stores, a.exclude)
    if not a.grep and len(keys) != 1:
        die("mkshop categories: give --grep REGEX (searches every store), "
            "or --store KEY for one store's full tree")
    if a.grep:
        try:
            re.compile(a.grep)
        except re.error as e:
            die(f"mkshop categories: bad --grep regex: {e}")
    timeout = a.timeout or DEFAULT_TIMEOUT

    def one(k):
        argv = ["categories"] + (["--grep", a.grep] if a.grep else [])
        r = ctx.run(k, argv, timeout)
        recs = as_records(r["data"]) if r["status"] == "ok" else []
        for rec in recs:
            rec["store"] = k
        shown = f"ok {len(recs)}" if r["status"] == "ok" else f"{r['status'].upper()}: {r['message']}"
        log(f"[{k}] categories{' --grep ' + repr(a.grep) if a.grep else ''} -> {shown} ({r['elapsed_s']:.1f}s)")
        st = {"status": r["status"], "count": len(recs) if r["status"] == "ok" else None,
              "message": r["message"], "warnings": r["warnings"], "elapsed_s": r["elapsed_s"]}
        return recs, st

    with ThreadPoolExecutor(max_workers=len(keys)) as ex:
        res = dict(zip(keys, ex.map(one, keys)))
    records, statuses = [], {}
    for k in keys:
        records += res[k][0]
        statuses[k] = res[k][1]
    env = {"command": "categories", "grep": a.grep, "stores": statuses, "count": len(records),
           "results": records}
    rc = overall_exit(statuses)
    if a.json is not None:
        write_json(env, a.json)
    if a.json != "-":
        if records and not (a.json and a.no_table):
            print_categories(records, a, sys.stdout)
        if a.json:
            print(f"wrote {len(records)} categories to {a.json}")
        print(footer_line(statuses, len(records), "categories"))
    if a.json != "-":
        sys.stdout.flush()
        with _print_lock:
            print("", file=sys.stderr)
            print_status_table(statuses, sys.stderr, with_kept=False)
    return rc


# Categories printed per shop for --grep, biggest first: eval-5's 226-row grep printed 54k
# characters with URLs and slugs, past what an agent's tool output shows inline (~30k).
CATS_SHOW = 20
CATS_PATH_WIDTH = 80


def _path_tail(path, width=CATS_PATH_WIDTH):
    """A breadcrumb cut from the left so the leaf stays visible: '… > Монитори > Гејмерски'."""
    if len(path) <= width:
        return path
    parts = path.split(" > ")
    while len(parts) > 1 and len("… > " + " > ".join(parts)) > width:
        parts.pop(0)
    tail = "… > " + " > ".join(parts)
    return tail if len(tail) <= width else "…" + path[-(width - 1):]


def print_categories(records, a, out):
    per = a.show if a.show is not None else (CATS_SHOW if a.grep else 0)
    per = max(per, 0)
    by = {}
    for r in records:
        by.setdefault(r.get("store"), []).append(r)
    rows, more = [], {}
    for k, rs in by.items():
        if a.grep:   # a full tree keeps the shop's own order
            rs = sorted(rs, key=lambda r: (r.get("count") is None, -(r.get("count") or 0)))
        rows += rs[:per] if per else rs
        if per and len(rs) > per:
            more[k] = len(rs) - per
    # what `list` takes: the id, else the slug, else the URL (some shops' menus give no id)
    cols = [("STORE", lambda r: r.get("store"), "l", None),
            ("COUNT", lambda r: "" if r.get("count") is None else r.get("count"), "r", None),
            ("ID / SLUG", lambda r: next((str(r[f]) for f in ("id", "slug", "url") if r.get(f) not in (None, "")), ""),
             "l", None)]
    if a.urls and any(r.get("slug") and str(r.get("slug")) != str(r.get("id")) for r in rows):
        cols.append(("SLUG", lambda r: r.get("slug") or "", "l", 40))
    cols.append(("PATH", lambda r: _path_tail(r.get("path") or r.get("name") or ""), "l", None))
    if a.urls:
        cols.append(("URL", lambda r: r.get("url"), "l", None))
    render_table(rows, cols, out)
    if more:
        print("... more categories not shown: " + ", ".join(f"{k} {n}" for k, n in more.items())
              + " (--show 0 for all, or --json)", file=out)


# --------------------------------------------------------------------------
# Commands: list, facets
# --------------------------------------------------------------------------
def cmd_list(ctx, a):
    key = parse_stores(a.store)[0] if a.store else None
    if not key or "," in (a.store or ""):
        die("mkshop list: --store KEY (exactly one store) is required")
    argv = ["list", a.category]
    if a.in_stock:
        argv.append("--in-stock")
    for tok in a.filter or []:
        argv += ["--filter", tok]
    if a.limit:
        argv += ["--limit", str(a.limit)]
    timeout = a.timeout or DEFAULT_LIST_TIMEOUT
    log(f"[{key}] list {a.category!r} (timeout {timeout:.0f}s; list walks every page)")
    r = ctx.run(key, argv, timeout)
    recs = [clean_record(x, key) for x in as_records(r["data"])] if r["data"] is not None else []
    annotate(recs)
    st = {"status": r["status"], "count": len(recs) if r["status"] in ("ok", "timeout") else None,
          "message": r["message"], "warnings": r["warnings"], "elapsed_s": r["elapsed_s"]}
    if r["status"] == "timeout" and recs:
        st["status"] = "partial"
    statuses = {key: st}
    log(f"[{key}] list -> {st['status']} {st['count'] if st['count'] is not None else ''} ({r['elapsed_s']:.1f}s)")
    kept, dropped = filter_records(recs, a, statuses)
    kept = sort_records(kept, a.sort)
    env = {"command": "list", "generated_at": now_iso(), "store": key, "category": a.category, "filters": a.filter or [],
           "in_stock": a.in_stock, "member_prices": MEMBER_PRICES, "dropped": dropped, "count": len(kept),
           "stores": statuses,
           "results": kept}
    rc = _emit_listing(env, a, kept, statuses, "products")
    if r["status"] == "not_found":
        return 2
    return rc


def cmd_facets(ctx, a):
    key = parse_stores(a.store)[0]
    caps = ctx.capabilities(key)
    if caps is not None and "facets" not in caps:
        warn(f"mkshop facets: {key} does not advertise the 'facets' capability "
             f"(capabilities: {', '.join(sorted(caps))}); trying anyway")
    timeout = a.timeout or DEFAULT_TIMEOUT
    r = ctx.run(key, ["facets", a.category], timeout)
    recs = as_records(r["data"]) if r["status"] == "ok" else []
    st = {"status": r["status"], "count": len(recs) if r["status"] == "ok" else None,
          "message": r["message"], "warnings": r["warnings"], "elapsed_s": r["elapsed_s"]}
    env = {"command": "facets", "store": key, "category": a.category, "stores": {key: st},
           "count": len(recs), "results": recs}
    if a.json is not None:
        write_json(env, a.json)
    if a.json != "-":
        if recs and not (a.json and a.no_table):
            cols = [("NAME", lambda x: x.get("name"), "l", 40),
                    ("VALUE", lambda x: x.get("value"), "l", 50),
                    ("COUNT", lambda x: "" if x.get("count") is None else x.get("count"), "r", None),
                    ("TOKEN (for list --filter)", lambda x: x.get("token"), "l", None)]
            render_table(recs, cols, sys.stdout)
        if a.json:
            print(f"wrote {len(recs)} facet values to {a.json}")
        print(footer_line({key: st}, len(recs), "facet values"))
    if r["status"] != "ok":
        warn(f"[{key}] facets: {r['status']}: {r['message']}")
    return {"ok": 0, "not_found": 2, "blocked": 3}.get(r["status"], 1)


# --------------------------------------------------------------------------
# Command: detail
# --------------------------------------------------------------------------
def route_refs(refs, store_opt):
    """-> list of (ref for the client, store key or None, error or None, input as given)."""
    fixed = parse_stores(store_opt)[0] if store_opt else None
    if store_opt and len(parse_stores(store_opt)) != 1:
        die("mkshop detail: --store takes exactly one store key")
    out = []
    for given in refs:
        ref = given.strip()
        if is_url(ref):
            if not re.match(r"^https?://", ref, re.I):
                ref = "https://" + ref
            k = store_for_url(ref)
            if not k:
                out.append((ref, None, "URL is not from a covered shop "
                                       f"({', '.join(d for s in REGISTRY.values() for d in s['domains'])})", given))
                continue
            if fixed and fixed != k:
                warn(f"mkshop: {ref} belongs to {k}, not --store {fixed}; routing it to {k}")
            out.append((ref, k, None, given))
        elif fixed:
            out.append((ref, fixed, None, given))
        else:
            out.append((ref, None, "bare id/code: pass --store KEY (or give the product URL)", given))
    return out


def run_details(ctx, routed, timeout, label="detail"):
    """routed: list of (ref, key, err[, input as given]). Returns (records in
    input order, statuses); every record's "input" is the input as given."""
    routed = [tuple(x) + (x[0],) if len(x) == 3 else tuple(x) for x in routed]
    by_store = {}
    for i, (ref, k, err, _given) in enumerate(routed):
        if k and not err:
            by_store.setdefault(k, []).append((i, ref))
    results = [None] * len(routed)
    statuses = {}

    def one(k):
        items = by_store[k]
        refs = [ref for _, ref in items]
        r = ctx.run(k, ["detail", *refs], timeout)
        recs = as_records(r["data"]) if r["data"] is not None and r["status"] in ("ok", "not_found") else []
        shown = f"ok {len(recs)}" if r["status"] == "ok" else f"{r['status'].upper()}: {r['message']}"
        log(f"[{k}] {label} {len(refs)} item(s) -> {shown} ({r['elapsed_s']:.1f}s)")
        return k, items, r, recs

    if by_store:
        with ThreadPoolExecutor(max_workers=len(by_store)) as ex:
            outs = list(ex.map(one, list(by_store)))
    else:
        outs = []
    for k, items, r, recs in outs:
        for rec in recs:
            clean_record(rec, k)
        assigned = {}
        if len(recs) == len(items):
            for (i, ref), rec in zip(items, recs):
                assigned[i] = rec
        else:
            # Fall back to matching on the echoed input / url / id.
            pool = list(recs)
            for i, ref in items:
                for rec in pool:
                    if ref in (rec.get("input"), rec.get("url"), rec.get("id")) or (
                            rec.get("url") and ref.rstrip("/") == str(rec["url"]).rstrip("/")):
                        assigned[i] = rec
                        pool.remove(rec)
                        break
        n_ok = 0
        for i, ref in items:
            rec = assigned.get(i)
            if rec is not None and rec.get("error") and r["status"] != "ok":
                rec = dict(rec, error=f"{r['status']}: {rec['error']}")
            if rec is None:
                msg = r["message"] if r["status"] != "ok" else "client returned no record for this input"
                rec = {"input": ref, "store": k, "error": f"{r['status']}: {msg}" if r["status"] != "ok" else msg}
            else:
                if not rec.get("error"):
                    n_ok += 1
            rec["input"] = routed[i][3]
            results[i] = rec
        statuses[k] = {"status": r["status"] if not (r["status"] == "ok" and n_ok == 0) else "not_found",
                       "count": n_ok, "message": r["message"], "warnings": r["warnings"],
                       "elapsed_s": r["elapsed_s"]}
    for i, (ref, k, err, given) in enumerate(routed):
        if err:
            results[i] = {"input": given, "store": k, "error": err}
    return results, statuses


def print_detail(rec, n, out):
    if rec.get("error"):
        print(f"[{n}] {rec.get('store') or '?'}  {rec.get('input')}\n    ERROR: {rec['error']}", file=out)
        return
    print(f"[{n}] {store_cell(rec)}  {rec.get('title')}", file=out)
    line = [price_text(rec, was=True), f"in stock: {stock_cell(rec.get('in_stock'))}"
            + (f" ({trunc(rec.get('stock_note'), 120)})" if rec.get("stock_note") else "")]
    if rec.get("warranty"):
        line.append(f"warranty: {rec['warranty']}")
    print("    " + " · ".join(line), file=out)
    vl = validity_line(rec)
    if vl:
        print("    " + vl, file=out)
    ids = [f"{k} {rec[k]}" for k in ("brand", "sku", "ean", "mpn") if rec.get(k)]
    if rec.get("mirror_of"):
        ids.append(f"mirror of {rec['mirror_of']}")
    if ids:
        print("    " + " · ".join(ids), file=out)
    if rec.get("category"):
        print(f"    category: {trunc(rec['category'], 160)}", file=out)
    for f in ("delivery_estimate", "shipping_mkd", "international_supplier"):
        if rec.get(f) not in (None, ""):
            print(f"    {f}: {trunc(rec[f], 200)}", file=out)
    pls = rec.get("per_location_stock")
    if isinstance(pls, list) and pls:
        parts = []
        for x in pls[:16]:
            if not isinstance(x, dict):
                continue
            mark = {True: "yes", False: "no"}.get(x.get("in_stock"), "?")
            q = f" {x['quantity']}" if x.get("quantity") is not None else ""
            parts.append(f"{x.get('location')}: {mark}{q}")
        more = f" (+{len(pls) - 16} more)" if len(pls) > 16 else ""
        print(f"    locations: {'; '.join(parts)}{more}", file=out)
    if isinstance(rec.get("extra"), dict) and rec["extra"]:
        kv = [f"{k}={trunc(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 60)}"
              for k, v in list(rec["extra"].items())[:10]]
        print(f"    extra: {'; '.join(kv)}", file=out)
    if rec.get("specs"):
        print(f"    specs: {trunc(rec['specs'], 400)}", file=out)
    print(f"    {rec.get('url')}", file=out)


def cmd_detail(ctx, a):
    if a.json not in (None, "-") and (is_url(a.json) or re.fullmatch(r"\d+", a.json)):
        # `detail --json URL ...`: the optional PATH swallowed the first REF.
        warn(f"mkshop detail: reading {a.json!r} as a REF (JSON goes to stdout); put REFs before a bare --json")
        a.refs, a.json = [a.json] + list(a.refs), "-"
    routed = route_refs(a.refs, a.store)
    results, statuses = run_details(ctx, routed, a.timeout or DEFAULT_TIMEOUT)
    annotate([r for r in results if not r.get("error")])
    env = {"command": "detail", "generated_at": now_iso(), "inputs": a.refs, "member_prices": MEMBER_PRICES, "stores": statuses,
           "count": sum(1 for r in results if not r.get("error")), "results": results}
    if a.json is not None:
        write_json(env, a.json)
    if a.json != "-":
        if not (a.json and a.no_table):
            for n, rec in enumerate(results, 1):
                print_detail(rec, n, sys.stdout)
        if a.json:
            print(f"wrote {len(results)} records to {a.json}")
    errs = [r for r in results if r.get("error")]
    if a.json != "-":
        if errs:
            warn(f"{len(errs)} of {len(results)} inputs failed: " +
                 "; ".join(f"{r['input']}: {r['error']}" for r in errs[:5]))
        if statuses:
            sys.stdout.flush()
            with _print_lock:
                print("", file=sys.stderr)
                print_status_table(statuses, sys.stderr, with_kept=False)
    if not errs:
        return 0
    if len(errs) < len(results):
        return 0
    if statuses and all(v["status"] == "blocked" for v in statuses.values()):
        return 3
    return 2 if all(v["status"] in ("not_found", "ok") for v in statuses.values()) else 1


# --------------------------------------------------------------------------
# Command: match
# --------------------------------------------------------------------------
CONF_RANK = {"exact": 0, "model": 1, "likely": 2}


def _brand_on(brand, other):
    """Is brand (alnum) present in record signature `other`?"""
    if not brand:
        return False
    if other.brand and (other.brand == brand or other.brand.startswith(brand)
                        or brand.startswith(other.brand)):
        return True
    if brand in (getattr(other, "for_only", None) or ()):
        return False   # 'HQ toner for HP LJ M141': HP is what it fits, not its maker
    return brand in other.tokens or (len(brand) >= 4 and brand in other.concat)


def _brand_ok(ref, cand):
    """-> (compatible, confirmed). Two brands that differ and do not appear in
    each other's titles are incompatible (unless the EAN says otherwise)."""
    rb, cb = ref.brand, cand.brand
    if rb and cb:
        if rb == cb or rb.startswith(cb) or cb.startswith(rb):
            return True, True
        if _brand_on(rb, cand) or _brand_on(cb, ref):
            return True, True
        return False, True
    if rb:
        return True, _brand_on(rb, cand)
    if cb:
        return True, _brand_on(cb, ref)
    fw = _first_word(ref)
    return True, bool(fw and has_tok(fw, cand))


def _codes_match(r, c):
    if r == c:
        return "equal"
    short, long_ = (r, c) if len(r) <= len(c) else (c, r)
    if len(short) >= 6 and long_.startswith(short):
        return "prefix"
    return None


def _composite_code(short, long_, short_sig):
    """long_ = [word] + short + [colour], where the word is in short_sig's
    title (JBL's LIVE770NCBLK = 'Live' + 770NC + black)."""
    if len(short) < 5 or len(long_) <= len(short):
        return False
    i = long_.find(short)
    if i < 0:
        return False
    pre, suf = long_[:i], long_[i + len(short):]
    if pre and not (pre.isalpha() and has_tok(pre, short_sig)):
        return False
    if suf and not (suf.isalpha() and (suf in COLOURS or suf in _CODE_COLOUR_SUFFIX)):
        return False
    return bool(pre or suf)


def find_code_hit(ref, cand):
    """The model code ref and cand share -> (ref code, cand code, how) or None.
    how: equal | prefix (QE55Q60D ~ QE55Q60DAUXXH) | in title (a 6+ char code
    inside the other's squashed title) | spaced (770NC ~ 'LIVE 770 NC') |
    composite (770NC ~ LIVE770NCBLK)."""
    same = sorted(ref.strong & cand.strong, key=len, reverse=True)
    if same:  # the very same code beats one that begins another (WHCH520W ~ WHCH520W, not ~ WHCH520WCE7)
        return (same[0], same[0], "equal")
    for rc in sorted(ref.strong, key=len, reverse=True):
        for cc in sorted(cand.strong, key=len, reverse=True):
            m = _codes_match(rc, cc)
            if m:
                return (rc, cc, m)
        if len(rc) >= 6 and rc in cand.concat:
            return (rc, rc, "in title")
        if rc in (cand.derived or {}):
            return (rc, rc, "spaced")
    for dc in sorted(ref.derived or {}, key=len, reverse=True):
        if dc in cand.strong:
            return (dc, dc, "spaced")
    for rc in sorted(ref.strong, key=len, reverse=True):
        for cc in sorted(cand.strong, key=len, reverse=True):
            if _composite_code(rc, cc, ref) or _composite_code(cc, rc, cand):
                return (rc, cc, "composite")
    return None


def _possible_colours(own, other):
    """own's possible colour words (Sig.maybe_colour) that other's title lacks."""
    if own.colours or not own.maybe_colour:
        return set()
    return {w for w in own.maybe_colour if not has_tok(w, other)}


def _title_colours(s):
    """Colours the title itself names (not a seller's colour attribute)."""
    return set(s.colours) if s.colours and getattr(s, "colour_src", "title") != "attr" else set()


def _letter_colours(code, sig):
    """Colours the last letter of a code may mean when it ends a short letter
    run after the model number ('whch520w' -> white, 'mdrzx110apw' -> white);
    the packaging tail is ignored ('wh1000xm5lce7' -> its base 'wh1000xm5l')."""
    base = next((b for b, full in (getattr(sig, "pkg_base", None) or {}).items() if full == code), code)
    mt = re.fullmatch(r"[a-z0-9]*\d[a-z]{0,2}([a-z])", base)
    return _COLOUR_LETTERS.get(mt.group(1), set()) if mt and len(base) >= 5 else set()


def _code_colour(cols, ra, rb):
    """The colour a vendor colour letter names, when the convention holds here:
    a title on one side names one of cols and no title names another colour;
    with no colour in either title, a colour attribute that agrees. -> colour
    or None."""
    ta, tb = _title_colours(ra), _title_colours(rb)
    named = (ta | tb) & cols
    if not ta and not tb:
        named = (set(ra.colours) | set(rb.colours)) & cols
        if (set(ra.colours) | set(rb.colours)) - cols:
            return None
    if len(named) == 1 and (not ta or ta & cols) and (not tb or tb & cols):
        return next(iter(named))
    return None


def _suffix_kind(x, long_, lsig, ssig):
    """What the letters a longer code adds to a shorter one are (QE55Q60D +
    AUXXH, WF1000XM5 + B, MDR-ZX110 + AP): 'packaging' (region / packaging
    tail), 'colour' (a colour word, abbreviation or a vendor colour letter a
    title confirms), 'edition' ('-GAMING' written after a separator), or None:
    letters after a model number name another model (NTH-100M, EH193KR)."""
    if x in COLOURS or x in _CODE_COLOUR_SUFFIX:
        return "colour"
    if x in REGIONS or _PKG_TAIL_RE.match(x):
        return "packaging"
    mt = re.fullmatch(r"([a-z])([a-z0-9]*)", x)
    if mt and mt.group(1) in _COLOUR_LETTERS and (not mt.group(2) or _PKG_TAIL_RE.match(mt.group(2))):
        if _code_colour(_COLOUR_LETTERS[mt.group(1)], lsig, ssig) and \
                (not _title_colours(lsig) or _title_colours(lsig) & _COLOUR_LETTERS[mt.group(1)]):
            return "colour"
    written = lsig.codes.get(long_, "")
    if x in GENERIC and re.search(r"[-\s/_.]" + re.escape(x) + r"$", written, re.I):
        return "edition"
    return None


def _cap(s):
    """Capacity for comparisons: the storage a configuration names (RAM is a
    dimension of its own), else the title's GB values except graphics memory."""
    if getattr(s, "storage", None):
        return set(s.storage)
    return set(s.units.get("gb", ())) - (getattr(s, "ram", None) or set()) - (getattr(s, "vram", None) or set())


_TIER_SHOWN = {"ryzen": "Ryzen ", "ryzenai": "Ryzen AI ", "corei": "Core i", "core": "Core ", "ultra": "Core Ultra "}


def _cpu_text(v):
    def one(x):
        mt = re.fullmatch(r"(ryzenai|ryzen|corei|core|ultra)(\d)", x)
        return _TIER_SHOWN[mt.group(1)] + mt.group(2) if mt else x.upper()
    return "/".join(one(x) for x in sorted(v))


def _cpu_diff(ref, cand):
    """CPU of a laptop / PC / phone configuration: model codes when both name
    one (13620H vs 13420H), else tiers (Ryzen 5 vs Ryzen 7, Core 5 vs Core i5).
    -> (conflict, text or None)."""
    rm, cm = getattr(ref, "cpu", None) or set(), getattr(cand, "cpu", None) or set()
    rt, ct = getattr(ref, "cpu_tier", None) or set(), getattr(cand, "cpu_tier", None) or set()
    rv, cv = (rm, cm) if rm and cm else (rt, ct) if rt and ct else (rm | rt, cm | ct)
    if not rv and not cv or rv == cv:
        return False, None
    if rv and cv:
        if rv & cv:
            return False, f"CPU {_cpu_text(cv)} vs ref {_cpu_text(rv)}"
        return True, f"CPU {_cpu_text(cv)} vs ref {_cpu_text(rv)}"
    return False, (f"CPU {_cpu_text(cv)} (ref silent)" if cv else f"CPU not stated (ref {_cpu_text(rv)})")


def _fmtset(v, f):
    return "/".join(f(x) for x in sorted(v, key=lambda x: (str(type(x)), x)))


def _dim_diff(name, rv, cv, f=str):
    """Compare one variant dimension. -> (conflict, text or None)."""
    if not rv and not cv:
        return False, None
    if rv and cv:
        if rv == cv:
            return False, None
        if rv <= cv or cv <= rv:
            bits = [f"+{f(x)}" for x in sorted(cv - rv, key=str)] + [f"-{f(x)}" for x in sorted(rv - cv, key=str)]
            return False, f"{name} {' '.join(bits)}"
        return True, f"{name} {_fmtset(cv, f)} vs ref {_fmtset(rv, f)}"
    if cv:
        return False, f"{name} {_fmtset(cv, f)} (ref silent)"
    return False, f"{name} not stated (ref {_fmtset(rv, f)})"


def _code(sig, c):
    """A part number as the title (or record) wrote it."""
    return sig.codes.get(c) or c.upper()


def _present(t, own, other):
    """Is token t of signature `own` present in `other`? Numbers also count
    when joined to the preceding word ('GMMK 2' ~ 'Gmmk2')."""
    if has_tok(t, other):
        return True
    for p in own.pairs:
        if p.endswith(t) and len(p) >= 4 and (p in other.concat or p in other.tokens):
            return True
    return False


def _adjacent_words(sig, code, skip):
    """Identifying words within two positions of a model code in the title."""
    pos = [p for p, t, k in sig.seq if k == "strong" and (t == code or t.startswith(code) or code.startswith(t))]
    span = set()
    for dc, (a, b) in (sig.derived or {}).items():
        if dc == code:  # a code written with spaces: its pieces are the code
            pos += [a, b]
            span.update(range(a, b + 1))
    if not pos:
        return set()
    out = set()
    incl = getattr(sig, "incl_pos", None)
    for p, t, k in sig.seq:
        if k in ("word", "weak") and t not in skip and p not in span and any(0 < abs(p - q) <= 2 for q in pos) \
                and t not in PRODUCT_TYPES and (incl is None or p < incl):   # 'Kufje', 'w/Mic' name no model
            out.add(t)
    return out


def score_candidate(ref, cand, cand_rec, ref_rec=None, via_ean_query=False):
    """Compare a candidate with the reference.
    -> {confidence: exact|model|likely|None, group: same|variant, reasons,
        differs, conflicts, score, cover} or {confidence: None, why_not}."""
    reasons, differs, conflicts = [], [], []
    conf = None
    ean_conflict = False
    if ref.ean and cand.ean:
        if ref.ean == cand.ean:
            conf = "exact"
            reasons.append("same EAN")
        else:
            ean_conflict = True
    # Gjirafa50 and ZirafaMall share SKUs for the same product.
    if conf is None and ref_rec and cand_rec.get("sku") and ref_rec.get("sku") \
            and {ref_rec.get("store"), cand_rec.get("store")} == {"gjirafa50", "zirafamall"} \
            and str(ref_rec["sku"]).lower() == str(cand_rec["sku"]).lower() and _twin_ok(ref, cand)[0]:
        conf = "model"
        reasons.append("same Gjirafa SKU")
    if conf is None and ref.bundle != cand.bundle:
        return {"confidence": None, "why_not": ("a set/combo that includes the product" if cand.bundle
                                                else "a single product; the reference is a set/combo")}
    if conf is None and bool(ref.accessory) != bool(cand.accessory):
        return {"confidence": None, "why_not": (f"accessory ({cand.accessory}) for the product, not the product"
                                                if cand.accessory else f"the product, not an accessory ({ref.accessory})")}
    bok, bknown = _brand_ok(ref, cand)
    if conf is None and not bok:
        return {"confidence": None, "why_not": f"brand differs ({cand.brand} vs {ref.brand})"}
    type_conflict = bool(ref.ptype and cand.ptype and ref.ptype != cand.ptype)
    # Model codes.
    code_hit = find_code_hit(ref, cand)
    # Full part numbers that disagree (GK-K0CC4-KM570 vs GK-K0CC2-KM570) beat
    # a shared family code (KM570).
    long_conflict = None
    rl = [c for c in ref.strong if len(c) >= 8]
    cl = [c for c in cand.strong if len(c) >= 8]
    if rl and cl and not any(_codes_match(r, c) for r in rl for c in cand.strong) \
            and not any(_codes_match(r, c) for r in ref.strong for c in cl) \
            and not (code_hit and code_hit[2] == "composite"):
        long_conflict = (sorted(cl)[0], sorted(rl)[0])
    # Different part numbers: a colour/region edition of the same model when
    # the titles agree (910-006560 Pale Grey vs 910-007501 Gray), otherwise a
    # different product (QE55Q60D vs QE65Q60D). Decided by the title below.
    code_conflict = None
    if conf is None and ref.strong and cand.strong and not code_hit:
        code_conflict = ("different model codes (" + ", ".join(sorted(_code(cand, c) for c in cand.strong))
                         + " vs ref " + ", ".join(sorted(_code(ref, c) for c in ref.strong)) + ")")
    if conf is None and code_hit:
        conf = "model"
        reasons.append(f"model code {_code(ref, code_hit[0])}"
                       + {"spaced": " (written with spaces)", "composite": f" (in {_code(cand, code_hit[1])})"}
                       .get(code_hit[2], ""))
    suffix = None
    if code_hit and code_hit[2] == "prefix":
        differs.append(f"code {_code(cand, code_hit[1])} vs ref {_code(ref, code_hit[0])}")
        # letters a longer code adds after the model number name another model
        # (MDR-ZX110AP, NTH-100M) unless they are a colour, region or packaging tail
        sh, lo = sorted(code_hit[:2], key=len)
        lsig, ssig = (cand, ref) if lo == code_hit[1] and len(code_hit[1]) > len(code_hit[0]) else (ref, cand)
        suffix = (lo[len(sh):], _suffix_kind(lo[len(sh):], lo, lsig, ssig), lsig)
        if suffix[1] is None:
            differs.append(f"model suffix {suffix[0].upper()} ({_code(lsig, lo)} is not {_code(ssig, sh)})")
            conflicts.append("model suffix")
    if long_conflict:
        differs.append(f"part number {_code(cand, long_conflict[0])} vs ref {_code(ref, long_conflict[1])}")
        conflicts.append("part number")
    # A model suffix written apart after the shared code on one side only, or
    # two different ones ('CVM-V01SP UC' vs 'CVM-V01SP').
    if code_hit and code_hit[2] in ("equal", "prefix"):
        rs = (getattr(ref, "code_suffix", None) or {}).get(code_hit[0])
        cs = (getattr(cand, "code_suffix", None) or {}).get(code_hit[1])
        if rs != cs:
            differs.append(f"model suffix {(cs or '-').upper()} after {_code(cand, code_hit[1])} vs ref "
                           f"{(rs or '-').upper()}")
            conflicts.append("model suffix")
    # Name next to a shared family code (ALU87B Bushido vs ALU87B Celestial).
    if code_hit and conf == "model":
        bw = {ref.brand, cand.brand} | brand_words(ref.brand) | brand_words(cand.brand)
        ra, ca = _adjacent_words(ref, code_hit[0], bw), _adjacent_words(cand, code_hit[1], bw)
        r_only = {t for t in ra if not has_tok(t, cand)}
        c_only = {t for t in ca if not has_tok(t, ref)}
        if r_only and c_only:
            differs.append(f"name {'/'.join(sorted(c_only))} vs ref {'/'.join(sorted(r_only))}")
            conflicts.append("name")
    # Title tokens.
    A = (ref.tokens | ref.strong) - {ref.brand, cand.brand}
    B = (cand.tokens | cand.strong) - {ref.brand, cand.brand}
    inter = {t for t in A if has_tok(t, cand)}
    back_inter = {t for t in B if has_tok(t, ref)}
    cover = len(inter) / len(A) if A else 0.0
    back = len(back_inter) / len(B) if B else 0.0
    # The model phrase (MX Master 3S, iPhone 16, Origins 2 1800) carries the
    # identity; its codes and numbers must agree. Codes elsewhere in a title
    # (a switch name, a key count) only count towards overlap.
    bw = brand_words(ref.brand) | brand_words(cand.brand) | {ref.brand, cand.brand}
    ident = {alnum(w[1]) for w in model_phrase(ref, bw) if w[2] != "tier"} - {""}
    cident = {alnum(w[1]) for w in model_phrase(cand, bw) if w[2] != "tier"} - {""}
    ref_crit = (ref.weak | ref.numbers) & ident if ident else (ref.weak | ref.numbers)
    miss_crit = {t for t in ref_crit if not _present(t, ref, cand)}
    cand_extra = {t for t in (cand.weak | cand.numbers) & (cident or cand.weak | cand.numbers)
                  if not _present(t, cand, ref)}
    if conf is None:
        why = None
        ident_ok = bool(ident) and all(_present(t, ref, cand) for t in ident) and (
            len(ident) >= 2 or any(len(t) >= 2 and re.search(r"\d", t) and re.search(r"[a-z]", t)
                                   for t in ident))
        weak_missing = {w for w in ref.weak & ref_crit if not _present(w, ref, cand)}
        # Series words in the model phrase: 'MX Master 3S' vs 'MX Anywhere 3S'.
        rp = {alnum(w[1]) for w in model_phrase(ref, bw) if w[2] == "word"}
        cp = {alnum(w[1]) for w in model_phrase(cand, bw) if w[2] == "word"}
        rp_miss = {t for t in rp if not _present(t, ref, cand)}
        cp_extra = {t for t in cp if not _present(t, cand, ref)}
        if not A:
            why = "reference has no identifying tokens"
        elif rp_miss and cp_extra and (ref.weak | ref.numbers | ref.versions):
            why = f"model name differs ({'/'.join(sorted(cp_extra))} vs ref {'/'.join(sorted(rp_miss))})"
        elif weak_missing:
            why = "model token(s) " + ", ".join(sorted(x.upper() for x in weak_missing)) + " missing"
        elif miss_crit and cand_extra:
            why = f"numbers differ ({', '.join(sorted(cand_extra))} vs ref {', '.join(sorted(miss_crit))})"
        elif len(miss_crit) > (1 if len(ref_crit) >= 3 else 0):
            why = f"missing {', '.join(sorted(miss_crit))}"
        elif cover < 0.6 and not (ident_ok and cover >= 0.25):
            why = f"title overlap {cover:.0%} < 60%"
        elif len(inter) < 2 and not ref.weak:
            why = "too few shared tokens"
        elif back < 0.2 and cover < 0.85 and not ident_ok:
            why = f"candidate title mostly unrelated ({back:.0%} of its tokens in ref)"
        elif not bknown and not (not ref.brand and cover >= 0.8 and not miss_crit):
            why = "brand not confirmed on both sides"
        if why and not via_ean_query:
            return {"confidence": None, "why_not": code_conflict or why, "cover": round(cover, 2)}
        if type_conflict:
            # a title-only lead for another kind of product (a party speaker
            # sharing a series name with headphones) is not a variant
            return {"confidence": None, "why_not": f"product type differs ({cand.ptype} vs ref {ref.ptype})",
                    "cover": round(cover, 2)}
        conf = "likely"
        if code_conflict and not long_conflict:
            differs.append("part number " + "/".join(sorted(_code(cand, c) for c in cand.strong))
                           + " vs ref " + "/".join(sorted(_code(ref, c) for c in ref.strong)))
            conflicts.append("part number")
        if why:
            reasons.append("returned by the shop's own search for the EAN; listing has no EAN to confirm")
        elif cover < 0.6:
            reasons.append(f"same brand, model name {' '.join(sorted(ident)).upper()} present, "
                           f"{cover:.0%} of reference tokens")
        else:
            reasons.append(f"{'same brand, ' if bknown else ''}{cover:.0%} of reference tokens")
    if type_conflict and conf == "model":
        differs.append(f"type {cand.ptype} vs ref {ref.ptype}")
        conflicts.append("type")
    # Variant dimensions. A title that names no known colour but ends in a
    # word the other lacks ('... 770NC Sand') may name one: 'sand?'.
    rcol, ccol = ref.colours, cand.colours
    # A vendor colour letter in the shared code (WH-CH520W, WH1000XM5L.CE7,
    # WF-1000XM5 ~ WF1000XM5B) is the colour of a listing whose title names
    # none, and outranks a seller's free-text colour attribute ('Темно сива' on
    # the WH-CH520B), once a title on either side confirms the letter.
    same_hit = bool(code_hit and code_hit[2] in ("equal", "in title", "spaced"))
    if code_hit and (same_hit or (suffix and suffix[1] == "colour")):
        lc = (_letter_colours(code_hit[0], ref) if same_hit
              else _COLOUR_LETTERS.get(suffix[0][:1], set()) if suffix[0][:1] in _COLOUR_LETTERS
              and suffix[0] not in COLOURS else set())
        col = _code_colour(lc, ref, cand) if lc else None
        if col:
            if not _title_colours(ref) and (same_hit or suffix[2] is ref):
                rcol = {col}
            if not _title_colours(cand) and (same_hit or suffix[2] is cand):
                ccol = {col}
    if conf != "exact":
        rcol = rcol or {w + "?" for w in _possible_colours(ref, cand)}
        ccol = ccol or {w + "?" for w in _possible_colours(cand, ref)}
    for name, rv, cv, f in (
            ("capacity", _cap(ref), _cap(cand), lambda x: f"{x}GB"),
            ("RAM", getattr(ref, "ram", None) or set(), getattr(cand, "ram", None) or set(), lambda x: f"{x}GB"),
            ("colour", rcol, ccol, str),
            ("size", ref.units.get("inch", set()), cand.units.get("inch", set()), lambda x: f'{x}"'),
            ("kit", ref.units.get("kit", set()), cand.units.get("kit", set()), str),
            ("region", ref.regions, cand.regions, lambda x: x.upper()),
            ("GPU", getattr(ref, "gpu", None) or set(), getattr(cand, "gpu", None) or set(), str.upper)):
        c, txt = _dim_diff(name, rv, cv, f)
        if txt:
            differs.append(txt)
        if c:
            conflicts.append(name)
    c, txt = _cpu_diff(ref, cand)
    if txt:
        differs.append(txt)
    if c:
        conflicts.append("CPU")
    # Opposite shades of one base colour ('Light Blue' vs 'Dark Blue') are two
    # editions, although both read as blue above.
    rsh, csh = getattr(ref, "shades", None) or set(), getattr(cand, "shades", None) or set()
    for base in sorted({b for b, _ in rsh} & {b for b, _ in csh}):
        rs, cs = {x for b, x in rsh if b == base}, {x for b, x in csh if b == base}
        if rs.isdisjoint(cs):
            differs.append(f"colour {'/'.join(sorted(cs))} {base} vs ref {'/'.join(sorted(rs))} {base}")
            if "colour" not in conflicts:
                conflicts.append("colour")
    for unit in sorted(set(ref.units) | set(cand.units)):
        if unit in ("gb", "inch", "kit", "pct"):
            continue
        rv, cv = ref.units.get(unit, set()), cand.units.get(unit, set())
        if rv and cv and not (rv <= cv or cv <= rv):
            conflicts.append(unit)
            differs.append(f"{unit} {_fmtset(cv, str)} vs ref {_fmtset(rv, str)}")
    # A different tier (Pro, Max, Ti, Plus) is a different model either way.
    # A version the reference does not state (Gen4, V2) is only a difference
    # when the reference names one.
    # With the very same full part number on both sides (or one that begins
    # the other: -WHITE, -GAMING; never a short model name such as AK820 next
    # to which 'Pro' / 'MAX' is written),
    # a tier or version only one title spells out ('OC', 'V2') is an
    # omission: DUAL-RTX5070-O12G already names the OC edition.
    short_code = min(code_hit[:2], key=len) if code_hit else ""
    same_code = bool(code_hit and code_hit[2] in ("equal", "prefix") and len(short_code) >= 9
                     and len(re.findall(r"[a-z]+|\d+", short_code)) >= 4)
    for name, rv, cv in (("tier", ref.tiers, cand.tiers), ("version", ref.versions, cand.versions)):
        if rv != cv:
            bits = [f"+{x}" for x in sorted(cv - rv)] + [f"-{x}" for x in sorted(rv - cv)]
            if name == "version" and not rv:
                differs.append(f"{name} {'/'.join(sorted(cv))} (ref silent)")
                continue
            if same_code and (code_hit[2] == "equal" or rv <= cv or cv <= rv):
                differs.append(f"{name} {' '.join(bits)} (same code)")
                continue
            differs.append(f"{name} {' '.join(bits)}")
            conflicts.append(name)
    if conf == "likely" and miss_crit:
        differs.append("ref number(s) " + ", ".join(sorted(miss_crit)) + " not in title")
    # Variant values the reference leaves open (free text 'MX Master 3S' has no
    # colour): offers are grouped by them so prices are compared like for like.
    open_dims = []
    dims = (("capacity", _cap(ref), _cap(cand), lambda x: f"{x}GB"),
            ("colour", rcol, ccol, str),
            ("size", ref.units.get("inch", set()), cand.units.get("inch", set()), lambda x: f'{x}"'))
    # An EAN match fixes the variant: the open dimensions would only echo what
    # the reference title happens not to spell out ('QE55Q7FAAUXXH' has no 55").
    for name, rv, cv, f in (() if conf == "exact" else dims):
        if not rv and cv:
            open_dims.append(f"{name} {_fmtset(cv, f)}")
    if ean_conflict:
        conflicts.append("ean")
        differs.append(f"EAN {cand.ean} vs ref {ref.ean}")
    group = "same" if conf == "exact" or not conflicts else "variant"
    # what makes it a variant first: tables cut DIFFERS short
    differs.sort(key=lambda x: not any(x.lower().startswith(c.lower()) for c in conflicts))
    score = {"exact": 3.0, "model": 2.0, "likely": 1.0}[conf] + cover
    return {"confidence": conf, "group": group, "reasons": reasons, "differs": differs,
            "conflicts": conflicts, "variant": "; ".join(open_dims) or None,
            "score": round(score, 3), "cover": round(cover, 2)}


def _brand_display(ref_rec, sig):
    """The brand as it should appear in a query: the brand field when its
    words occur in the title, else the title word that matches it."""
    b = (ref_rec or {}).get("brand")
    if b and sig.brand and sig.brand in sig.concat:
        return str(b)
    for orig, f, kind, _pos in sig.kept:
        a = alnum(f)
        if sig.brand and (a == sig.brand or (len(a) >= 3 and sig.brand.startswith(a))):
            return orig
    return None


def build_queries(ref_rec, sig, title, free_text=None):
    """-> [(query, kind)]: model codes (MPN first), the user's own wording for a
    free-text reference, the model phrase with the brand, and a cleaned title
    (brand + first two identifying tokens)."""
    qs, seen = [], set()

    plus_in_title = bool(re.search(r"\w\+", title or "")) and not re.search(r"\bplus\b", title or "", re.I)

    def add(q, kind):
        q = re.sub(r"\s+", " ", q or "").strip()
        if plus_in_title:
            q = re.sub(r"(?<=\w) plus\b", "+", q, flags=re.I)
        k = alnum_words(q)
        if q and k and k not in seen and len(alnum(q)) >= 3:
            seen.add(k)
            qs.append((q, kind))

    ref_rec = ref_rec or {}
    codes = []
    if ref_rec.get("mpn") and _is_strong_code(str(ref_rec["mpn"])):
        codes.append(str(ref_rec["mpn"]))
    for orig, f, kind, _pos in sig.kept:
        if kind == "strong":
            codes.append(orig)
    if ref_rec.get("store") in SKU_IS_MPN and ref_rec.get("sku") and _is_strong_code(str(ref_rec["sku"])):
        codes.append(str(ref_rec["sku"]))
    seen_codes = set()
    for c in codes:
        if alnum(c) not in seen_codes and len(seen_codes) < 2:
            seen_codes.add(alnum(c))
            add(c, "model code")
            # 'WH1000XM5L.CE7': shops that print 'WH-1000XM5L' find only the base code
            mt = _PKG_SPLIT_RE.match(str(c))
            if mt and alnum(mt.group(1)) in (sig.pkg_base or {}):
                add(mt.group(1), "model code")
    if free_text and len(free_text.split()) <= 8:
        # 'Samsung 990 EVO Plus 1TB': the model phrase alone ('Samsung 990')
        # also finds the 990 PRO and the plain 990; the words as typed are the
        # sharpest query on AND-matching engines.
        add(free_text, "text")
    if not seen_codes and sig.derived:
        # 'JBL LIVE 770 NC' -> 770NC, as most shops write it. A leading 4-letter
        # piece is probably a word ('LIVE'), and digits + a 3-letter word
        # ('990 EVO') a series name, so neither is searched as a code.
        dcs = [d for d in sorted(sig.derived, key=len, reverse=True)
               if not re.match(r"[a-z]{4}\d", d) and not re.fullmatch(r"\d+[a-z]{3,4}", d)]
        if dcs:
            add(dcs[0].upper(), "model code")
    brand = _brand_display(ref_rec, sig)
    bw = brand_words(brand) | brand_words((ref_rec or {}).get("brand")) | ({sig.brand} if sig.brand else set())
    phrase = model_phrase(sig, bw)
    if phrase and _phrase_anchored(phrase, sig):
        span = " ".join(w[0] for w in phrase)
        adj = brand_run_before(sig, phrase[0][3], bw)
        if adj:
            add(adj + " " + span, "model phrase")
        else:
            if brand:  # for AND-style search engines
                add(brand + " " + span, "model phrase")
            if len(phrase) >= 2 or len(alnum(span)) >= 4:  # for ordered-phrase engines
                add(span, "model phrase")
    core = [w[0] for w in sig.kept if w[2] in ("word", "weak", "number", "version", "tier")
            and alnum(w[1]) not in bw][:2]
    prefix = [brand] if brand else []
    if len(prefix + core) >= 2 and len(qs) < 4:
        add(" ".join(prefix + core), "title")
    if not qs and title:
        add(" ".join(title.split()[:4]), "title")
    return qs[:4]


_MPN_RE = re.compile(r"(?:\bP/?N|\bMPN|Part\s*(?:No\.?|Number)|Product\s*code|Model\s*(?:No\.?|number))"
                     r"\s*[:#.]?\s*([A-Za-z0-9][A-Za-z0-9\-./]{3,30}[A-Za-z0-9])", re.I)


def _mpn_from_text(text):
    """A manufacturer part number stated in specs text ('PN:910-007501')."""
    for m in _MPN_RE.finditer(text or ""):
        code = m.group(1)
        if re.fullmatch(r"\d{8,14}", code):
            continue  # a bare barcode, not a part number
        if _is_strong_code(code):
            return code
    return None


def _slug_text(url):
    path = urllib.parse.urlsplit(url if "://" in url else "https://" + url).path
    segs = [s for s in path.split("/") if s]
    best = max(segs, key=lambda s: (s.count("-"), len(s))) if segs else ""
    best = re.sub(r"\.(html?|aspx|php)$", "", best)
    words = [w for w in re.split(r"[-_+]+", urllib.parse.unquote(best)) if w and not re.fullmatch(r"\d{5,}", w)]
    return " ".join(words)


def resolve_reference(ctx, ref, timeout):
    """-> dict(kind, record, title, brand, ean, store, notes, status)."""
    ref = ref.strip()
    out = {"input": ref, "kind": None, "record": None, "title": None, "brand": None,
           "ean": None, "mpn": None, "store": None, "notes": [], "status": None}
    digits = re.sub(r"[\s-]", "", ref)
    if re.fullmatch(r"\d{8,14}", digits):
        out["kind"] = "ean"
        out["ean"] = norm_gtin(digits)
        out["ean_raw"] = digits
        if not gtin_check_ok(digits):
            out["notes"].append("the EAN's check digit is invalid; matching it literally")
        return out
    if is_url(ref):
        out["kind"] = "url"
        if not re.match(r"^https?://", ref, re.I):
            ref = "https://" + ref
        k = store_for_url(ref)
        if not k:
            die(f"mkshop match: {ref} is not a URL from a covered shop")
        out["store"] = k
        recs, st = run_details(ctx, [(ref, k, None)], timeout, label="detail (reference)")
        rec = recs[0]
        out["status"] = st.get(k)
        if rec and not rec.get("error"):
            out["record"] = rec
            out["title"] = rec.get("title")
            out["brand"] = rec.get("brand")
            out["ean"] = norm_gtin(rec.get("ean"))
            out["ean_raw"] = rec.get("ean")
            out["mpn"] = rec.get("mpn") or _mpn_from_text(rec.get("specs"))
            if out["mpn"] and not rec.get("mpn"):
                out["notes"].append(f"part number {out['mpn']} read from the product's specs text")
        else:
            out["title"] = _slug_text(ref)
            out["notes"].append(f"could not read the reference product ({rec.get('error') if rec else 'no record'}); "
                                f"matching on the words in its URL: {out['title']!r}")
        return out
    out["kind"] = "text"
    out["title"] = ref
    return out


def _match_store(ctx, key, queries, limit, timeout, exclude_ids):
    """Run a store's queries sequentially. queries: list of (q, kind)."""
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    cands, per_q = {}, []
    warnings = []
    status_bad = None
    for n, (q, kind) in enumerate(queries):
        if n:
            time.sleep(STORE_GAP)
        argv = ["search", q] + list(REGISTRY[key].get("search_args") or []) + ["--limit", str(limit)]
        r = ctx.run(key, argv, deadline - time.monotonic())
        warnings += [w for w in r["warnings"] if w not in warnings]
        recs = as_records(r["data"]) if r["data"] is not None else []
        per_q.append({"query": q, "kind": kind, "status": r["status"],
                      "count": len(recs) if r["status"] == "ok" else None, "message": r["message"],
                      "elapsed_s": r["elapsed_s"]})
        shown = f"ok {len(recs)}" if r["status"] == "ok" else f"{r['status'].upper()}: {r['message']}"
        log(f"[{key}] match search ({kind}) {q!r} -> {shown} ({r['elapsed_s']:.1f}s)")
        for rec in recs:
            clean_record(rec, key)
            rid = rec_id(rec)
            if (key, rid) in exclude_ids:
                continue
            if rid not in cands:
                rec["found_by"] = [q]
                rec["_kinds"] = {kind}
                cands[rid] = rec
            else:
                cands[rid]["found_by"].append(q)
                cands[rid]["_kinds"].add(kind)
        if r["status"] in ("blocked", "timeout") or (r["status"] == "error" and (r["message"] or "").startswith("client not found")):
            status_bad = r
            break
    oks = [e for e in per_q if e["status"] == "ok"]
    if status_bad and not oks:
        status, msg = status_bad["status"], status_bad["message"]
    elif not oks:
        status, msg = (per_q[0]["status"] if per_q else "error"), (per_q[0]["message"] if per_q else "no queries")
        if status == "not_found":
            status = "error"
    elif len(oks) < len(per_q):
        status = "partial"
        msg = "; ".join(f"{e['query']!r}: {e['status']}" for e in per_q if e["status"] != "ok")
    else:
        status, msg = "ok", None
    return {"status": status, "message": msg, "queries": per_q, "count": len(cands), "warnings": warnings[:8],
            "elapsed_s": round(time.monotonic() - t0, 2), "candidates": list(cands.values())}


def _combine_status(phases):
    """phases: list of per-phase dicts for one store -> (status, message)."""
    if not phases:
        return "ok", "not searched: nothing to search for"
    good = [x for x in phases if x["status"] in ("ok", "partial")]
    bad = [x for x in phases if x["status"] not in ("ok", "partial")]
    if good and not bad:
        st = "partial" if any(x["status"] == "partial" for x in good) else "ok"
        return st, "; ".join(x["message"] for x in good if x.get("message")) or None
    if good:
        return "partial", "; ".join(f"{x['status']}: {x['message']}" for x in bad)
    return bad[0]["status"], bad[0]["message"]


def is_bare_code(sig, brand=None):
    """Is a title (almost) only a model code ('SONY WHULT900NB.CE7', Setec's
    style)? Shops that do not index EANs or codes find such a product only by
    its marketed name."""
    bw = brand_words(brand) | ({sig.brand} if sig.brand else set())
    words = [w for w in sig.kept if w[2] in ("word", "weak", "number", "tier", "version")
             and alnum(w[1]) not in bw]
    return bool(sig.strong) and not words


def name_phrase(text, brand=None):
    """The series / marketing name a title gives besides its codes:
    'Слушалки Sony ULT Wear WHULT900N, безжични' -> 'ULT Wear'. The first run of
    up to three adjacent Latin name tokens right after the brand or a model
    code; product nouns and what follows a 'with' ('w/Microphone') are no name.
    None when there is none."""
    s = signature(text, brand)
    bw = brand_words(brand) | ({s.brand} if s.brand else set())
    if not s.brand:
        bw |= infer_brand(s)[1]
    anchor = {w[3] for w in s.kept if alnum(w[1]) in bw or w[2] == "strong"}
    run = []
    for w in s.kept:
        if s.incl_pos is not None and w[3] >= s.incl_pos:
            break
        a = alnum(w[1])
        if w[2] not in ("word", "weak", "number", "tier", "version") or a in bw:
            continue
        noun = a in PRODUCT_TYPES or a in ACCESSORY_WORDS or a in _ACC_PARTS
        if _CYR_RE.search(w[0]) or noun or (run and w[3] != run[-1][3] + 1):
            if run:
                break
            continue
        if not run and w[3] - 1 not in anchor:
            continue
        run.append(w)
        if len(run) == 3:
            break
    if not any(w[2] == "word" and len(alnum(w[1])) >= 2 for w in run):
        return None
    return " ".join(w[0] for w in run)


def marketed_names(brand_shown, ref_rec, confirmed):
    """Marketed-name queries for a bare-code reference -> list of 'Brand Name'
    strings, most supported first. Sources: titles of offers confirmed by EAN
    or model code (weight 2) and the reference's own specs text right after a
    mention of the brand (weight 1)."""
    votes = {}
    shown = {}

    def vote(name, w):
        if not name:
            return
        k = alnum_words(name)
        votes[k] = votes.get(k, 0) + w
        shown.setdefault(k, name)
    brand = (ref_rec or {}).get("brand") or brand_shown
    for c in confirmed:
        vote(name_phrase(c.get("title"), c.get("brand") or brand), 2)
    specs = (ref_rec or {}).get("specs") or ""
    if brand and specs:
        for m in re.finditer(r"(?i)\b" + re.escape(str(brand)) + r"\b\s+([^,;:()\n]{2,60})", specs):
            seg = m.group(1)
            n = name_phrase(f"{brand} {seg}", brand)
            if n and alnum_words(seg).startswith(alnum_words(n)):  # right after the brand
                vote(n, 1)
    out = []
    for k in sorted(votes, key=lambda k: -votes[k]):
        name = shown[k]
        q = name if brand_shown and alnum(brand_shown) in alnum(name) else f"{brand_shown or ''} {name}".strip()
        out.append(q)
    return out


def cmd_match(ctx, a):
    if not a.ref and a.json not in (None, "-") and not re.search(r"\.json$", a.json):
        warn(f"mkshop match: reading {a.json!r} as REF (JSON goes to stdout); put REF before a bare --json")
        a.ref, a.json = a.json, "-"
    if not a.ref:
        die("mkshop match: give REF (a product URL, an EAN, or free text)")
    keys = parse_stores(a.stores, a.exclude)
    timeout = a.timeout or DEFAULT_TIMEOUT
    ctx.infos(keys)  # capabilities (EAN search / EAN in detail), fetched in parallel
    ref = resolve_reference(ctx, a.ref, timeout)
    ref_rec = ref["record"] or {}
    exclude_ids = set()
    if ref_rec and ref.get("store"):
        exclude_ids.add((ref["store"], rec_id(ref_rec)))
    phases = {k: [] for k in keys}
    all_cands = {}
    dead = set()  # blocked / timed out / missing client: do not ask again

    def run_phase(plan, label):
        plan = {k: q for k, q in plan.items() if q and k not in dead}
        if not plan:
            return
        log(f"match: {label} in {len(plan)} store(s)")
        with ThreadPoolExecutor(max_workers=len(plan)) as ex:
            futs = {k: ex.submit(_match_store, ctx, k, qs, a.limit, timeout, exclude_ids)
                    for k, qs in plan.items()}
            for k, f in futs.items():
                res = f.result()
                phases[k].append(res)
                if res["status"] in ("blocked", "timeout") or (
                        res["status"] == "error" and (res["message"] or "").startswith("client not found")):
                    dead.add(k)
                for c in res["candidates"]:
                    kk = (k, rec_id(c))
                    if kk in all_cands:
                        old = all_cands[kk]
                        old["found_by"] += [q for q in c["found_by"] if q not in old["found_by"]]
                        old["_kinds"] |= c["_kinds"]
                    else:
                        all_cands[kk] = c

    ean_skipped = []
    # Phase 1: EAN, where the shop's search matches barcodes (or might).
    if ref["ean"]:
        plan = {}
        for k in keys:
            es = ctx.ean_searchable(k)
            if es is False:
                ean_skipped.append(k)
            else:
                plan[k] = [(f, "ean" if es else "ean?") for f in ean_query_forms(ref["ean"], ref.get("ean_raw"))]
        run_phase(plan, f"EAN {ref['ean']}")
    # With only an EAN, take the reference title from what the EAN found.
    if not ref["title"] and ref["ean"]:
        exact = [c for c in all_cands.values() if norm_gtin(c.get("ean")) == ref["ean"]]
        if not exact:
            per_store = {}
            for (k, _i), c in all_cands.items():
                per_store.setdefault(k, []).append(c)
            exact = [c for k, cs in per_store.items()
                     if ctx.ean_searchable(k) is True and len(cs) <= 2 for c in cs]
        if exact:
            best = max(exact, key=lambda c: (len(record_sig(c).strong), bool(c.get("brand")),
                                             -len(c.get("title") or "")))
            ref["title"], ref["brand"], ref["mpn"] = best.get("title"), best.get("brand"), best.get("mpn")
            ref["notes"].append(f"reference title taken from {best.get('store')}: {best.get('title')!r}")
        else:
            ref["notes"].append("no shop's search returned this EAN, so there is no title to search by")
    extra = [ref_rec["sku"]] if ref_rec.get("store") in SKU_IS_MPN and ref_rec.get("sku") else []

    def ref_sig(prefix=""):
        # the reference title (with an alternative name before it), plus the
        # colour / product type its record states outside the title
        t = f"{prefix} {ref['title'] or ''}".strip()
        return enrich_sig(signature(t, ref["brand"], ref["ean"], ref["mpn"], extra), ref_rec)
    sig_ref = ref_sig()
    qrec = dict(ref_rec or {}, mpn=ref["mpn"] or (ref_rec or {}).get("mpn"))
    if ref["kind"] == "text" and not ref["brand"]:
        # A brand named in the text keys the model phrase and the title query
        # ('Samsung 990 EVO Plus' -> 'Samsung 990', 'Samsung 990 EVO').
        b, _bw = infer_brand(sig_ref)
        shown = next((w[0] for w in sig_ref.kept if alnum(w[1]) == b), None) if b else None
        if shown:
            ref["brand"] = shown
            sig_ref = ref_sig()
            ref["notes"].append(f"brand taken as {shown!r} (a known brand named in the text)")
    queries = build_queries(qrec, sig_ref, ref["title"],
                            free_text=ref["title"] if ref["kind"] == "text" else None) if ref["title"] else []
    # --also: other names the product is sold under (a marketing name for a
    # code, a code for a name). Searched everywhere the EAN did not settle it,
    # and candidates are also scored against "<name> <reference title>".
    alt_names = []
    for t in getattr(a, "also", None) or []:
        t = re.sub(r"\s+", " ", t or "").strip()
        if t and alnum_words(t) not in {alnum_words(q) for q, _k in queries}:
            queries.append((t, "also"))
            alt_names.append(t)
    # Phase 2: model codes, model phrase, cleaned title. Shops that already
    # returned the exact EAN are done.
    exact_stores = {k for (k, _i), c in all_cands.items()
                    if ref["ean"] and norm_gtin(c.get("ean")) == ref["ean"]}
    if queries:
        plan = {k: list(queries) for k in keys if k not in exact_stores}
        run_phase(plan, "queries " + ", ".join(repr(q) for q, _ in queries))
    if not sig_ref.brand and all_cands:
        # Free text: take the brand that candidates file and the text names.
        votes = {}
        for c in all_cands.values():
            b = c.get("brand")
            if b and alnum(b) and _brand_on(alnum(b), sig_ref):
                votes[str(b)] = votes.get(str(b), 0) + 1
        if votes:
            ref["brand"] = max(votes, key=lambda b: (votes[b], -len(b)))
            sig_ref = ref_sig()
            ref["notes"].append(f"brand taken as {ref['brand']!r} (filed so by the shops and named in the text)")
    scored, near = [], []
    alt_sigs = []  # (name, signature of "<name> <reference title>")

    def best_score(c):
        """Score c against the reference and each alternative name; keep the
        best result (confidence, then same product over variant, then score)."""
        csig = record_sig(c)
        best, via = None, None
        for name, sg in [(None, sig_ref)] + alt_sigs:
            res = score_candidate(sg, csig, c, ref_rec or None, via_ean_query="ean" in c["_kinds"])
            rank = ((CONF_RANK[res["confidence"]], res["group"] != "same", name is not None, -res["score"])
                    if res.get("confidence") else (9,))
            if best is None or rank < best[0]:
                best, via = (rank, res), name
        res = best[1]
        if via and res.get("confidence"):
            res["reasons"] = list(res.get("reasons") or []) + [f"via the name {via!r}"]
        return res

    def rescore():
        scored.clear()
        near.clear()
        for (k, _i), c in all_cands.items():
            res = best_score(c)
            if res.get("confidence"):
                c["match"] = res
                scored.append(c)
            else:
                c["match"] = {"confidence": None, "why_not": res.get("why_not"), "cover": res.get("cover")}
                near.append(c)

    alt_sigs[:] = [(n, ref_sig(n)) for n in alt_names]
    rescore()
    verify_status = {}
    # Verify: with an EAN reference, re-read the best candidates that have no
    # EAN in shops whose detail carries one.
    if ref["ean"] and a.verify > 0:
        todo = {}
        for c in sorted(scored, key=lambda c: -c["match"]["score"]):
            k = c["store"]
            if c.get("ean") or k in dead or not ctx.ean_in_detail(k) or not (c.get("url") or c.get("id")):
                continue
            if len(todo.setdefault(k, [])) < a.verify:
                todo[k].append(c)
        todo = {k: v for k, v in todo.items() if v}
        if todo:
            routed, owners = [], []
            for k, cs in todo.items():
                for c in cs:
                    routed.append((c.get("url") or c.get("id"), k, None))
                    owners.append(c)
            log(f"match: confirming EANs of {len(routed)} candidate(s) via detail in {', '.join(todo)}")
            recs, verify_status = run_details(ctx, routed, timeout, label="detail (confirm EAN)")
            for c, d in zip(owners, recs):
                if d and not d.get("error"):
                    c["verified_by_detail"] = True
                    for f in ("ean", "mpn", "warranty"):
                        if d.get(f) and not c.get(f):
                            c[f] = d[f]
            rescore()
    # A bare-code reference ('SONY WHULT900NB.CE7'): shops without EAN or code
    # search list it under its marketed name ('Sony ULT Wear'), which the
    # offers confirmed by EAN / model code, or the reference's specs, give.
    marketed = []
    if ref["title"] and is_bare_code(sig_ref, ref["brand"]):
        confirmed = [c for c in scored if c["match"]["confidence"] in ("exact", "model")
                     and c["match"]["group"] == "same"]
        confirmed.sort(key=lambda c: CONF_RANK[c["match"]["confidence"]])
        brand_shown = _brand_display(ref_rec, sig_ref) or ref["brand"]
        if brand_shown and str(brand_shown).isupper() and len(str(brand_shown)) > 3:
            brand_shown = str(brand_shown).title()
        done = {alnum_words(q) for q, _k in queries}
        marketed = [n for n in marketed_names(brand_shown, ref_rec, confirmed) if alnum_words(n) not in done][:1]
        if marketed:
            name = marketed[0]
            src = "offers confirmed by EAN/model code" if confirmed else "the reference's specs"
            ref["notes"].append(f"the reference title is a bare model code; also searched by its marketed name "
                                f"{name!r} (from {src})")
            exact_now = {c["store"] for c in scored if c["match"]["confidence"] == "exact"}
            plan = {k: [(name, "marketed name")] for k in keys
                    if k not in exact_now and (not ref["ean"] or ctx.ean_searchable(k) is not True)}
            run_phase(plan, f"marketed name {name!r}")
            queries.append((name, "marketed name"))
            alt_sigs.append((name, ref_sig(name)))
            rescore()
    annotate(scored)
    dropped_stock = 0
    if a.in_stock:
        n = len(scored)
        scored = [c for c in scored if c.get("in_stock") is not False]
        dropped_stock = n - len(scored)
    for c in scored + near:
        c.pop("_kinds", None)
    scored.sort(key=lambda c: (0 if c["match"]["group"] == "same" else 1, CONF_RANK[c["match"]["confidence"]],
                               effective_price(c) if effective_price(c) is not None else 1 << 62,
                               uses_member_price(c)))
    near.sort(key=lambda c: -(c["match"].get("cover") or 0))
    statuses = {}
    for k in keys:
        st, msg = _combine_status(phases[k])
        statuses[k] = {"status": st, "message": msg,
                       "count": sum(1 for (s2, _i) in all_cands if s2 == k),
                       "kept": sum(1 for c in scored if c.get("store") == k),
                       "elapsed_s": round(sum(x["elapsed_s"] for x in phases[k]), 2),
                       "queries": [q for x in phases[k] for q in x["queries"]],
                       "warnings": [w for x in phases[k] for w in x.get("warnings", [])][:8]}
        if k in verify_status:
            statuses[k]["verify"] = {kk: verify_status[k].get(kk) for kk in ("status", "count", "message")}
    ref_out = {k: ref[k] for k in ("input", "kind", "store", "title", "brand", "ean", "mpn", "notes")}
    if ref_rec:
        ref_out["record"] = ref_rec
    ref_out["queries"] = [{"query": q, "kind": kind} for q, kind in queries]
    if marketed:
        ref_out["marketed_name"] = marketed[0]
    ref_out["ean_search_skipped"] = ean_skipped
    env = {"command": "match", "generated_at": now_iso(), "reference": ref_out, "member_prices": MEMBER_PRICES, "stores": statuses,
           "count": len(scored),
           "dropped": {"out of stock": dropped_stock} if dropped_stock else {},
           "results": scored, "near_misses": near[:25]}
    if a.json is not None:
        write_json(env, a.json)
    if a.json != "-":
        if a.json is not None:
            print(f"wrote {len(scored)} candidates to {a.json}")
        print_match(env, sys.stdout, a)
        sys.stdout.flush()
        with _print_lock:
            print("", file=sys.stderr)
            print_status_table(statuses, sys.stderr)
    if not ref["title"] and not ref["ean"]:
        return 2
    return overall_exit(statuses)


def print_match(env, out, a):
    ref = env["reference"]
    rec = ref.get("record") or {}
    print(f"REFERENCE ({ref['kind']}): {ref.get('title') or ref['input']}", file=out)
    bits = []
    if rec:
        ut = until_text(rec, always=True)
        bits.append(f"{store_cell(rec)} {price_text(rec)}{' (' + ut + ')' if ut else ''}, "
                    f"stock {stock_cell(rec.get('in_stock'))}")
    for k in ("brand", "ean", "mpn"):
        if ref.get(k):
            bits.append(f"{k} {ref[k]}")
    if bits:
        print("  " + " · ".join(bits), file=out)
    if rec.get("url"):
        print(f"  {rec['url']}", file=out)
    for n in ref.get("notes") or []:
        print(f"  note: {n}", file=out)
    qs = [q["query"] for q in ref.get("queries") or []]
    print(f"  searched: {'EAN ' + ref['ean'] + '; ' if ref.get('ean') else ''}{'; '.join(map(repr, qs)) or '-'}", file=out)
    if ref.get("ean_search_skipped"):
        print(f"  EAN search skipped (shop search ignores EANs): {', '.join(ref['ean_search_skipped'])}", file=out)
    res = env["results"]
    same = [c for c in res if c["match"]["group"] == "same"]
    var = [c for c in res if c["match"]["group"] != "same"]
    near = env.get("near_misses")[:15] if getattr(a, "near", False) and env.get("near_misses") else []

    def cols(rows):
        # price columns per table: a member column only where its rows need one
        return ([("CONF", lambda c: c["match"]["confidence"], "l", None)] + price_columns(rows)
                + [("STORE", store_cell, "l", 24),
                   ("STOCK", lambda c: stock_cell(c.get("in_stock")), "l", None),
                   ("TITLE", lambda c: c.get("title"), "l", a.title_width),
                   ("DIFFERS", lambda c: "; ".join(c["match"].get("differs") or []), "l", 60),
                   ("URL", lambda c: c.get("url"), "l", None)])
    noted = []

    vnoted = []

    def footnote(rows):
        # under each section, for that section's rows; explained in full once
        note = member_footnote(rows, again=bool(noted))
        if note:
            print(note, file=out)
            noted.append(note)
        note = valid_footnote(rows)
        if note and not vnoted:
            print(note, file=out)
            vnoted.append(note)
    print("", file=out)
    if same:
        groups = {}
        for c in same:
            groups.setdefault(c["match"].get("variant") or "", []).append(c)
        stores = sorted({c.get("mirror_of") or c["store"] for c in same})
        head = f"SAME PRODUCT: {len(same)} offer(s) in {len(stores)} shop(s)"
        if len(groups) > 1:
            head += ("; the reference does not fix " + ", ".join(sorted({d.split()[0] for c in same for d in
                                                                           (c["match"].get("variant") or "").split("; ") if d}))
                     + ", so offers are split by it")
        print(head, file=out)
        for g, rows in sorted(groups.items(), key=lambda kv: min((effective_price(c) or 1 << 62) for c in kv[1])):
            sub = f"  {g or 'as referenced'}: {len(rows)} offer(s)" + _group_prices(rows)
            if len(groups) > 1 or g:
                print(sub, file=out)
            else:
                print(sub.strip().replace("as referenced: ", "", 1), file=out)
            render_table(rows, cols(rows), out)
        footnote(same)
    else:
        print("SAME PRODUCT: no offers found", file=out)
    if var:
        print("", file=out)
        print("VARIANTS / RELATED (not the same item: differing colour, capacity, tier, version or EAN):", file=out)
        render_table(var, cols(var), out)
        footnote(var)
    if near:
        print("", file=out)
        print("NEAR MISSES (rejected):", file=out)
        render_table(near, price_columns(near, was=False) + [
            ("STORE", store_cell, "l", 24),
            ("WHY NOT", lambda c: c["match"].get("why_not"), "l", 50),
            ("TITLE", lambda c: c.get("title"), "l", a.title_width),
            ("URL", lambda c: c.get("url"), "l", None)], out)
        footnote(near)
    print(footer_line(env["stores"], len(res), "candidates")
          + ("; exact = same EAN, model = same model code/MPN, likely = same brand + strong title overlap (confirm it)"),
          file=out)


def _group_prices(rows):
    """', cheapest X MKD at S (range A-B)' for one group of same-product offers,
    on effective prices; says so, with the condition, when the cheapest is a
    member price, and adds the cheapest offer without one."""
    priced = [c for c in rows if effective_price(c) is not None]
    if not priced:
        return ""
    best = min(priced, key=lambda c: (effective_price(c), uses_member_price(c)))
    s = f", cheapest {fmt_price(effective_price(best))} MKD at {store_cell(best)}" + until_summary(best)
    if len(priced) > 1:
        lo, hi = min(map(effective_price, priced)), max(map(effective_price, priced))
        s += f" (range {fmt_price(lo)}-{fmt_price(hi)}"
        s += ", member prices included)" if any(uses_member_price(c) for c in priced) else ")"
    if uses_member_price(best):
        plain = min((c for c in rows if c.get("price_mkd") is not None), key=lambda c: c["price_mkd"])
        s += (f"; that is a member price ({member_condition(best)}); without one: "
              f"{fmt_price(plain['price_mkd'])} MKD at {store_cell(plain)}")
    elif not MEMBER_PRICES:
        mem = min((c for c in rows if _member_lower(c)), key=lambda c: c["member_price_mkd"], default=None)
        if mem and mem["member_price_mkd"] < effective_price(best):
            s += (f"; {fmt_price(mem['member_price_mkd'])} MKD at {store_cell(mem)} with a member price "
                  f"({member_condition(mem)}; MEMBER column)")
    return s


# --------------------------------------------------------------------------
# Command: group
# --------------------------------------------------------------------------
# Fields that describe one moment's offer: a record seen in several saved runs
# takes them from its freshest copy only (other fields fill gaps from any copy).
_OFFER_FIELDS = {"price_mkd", "regular_price_mkd", "member_price_mkd", "member_price_condition",
                 "member_price_name", "in_stock", "stock_note", "per_location_stock",
                 "delivery_estimate", "shipping_mkd", "price_valid_until", "member_price_valid_until"}
# mkshop's own annotations: recomputed for the union, never carried over.
_DERIVED_FIELDS = ("match", "_kinds", "group_link", "product", "mirror_of", "model_key", "match_key", "accessory",
                   "search_only",
                   "effective_price_mkd", "price_condition", "effective_price_valid_until", "sources")


def load_group_inputs(paths):
    """Read saved mkshop JSON envelopes (search / list / match / detail /
    group) and plain client JSON lists. -> (items [(freshness, input no,
    record)], inputs info, store statuses the envelopes reported)."""
    items, inputs, env_status = [], [], {}
    walks = {"stores": set(), "inputs": set(), "rows": set()}
    for n, path in enumerate(paths):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except OSError as e:
            die(f"mkshop group: cannot read {path}: {e}")
        except ValueError as e:
            die(f"mkshop group: {path} is not valid JSON: {e}")
        info = {"path": path, "command": None, "generated_at": None, "records": 0, "used": 0, "skipped": 0}
        fresh = os.path.getmtime(path)
        rows = []
        if isinstance(data, dict) and "command" in data:
            cmd = info["command"] = data.get("command")
            ga = parse_until(data.get("generated_at"))
            if ga:
                info["generated_at"] = data["generated_at"]
                fresh = ga.timestamp() if ga.tzinfo else time.mktime(ga.timetuple())
            if cmd == "list":
                stores = data.get("stores") or {}
                store = data.get("store") or (next(iter(stores)) if len(stores) == 1 else None)
                info.update(store=store, category=data.get("category"), filters=data.get("filters") or [])
                # a walk that failed (blocked, not found, error) walked nothing
                if store and (stores.get(store) or {}).get("status", "ok") in ("ok", "partial"):
                    walks["stores"].add(store)
                    walks["inputs"].add(n)
            elif cmd == "group" and data.get("walked"):
                # a saved group keeps its walks: its offers in walked shops without search_only came from one
                walks["stores"].update(data["walked"])
                for pr in data.get("results") or []:
                    for o in (pr.get("offers") or []) if isinstance(pr, dict) else []:
                        if isinstance(o, dict) and o.get("store") in data["walked"] and not o.get("search_only"):
                            walks["rows"].add((o["store"], str(o.get("id") or o.get("url"))))
            if cmd == "group":
                rows = [o for p in data.get("results") or [] if isinstance(p, dict) for o in p.get("offers") or []]
            elif cmd in ("search", "list", "detail", "match"):
                rows = list(data.get("results") or [])
                ref = data.get("reference") or {}
                if cmd == "match" and isinstance(ref.get("record"), dict):
                    rows.append(ref["record"])   # the reference is an offer too
            else:
                warn(f"mkshop group: {path} is a '{cmd}' envelope, which holds no product records; skipped")
            for k, st in (data.get("stores") or {}).items():
                if isinstance(st, dict) and k in REGISTRY:
                    env_status.setdefault(k, []).append((st.get("status"), st.get("message"), path))
        else:
            info["command"] = "records"
            rows = as_records(data)
        for r in rows:
            if not isinstance(r, dict) or r.get("error"):
                info["skipped"] += 1
                continue
            r = dict(r)
            store = r.get("store") if r.get("store") in REGISTRY else (
                store_for_url(r["url"]) if isinstance(r.get("url"), str) and r.get("url") else None)
            if store not in REGISTRY or not r.get("title") or not (r.get("id") or r.get("url")):
                info["skipped"] += 1
                continue
            r["store"] = store
            for f in _DERIVED_FIELDS:
                r.pop(f, None)
            clean_record(r, store)
            items.append((fresh, n, r))
            info["used"] += 1
        info["records"] = len(rows)
        inputs.append(info)
    return items, inputs, env_status, walks


def _filled(v):
    return v not in (None, "", [], {})


def union_records(items):
    """One record per store + id (url when there is no id): the freshest copy,
    with gaps (EAN, MPN, specs, warranty, ...) filled from the other copies.
    A price window is taken from an older copy only when it is an end date and
    its price agrees; 'unknown' (key absent) never becomes 'standing' (null)."""
    by, order = {}, []
    for fresh, n, r in items:
        k = (r["store"], str(r.get("id") or r.get("url")))
        if k not in by:
            order.append(k)
        by.setdefault(k, []).append((fresh, n, r))
    out = []
    for k in order:
        lst = sorted(by[k], key=lambda x: (x[0], sum(1 for v in x[2].values() if _filled(v))), reverse=True)
        base = dict(lst[0][2])
        found = list(base.get("found_by") or [])
        for _f, _n, other in lst[1:]:
            for f, v in other.items():
                if f in _OFFER_FIELDS or f == "found_by" or not _filled(v) or _filled(base.get(f)):
                    continue
                base[f] = v
            for f, pf in (("price_valid_until", "price_mkd"), ("member_price_valid_until", "member_price_mkd")):
                # an end date seen earlier for the same price fills a gap; an older
                # null does not: 'standing' then is no evidence the freshest copy
                # (which could not tell) is standing now
                if f not in base and isinstance(other.get(f), str) and other.get(pf) == base.get(pf):
                    base[f] = other[f]
            found += [q for q in other.get("found_by") or [] if q not in found]
        if found:
            base["found_by"] = found
        base["sources"] = sorted({n for _f, n, _r in lst})
        out.append(clean_record(base, base["store"]))
    return out


def _group_ean(r):
    """A record's EAN when it can key a product: 8-14 digits and not a
    placeholder (0000000000000, 1111111111111). The check digit is not
    required, as in match: two shops printing the same digits is the signal."""
    e = norm_gtin(r.get("ean"))
    return e if e and len(set(e.lstrip("0"))) > 1 else None


def _sig_plus(sig, codes=(), derived=()):
    """A copy of sig that also carries a product's alias codes."""
    s = Sig()
    for k in Sig.__slots__:
        setattr(s, k, getattr(sig, k))
    s.strong = set(sig.strong) | set(codes)
    s.codes = dict(sig.codes)
    for c in codes:
        s.codes.setdefault(c, c.upper())
    s.derived = dict(sig.derived or {})
    for d in derived:
        s.derived.setdefault(d, (-100, -100))
    return s


class _Groups:
    """Union-find over records with per-group EAN, alias codes and members."""

    def __init__(self, recs, sigs, eans):
        self.p = list(range(len(recs)))
        self.members = {i: [i] for i in range(len(recs))}
        self.ean = {i: eans[i] for i in range(len(recs))}
        self.lit = {i: set(sigs[i].strong) for i in range(len(recs))}
        self.der = {i: set(sigs[i].derived or {}) for i in range(len(recs))}

    def find(self, i):
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return a
        if len(self.members[a]) < len(self.members[b]):
            a, b = b, a
        self.p[b] = a
        self.members[a] += self.members.pop(b)
        self.ean[a] = self.ean[a] or self.ean.pop(b)
        self.ean.pop(b, None)
        self.lit[a] |= self.lit.pop(b)
        self.der[a] |= self.der.pop(b)
        return a

    def roots(self):
        return list(self.members)


def _main_tokens(s):
    """The tokens that name a model: codes, short codes, codes written with
    spaces, and the words / numbers of an anchored model phrase (brand, product
    nouns and colours left out)."""
    bw = brand_words(s.brand) | ({s.brand} if s.brand else set())
    out = set(s.strong) | set(s.weak) | set(s.derived or {})
    phrase = model_phrase(s, bw)
    if _phrase_anchored(phrase, s):
        out |= {alnum(w[1]) for w in phrase if w[2] != "tier"}
    return {t for t in out if t and t not in bw and t not in PRODUCT_TYPES and t not in COLOURS}


# Colour words shops use for one finish (a Gjirafa50 'të hirta' for a silver
# BTHS-01-SV): the same for the Gjirafa SKU-twin check only.
_NEAR_COLOUR = {"silver": "grey", "graphite": "grey", "navy": "blue", "cream": "beige"}


def _common_run(a, b):
    """The longest common substring of two codes."""
    best = ""
    for i in range(len(a)):
        for j in range(i + len(best) + 1, len(a) + 1):
            if a[i:j] in b:
                best = a[i:j]
            else:
                break
    return best


def _twin_ok(sa, sb):
    """Gjirafa50 and ZirafaMall list one product under one SKU, but ZirafaMall's
    data has errors (Gembird headphones under the SKU of a brake cylinder, a
    blue headset under a black one's). The twin holds unless the titles say
    otherwise: the product types (from the titles) differ, the colours conflict,
    or brand and model tokens do not agree. A rebrand (HP / Poly, Dell /
    Alienware) passes when a model code or two model tokens with a number agree.
    -> (ok, why)."""
    ta, tb = getattr(sa, "tptype", None), getattr(sb, "tptype", None)
    if ta and tb and ta != tb:
        return False, f"product type {tb} vs {ta}"
    ca, cb = _title_colours(sa), _title_colours(sb)
    if ca and cb and {_NEAR_COLOUR.get(c, c) for c in ca}.isdisjoint({_NEAR_COLOUR.get(c, c) for c in cb}):
        return False, f"colour {'/'.join(sorted(cb))} vs {'/'.join(sorted(ca))}"
    for base in {b for b, _ in sa.shades or ()} & {b for b, _ in sb.shades or ()}:
        if {x for b, x in sa.shades if b == base}.isdisjoint({x for b, x in sb.shades if b == base}):
            return False, f"colour shades of {base} differ"
    # not one word in common ('Kufje Gembird MHS-U-001' / 'Главен цилиндар за сопирачки за Forte')
    if not any(len(t) >= 3 and has_tok(t, y) for x, y in ((sa, sb), (sb, sa)) for t in x.tokens | x.strong):
        return False, "no word in common"
    bok, bknown = _brand_ok(sa, sb)
    if bok and not bknown:   # one side files no brand: its first word may be the other's sub-brand (Alienware)
        bknown = any(fw and len(fw) >= 3 and has_tok(fw, y) for fw, y in ((_first_word(sa), sb), (_first_word(sb), sa)))
    ma, mb = _main_tokens(sa), _main_tokens(sb)
    shared = (ma & mb) | {t for t in ma if _present(t, sa, sb) or t in sb.versions} \
        | {t for t in mb if _present(t, sb, sa) or t in sa.versions}
    hit = find_code_hit(sa, sb)
    if not bok:
        if hit or (len(shared) >= 2 and any(re.search(r"\d", t) for t in shared)):
            return True, None
        return False, f"brand {sb.brand} vs {sa.brand}"
    if ma and mb:
        # a typo in one shop's code ('PR-HJE125E' for RP-HJE125E) still shares a long run
        typo = any(len(x) >= 5 and len(re.findall(r"\d", x)) >= 3 for ca_ in sa.strong for cb_ in sb.strong
                   for x in [_common_run(ca_, cb_)])
        return (True, None) if (shared or hit or typo) else (False, "model " + "/".join(sorted(mb))[:40]
                                                             + " vs " + "/".join(sorted(ma))[:40])
    if bknown or not (sa.brand or sb.brand):
        return True, None
    return False, f"brand {sa.brand or sb.brand} not named in the other title"


def _sample(members, recs, eans, k=4):
    """Up to k members, one per shop first, those with an EAN / codes first."""
    order = sorted(members, key=lambda i: (eans[i] is None, -len(recs[i].get("title") or "")))
    out, seen = [], set()
    for i in order:
        if recs[i]["store"] not in seen:
            out.append(i)
            seen.add(recs[i]["store"])
    out += [i for i in order if i not in out]
    return out[:k]


def _full_config_code(code, sig):
    """Does a laptop / PC code fix the configuration (X1504VA-BQ4105,
    FA506NCG-HN207, Lenovo's 83K10095RM) rather than name the platform that
    several configurations share (X1504VA, FA507NU, 15ABR8)?"""
    if len(code) >= 10:
        return True
    w = sig.codes.get(code, "")
    mt = re.search(r"[-\s]([A-Za-z0-9]+)$", w)
    return bool(mt and len(mt.group(1)) >= 3 and re.search(r"\d", mt.group(1))
                and len(alnum(w)) - len(mt.group(1)) >= 4)


def _has_config(s):
    return bool((s.cpu or s.cpu_tier) and s.ram and s.storage)


def _overlap(sa, sb):
    """The larger share of either title's identifying tokens found in the other."""
    out = 0.0
    for x, y in ((sa, sb), (sb, sa)):
        a = (x.tokens | x.strong) - {sa.brand, sb.brand, ""}
        if a:
            out = max(out, sum(1 for t in a if has_tok(t, y)) / len(a))
    return out


def _median_price(members, recs):
    ps = sorted(p for p in (effective_price(recs[i]) for i in members) if p)
    return ps[len(ps) // 2] if ps else None


PRICE_APART = 3   # a code join between offers this many times apart in price is refused


def _join_check(g, a, b, recs, sigs, eans, strict=False):
    """May groups a and b be one product? Every sampled pair must match on a
    model code (aliases included) with no variant conflict. -> (ok, why,
    (open, colour open)): open counts variant values one side states and the
    other leaves out (a colour-less DUAL-RTX5070-O12G vs a White listing), so
    the better fitting of several candidates can win. Refused besides: an
    accessory or spare part next to the product (also when the prices are
    PRICE_APART times apart), a code that only begins the other without the
    same brand and most of the title, a version one side states, a laptop / PC
    joined by a prefix, by a CPU / GPU code or by a platform code without the
    same configuration stated on both sides. strict: also refuse any stated
    difference (code spelling aside), for breaking a tie."""
    la, lb = g.lit[a], g.lit[b]
    da, db = g.der[a] - la, g.der[b] - lb
    open_, colour_open = 0, False
    pa, pb = _median_price(g.members[a], recs), _median_price(g.members[b], recs)
    if pa and pb and min(pa, pb) * PRICE_APART < max(pa, pb):
        return False, (f"prices {fmt_price(min(pa, pb))} vs {fmt_price(max(pa, pb))} MKD, over {PRICE_APART}x apart: "
                       "an accessory, a part or a set, not the same item"), (0, False)
    for i in _sample(g.members[a], recs, eans):
        si = _sig_plus(sigs[i], la, da)
        for j in _sample(g.members[b], recs, eans):
            sj = _sig_plus(sigs[j], lb, db)
            res = score_candidate(si, sj, recs[j])
            if not res.get("confidence"):
                if bool(sigs[i].accessory) != bool(sigs[j].accessory):
                    return False, (f"one is an accessory or spare part ({sigs[i].accessory or sigs[j].accessory}) "
                                   "for the other"), (0, False)
                return False, res.get("why_not") or "no match", (0, False)
            if res["group"] != "same":
                return False, "; ".join(res.get("differs") or []) or "variant", (0, False)
            if res["confidence"] == "likely":
                return False, "titles only", (0, False)
            if strict and [x for x in res.get("differs") or [] if not x.startswith("code ")]:
                return False, "; ".join(res["differs"]), (0, False)
            hit = find_code_hit(si, sj)
            comp = (sigs[i].system and not sigs[i].accessory) or (sigs[j].system and not sigs[j].accessory)
            if comp and any(x.startswith("code ") and " vs ref " in x for x in res.get("differs") or []):
                # G614PR ~ G614PR-RV132W: laptop / PC configurations share
                # the model's prefix but differ in CPU, memory or storage
                return False, "a laptop / PC code joins only in full, not as a prefix", (0, False)
            if hit and comp:
                code = _code(si, hit[0])
                if _CPU_CODE_RE.match(hit[0]) or _GPU_CODE_RE.match(hit[0]):
                    return False, f"{code} names a CPU / GPU many systems share, not this one", (0, False)
                if not (_full_config_code(hit[0], si) or _full_config_code(hit[1], sj)) \
                        and not (_has_config(sigs[i]) and _has_config(sigs[j])):
                    # FA507NU Ryzen 5 vs FA507NU Ryzen 7: one platform, several configurations
                    return False, (f"{code} names a laptop / PC platform; configurations join only with CPU, RAM "
                                   "and storage stated alike on both sides"), (0, False)
            if hit and hit[2] == "prefix":
                if not _brand_ok(si, sj)[1] or _overlap(si, sj) < 0.5:
                    return False, (f"code {_code(si, hit[0])} only begins {_code(sj, hit[1])}, and brand or title "
                                   "do not confirm it"), (0, False)
            short = min(hit[:2], key=len) if hit else ""
            same_code = bool(hit and hit[2] in ("equal", "prefix") and len(short) >= 9
                             and len(re.findall(r"[a-z]+|\d+", short)) >= 4)
            one_sided = [x for x in res.get("differs") or [] if re.fullmatch(r"version .* \(ref silent\)", x)]
            if one_sided and not same_code:
                # B850M-PLUS II vs B850M-PLUS WIFI: a version is stated on one side only
                return False, f"{one_sided[0].replace(' (ref silent)', '')} stated on one side only", (0, False)
            opened = [x for x in (res.get("variant") or "").split("; ") if x] \
                + [x for x in res.get("differs") or [] if "not stated" in x]
            # a colour only one side states: black is the plain edition most
            # titles leave out; any other colour is an edition of its own
            odd = [x for x in opened if x.startswith("colour") and "?" not in x and not re.fullmatch(
                r"colour (black|not stated \(ref black\))", x)]
            if odd:
                return False, f"{odd[0]} (colour stated on one side only)", (0, False)
            open_ = max(open_, len(opened))
            colour_open = colour_open or any(x.startswith("colour") for x in opened)
    return True, None, (open_, colour_open)


def group_records(recs):
    """Group records into products: same EAN; Gjirafa50 / ZirafaMall SKU
    twins; then model codes / MPNs / part numbers, where every code a group
    carries (its members' title codes, MPNs, part numbers from specs) is an
    alias of that product, so a shop that prints only the vendor part number
    joins the shop that prints only the marketing code. Codes join exactly, as
    a 6+ character prefix (QE55Q60D ~ QE55Q60DAUXXH) or written with spaces.
    A join must pass score_candidate for sampled member pairs with no variant
    conflict (colour, capacity, size, tier, version, region, CPU / RAM /
    storage / GPU, part number, name next to the code) and none of
    _join_check's refusals; a SKU twin must pass _twin_ok. Of several
    candidates the best fitting wins (an EAN product before a listing without
    one); a tie joins nothing unless the tied listings name the same colour and
    agree on everything. -> (groups [list of record indices], sigs, eans, hints
    [(record a, record b, code, why)], the union-find)."""
    sigs = []
    for r in recs:
        s = record_sig(r)
        pn = _mpn_from_text(r.get("specs")) if r.get("specs") else None
        if pn and alnum(pn) not in s.strong:
            s.strong.add(alnum(pn))
            s.codes.setdefault(alnum(pn), pn.upper())
        sigs.append(s)
    eans = [_group_ean(r) for r in recs]
    g = _Groups(recs, sigs, eans)
    by_ean = {}
    for i, e in enumerate(eans):
        if e:
            by_ean.setdefault(e, []).append(i)
    for idx in by_ean.values():
        for i in idx[1:]:
            g.union(idx[0], i)
    by_sku = {}
    for i, r in enumerate(recs):
        if r["store"] in ("gjirafa50", "zirafamall") and r.get("sku"):
            by_sku.setdefault(str(r["sku"]).strip().lower(), []).append(i)
    hints = []
    for sku, idx in by_sku.items():
        for i in idx[1:]:
            a, b = g.find(idx[0]), g.find(i)
            if a != b and not (g.ean.get(a) and g.ean.get(b) and g.ean[a] != g.ean[b]) \
                    and {recs[idx[0]]["store"], recs[i]["store"]} == {"gjirafa50", "zirafamall"}:
                ok, why = _twin_ok(sigs[idx[0]], sigs[i])
                if ok:
                    g.union(a, b)
                else:
                    # ZirafaMall data errors: not the same product, so not a mirror either
                    hints.append((idx[0], i, sku, f"same Gjirafa SKU {sku}, but the titles disagree ({why})"))
                    for k in (idx[0], i):
                        if recs[k]["store"] == "zirafamall":
                            recs[k]["mirror_of"] = None
    for _round in range(6):
        idx, didx = {}, {}
        for root in g.roots():
            for c in g.lit[root]:
                idx.setdefault(c, set()).add(root)
            for d in g.der[root] - g.lit[root]:
                didx.setdefault(d, set()).add(root)
        codes_sorted = sorted(idx)
        merged = False
        # Only groups without an EAN look for a product to join (two EAN groups
        # never merge), so a listing that fits two EAN products equally stays
        # apart instead of being pulled in from either side.
        for root in sorted((r for r in g.roots() if not g.ean.get(r)), key=lambda r: (len(g.members[r]), r)):
            if root not in g.members or g.ean.get(g.find(root)):
                continue  # joined another group this round
            targets = {}   # group -> (code, 2 = same code, 1 = prefix)

            def add_t(ts, c, strength):
                for t in ts:
                    if targets.get(t, (None, 0))[1] < strength:
                        targets[t] = (c, strength)
            for c in g.lit[root]:
                add_t(idx.get(c, ()), c, 2)
                add_t(didx.get(c, ()), c, 2)   # another group writes it with spaces
                if len(c) >= 6:
                    k = bisect.bisect_left(codes_sorted, c)
                    while k < len(codes_sorted) and codes_sorted[k].startswith(c):
                        add_t(idx[codes_sorted[k]], c, 1)
                        k += 1
                    for n in range(6, len(c)):
                        if c[:n] in idx:
                            add_t(idx[c[:n]], c[:n], 1)
            for d in g.der[root] - g.lit[root]:
                add_t(idx.get(d, ()), d, 2)
            me = g.find(root)
            cands = {}
            for t, (c, strength) in targets.items():
                t = g.find(t)
                if t == me or (g.ean.get(t) and g.ean.get(me) and g.ean[t] != g.ean[me]):
                    continue
                if cands.get(t, (None, 0))[1] < strength:
                    cands[t] = (c, strength)
            passing, colour_sibs = [], 0
            for t, (c, strength) in cands.items():
                ok, why, open_ = _join_check(g, me, t, recs, sigs, eans)
                if ok:
                    # equal fit: an EAN product wins over another listing without
                    # one (that listing fits it too and follows); two EAN products tie
                    passing.append(((open_[0], -strength, 0 if g.ean.get(t) else 1), t, c, open_[1]))
                else:
                    hints.append((me, t, c, why))
                    colour_sibs += "colour stated on one side only" in why
            # Several fit: the one whose variant values agree best wins, then
            # the same code over a prefix (DUAL-RTX5070-O12G joins the group
            # that prints exactly that, not the -WHITE edition it begins); a
            # tie is ambiguous and joins nothing.
            # A listing silent on colour joins a Black product only when no
            # other colour of the model is in play (JBL T520BT comes in four).
            passing.sort(key=lambda x: x[0])
            if passing and passing[0][3] and colour_sibs:
                for _k, t, c, _o in passing:
                    hints.append((me, t, c, "colour not stated, and the model comes in several colours"))
            elif passing and (len(passing) == 1 or passing[0][0] < passing[1][0]):
                g.union(me, passing[0][1])
                merged = True
                for _k, t, c, _o in passing[1:]:
                    hints.append((me, t, c, "the code also fits this product, less well"))
            elif passing:
                # a tie among listings that would all join each other and name
                # the same colour (three listings of one product, none with an
                # EAN) is no ambiguity: join one, the rest follow. Colourways the
                # vocabulary does not know ('Кристал Вино Касис') stay apart.
                tied = [x for x in passing if x[0] == passing[0][0]]
                cols = [set().union(*(_title_colours(sigs[i]) for i in g.members[r]))
                        for r in [me] + [t for _k, t, _c, _o in tied]]
                roots = [me] + [t for _k, t, _c, _o in tied]
                if not any(g.ean.get(t) for t in roots) and cols[0] and all(c == cols[0] for c in cols) \
                        and all(_join_check(g, x, y, recs, sigs, eans, strict=True)[0]
                                for n_, x in enumerate(roots) for y in roots[n_ + 1:]):
                    g.union(me, tied[0][1])
                    merged = True
                else:
                    for _k, t, c, _o in passing:
                        hints.append((me, t, c, "the code fits several products equally"))
        if not merged:
            break
    groups = [sorted(m) for m in g.members.values()]
    # hints between records that ended in different groups
    out_hints, seen = [], set()
    noise = ("brand differs", "accessory (", "the product, not an accessory", "a set/combo", "a single product",
             "titles only", "no match", "reference has no", "title overlap", "too few", "candidate title")
    for a, b, c, why in hints:
        ga, gb = g.find(a), g.find(b)
        if why and why.startswith(noise):
            continue  # not the same kind of thing at all: no lead worth showing
        if ga != gb and (min(ga, gb), max(ga, gb)) not in seen:
            seen.add((min(ga, gb), max(ga, gb)))
            out_hints.append((ga, gb, c, why))
    return groups, sigs, eans, out_hints, g


def _offer_key(r):
    p = effective_price(r)
    return (p is None, p if p is not None else 0, uses_member_price(r), STORE_KEYS.index(r["store"])
            if r.get("store") in REGISTRY else 99)


def _rep_title(members, recs, sigs):
    """The title that names the product best: mostly Latin, with the brand and
    a model code, of ordinary length."""
    def score(i):
        t = recs[i].get("title") or ""
        letters = re.findall(r"[^\W\d_]", t)
        latin = sum(1 for ch in letters if ch.isascii()) / max(1, len(letters))
        b = alnum(recs[i].get("brand"))
        return (latin >= 0.9, bool(sigs[i].strong), bool(b and b in alnum(t)), latin >= 0.6, -abs(len(t) - 50))
    return recs[max(members, key=score)].get("title")


def _link_of(i, members, recs, sigs, eans, product_ean):
    """How record i belongs to its product: ean | gjirafa sku | code X."""
    if len(members) == 1:
        return "single"
    if eans[i] and eans[i] == product_ean:
        return "ean"
    r = recs[i]
    if r["store"] in ("gjirafa50", "zirafamall") and r.get("sku"):
        sk = str(r["sku"]).strip().lower()
        if any(recs[j]["store"] != r["store"] and str(recs[j].get("sku") or "").strip().lower() == sk
               for j in members if j != i):
            return "gjirafa sku"
    others = set().union(*(sigs[j].strong for j in members if j != i))
    oder = set().union(*(set(sigs[j].derived or {}) for j in members if j != i))
    common = sigs[i].strong & others
    if common:
        c = max(common, key=len)
        return "code " + sigs[i].codes.get(c, c.upper())
    sp = (set(sigs[i].derived or {}) & others) | (sigs[i].strong & oder)
    if sp:
        return "code " + max(sp, key=len).upper() + " (spaced)"
    for c in sorted(sigs[i].strong, key=len, reverse=True):
        for o in others:
            if _codes_match(c, o) == "prefix":
                return "code " + sigs[i].codes.get(c, c.upper()) + " (prefix)"
    return "code (via the product's aliases)"


def build_product(members, recs, sigs, eans):
    ean = next((eans[i] for i in members if eans[i]), None)
    offers = []
    for i in members:
        o = dict(recs[i])
        o["group_link"] = _link_of(i, members, recs, sigs, eans, ean)
        offers.append(o)
    offers.sort(key=_offer_key)
    # alias codes, the ones most shops print first; a code that is the
    # prefix of a listed one adds nothing
    per = {}
    for i in members:
        for c in sigs[i].strong:
            per.setdefault(c, set()).add(recs[i]["store"])
    codes = []
    for c in sorted(per, key=lambda c: (-len(per[c]), -len(c))):
        if any(x.startswith(c) for x in codes):
            continue
        codes.append(c)
    shown = {}
    for i in members:
        for c, w in sigs[i].codes.items():
            shown.setdefault(c, w)
    brands = {}
    for i in members:
        b = recs[i].get("brand")
        if b:
            brands[str(b)] = brands.get(str(b), 0) + 1
    return {"title": _rep_title(members, recs, sigs),
            "brand": max(brands, key=lambda b: (brands[b], -len(b))) if brands else None,
            "ean": ean, "codes": [shown.get(c, c.upper()) for c in codes[:6]],
            "model_key": recs[members[0]].get("model_key"), "offers": offers}


def summarize_product(p):
    """Recompute a product's summary fields from its (filtered) offers."""
    offers = p["offers"]
    priced = [o for o in offers if effective_price(o) is not None]
    best = priced[0] if priced else (offers[0] if offers else None)
    shops = []
    for o in offers:
        s = o.get("mirror_of") or o["store"]
        if s not in shops:
            shops.append(s)
    stock = {"yes": sum(1 for o in offers if o.get("in_stock") is True),
             "no": sum(1 for o in offers if o.get("in_stock") is False),
             "unknown": sum(1 for o in offers if o.get("in_stock") is None)}
    p["n_offers"] = len(offers)
    p["n_shops"] = len(shops)
    p["shops"] = shops
    p["in_stock"] = stock
    p["mirrors"] = [{"store": o["store"], "seller": o.get("seller"), "mirror_of": o["mirror_of"], "id": o.get("id")}
                    for o in offers if o.get("mirror_of")]
    p["links"] = {}
    for o in offers:
        k = o["group_link"].split(" ")[0] if o["group_link"] != "gjirafa sku" else "gjirafa sku"
        p["links"][k] = p["links"].get(k, 0) + 1
    if best is None:
        p["best"] = None
    else:
        known, until = effective_valid_until(best)
        p["best"] = {"store": best["store"], "seller": best.get("seller"), "effective_price_mkd": effective_price(best),
                     "price_mkd": best.get("price_mkd"), "member_price": bool(uses_member_price(best)),
                     "price_condition": best.get("price_condition"), "in_stock": best.get("in_stock"),
                     "mirror_of": best.get("mirror_of"), "title": best.get("title"), "url": best.get("url")}
        if known:
            p["best"]["effective_price_valid_until"] = until
    p["member_price"] = bool(best and uses_member_price(best))
    return p


def cmd_group(ctx, a):
    files = list(a.files)
    argv = sys.argv[1:]
    if a.json not in (None, "-") and os.path.isfile(a.json) and "--json" in argv and a.json in argv \
            and argv.index("--json") < min((argv.index(f) for f in files if f in argv), default=len(argv)) \
            and (not files or os.path.getsize(a.json) > 0):
        # `group --json a.json b.json`: the optional PATH swallowed an input file
        warn(f"mkshop group: reading {a.json!r} as an input FILE (JSON goes to stdout); "
             "put FILEs before a bare --json")
        files, a.json = [a.json] + files, "-"
    if not files:
        die("mkshop group: give one or more saved JSON FILEs (mkshop --json output or client JSON lists)")
    items, inputs, env_status, walks = load_group_inputs(files)
    recs = union_records(items)
    if not recs:
        die("mkshop group: no product records in " + ", ".join(files)
            + " (give saved search / list / match / detail envelopes or client JSON lists)")
    annotate(recs)
    log(f"group: {len(items)} records from {len(files)} file(s), {len(recs)} after the union by store + id")
    # A record from a shop you walked by category that none of the saved walks holds: the
    # walk missed it (another category, a filter, a blank attribute) or it is an accessory.
    walked = walks["stores"]
    for r in recs:
        if (r["store"] in walked and not set(r.get("sources") or []) & walks["inputs"]
                and (r["store"], str(r.get("id") or r.get("url"))) not in walks["rows"]):
            r["search_only"] = True
        else:
            r.pop("search_only", None)
    search_only_all = sum(1 for r in recs if r.get("search_only"))
    if a.only_search and not walked:
        die("mkshop group: --only-search needs at least one saved `list` envelope among the FILEs")
    groups, sigs, eans, hints, _g = group_records(recs)
    products = [build_product(m, recs, sigs, eans) for m in groups]
    root_of = {}
    for n, m in enumerate(groups):
        products[n]["_group"] = n
        for i in m:
            root_of[i] = n
    # filters on offers (the grouping itself never depends on them)
    dropped = {}

    def drop(why):
        dropped[why] = dropped.get(why, 0) + 1
    for p in products:
        kept = []
        for o in p["offers"]:
            e = effective_price(o)
            if a.in_stock and o.get("in_stock") is False:
                drop("out of stock")
            elif a.min_price is not None and (e is None or e < a.min_price):
                drop("below --min-price")
            elif a.max_price is not None and (e is None or e > a.max_price):
                drop("above --max-price")
            elif a.only_search and not o.get("search_only"):
                drop("in a category walk (--only-search)")
            else:
                kept.append(o)
        if a.hide_mirrors:
            own = {o["store"] for o in kept if not o.get("mirror_of")}
            n0 = len(kept)
            kept = [o for o in kept if not (o.get("mirror_of") and o["mirror_of"] in own)]
            if n0 > len(kept):
                dropped["mirror offer (--hide-mirrors; its shop's own offer is listed)"] = \
                    dropped.get("mirror offer (--hide-mirrors; its shop's own offer is listed)", 0) + n0 - len(kept)
        p["offers"] = kept
        summarize_product(p)
    products = [p for p in products if p["offers"]]
    big = 1 << 62
    if a.sort == "shops":
        products.sort(key=lambda p: (-p["n_shops"], (p["best"] or {}).get("effective_price_mkd") or big))
    elif a.sort == "title":
        products.sort(key=lambda p: fold(p["title"]))
    else:
        products.sort(key=lambda p: ((p["best"] or {}).get("effective_price_mkd") is None,
                                     (p["best"] or {}).get("effective_price_mkd") or 0, -p["n_shops"]))
    for n, p in enumerate(products, 1):
        p["product"] = n
        for o in p["offers"]:
            o["product"] = n
    # related products: kept apart although they share a code (hints are
    # (record, record) of the two groups; a product filtered away has none)
    prod_of_group = {}
    for p in products:
        prod_of_group[p["_group"]] = p["product"]
    related = []
    seen = set()
    shown_code = {}
    for sg in sigs:
        for c, w in sg.codes.items():
            shown_code.setdefault(c, w)
    for ga, gb, c, why in hints:
        pa, pb = prod_of_group.get(root_of.get(ga)), prod_of_group.get(root_of.get(gb))
        if pa and pb and pa != pb and (min(pa, pb), max(pa, pb)) not in seen:
            seen.add((min(pa, pb), max(pa, pb)))
            related.append({"products": [min(pa, pb), max(pa, pb)],
                            "code": shown_code.get(c, c.upper()) if not why.startswith("same Gjirafa SKU") else None,
                            "why_apart": why})
    # same model name in the titles but no EAN / code joins them (a shop that
    # lists no code, an EAN only in detail): a lead, never a merge
    by_key = {}
    for p in products:
        for k in {o.get("model_key") for o in p["offers"] if o.get("model_key")}:
            if re.search(r"\d", k.split(":")[2] if k.count(":") >= 2 else ""):
                by_key.setdefault(k, []).append(p["product"])
    pcodes = {p["product"]: {alnum(c) for c in p["codes"]} for p in products}
    for k, ps in by_key.items():
        for x in ps[1:]:
            pair = (min(ps[0], x), max(ps[0], x))
            ca, cb = pcodes[pair[0]], pcodes[pair[1]]
            if ca and cb and not any(_codes_match(u, v) for u in ca for v in cb):
                continue  # different part numbers: known to be different products
            if pair not in seen:
                seen.add(pair)
                related.append({"products": list(pair), "code": None,
                                "why_apart": f"same model name in the titles ({k}) but no EAN or code links them: "
                                             "confirm with detail (EAN / MPN) or match"})
    for p in products:
        p["related"] = [{"product": x["products"][1] if x["products"][0] == p["product"] else x["products"][0],
                         "code": x["code"], "why_apart": x["why_apart"]}
                        for x in related if p["product"] in x["products"]]
        p.pop("_group", None)
    offers_all = [o for p in products for o in p["offers"]]
    statuses = {}
    for k in STORE_KEYS:
        seen_st = env_status.get(k, [])
        n_rec = sum(1 for r in recs if r["store"] == k)
        if not seen_st and not n_rec:
            continue
        good = [x for x in seen_st if x[0] in ("ok", "partial")]
        bad = [x for x in seen_st if x[0] not in ("ok", "partial")]
        st = "ok" if good or not seen_st else bad[0][0]
        msg = "; ".join(f"{x[0]} in {os.path.basename(x[2])}" + (f": {x[1]}" if x[1] else "") for x in bad) or None
        statuses[k] = {"status": st, "count": n_rec, "kept": sum(1 for o in offers_all if o["store"] == k),
                       "message": msg, "warnings": [], "elapsed_s": 0.0}
    search_only = {}
    if walked:
        for o in offers_all:
            if o.get("search_only"):
                search_only[o["store"]] = search_only.get(o["store"], 0) + 1
    env = {"command": "group", "generated_at": now_iso(), "inputs": inputs, "member_prices": MEMBER_PRICES,
           "filters": {k: v for k, v in (("in_stock", a.in_stock), ("min_price", a.min_price),
                                         ("max_price", a.max_price), ("hide_mirrors", a.hide_mirrors),
                                         ("only_search", a.only_search), ("sort", a.sort)) if v not in (None, False)},
           "dropped": dropped, "records": len(recs), "count": len(products), "offers": len(offers_all),
           "walked": sorted(walked), "search_only": search_only, "search_only_before_filters": search_only_all,
           "related": related, "stores": statuses, "results": products}
    if a.csv:
        rows = [dict(o, product_title=p["title"], product_codes=" ".join(([f"EAN {p['ean']}"] if p["ean"] else [])
                                                                         + p["codes"][:3]),
                     product_shops=p["n_shops"]) for p in products for o in p["offers"]]
        write_csv(rows, a.csv, lead=("product", "product_title", "product_codes", "product_shops", "group_link"))
    if a.json is not None:
        write_json(env, a.json)
    if a.json != "-" and a.csv != "-":
        wrote = [p for p in (a.json, a.csv) if p]
        if not (wrote and a.no_table):
            print_group(env, sys.stdout, a)
        if wrote:
            print(f"wrote {len(products)} products ({len(offers_all)} offers) to " + ", ".join(wrote))
        if wrote and a.no_table:
            print(_group_footer(env))
        sys.stdout.flush()
        with _print_lock:
            print("", file=sys.stderr)
            render_table(list(statuses), [
                ("store", lambda k: k, "l", None),
                ("status", lambda k: statuses[k]["status"], "l", None),
                ("records", lambda k: statuses[k]["count"], "r", None),
                ("kept", lambda k: statuses[k]["kept"], "r", None),
                ("note", lambda k: statuses[k]["message"] or "", "l", 150)], sys.stderr)
    return 0


GROUP_SEARCH_ONLY_SHOW = 15


def print_search_only(env, out, a):
    """What search found in a shop you walked by category that none of the walks holds."""
    if not env.get("walked") or all(i["command"] == "list" for i in env["inputs"]):
        return
    offers = [o for p in env["results"] for o in p["offers"] if o.get("search_only")]
    if not offers:
        n = env.get("search_only_before_filters") or 0
        print("FOUND ONLY BY SEARCH: " + (f"{n} offers, all removed by the filters" if n else
              "nothing; every search hit in " + ", ".join(env["walked"]) + " is in a saved category walk"), file=out)
        return
    per = {}
    for o in offers:
        n = per.setdefault(o["store"], [0, 0])
        n[0] += 1
        n[1] += bool(o.get("accessory"))
    print("", file=out)
    print("FOUND ONLY BY SEARCH (in shops you walked by category, but in none of the saved walks: another "
          "category, a filter or a blank attribute dropped them, or they are accessories): "
          + ", ".join(f"{k} {n}" + (f" ({acc} accessories)" if acc else "") for k, (n, acc) in per.items()),
          file=out)
    offers.sort(key=lambda o: (bool(o.get("accessory")), effective_price(o) is None, effective_price(o) or 0))
    rows = offers[:GROUP_SEARCH_ONLY_SHOW]
    cols = ([("#", lambda o: o["product"], "r", None)] + price_columns(rows, was=False)
            + [("STORE", store_cell, "l", 24),
               ("ACC", lambda o: o.get("accessory") or "", "l", 14),
               ("CATEGORY", lambda o: _path_tail(o.get("category") or "", 40), "l", None),
               ("TITLE", lambda o: o.get("title"), "l", a.title_width),
               ("URL", lambda o: o.get("url"), "l", None)])
    render_table(rows, cols, out)
    if len(offers) > len(rows):
        print(f"... {len(offers) - len(rows)} more (accessories last; --only-search lists them all, "
              "JSON offers carry search_only: true)", file=out)
    for note in (member_footnote(rows, again=True), valid_footnote(rows)):
        if note:
            print(note, file=out)


def _group_footer(env):
    shops = sorted({o.get("mirror_of") or o["store"] for p in env["results"] for o in p["offers"]})
    s = (f"-- {env['count']} products, {env['offers']} offers from {len(shops)} shops "
         f"({env['records']} records in {len(env['inputs'])} file(s))")
    bad = [f"{k} ({v['status']})" for k, v in env["stores"].items() if v["status"] not in ("ok", "partial")]
    if bad:
        s += "; NOT searched in some inputs: " + ", ".join(bad)
    if env.get("search_only"):
        s += f"; found only by search (not in a category walk): {sum(env['search_only'].values())} offers in " \
             + ", ".join(env["search_only"])
    return s + _dropped_note(env.get("dropped"))


def _stock_summary(p):
    st = p["in_stock"]
    return f"{st['yes']}/{p['n_offers']}" + (f" ?{st['unknown']}" if st["unknown"] else "")


def print_group(env, out, a):
    prods = env["results"]
    show = a.show if a.show and a.show > 0 else len(prods)
    rows = prods[:show]
    # the best offers as records, for the shared price cells
    best_rec = {p["product"]: next((o for o in p["offers"] if o.get("url") == (p["best"] or {}).get("url")
                                    and o["store"] == (p["best"] or {}).get("store")), None) for p in rows}
    print(f"PRODUCTS: {len(prods)} (from {env['offers']} offers; one row per product: same EAN, or the same "
          f"model code / part number; variants stay apart)", file=out)
    has_mirror = any(p["mirrors"] for p in rows)
    brecs = [best_rec[p["product"]] or {} for p in rows]

    def bcell(p):
        r = best_rec[p["product"]]
        if not r or effective_price(r) is None:
            return ""
        return fmt_price(effective_price(r)) + ("*" if uses_member_price(r) else (" " if any(
            uses_member_price(x) for x in brecs if x) else ""))
    cols = [("#", lambda p: p["product"], "r", None), ("BEST", bcell, "r", None)]
    if any(until_text(r) for r in brecs if r):
        cols.append(("VALID", lambda p: until_text(best_rec[p["product"]] or {}), "l", None))
    cols += [("AT", lambda p: store_cell(best_rec[p["product"]] or {"store": ""}), "l", 22),
             ("SHOPS", lambda p: p["n_shops"], "r", None),
             ("STOCK", _stock_summary, "l", None)]
    if has_mirror:
        cols.append(("MIRRORS", lambda p: len(p["mirrors"]) or "", "r", None))
    cols += [("CODES", lambda p: " · ".join(([f"EAN {p['ean']}"] if p["ean"] else []) + p["codes"][:3]), "l", 46),
             ("TITLE", lambda p: p["title"], "l", a.title_width)]
    if rows:
        render_table(rows, cols, out)
    if show < len(prods):
        print(f"... {len(prods) - show} more products not shown (use --show 0 for all, or --json/--csv)", file=out)
    # offers, product by product
    per = a.offers if a.offers and a.offers > 0 else None
    orows, hidden = [], 0
    for p in rows:
        os_ = p["offers"][:per] if per else p["offers"]
        hidden += len(p["offers"]) - len(os_)
        orows += os_
    print("", file=out)
    print("OFFERS by product, cheapest first (LINK: how the offer joined its product; read the titles of "
          "'code' links):", file=out)
    ocols = ([("#", lambda o: o["product"], "r", None)] + price_columns(orows)
             + [("STORE", store_cell, "l", 24)])
    if any(o.get("mirror_of") for o in orows):
        ocols.append(("MIRROR", lambda o: o.get("mirror_of") or "", "l", None))
    ocols += [("STOCK", lambda o: stock_cell(o.get("in_stock")), "l", None),
              ("LINK", lambda o: o["group_link"], "l", 40),
              ("TITLE", lambda o: o.get("title"), "l", a.title_width),
              ("URL", lambda o: o.get("url"), "l", None)]
    if orows:
        render_table(orows, ocols, out)
    if hidden:
        print(f"... {hidden} more offers not shown (--offers 0 shows every offer)", file=out)
    for note in (member_footnote(orows + [r for r in brecs if r]), valid_footnote(orows)):
        if note:
            print(note, file=out)
    if has_mirror:
        print("MIRRORS: marketplace listings of a covered shop's own catalogue (Ananas seller = that shop; "
              "ZirafaMall SKU = a Gjirafa50 SKU); SHOPS counts them once, under that shop.", file=out)
    print_search_only(env, out, a)
    rel = [x for x in env.get("related") or [] if x["products"][0] <= show or x["products"][1] <= show]
    if rel:
        print("RELATED (kept apart: they share a code or a model name, but no EAN / code check joins them):",
              file=out)
        for x in rel[:8]:
            what = f"code {x['code']}; " if x.get("code") else ""
            print(f"  #{x['products'][0]} and #{x['products'][1]}: {what}{trunc(x['why_apart'], 130)}", file=out)
        if len(rel) > 8:
            print(f"  ... {len(rel) - 8} more in the JSON's 'related'", file=out)
    print(_group_footer(env), file=out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
EPILOG = """\
examples:
  mkshop.py stores
  mkshop.py search "samsung galaxy a56" --q "SM-A566" --in-stock --max-price 25000
  mkshop.py search "robot vacuum" --q "робот правосмукалка" --stores setec,neptun,ananas --json rv.json
  mkshop.py categories --grep 'монитор|monitor'
  mkshop.py categories --store neptun --json neptun_tree.json
  mkshop.py list --store neptun 123 --in-stock --json neptun_monitors.json
  mkshop.py facets --store setec monitori-30374
  mkshop.py list --store setec monitori-30374 --filter 'Refresh Rate::165 Hz'
  mkshop.py detail https://setec.mk/products/... https://www.neptun.mk/categories/...
  mkshop.py detail 299848 --store neptun
  mkshop.py match "https://www.anhoch.com/products/..."   # a product URL
  mkshop.py match 8806095467924                           # an EAN
  mkshop.py match "Logitech MX Master 3S"                 # free text
  mkshop.py match https://setec.mk/products/sony-whult900nbce7-1 --also "Sony ULT Wear"
  mkshop.py list --store gjirafa50 <cat> --json g50.json; mkshop.py list --store zirafamall <cat> --json zm.json
  mkshop.py group g50.json zm.json gpu_search.json --in-stock --csv products.csv

JSON output (--json PATH, or --json - for stdout) is an envelope:
  {"command": ..., "generated_at": <local ISO time>, "stores": {key: {"status",
   "count", "kept", "message", "warnings", "elapsed_s", ...}}, "results": [records...]}
Save search / list / match / detail envelopes and merge them with `group`.
Listing records follow references/client-contract.md, plus mkshop's
"mirror_of" (store key whose catalogue this listing mirrors, or null),
"match_key" ("ean:<13 digits>" when the record has an EAN, else its model_key),
"model_key" ("model:<brand>:<model tokens>[:<capacity>][:<size>][:<tier>][:<version>][:<colour>]",
regardless of EAN; use it to bridge listings with and without an EAN) and
"found_by" (the queries that returned the record).

Member prices: a client may report member_price_mkd, a lower price that needs
the shop's loyalty card or membership (price_mkd stays the price without it).
mkshop adds "effective_price_mkd" (the member price when it is lower, else
price_mkd) and "price_condition" (what the member price needs, in the client's
words, or null). Sorting, --min-price/--max-price, the PRICE column and match
summaries use the effective price; tables mark member prices with '*', show the
price without it as NON-MEMBER and print the conditions under the table. The
envelope's "member_prices" says whether this was on. --no-member-prices (any
command) uses price_mkd only: effective_price_mkd = price_mkd, price_condition
null, and the member price appears in a MEMBER column instead.

Price windows: a client may report price_valid_until / member_price_valid_until
(ISO 8601 end of that price; null = standing; absent = not exposed). mkshop
adds "effective_price_valid_until" for the effective price (string, null, or
"unknown"; absent when the record has neither field). Tables add a VALID column
('until 04.10', 'ended 02.10') for ends within 7 days or past, detail prints a
'valid:' line, CSV adds the three columns when any record has one, match
summaries say when the cheapest price ends, and group's PRODUCTS table marks the
best offer (its JSON "best" carries the window).

Store keys: """ + ", ".join(STORE_KEYS) + """ (alias gjirafa = gjirafa50,zirafamall).
Environment: MKSHOP_STORES_DIR overrides the client directory, MKSHOP_PYTHON the
interpreter used to run clients."""


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--stores-dir", default=argparse.SUPPRESS,
                        help="directory with the store clients (default: scripts/stores, or $MKSHOP_STORES_DIR)")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        help=f"seconds per store before it is killed and reported as timeout "
                             f"(default {DEFAULT_TIMEOUT}; list {DEFAULT_LIST_TIMEOUT})")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="no progress lines on stderr (the final status table still prints)")
    common.add_argument("--keep-tmp", action="store_true", default=argparse.SUPPRESS,
                        help="keep the clients' raw JSON files (path printed on stderr)")
    common.add_argument("--no-member-prices", action="store_true", default=argparse.SUPPRESS,
                        help="rank, filter and show by price_mkd only; by default a lower member price "
                             "(loyalty card / membership) is the effective price (see 'Member prices' in mkshop.py -h)")

    p = argparse.ArgumentParser(
        prog="mkshop.py", description=__doc__.split("\n\n")[0].strip() + "\n\n" + "\n\n".join(__doc__.split("\n\n")[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG, parents=[common])
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    def add_listing_opts(sp, json_help):
        sp.add_argument("--min-price", type=int,
                        help="drop records whose effective price is below N MKD (and unpriced ones)")
        sp.add_argument("--max-price", type=int,
                        help="drop records whose effective price is above N MKD (and unpriced ones)")
        sp.add_argument("--sort", choices=["price", "store", "title"], default="price",
                        help="sort order (default price: effective price, ascending; unpriced last)")
        sp.add_argument("--json", nargs="?", const="-", metavar="PATH", help=json_help)
        sp.add_argument("--csv", metavar="PATH", help="also write the kept records as CSV ('-' = stdout)")
        sp.add_argument("--show", type=int, default=DEFAULT_SHOW,
                        help=f"rows to print in the table (default {DEFAULT_SHOW}; 0 = all). The table prints "
                             "with --json/--csv PATH too; JSON/CSV always hold every row")
        sp.add_argument("--no-table", action="store_true", help="print only the 'wrote' line and the footer when --json/--csv go to a file (by default the table prints too)")
        sp.add_argument("--title-width", type=int, default=TITLE_WIDTH, help="truncate titles in the table to N chars")

    sp = sub.add_parser("stores", parents=[common], help="list shops, domains, what they sell, capabilities",
                        description="Registry plus each client's `info` (capabilities, notes). Network-free.")
    sp.add_argument("--stores", default="all", help="comma list of store keys (default all)")
    sp.add_argument("--exclude", help="comma list of store keys to leave out")
    sp.add_argument("--json", nargs="?", const="-", metavar="PATH", help="JSON to PATH, or stdout if no PATH")

    sp = sub.add_parser(
        "search", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="cross-store full-text search, merged and sorted",
        description="Run every selected store's own search in parallel (one worker per store; several\n"
                    "queries run one after another inside a store), union per store by id, filter,\n"
                    "merge and sort. Search engines differ per shop (title-only, AND vs OR, fuzzy,\n"
                    "Latin vs Cyrillic), so give several phrasings with --q. Search is for recall;\n"
                    "category listing (`list`) is the complete set for a category.\n\n"
                    "--in-stock drops records with in_stock false; in_stock null (orderable, shop gives\n"
                    "no availability, or 'ask for stock') is kept unless --strict-stock.\n\n"
                    "PRICE is the effective price: a shop's lower member price (needs its loyalty card\n"
                    "or membership) when it has one, marked '*', else the price everyone pays. Sorting\n"
                    "and --min-price/--max-price use it; NON-MEMBER shows the price without it, and\n"
                    "a line under the table names each shop's condition. --no-member-prices uses the\n"
                    "price without it throughout.")
    sp.add_argument("query", nargs="?", help="search text (Latin or Cyrillic, model codes, EANs)")
    sp.add_argument("--q", action="append", metavar="QUERY", help="additional query (repeatable); results are unioned")
    sp.add_argument("--stores", default="all", help="comma list of store keys, or 'all' (default)")
    sp.add_argument("--exclude", help="comma list of store keys to leave out")
    sp.add_argument("--in-stock", action="store_true", help="drop records the shop marks out of stock")
    sp.add_argument("--strict-stock", action="store_true", help="with --in-stock, also drop records whose stock is unknown")
    sp.add_argument("--strict", action="store_true",
                    help="keep only records whose title/brand/sku/ean contain every word of a query that found them "
                         "(trims OR-fallback and fuzzy noise; transliteration-aware)")
    sp.add_argument("--hide-mirrors", action="store_true",
                    help="drop marketplace listings that mirror a shop also searched (Ananas seller Нексио -> neksio, ...)")
    sp.add_argument("--limit-per-store", type=int, default=DEFAULT_LIMIT_PER_STORE,
                    help=f"max hits requested from each store per query (default {DEFAULT_LIMIT_PER_STORE}; "
                         "0 = the client's own default). A store that hits it is flagged")
    add_listing_opts(sp, "write the JSON envelope to PATH ('-' or no PATH = stdout)")

    sp = sub.add_parser("categories", parents=[common], help="grep category trees across stores",
                        description="With --grep: every selected store's categories whose name, path or slug match\n"
                                    "REGEX (case-insensitive; clients also match Latin transliterations of Cyrillic).\n"
                                    "With --store KEY and no --grep: that store's full tree. Use the printed ID (or\n"
                                    "slug, or URL) as CATEGORY for `list` and `facets`.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--grep", metavar="REGEX", help="regex; try a Macedonian stem and an English one: 'монитор|monitor'")
    sp.add_argument("--store", help="one store key (full tree when --grep is not given)")
    sp.add_argument("--stores", default="all", help="comma list of store keys (default all)")
    sp.add_argument("--exclude", help="comma list of store keys to leave out")
    sp.add_argument("--json", nargs="?", const="-", metavar="PATH", help="JSON envelope to PATH, or stdout if no PATH")
    sp.add_argument("--show", type=int, metavar="N",
                    help=f"categories printed per store (default {CATS_SHOW} with --grep, biggest first; all for a "
                         "full tree; 0 = all). JSON always holds every category")
    sp.add_argument("--urls", action="store_true", help="add the URL (and slug) columns to the table; the JSON has them")
    sp.add_argument("--no-table", action="store_true",
                    help="with --json PATH, print only the 'wrote' line and the footer (by default the table prints too)")

    sp = sub.add_parser("list", parents=[common], help="every product in one store's category",
                        description="Walks all pages of CATEGORY (an id, slug or url printed by `categories`).\n"
                                    "Parent categories include descendants where the shop's own page does.\n"
                                    "--filter tokens come from `facets` and AND together and are applied by the\n"
                                    "shop's client on its own terms; --min-price/--max-price and sorting use the\n"
                                    "effective price (member price included, as in `search`).",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--store", required=True, help="store key")
    sp.add_argument("category", help="category id, slug or URL from `categories`")
    sp.add_argument("--in-stock", action="store_true", help="passed to the client: in-stock items only")
    sp.add_argument("--filter", action="append", metavar="TOKEN", help="facet token from `facets` (repeatable, AND)")
    sp.add_argument("--limit", type=int, help="stop after N products")
    add_listing_opts(sp, "write the JSON envelope to PATH ('-' or no PATH = stdout)")

    sp = sub.add_parser("facets", parents=[common], help="structured attributes of a category (filter tokens)",
                        description="Name / value / count / token for CATEGORY, where the shop exposes structured\n"
                                    "attributes (`stores` shows which have the 'facets' capability). Pass a token to\n"
                                    "`list --filter` exactly as printed.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--store", required=True, help="store key")
    sp.add_argument("category", help="category id, slug or URL from `categories`")
    sp.add_argument("--json", nargs="?", const="-", metavar="PATH", help="JSON envelope to PATH, or stdout if no PATH")
    sp.add_argument("--no-table", action="store_true",
                    help="with --json PATH, print only the 'wrote' line and the footer (by default the table prints too)")

    sp = sub.add_parser("detail", parents=[common], help="full records for URLs or ids",
                        description="One record per input, in input order. URLs are routed to their shop by domain;\n"
                                    "bare ids / on-site codes need --store. A failing input yields\n"
                                    "{\"input\", \"error\"} and the rest continue. Several shops run in parallel.\n"
                                    "A lower member price is printed first, with its condition and the price without it.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("refs", nargs="+", metavar="REF", help="product URL, or id/code with --store")
    sp.add_argument("--store", help="store key for bare ids")
    sp.add_argument("--json", nargs="?", const="-", metavar="PATH", help="JSON envelope to PATH, or stdout if no PATH")
    sp.add_argument("--no-table", action="store_true",
                    help="with --json PATH, print only the 'wrote' line (by default the records print too)")

    sp = sub.add_parser(
        "match", parents=[common], help="find the same product in other stores",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="REF is a product URL from a covered shop (its detail gives title, brand, EAN, MPN),\n"
                    "an EAN (8-14 digits) or free text. Each store is searched by EAN (where its search\n"
                    "matches barcodes), then by model codes / MPN from the title (SM-S931B, QE55Q60D,\n"
                    "CT1000P3PSSD8, 920-014207), a model phrase (Logitech MX Master 3S) and a cleaned\n"
                    "title. Candidates are labelled:\n"
                    "  exact   same EAN\n"
                    "  model   same model code / MPN (or the same Gjirafa SKU on Gjirafa50/ZirafaMall)\n"
                    "  likely  same brand and strong title overlap, no conflicting model numbers:\n"
                    "          a lead to confirm, not a match\n"
                    "Rows whose colour, capacity, size, tier (Pro/Max/Ultra/Ti...), version (V2), part\n"
                    "number or EAN differ from the reference go to a separate VARIANTS group with the\n"
                    "differing tokens shown; they are never merged into the same-product group. When\n"
                    "the reference leaves a variant open (free text without a colour), same-product\n"
                    "offers are printed in sub-groups per value, so prices compare like for like.\n"
                    "Accessories for the product ('Case for Galaxy A56') never match the product.\n"
                    "With an EAN reference, up to --verify top candidates per shop that lack an EAN\n"
                    "are re-read with `detail` to confirm it. Rejected candidates and the reason are\n"
                    "in the JSON's near_misses (and printed with --near).\n\n"
                    "Prices are effective prices, as in `search`: the reference line, the ranking and the\n"
                    "'cheapest' summaries use a lower member price where a shop has one and say so, with\n"
                    "its condition and the cheapest offer without one. --no-member-prices turns this off.\n"
                    "A summary also says when the cheapest offer's price has a known end date.\n\n"
                    "Codes written with spaces match the glued form ('LIVE 770 NC' ~ 770NC, 'WH 1000 XM5'\n"
                    "~ WH1000XM5), and a vendor code that wraps the model code (LIVE770NCBLK) counts as it.\n"
                    "Colours include marketing names (Sand, Latte, Forest Gray, Smoky Pink, Midnight Blue)\n"
                    "and shop abbreviations (WHT, BLK); a title that names no known colour but ends in a\n"
                    "word the reference lacks ('... 770NC Sandstone') shows it as a possible colour\n"
                    "('colour sandstone?'), a variant when the reference states its colour. A title-only\n"
                    "('likely') lead for another kind of product (a speaker for headphones, by the title's\n"
                    "product noun or the record's category) is rejected.\n\n"
                    "A reference whose title is only a model code (Setec: 'SONY WHULT900NB.CE7') is also\n"
                    "searched by its marketed name, taken from offers confirmed by EAN / model code or\n"
                    "from its specs ('Sony ULT Wear'; never from a 'with' phrase such as 'w/Microphone'),\n"
                    "in shops whose search does not match EANs; the note line says which name. --also\n"
                    "adds such names yourself. A code with a regional / packaging tail after a dot or a\n"
                    "slash ('WH1000XM5L.CE7', 'SM-S931B/DS') is also searched and compared as its base code.\n\n"
                    "Letters a longer code adds after the model number name another model (MDR-ZX110AP,\n"
                    "NTH-100M: VARIANTS) unless they are a region / packaging tail (QE55Q60D-AUXXH) or a\n"
                    "colour (-WHITE, BLK, or a vendor colour letter such as Sony's WH-CH520W / B / L that\n"
                    "a title confirms); such a letter in a shared full code is the colour of a listing\n"
                    "whose title names none, and outranks a seller's colour attribute. Laptop / PC / phone\n"
                    "configurations are variant dimensions: CPU (model code, else tier: Ryzen 5 vs 7, Core\n"
                    "i5 vs Core 5), RAM, storage and GPU; '8/256GB', '8+256GB', '12/256' are RAM + storage.")
    sp.add_argument("ref", metavar="REF", nargs="?", help="product URL, EAN, or free text")
    sp.add_argument("--also", action="append", metavar="TEXT",
                    help="another name the product is sold under (repeatable): searched in every shop the EAN "
                         "did not settle, and candidates are also scored against it ('Sony ULT Wear' for "
                         "WHULT900N, a code for a marketing name)")
    sp.add_argument("--stores", default="all", help="comma list of store keys to search (default all)")
    sp.add_argument("--exclude", help="comma list of store keys to leave out")
    sp.add_argument("--limit", type=int, default=DEFAULT_MATCH_LIMIT,
                    help=f"hits per query per store (default {DEFAULT_MATCH_LIMIT})")
    sp.add_argument("--verify", type=int, default=3,
                    help="detail-check up to N candidates per store for their EAN (default 3; 0 = off)")
    sp.add_argument("--in-stock", action="store_true", help="drop candidates marked out of stock")
    sp.add_argument("--near", action="store_true", help="also print rejected near misses with the reason")
    sp.add_argument("--title-width", type=int, default=60, help="truncate titles in the table to N chars")
    sp.add_argument("--json", nargs="?", const="-", metavar="PATH",
                    help="JSON envelope (reference, stores, results with a 'match' object, near_misses)")

    sp = sub.add_parser(
        "group", parents=[common], help="one row per product from saved JSON runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Merge saved runs into products. FILEs are mkshop JSON envelopes (search, list,\n"
                    "match: its candidates plus the reference record, detail, group) or plain client JSON\n"
                    "lists. No shop is contacted.\n\n"
                    "1. Union: one record per shop + id; the freshest copy (envelope generated_at, else\n"
                    "   the file's mtime) gives price and stock, older copies fill gaps (EAN, MPN, specs).\n"
                    "2. Mirrors are re-detected across the union: Ananas sellers that are covered shops,\n"
                    "   ZirafaMall listings whose SKU is a Gjirafa50 SKU in any input (the vendor name\n"
                    "   'Basics from GjirafaMall' alone proves nothing: it also sells goods Gjirafa50 lacks).\n"
                    "3. Products: same EAN; Gjirafa50/ZirafaMall SKU twins; then model codes, MPNs and\n"
                    "   part numbers (from titles, mpn fields, specs). Every code a product's records\n"
                    "   carry is an alias of it, so a shop that prints the marketing code\n"
                    "   (DUAL-RTX5070-O12G) and one that prints the vendor part number (90YV0M17-M0NA00)\n"
                    "   meet through a third shop's EAN group that has both. Codes join exactly, by a 6+\n"
                    "   character prefix (QE55Q60D ~ QE55Q60DAUXXH) or written with spaces (LIVE 770 NC ~\n"
                    "   770NC). Every join is checked with match's variant logic: colour, capacity, size,\n"
                    "   tier, version, region (keyboard layout), CPU / RAM / storage / GPU, conflicting part\n"
                    "   numbers or a different name next to a shared family code keep products apart, and\n"
                    "   two different EANs never merge. Refused besides: a code that only begins the other\n"
                    "   when the added letters name another model (MDR-ZX110 vs MDR-ZX110AP, NTH-100 vs\n"
                    "   NTH-100M) or brand and title do not confirm it; a version one side states; a laptop\n"
                    "   / PC joined by a CPU / GPU code, or by a platform code (FA507NU, 15ABR8, X1504VA)\n"
                    "   without the same CPU, RAM and storage stated on both sides; an accessory or spare\n"
                    "   part (ear pads, battery, charging case, cable, mount; 'for X', 'replacement') next to\n"
                    "   the product, a toner / cartridge / refill next to the printer it fits, two makers\n"
                    "   (a brand named only after 'for' is what the item fits: 'TJ refill for Canon' is no\n"
                    "   Canon), or offers over 3x apart in price. Sizes, volumes, model years and port\n"
                    "   counts (10x15cm, 135ml, 2018-2023, 24port, 8-Zone) are no codes. A Gjirafa SKU\n"
                    "   twin whose titles disagree (product type, colour, or brand and model tokens) is no\n"
                    "   twin and no mirror.\n"
                    "   When a code fits several products (colour editions share one model code), the one\n"
                    "   whose variant values agree wins, an EAN product before another listing; a tie joins\n"
                    "   nothing unless the tied listings name the same colour and agree on everything, and\n"
                    "   a listing silent on its colour while the model comes in several joins nothing.\n"
                    "   Titles alone never join. Products kept apart that share a code or a model name\n"
                    "   (refused twins and accessories included) are listed as RELATED.\n\n"
                    "Output: a PRODUCTS table (BEST = lowest effective price, '*' = member price; VALID\n"
                    "'until 04.10' when that price ends within 7 days; SHOPS counts a mirror once, under\n"
                    "its shop; STOCK = offers in stock / offers, ?N unknown; CODES = EAN and alias\n"
                    "codes), then OFFERS per product, cheapest first, with LINK (ean, gjirafa sku, code\n"
                    "X). Filters work on offers after grouping: --in-stock drops in_stock false,\n"
                    "--min/--max-price use the effective price, --hide-mirrors drops a mirror offer when\n"
                    "its shop's own offer is in the same product. --json: envelope with results = products\n"
                    "({product, title, brand, ean, codes, best, n_shops, shops, in_stock, member_price,\n"
                    "mirrors, links, related, offers[]}); --csv: one row per offer with product columns.")
    sp.add_argument("files", nargs="*", metavar="FILE", help="saved mkshop --json output or client JSON list")
    sp.add_argument("--in-stock", action="store_true", help="drop offers the shop marks out of stock")
    sp.add_argument("--hide-mirrors", action="store_true",
                    help="drop mirror offers whose shop's own offer is in the same product")
    sp.add_argument("--only-search", action="store_true",
                    help="keep only offers found by search/match/detail in a shop you also walked with `list`, "
                         "but in none of the saved walks (the FOUND ONLY BY SEARCH section, in full)")
    sp.add_argument("--offers", type=int, default=GROUP_SHOW_OFFERS,
                    help=f"offers printed per product in the table (default {GROUP_SHOW_OFFERS}; 0 = all)")
    add_listing_opts(sp, "write the JSON envelope to PATH ('-' or no PATH = stdout)")
    for act in sp._actions:
        if act.dest == "sort":
            act.choices = ["price", "shops", "title"]
            act.help = "product order: price (best effective price, default), shops (most shops first), title"
        elif act.dest == "show":
            act.default = GROUP_SHOW
            act.help = (f"products to print (default {GROUP_SHOW}; 0 = all). The table prints with "
                        "--json/--csv PATH too; JSON/CSV always hold every product")
        elif act.dest in ("min_price", "max_price"):
            act.help = act.help.replace("records", "offers")
        elif act.dest == "csv":
            act.help = "also write the offers as CSV, one row per offer with product columns ('-' = stdout)"
    return p


def main(argv=None):
    global QUIET, MEMBER_PRICES
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = build_parser()
    a = p.parse_args(argv)
    QUIET = bool(getattr(a, "quiet", False))
    MEMBER_PRICES = not getattr(a, "no_member_prices", False)
    stores_dir = getattr(a, "stores_dir", None) or os.environ.get("MKSHOP_STORES_DIR") or DEFAULT_STORES_DIR
    stores_dir = os.path.abspath(os.path.expanduser(stores_dir))
    if not os.path.isdir(stores_dir):
        die(f"mkshop: stores directory not found: {stores_dir}")
    python = os.environ.get("MKSHOP_PYTHON") or sys.executable or "python3"
    a.timeout = getattr(a, "timeout", None)
    ctx = Ctx(stores_dir, python, keep_tmp=bool(getattr(a, "keep_tmp", False)))
    if getattr(a, "keep_tmp", False):
        warn(f"mkshop: raw client output kept in {ctx.tmp}")

    def on_signal(signum, _frame):
        # Kill the clients first: worker threads are blocked waiting on them,
        # and the thread pools wait for their workers before unwinding.
        _kill_all()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        fn = {"stores": cmd_stores, "search": cmd_search, "categories": cmd_categories,
              "list": cmd_list, "facets": cmd_facets, "detail": cmd_detail, "match": cmd_match,
              "group": cmd_group}[a.cmd]
        return fn(ctx, a)
    except KeyboardInterrupt:
        _kill_all()
        warn("mkshop: interrupted")
        return 130
    except BrokenPipeError:
        _kill_all()
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
