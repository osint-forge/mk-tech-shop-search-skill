#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.28", "beautifulsoup4>=4.11"]
# ///
"""Hivetec (https://hivetec.mk) catalogue client. See references/client-contract.md
and references/hivetec.md.

Hivetec is WordPress + WooCommerce (Woodmart theme) behind Cloudflare. Everything comes from the
anonymous WooCommerce Store API, the JSON API the WooCommerce blocks use:

    GET /wp-json/wc/store/v1/products?category=<ids>&search=<s>&sku=<code>&include=<ids>
                                      &stock_status=instock&per_page=100&page=N&_fields=...
    GET /wp-json/wc/store/v1/products/<slug>
    GET /wp-json/wc/store/v1/products/categories?per_page=100&page=N
    GET /wp-json/wp/v2/pages?slug=kupuvanje-i-isporaka-na-proizvodi   (delivery policy, once)

Every product object carries prices (minor units), stock flags, categories, attributes, the short
description (brand + warranty) and the full description, so no HTML is parsed.

Cloudflare challenges any request whose QUERY STRING is longer than ~200 characters (196 -> 200,
206 -> 403 "Just a moment..."). The client builds every query itself (commas left literal) and never
sends one longer than MAX_QUERY_CHARS. Python-urllib's User-Agent gets a Cloudflare 1010 ban; the
Chrome UA below is fine.

The store's own search matches the WHOLE query as one case-insensitive substring of the title or
SKU (no word AND, no descriptions). For a multi-word query this client searches the rarest word on
the server and keeps the hits whose title/SKU contain every word (1-2 character words must be whole
tokens there); --phrase sends it verbatim. A Cyrillic word that finds nothing is retried as Latin.

    hivetec.py info
    hivetec.py search "<query>" [--limit N] [--in-stock] [--category REF] [--phrase] [--json PATH]
    hivetec.py categories [--grep REGEX] [--json PATH]
    hivetec.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    hivetec.py detail <url|id|slug|sku:CODE> [...] [--json PATH]
    hivetec.py facets <category> [--json PATH]
    common options: --quiet, -v/--verbose (log every request)

Category refs: a term id ("610"), slug ("gaming-monitori"), a hivetec.mk /product-category/<slug>/
URL (a /page/N/ suffix is ignored), an exact category name or breadcrumb path as `categories`
prints it, or a comma list of these (union).
Detail refs: a product URL, ?p=<id>, a product id, a variation id (resolved to its product), a
slug, or sku:<code>. For the few variable products, detail prices every variation
(extra.variations), and a link that names a variant (?attribute_...=..., a variation id) reports
that variant's price and stock instead of the lowest one.
Filter tokens (from `facets`): <attribute>=<value>[,<value>...] such as brand=lenovo,
panel=ips, refreshrate=240hz-360hz; cat=<id|slug> (also filed in that category);
price=MIN-MAX (MKD, either end optional); stock=in|out. Commas OR inside a token, several
--filter flags AND.

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
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import requests

STORE = "hivetec"
NAME = "Hivetec"
BASE = "https://hivetec.mk"
API = BASE + "/wp-json/wc/store/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PER_PAGE = 100           # Store API maximum
PACE_S = 0.6             # pause between consecutive requests (the origin queues parallel requests)
MAX_RETRIES = 4
# Cloudflare challenges query strings longer than ~200 chars (measured 2026-10-03: 196 -> 200,
# 206 -> 403 challenge). Stay well below it.
MAX_QUERY_CHARS = 170
INCLUDE_CHARS = 120      # budget for the literal-comma include=... value when batching ids
# Trimmed listing objects (~3x smaller than full ones; same latency). Dropped automatically when the
# query would get too long, e.g. for a long search string.
LIST_FIELDS = ("id,name,sku,permalink,prices,is_in_stock,stock_availability,categories,"
               "attributes,short_description")
COUNT_WORDS_MAX = 4      # multi-word search: count at most this many words to pick the anchor
MAX_VARIATIONS = 12      # detail: price at most this many variations of a variable product (1 request each)
DELIVERY_POLICY_SLUG = "kupuvanje-i-isporaka-na-proizvodi"

SELLS = ("Gaming and PC specialist (~2,450 products, nearly all English titles): PC components "
         "(cases, air and water cooling, fans, thermal paste, GPUs, motherboards, CPUs, RAM, PSUs, "
         "SSD/HDD), peripherals (keyboards, mice, headsets and headphones, speakers, webcams, "
         "microphones, mouse pads, controllers, racing wheels and cockpits), monitors (mostly gaming) "
         "and monitor arms, gaming chairs and desks, laptops (gaming and regular), tablets, prebuilt "
         "gaming/office PCs, consoles and console games, networking (routers, mesh, range extenders), "
         "UPS, a few printers, projectors and presenters, smartwatches, e-scooters, action cameras, "
         "drones, power banks, cables and adapters. No phones, no TVs, no home appliances.")
NOTES = ("price_mkd = the current WooCommerce price (Store API minor units / 100), incl. VAT. "
         "Almost every product (2437 of 2446) shows a struck-through 'regular' price ~8-25% higher: "
         "a permanent list price, not a real promotion. 4 variable laptops: in listings price_mkd is "
         "the lowest variant ('from'); detail prices every variant and honours a variant link or "
         "variation id. Stock is one yes/no web flag: no quantities, no per-store split. No EAN "
         "anywhere; sku is often the manufacturer part number. Search = the whole query as ONE "
         "substring of title or SKU on the server; this client emulates word-AND by searching the "
         "rarest word and filtering; titles are English, so Cyrillic words find nothing (the client "
         "retries a Latin transliteration and points to matching categories). Facets = WooCommerce "
         "attributes (brand, colour, type, monitor size/panel/refresh/resolution, GPU series, ...) "
         "plus co-categories and price. Flat, multi-valued category taxonomy mixing product types "
         "with brand and promo categories. Delivery: store-wide 72 h after order confirmation "
         "(working days), free over 4,500 den (site banner). Cloudflare challenges query strings "
         "over ~200 chars; sequential requests only.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter", "warranty",
                "delivery_estimate"]

CHALLENGE_MARKERS = (
    "just a moment", "cf-chl", "challenge-platform", "turnstile", "captcha",
    "checking your browser", "attention required", "are you a robot", "ddos-guard",
    # Cloudflare 1xxx block pages (Error 1010 "banned your access based on your browser's signature")
    "access denied", "error 1010", "error 1020", "you have been blocked",
)

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh",
           # Serbian / Russian letters people may type
           "ђ": "dj", "ћ": "c", "й": "j", "я": "ja", "ю": "ju", "ы": "y", "э": "e", "щ": "sht",
           "ъ": "", "ь": "", "ё": "jo", "і": "i", "ї": "ji", "є": "je"}
# The shop's own slugs drop the diacritic digraphs (ГЛУВЧИЊА -> gluvcinja, КУЌИШТА -> kukista).
_MK_LAT_SIMPLE = dict(_MK_LAT, **{"ж": "z", "ч": "c", "ш": "s", "ќ": "k", "ѓ": "g", "џ": "dz"})

# English words added to a category's --grep haystack when its (Macedonian) name contains the
# stem, so `categories --grep headphone` finds СЛУШАЛКИ. Lowercase stems.
_EN_TAGS = {
    "тастатур": "keyboard keyboards", "глувч": "mouse mice", "слушалк": "headphones headset earphones",
    "звучниц": "speakers", "монитор": "monitor monitors display screen", "графичк": "graphics card gpu video card",
    "процесор": "cpu processor", "матичн": "motherboard mainboard", "напојув": "power supply psu",
    "рам мемор": "ram memory dimm", "десктоп мемор": "ram desktop memory dimm",
    "лаптоп мемор": "ram laptop memory sodimm", "куќишт": "pc case chassis",
    "кулер": "cooler cooling fan fans", "водено ладење": "water cooling liquid aio",
    "воздушно ладење": "air cooler cpu cooler", "термални": "thermal paste pads",
    "хард диск": "hdd hard disk drive storage", "ссд": "ssd solid state drive storage nvme",
    "надворешни хард": "external drive storage", "усб мемор": "usb flash drive stick pendrive",
    "мемориски картич": "memory card sd microsd", "веб камер": "webcam web camera",
    "микрофон": "microphone mic", "столиц": "chair chairs", "биро": "desk desks table",
    "волани": "steering wheel racing", "контролер": "controller gamepad joystick",
    "конзол": "console consoles", "игри": "games game", "виртуелна реалност": "vr virtual reality",
    "лаптоп": "laptop notebook", "таблет": "tablet", "паметни часовниц": "smartwatch smart watch",
    "паметни уреди": "smart home devices tv box", "рутери": "router wifi mesh access point network",
    "мрежна опрема": "network networking switch", "пасивна мрежна": "network patch cable rack",
    "кабли": "cable cables", "конвертори": "adapter converter", "принтер": "printer printers",
    "потрошен материјал": "toner ink paper consumables", "проектори": "projector projectors",
    "презентери": "presenter pointer", "упс": "ups power backup", "стабилизатор": "stabilizer avr",
    "тротинет": "e-scooter scooter electric", "акциони камери": "action camera gopro",
    "камери": "camera cameras", "дронови": "drone drones", "ташни": "bag bags backpack",
    "полначи": "charger chargers", "батерии": "batteries battery", "лед осветлув": "led lighting light",
    "алат": "tools", "заштитни очила": "glasses eyewear", "овлажнувачи": "humidifier",
    "режачи": "optical drive dvd burner", "облека": "apparel clothing merch",
    "гејминг": "gaming", "конфигурац": "pc build desktop computer", "десктопи": "desktop pc computer",
    "подлоги": "mouse pad mousepad", "сет тастатури": "combo keyboard mouse set",
    "додатоци": "accessories", "стриминг": "streaming capture card", "предначарка": "preorder",
    "преднарачка": "preorder", "препорачуваме": "recommended",
}


class Blocked(RuntimeError):
    """Response is a bot challenge / block page / login wall rather than data (exit 3)."""


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
    """HTML fragment -> plain text. Block-level tags become `block_sep`, inline tags vanish
    (so 'Гаранција: 36<strong>0 дена</strong>' stays '360 дена')."""
    if not fragment:
        return ""
    t = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d|/tr|/ul|/ol|p|li|tr|h\d)\b[^>]*>", block_sep, fragment)
    t = re.sub(r"<[^>]+>", "", t)
    return htmlmod.unescape(t).replace("\xa0", " ")


def translit(text, simple=False):
    table = _MK_LAT_SIMPLE if simple else _MK_LAT
    return "".join(table.get(ch, ch) for ch in (text or "").lower())


def has_cyrillic(text):
    return bool(re.search(r"[Ѐ-ӿ]", text or ""))


def fold(text):
    """Case- and accent-insensitive form for client-side substring matching (MySQL's _ci collation
    behaves the same way on the server)."""
    t = unicodedata.normalize("NFKD", htmlmod.unescape(text or "").casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.replace("″", '"').replace("“", '"').replace("”", '"').replace("’", "'")


def en_tags(text):
    low = (text or "").lower()
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def minor_to_mkd(amount, minor_unit):
    """Store API money is a string in minor units: '995000' with minor_unit 2 -> 9950."""
    if amount in (None, ""):
        return None
    return int(round(int(amount) / (10 ** int(minor_unit or 0))))


def id_chunks(ids, budget=INCLUDE_CHARS):
    """Split ids so each literal comma-joined list fits in `budget` characters."""
    chunk, size = [], 0
    for i in ids:
        add = len(str(i)) + (1 if chunk else 0)
        if chunk and (size + add > budget or len(chunk) >= PER_PAGE):
            yield chunk
            chunk, size, add = [], 0, len(str(i))
        chunk.append(i)
        size += add
    if chunk:
        yield chunk


def deep_unquote(text):
    """Undo repeated percent-encoding ('%25d1%2581' -> '%d1%81' -> 'с')."""
    for _ in range(3):
        nxt = unquote(text)
        if nxt == text:
            break
        text = nxt
    return text


def short_tax(taxonomy):
    return taxonomy[3:] if (taxonomy or "").startswith("pa_") else (taxonomy or "")


def term_slug(term):
    return unquote(term.get("slug") or "")


def _title_of(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.S | re.I)
    return collapse(htmlmod.unescape(m.group(1)))[:80] if m else None


# --------------------------------------------------------------------------- client

class Hivetec:
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
        self._delivery = False   # False = not fetched yet; None = fetched, nothing found
        self._by_id_cache = {}

    # ------------------------------------------------------------------ http
    def _get(self, path, params=None, allow_404=False):
        """GET JSON -> (data, response). Builds the query itself so commas stay literal (Cloudflare
        counts characters, and %2C triples a comma's cost)."""
        url = path if path.startswith("http") else API + path
        qs = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], safe=",")
        if len(qs) > MAX_QUERY_CHARS:
            raise Usage(f"query string would be {len(qs)} chars; Cloudflare challenges query strings "
                        f"longer than ~200 chars on this site, so it was not sent (shorten the search "
                        f"text): {qs[:80]}...")
        full = url + ("?" + qs if qs else "")
        r = None
        block_retried = False
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(full, timeout=60)
            except requests.RequestException as e:
                self._last = time.monotonic()
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"GET {full} failed: {e}") from e
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                print(f"[hivetec] GET {r.url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s "
                      f"{len(r.content)} B", file=sys.stderr)
            try:
                self._check_block(r)
            except Blocked as e:
                # One unexplained transient block was seen on an ordinary 3-request run (2026-10-03);
                # the same run succeeded seconds later. Retry once after a pause, then give up (exit 3).
                if block_retried or attempt == MAX_RETRIES:
                    raise
                block_retried = True
                log(f"  looks blocked ({e}); retrying once in 5s")
                time.sleep(5)
                continue
            if r.status_code in (429, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    if r.status_code == 429:
                        raise Blocked(f"HTTP 429 (rate limited) {r.url} after {MAX_RETRIES} attempts, "
                                      f"retry-after={r.headers.get('Retry-After')!r} "
                                      f"cf-ray={r.headers.get('cf-ray')}")
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** attempt
                log(f"  HTTP {r.status_code}, retrying in {min(delay, 60)}s")
                time.sleep(min(delay, 60))
                continue
            if r.status_code == 404 and allow_404:
                return None, r
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "json" not in ctype:
                raise RuntimeError(f"GET {r.url}: expected JSON, got HTTP {r.status_code} ({ctype}); "
                                   f"body starts {r.text[:160]!r}")
            try:
                return r.json(), r
            except ValueError as e:
                raise RuntimeError(f"GET {r.url}: invalid JSON: {r.text[:160]!r}") from e
        raise RuntimeError(f"GET {full}: still HTTP {r.status_code if r is not None else '?'} "
                           f"after {MAX_RETRIES} attempts")

    @staticmethod
    def _check_block(r):
        ctype = r.headers.get("content-type", "")
        mitigated = r.headers.get("cf-mitigated")
        body = r.text[:20000] if ("html" in ctype or r.status_code in (401, 403, 503)) else ""
        marker = next((m for m in CHALLENGE_MARKERS if m in body.lower()), None)
        url = r.url if len(r.url) <= 160 else r.url[:157] + "..."
        evidence = (f"HTTP {r.status_code} {url} cf-mitigated={mitigated!r} "
                    f"cf-ray={r.headers.get('cf-ray')} title={_title_of(body)!r}"
                    + (f" marker={marker!r}" if marker else ""))
        if mitigated or r.status_code in (401, 403):
            raise Blocked(evidence)
        if marker and ("html" in ctype):
            raise Blocked(evidence)

    # ------------------------------------------------------------- listing
    def walk(self, limit=None, fields=LIST_FIELDS, label="", **params):
        """Walk every page of /products with the given filters -> (products, meta).
        Verifies the result against the X-WP-Total header."""
        base = {k: v for k, v in params.items() if v is not None}
        base["per_page"] = min(PER_PAGE, limit) if limit else PER_PAGE
        if fields:
            probe = urlencode(dict(base, _fields=fields, page=99), safe=",")
            if len(probe) <= MAX_QUERY_CHARS:
                base = dict({"_fields": fields}, **base)
        out, seen, page, meta = [], set(), 1, {}
        while True:
            data, r = self._get("/products", params=dict(base, page=page))
            if not isinstance(data, list):
                raise RuntimeError(f"/products returned {type(data).__name__}, expected a list")
            if page == 1:
                meta = {"total": int(r.headers.get("X-WP-Total", len(data)) or 0),
                        "pages": int(r.headers.get("X-WP-TotalPages", 1) or 0)}
                if meta["pages"] > 1:
                    log(f"  {label or 'products'}: {meta['total']} over {meta['pages']} page(s)")
            for p in data:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    out.append(p)
            if limit and len(out) >= limit:
                meta["pagesFetched"] = page
                return out[:limit], meta
            if page >= meta["pages"] or not data:
                break
            page += 1
        meta["pagesFetched"] = page
        total = meta["total"]
        if len(out) < total and "orderby" not in base:
            # Page boundaries shifted mid-walk (catalogue edit, unstable date ties): walk again in a
            # different order and merge instead of silently returning a short list.
            log(f"  collected {len(out)} of {total}; re-walking ordered by title to fill the gap")
            more, m2 = self.walk(fields=fields, label=label, **dict(params, orderby="title", order="asc"))
            for p in more:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    out.append(p)
            meta["pagesFetched"] += m2["pagesFetched"]
        if len(out) != total:
            warn(f"collected {len(out)} unique products but the site reports X-WP-Total={total}")
        return out, meta

    def count(self, **params):
        """Cheap hit count (X-WP-Total of a 1-row page)."""
        _, r = self._get("/products", params=dict({k: v for k, v in params.items() if v is not None},
                                                  _fields="id", per_page=1))
        return int(r.headers.get("X-WP-Total", 0) or 0)

    # -------------------------------------------------------------- records
    @staticmethod
    def brand_of(p):
        for a in p.get("attributes") or []:
            if a.get("taxonomy") == "pa_brand" and a.get("terms"):
                return unescape(a["terms"][0]["name"])
        for b in p.get("brands") or []:
            if b.get("name"):
                return unescape(b["name"])
        m = re.search(r"Производител\s*:\s*([^\n]+)", html_text(p.get("short_description"), "\n"))
        return collapse(m.group(1)) or None if m else None

    @staticmethod
    def attributes_of(p):
        return {unescape(a.get("name") or short_tax(a.get("taxonomy"))):
                ", ".join(unescape(t.get("name")) for t in a.get("terms") or [])
                for a in p.get("attributes") or [] if a.get("terms")}

    def to_record(self, p):
        pr = p.get("prices") or {}
        if pr.get("currency_code") not in (None, "MKD"):
            raise RuntimeError(f"product {p.get('id')}: unexpected currency {pr.get('currency_code')!r}")
        minor = pr.get("currency_minor_unit", 2)
        price = minor_to_mkd(pr.get("price"), minor)
        regular = minor_to_mkd(pr.get("regular_price"), minor)
        if regular is None or price is None or regular <= price:
            regular = None   # only a real strike-through price is reported
        in_stock = p.get("is_in_stock")
        avail = p.get("stock_availability") or {}
        note = collapse(html_text(avail.get("text")))
        if not note:
            if in_stock is True:
                note = "no quantity shown"
            elif in_stock is False:
                note = "out of stock"
        if p.get("is_on_backorder"):
            note = f"{note}; on backorder" if note else "on backorder"
        if p.get("low_stock_remaining") is not None:
            note = f"{note}; only {p['low_stock_remaining']} left"
        cats = [unescape(c.get("name")) for c in p.get("categories") or []]
        attrs = self.attributes_of(p)
        rng = pr.get("price_range") or None
        if rng:
            lo, hi = minor_to_mkd(rng.get("min_amount"), minor), minor_to_mkd(rng.get("max_amount"), minor)
            if lo is not None and hi is not None and hi != lo:
                attrs["variant price range (MKD)"] = f"{lo}-{hi}"
        rec = {
            "store": STORE,
            "id": str(p["id"]),
            "sku": collapse(p.get("sku")) or None,
            "title": unescape(p.get("name")),
            "url": p.get("permalink") or f"{BASE}/?p={p['id']}",
            "brand": self.brand_of(p),
            "price_mkd": price,
            "regular_price_mkd": regular,
            "in_stock": in_stock if isinstance(in_stock, bool) else None,
            "stock_note": note or None,
            "category": " | ".join(c for c in cats if c) or None,
            "ean": None,   # the shop publishes no EAN/GTIN anywhere
        }
        if attrs:
            rec["attributes"] = attrs
        return rec

    # ------------------------------------------------------------ categories
    def _categories(self):
        if self._cats is None:
            cats, page = [], 1
            while True:
                data, r = self._get("/products/categories", params={"per_page": 100, "page": page})
                if not isinstance(data, list):
                    raise RuntimeError("/products/categories did not return a list")
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
        if re.match(r"^(https?://|/|(www\.)?hivetec\.mk/)", ref, re.I):
            u = urlparse(ref if "://" in ref else
                         ("https://" + ref if re.match(r"(?i)(www\.)?hivetec\.mk/", ref) else BASE + ref))
            if u.netloc and not re.fullmatch(r"(?i)(www\.)?hivetec\.mk(:\d+)?", u.netloc):
                raise Usage(f"not a hivetec.mk URL: {ref}")
            m = re.search(r"/product-category/(?:[^/]+/)*?([^/]+)/?(?:page/\d+/?)?$", u.path)
            if not m:
                raise NotFound(f"not a /product-category/<slug>/ URL: {ref}")
            parts = [m.group(1)]
        else:
            parts = [x.strip() for x in ref.split(",") if x.strip()]
        by_slug = {unquote(c["slug"]).lower(): c for c in cats.values()}
        by_name = {}
        for c in cats.values():
            by_name.setdefault(c["_name"].casefold(), []).append(c)
            if c["_path"] != c["_name"]:   # the breadcrumb `categories` prints ("ПРОЦЕСОРИ > INTEL")
                by_name.setdefault(c["_path"].casefold(), []).append(c)
        if len(by_name.get(collapse(ref).casefold(), [])) == 1:   # a name that itself contains a comma
            return [by_name[collapse(ref).casefold()][0]["id"]]
        ids = []
        for x in parts:
            if x.isdigit() and int(x) in cats:
                ids.append(int(x))
            elif x.lower() in by_slug:
                ids.append(by_slug[x.lower()]["id"])
            elif collapse(x).casefold() in by_name and len(by_name[collapse(x).casefold()]) == 1:
                ids.append(by_name[collapse(x).casefold()][0]["id"])
            else:
                rx = re.compile(re.escape(x), re.I)
                near = [c for c in cats.values() if any(rx.search(f) for f in self._cat_haystack(c) if f)]
                hint = ("; did you mean: " + ", ".join(f"{c['id']} {unquote(c['slug'])} ({c['_name']})"
                                                         for c in near[:8])) if near else ""
                raise NotFound(f"unknown category {x!r} (the API would silently return 0 products)"
                               f"{hint}. Run `categories --grep ...`")
        return list(dict.fromkeys(ids))

    def _cat_label(self, ids):
        cats = self._categories()
        return ", ".join(f"{i} {cats[i]['_name']}" for i in ids)

    # ------------------------------------------------------------- search
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
                # the stem must start a word ("top" must not hit "laptop" / "desktop")
                if len(stem) >= 3 and any(re.search(r"(?<![^\W_])" + re.escape(s), hay)
                                          for s in {stem, translit(stem)}):
                    hits.append(c)
                    break
        return sorted(hits, key=lambda c: -(c.get("count") or 0))

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
        words = [q] if phrase else list(dict.fromkeys(q.split(" ")))
        short = [w for w in words if len(w) <= 2]
        if short and not phrase:
            log(f"  note: {', '.join(map(repr, short))}: words of 1-2 characters "
                + ("match inside many titles/SKUs" if len(words) == 1 else
                   "are matched client-side as whole tokens (not as substrings)"))
        subst = {}

        if len(words) == 1:
            w = words[0]
            prods, meta = self.walk(limit=limit, label=f"search {w!r}", search=w, **base)
            if not prods and has_cyrillic(w) and translit(w) != w.lower():
                log(f"  {w!r}: 0 hits (titles are English); retrying the transliteration "
                    f"{translit(w)!r}")
                subst[w] = translit(w)
                prods, meta = self.walk(limit=limit, label=f"search {translit(w)!r}",
                                        search=translit(w), **base)
            log(f"  search {subst.get(w, w)!r}: site reports {meta.get('total')} hit(s) "
                f"(one substring of title or SKU)")
        else:
            counts = {}
            # 1-2 character words ("g", "x2"...) are poor anchors and are checked as whole tokens.
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
            words = [subst.get(w, w) for w in words]
            counts = {subst.get(w, w): n for w, n in counts.items()}
            if min(counts.values()) == 0:
                zero = [w for w, n in counts.items() if n == 0]
                log(f"  no title or SKU contains {', '.join(map(repr, zero))}; 0 results")
                prods, meta = [], {"total": 0}
            else:
                anchor = min(counts, key=lambda w: (counts[w], -len(w)))
                got, meta = self.walk(label=f"search {anchor!r}", search=anchor, **base)
                checks = []
                for w in words:
                    if w == anchor:
                        continue   # already matched by the server
                    # A Cyrillic word that was not counted (only the COUNT_WORDS_MAX longest are) was
                    # never transliterated: accept it as typed or in either Latin form.
                    alts = {fold(w)}
                    if has_cyrillic(w):
                        alts |= {fold(translit(w)), fold(translit(w, simple=True))}
                    if len(w) <= 2 and len(long_words) < len(words):
                        rx = re.compile(r"(?<![^\W_])(?:" + "|".join(map(re.escape, sorted(alts)))
                                        + r")(?![^\W_])")
                        checks.append(lambda hay, rx=rx: bool(rx.search(hay)))
                    else:
                        checks.append(lambda hay, alts=tuple(alts): any(o in hay for o in alts))
                prods = [p for p in got
                         if all(chk(fold(p.get("name")) + " " + fold(p.get("sku"))) for chk in checks)]
                log(f"  the store matches the whole query as ONE substring of title or SKU; searched "
                    f"the rarest word {anchor!r} ({meta.get('total')} hits; counts "
                    f"{', '.join(f'{w}={n}' for w, n in counts.items())}) and kept {len(prods)} whose "
                    f"title/SKU contain every word")
                if limit:
                    prods = prods[:limit]
        recs = [self.to_record(p) for p in prods]
        if not recs:
            hint = self._category_hint(q.split(" "))
            if hint:
                log("  categories that match the query words (try `list <id>`): " + "; ".join(
                    f"{c['id']} {unquote(c['slug'])} {c['_name']} ({c.get('count')})" for c in hint[:8]))
            elif has_cyrillic(q):
                log("  titles are English: search with English words or model codes, or use "
                    "`categories --grep`")
        return recs

    # --------------------------------------------------------------- filters
    def _vocab(self, prods):
        """Attribute vocabulary of a product set: short key -> {label, taxonomy, terms{id: term}}."""
        vocab = {}
        for p in prods:
            for a in p.get("attributes") or []:
                tax = a.get("taxonomy") or ""
                if not tax:
                    continue
                v = vocab.setdefault(short_tax(tax), {"label": unescape(a.get("name")) or tax,
                                                      "taxonomy": tax, "terms": {}})
                for t in a.get("terms") or []:
                    v["terms"][t["id"]] = t
        return vocab

    @staticmethod
    def _term_matches(term, val):
        val, name = fold(val.strip()), unescape(term.get("name"))
        return (val == str(term.get("id")) or val == fold(term_slug(term)) or val == fold(name)
                or val in (fold(translit(name)), fold(translit(name, simple=True))))

    def parse_filters(self, tokens, prods):
        """Filter tokens -> list of (description, predicate(product, record))."""
        vocab = self._vocab(prods)
        aliases = {}
        for key, v in vocab.items():
            for al in (key, v["taxonomy"], v["label"]):
                aliases[fold(al)] = key
        preds = []
        for tok in tokens:
            if "=" not in tok:
                raise Usage(f"bad --filter {tok!r}: expected KEY=VALUE (see `facets <category>`)")
            k, v = tok.split("=", 1)
            k, vals = fold(k.strip()), [x.strip() for x in v.split(",") if x.strip()]
            if not vals:
                raise Usage(f"bad --filter {tok!r}: empty value")
            if k == "price":
                m = re.fullmatch(r"\s*(\d[\d.,]*)?\s*-\s*(\d[\d.,]*)?\s*", v)
                if not m or not (m.group(1) or m.group(2)):
                    raise Usage(f"bad --filter {tok!r}: expected price=MIN-MAX, price=-MAX or price=MIN-")
                lo = int(re.sub(r"\D", "", m.group(1))) if m.group(1) else None
                hi = int(re.sub(r"\D", "", m.group(2))) if m.group(2) else None
                preds.append((tok, lambda p, r, lo=lo, hi=hi: r["price_mkd"] is not None
                              and (lo is None or r["price_mkd"] >= lo) and (hi is None or r["price_mkd"] <= hi)))
            elif k == "stock":
                want = {x.lower() for x in vals}
                if not want <= {"in", "out"}:
                    raise Usage(f"bad --filter {tok!r}: stock=in or stock=out")
                preds.append((tok, lambda p, r, want=want: ("in" if r["in_stock"] else "out") in want))
            elif k in ("cat", "category"):
                ids = set()
                for x in vals:
                    ids.update(self.resolve_category(x))
                preds.append((tok, lambda p, r, ids=ids: any(c["id"] in ids for c in p.get("categories") or [])))
            elif k == "brand" or aliases.get(k) == "brand":
                terms = vocab.get("brand", {}).get("terms", {}).values()
                known = {fold(self.brand_of(p) or "") for p in prods} - {""}
                ok = [x for x in vals if fold(x) in known or any(self._term_matches(t, x) for t in terms)]
                if not ok:
                    raise Usage(f"--filter {tok!r}: no product in this category has that brand; brands here: "
                                + ", ".join(sorted({self.brand_of(p) for p in prods if self.brand_of(p)})[:40]))
                if len(ok) < len(vals):
                    warn(f"--filter {tok!r}: unknown value(s) ignored: {', '.join(set(vals) - set(ok))}")
                preds.append((tok, lambda p, r, ok=ok: any(
                    fold(x) == fold(r.get("brand") or "") or any(
                        self._term_matches(t, x) for a in p.get("attributes") or []
                        if a.get("taxonomy") == "pa_brand" for t in a.get("terms") or []) for x in ok)))
            elif k in aliases:
                key = aliases[k]
                tax, terms = vocab[key]["taxonomy"], vocab[key]["terms"].values()
                ok = [x for x in vals if any(self._term_matches(t, x) for t in terms)]
                if not ok:
                    raise Usage(f"--filter {tok!r}: unknown value; values of {vocab[key]['label']} here: "
                                + ", ".join(f"{term_slug(t)} ({unescape(t['name'])})" for t in terms))
                if len(ok) < len(vals):
                    warn(f"--filter {tok!r}: unknown value(s) ignored: {', '.join(set(vals) - set(ok))}")
                preds.append((tok, lambda p, r, tax=tax, ok=ok: any(
                    self._term_matches(t, x) for a in p.get("attributes") or [] if a.get("taxonomy") == tax
                    for t in a.get("terms") or [] for x in ok)))
            else:
                raise Usage(f"--filter {tok!r}: unknown key {k!r}; keys in this category: "
                            + ", ".join(sorted(set(vocab) | {"brand", "cat", "price", "stock"})))
        return preds

    # ------------------------------------------------------------------ list
    def list_category(self, ref, in_stock=False, limit=None, filters=None):
        ids = self.resolve_category(ref)
        cats = self._categories()
        expected = sum(cats[i].get("count") or 0 for i in ids)
        params = {"category": ",".join(map(str, ids))}
        server_stock = in_stock and not filters
        if server_stock:
            params["stock_status"] = "instock"
        prods, meta = self.walk(limit=None if filters else limit, label=f"category {self._cat_label(ids)}",
                                **params)
        log(f"  category {self._cat_label(ids)}: X-WP-Total {meta.get('total')} (term count "
            f"{'+'.join(str(cats[i].get('count')) for i in ids)}), fetched {len(prods)} over "
            f"{meta.get('pagesFetched')} page(s){' [in stock, server-side]' if server_stock else ''}")
        if not prods:
            if server_stock and expected:
                log(f"  none of the {expected} product(s) in this category is in stock")
                return []
            warn(f"category {self._cat_label(ids)} returned 0 products (term count {expected}); "
                 f"soft block or catalogue change?")
            raise NotFound(f"category {ref!r}: the store returned no products")
        recs = [self.to_record(p) for p in prods]
        if filters:
            preds = self.parse_filters(filters, prods)
            keep = [(p, r) for p, r in zip(prods, recs) if all(f(p, r) for _, f in preds)]
            if in_stock:
                keep = [(p, r) for p, r in keep if r["in_stock"]]
            log(f"  filters {' AND '.join(t for t, _ in preds)}{' + in stock' if in_stock else ''}: "
                f"{len(keep)} of {len(recs)}")
            recs = [r for _, r in keep]
            if limit:
                recs = recs[:limit]
        elif in_stock:
            recs = [r for r in recs if r["in_stock"]]
        return recs

    # ---------------------------------------------------------------- facets
    def facets(self, ref):
        ids = self.resolve_category(ref)
        prods, meta = self.walk(label=f"category {self._cat_label(ids)}", category=",".join(map(str, ids)))
        if not prods:
            warn(f"category {self._cat_label(ids)} returned 0 products")
            raise NotFound(f"category {ref!r}: the store returned no products")
        recs = [self.to_record(p) for p in prods]
        out = []

        def add(name, value, members, token):
            out.append({"name": name, "value": value, "count": len(members),
                        "in_stock_count": sum(1 for r in members if r["in_stock"]), "token": token})

        # brand (pa_brand term, or the short description's "Производител:" when the term is missing)
        brands = {}
        for p, r in zip(prods, recs):
            if not r["brand"]:
                continue
            slug = next((term_slug(t) for a in p.get("attributes") or [] if a.get("taxonomy") == "pa_brand"
                         for t in a.get("terms") or []), None)
            brands.setdefault(fold(r["brand"]), [r["brand"], slug, []])[2].append(r)
        for name, slug, members in sorted(brands.values(), key=lambda x: (-len(x[2]), x[0].lower())):
            tok_val = slug if slug and re.fullmatch(r"[\w.+-]+", slug) else name
            add("brand (БРЕНД)", name, members, f"brand={tok_val}")
        # every other attribute taxonomy
        vocab = self._vocab(prods)
        for key in sorted(vocab, key=lambda k: vocab[k]["label"]):
            if key == "brand":
                continue
            v = vocab[key]
            for t in sorted(v["terms"].values(), key=lambda t: unescape(t["name"]).lower()):
                members = [r for p, r in zip(prods, recs) if any(
                    tt["id"] == t["id"] for a in p.get("attributes") or [] if a.get("taxonomy") == v["taxonomy"]
                    for tt in a.get("terms") or [])]
                if members:
                    add(f"{v['label']} ({key})", unescape(t["name"]), members, f"{key}={term_slug(t)}")
        # co-categories (the taxonomy is flat and multi-valued: brand / series / promo categories)
        co = {}
        for p, r in zip(prods, recs):
            for c in p.get("categories") or []:
                if c["id"] not in ids:
                    co.setdefault(c["id"], (c, []))[1].append(r)
        for cid, (c, members) in sorted(co.items(), key=lambda x: -len(x[1][1])):
            add("also in category", unescape(c.get("name")), members, f"cat={unquote(c.get('slug') or str(cid))}")
        prices = [r["price_mkd"] for r in recs if r["price_mkd"] is not None]
        if prices:
            add("price (MKD)", f"{min(prices)}-{max(prices)}", recs, f"price={min(prices)}-{max(prices)}")
        add("stock", "in stock", [r for r in recs if r["in_stock"]], "stock=in")
        add("stock", "out of stock", [r for r in recs if r["in_stock"] is False], "stock=out")
        log(f"  {len(prods)} products, {len(out)} facet values")
        return out

    # ---------------------------------------------------------------- detail
    @staticmethod
    def parse_product_ref(ref):
        """-> ('id', int) | ('slug', str) | ('sku', str) | ('bare', str)"""
        s = unquote((ref or "").strip())
        if not s:
            raise Usage("empty product reference")
        m = re.match(r"(?i)^(sku|code|mpn):\s*(.+)$", s)
        if m:
            return "sku", m.group(2).strip()
        m = re.match(r"(?i)^id:\s*(\d+)$", s)
        if m:
            return "id", int(m.group(1))
        if re.match(r"^(https?://|/|(www\.)?hivetec\.mk)", s, re.I):
            u = urlparse(s if "://" in s else ("https://" + s.lstrip("/") if "hivetec.mk" in s else BASE + s))
            if u.netloc and "hivetec.mk" not in u.netloc:
                raise Usage(f"not a hivetec.mk URL: {ref}")
            m = re.search(r"[?&](?:p|post|product_id|add-to-cart)=(\d+)", "?" + u.query)
            if m:
                return "id", int(m.group(1))
            m = re.search(r"/product/(?:[^/]+/)*?([^/]+)/?$", u.path)
            if m:
                return "slug", m.group(1)
            raise Usage(f"not a product URL (expected /product/<slug>/ or ?p=<id>): {ref}")
        if re.fullmatch(r"\d+", s):
            return "id", int(s)
        return "bare", s

    @staticmethod
    def variant_wish(ref):
        """The variant a product URL selects: (attribute values from attribute_* params, folded and
        fully unquoted; variation_id or None). The shop's variant links double-encode the value."""
        q = urlparse((ref or "").strip()).query
        vals, vid = [], None
        for k, v in parse_qsl(q):
            if k.startswith("attribute_") and v:
                vals.append(deep_unquote(v).casefold())
            elif k == "variation_id" and v.isdigit():
                vid = int(v)
        return vals, vid

    def _by_sku(self, code):
        data, _ = self._get("/products", params={"sku": code})
        return data[0] if isinstance(data, list) and data else None

    def _by_slug(self, slug):
        data, _ = self._get(f"/products/{slug}", allow_404=True)
        return data if isinstance(data, dict) and data.get("id") else None

    def _by_id(self, pid):
        """/products/<id> also returns a variation (type 'variation', with 'parent'), which the
        include= listing does not."""
        pid = int(pid)
        if pid not in self._by_id_cache:
            data, _ = self._get(f"/products/{pid}", allow_404=True)
            self._by_id_cache[pid] = data if isinstance(data, dict) and data.get("id") else None
        return self._by_id_cache[pid]

    def _variations(self, p):
        """Each variation of a variable product with its own price and stock (1 request each)."""
        rows = []
        for v in (p.get("variations") or [])[:MAX_VARIATIONS]:
            row = {"id": v.get("id"),
                   "attributes": {unescape(a.get("name")): deep_unquote(a.get("value") or "")
                                  for a in v.get("attributes") or []}}
            vd = self._by_id(v["id"]) if v.get("id") else None
            if vd:
                vr = self.to_record(vd)
                row.update({k: vr[k] for k in ("price_mkd", "regular_price_mkd", "in_stock", "sku", "url")})
            rows.append(row)
        return rows

    def delivery_estimate(self):
        """Store-wide delivery promise from the 'Купување и испорака' policy page (fetched once)."""
        if self._delivery is False:
            self._delivery = None
            try:
                data, _ = self._get(BASE + "/wp-json/wp/v2/pages",
                                    params={"slug": DELIVERY_POLICY_SLUG, "_fields": "content"})
                text = collapse(html_text(data[0]["content"]["rendered"])) if data else ""
                m = re.search(r"[^.]*време\s+за\s+испорака[^.]*\.", text)
                if m:
                    self._delivery = (f"{collapse(m.group(0))} Working days; orders after 16:00 are "
                                      f"confirmed the next working day (store-wide policy, "
                                      f"{BASE}/{DELIVERY_POLICY_SLUG}/)")
            except Blocked:
                raise
            except (RuntimeError, Usage, KeyError, IndexError, TypeError) as e:
                log(f"  note: delivery policy page unavailable: {e}")
        return self._delivery

    def detail_record(self, p, ref, want_vid=None, want_vals=None):
        rec = self.to_record(p)
        variations = self._variations(p) if p.get("type") == "variable" else []
        selected = None
        if variations and (want_vid or want_vals):
            selected = next((v for v in variations if v["id"] == want_vid), None) if want_vid else next(
                (v for v in variations
                 if all(any(w == x.casefold() for x in v["attributes"].values()) for w in want_vals)), None)
            if selected and selected.get("price_mkd") is not None:
                # the link names one variant: report what that variant costs, not the "from" price
                rec.update({k: selected[k] for k in ("price_mkd", "regular_price_mkd", "in_stock", "url")})
                rec["stock_note"] = "no quantity shown" if selected["in_stock"] else "out of stock"
                log(f"  {ref}: variant {selected['id']} "
                    f"({', '.join(selected['attributes'].values())}) = {selected['price_mkd']} MKD")
            elif not selected:
                log(f"  {ref}: no variation matches the link; reporting the lowest variant price")
        short = html_text(p.get("short_description"), "\n")
        m = re.search(r"Гаранција\s*:\s*([^\n]+)", short)
        warranty_raw = collapse(m.group(1)) if m else None
        days = None
        if warranty_raw:
            md = re.search(r"(\d+)\s*(?:дена|ден|days?)\b", warranty_raw, re.I)
            days = int(md.group(1)) if md else None
        warranty = warranty_raw if warranty_raw and days != 0 else None   # "0 дена" = not entered
        desc = collapse(html_text(p.get("description")))
        me = re.search(r"\b(?:EAN|GTIN|UPC|barcode|баркод)\b\s*(?:code|код)?\s*[:#]?\s*(\d{8,14})\b", desc, re.I)
        attrs = "; ".join(f"{k}: {v}" for k, v in (rec.get("attributes") or {}).items())
        specs = " | ".join(x for x in (collapse(short), desc, attrs) if x)
        pr = p.get("prices") or {}
        minor = pr.get("currency_minor_unit", 2)
        rng = pr.get("price_range") or None
        rec.update({
            "input": ref,
            "ean": me.group(1) if me else None,
            "warranty": warranty,
            "specs": specs or None,
            "per_location_stock": None,   # one web stock flag only; no per-store breakdown exposed
            "delivery_estimate": self.delivery_estimate() if rec["in_stock"] else None,
            "extra": {
                "warranty_raw": warranty_raw,
                "warranty_days": days,
                "type": p.get("type"),
                "price_range_mkd": ([minor_to_mkd(rng.get("min_amount"), minor),
                                     minor_to_mkd(rng.get("max_amount"), minor)] if rng else None),
                "variations": variations or None,
                "selected_variation": selected["id"] if selected else None,
                "strike_through_note": ("the struck-through 'regular' price is a near-permanent list "
                                        "price on this shop (2437 of 2446 products), not a time-limited sale"
                                        if rec["regular_price_mkd"] else None),
                "category_ids": [c["id"] for c in p.get("categories") or []],
                "slug": p.get("slug"),
                "delivery_fee_note": ("free delivery over 4,500 den per the site header banner (checked "
                                      "2026-10-03); the fee below that is not published"),
            },
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
            ids = list(dict.fromkeys(v for _, k, _ in parsed if k and k[0] == "id" for v in [k[1]]))
            found = {}
            for chunk in id_chunks(ids):
                got, _ = self.walk(fields=None, label="detail", include=",".join(map(str, chunk)))
                found.update({p["id"]: p for p in got})
            for i, (ref, k, err) in enumerate(parsed):
                if err:
                    log(f"  {err}")
                    out.append({"input": ref, "error": err})
                    continue
                try:
                    kind, val = k
                    p, how = None, None
                    if kind == "id":
                        p = found.get(val) or self._by_id(val)   # _by_id also finds variation ids
                        if p is None:
                            p, how = self._by_sku(str(val)), f"no product id {val}; matched SKU {val}"
                    elif kind == "slug":
                        p = self._by_slug(val)
                    elif kind == "sku":
                        p = self._by_sku(val)
                    else:   # bare text: a slug or a SKU
                        p = self._by_slug(val) if re.fullmatch(r"[\w%-]+", val) else None
                        if p is None:
                            p, how = self._by_sku(val), f"matched SKU {val!r}"
                    want_vals, want_vid = self.variant_wish(ref) if kind != "bare" else ([], None)
                    if p is not None and p.get("type") == "variation":
                        how = f"variation {p['id']} of product {p.get('parent')}"
                        want_vid = p["id"]
                        p = found.get(p.get("parent")) or (self._by_id(p["parent"]) if p.get("parent") else None)
                    if p is None:
                        raise NotFound(f"no product for {kind} {val!r} (unknown, unpublished or removed)")
                    if how:
                        log(f"  {ref}: {how}")
                    out.append(self.detail_record(p, ref, want_vid=want_vid, want_vals=want_vals))
                    ok += 1
                except NotFound as e:
                    log(f"  not found: {ref} ({e})")
                    out.append({"input": ref, "error": f"not found: {e}"})
                except Usage as e:
                    out.append({"input": ref, "error": f"bad input: {e}"})
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
            print(f"        id {r.get('id')} | SKU {r.get('sku')} | warranty {r.get('warranty')} | "
                  f"{r.get('category')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{r['id']:>6} {cnt}  {r['path']}  [{r['slug']}]")


def print_facets(recs):
    for r in recs:
        print(f"{r['count']:>5} ({r.get('in_stock_count', '?'):>3} in stock)  {r['name']}: {r['value']}"
              f"   --filter '{r['token']}'")


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
    for _stream in (sys.stdout, sys.stderr):   # UTF-8 output on every platform, even when piped
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    ap = argparse.ArgumentParser(description="Hivetec (hivetec.mk) catalogue client")
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
                   help="send the query verbatim (the store matches it as one substring of title/SKU)")
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
    c = Hivetec(verbose=a.verbose or a.g_verbose)
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
