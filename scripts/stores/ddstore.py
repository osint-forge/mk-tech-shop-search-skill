#!/usr/bin/env python3
"""DDStore (https://ddstore.mk) catalogue client for mk-shop-search.

Contract: references/client-contract.md. Store details: references/ddstore.md.

DDStore runs Magento 2 (Sm/xtore theme, Amasty layered navigation, Magnetix multi-offer module)
behind Cloudflare. Data sources, in order of preference:
  1. Magento GraphQL  POST https://ddstore.mk/graphql  (anonymous; header "Store: mk")
     categoryList (tree + counts), products(search:, filter:{category_id, <attribute codes>, price})
     with aggregations (= the site's layered-navigation facets), route(url:) for URLs/ids,
     customAttributeMetadataV2 for option-id -> label maps.
  2. Server-rendered HTML (automatic fallback when GraphQL is unavailable, or --backend html):
     category pages /mk/<url_path>.html?<filters>&p=N (24 per page), search
     /mk/catalogsearch/result/?q=...&p=N, product pages (JSON-LD + "Повеќе информации" table).
Product pages are also read in GraphQL mode by `detail`, for the Magnetix offer list
("N понуди за овој производ"), which GraphQL does not expose.

Cloudflare challenges non-browser User-Agents (python-requests default, curl, empty, headless),
so a desktop Chrome UA is mandatory. Never request custom_attributesV2 without the
is_visible_on_front filter: unfiltered it exposes internal distributor/purchase-price fields.

Usage:
    ddstore.py info
    ddstore.py search "<query>" [--limit N] [--in-stock] [--category CAT] [--json PATH]
    ddstore.py categories [--grep REGEX] [--json PATH]
    ddstore.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    ddstore.py detail <url-or-id> [...] [--no-offers] [--json PATH]
    ddstore.py facets <category> [--json PATH]
    common options: --backend {auto,graphql,html}  -v/--verbose  --quiet

<category> is a category id (545), its url_path slug (monitorstvandprojectors/monitorsandequipment/
monitors), a category URL (filters in its query string are kept), or an exact category name.
Filter tokens (from `facets`): CODE=OPTION_ID[,OPTION_ID...] (values OR, flags AND), e.g.
brand=2215, diagonal_televisions=9571,9575; an option label also works (brand=Samsung);
price=FROM-TO filters on the NET price (VAT excluded, as the site's slider does);
price_mkd=FROM-TO filters exactly on the VAT-inclusive price the buyer pays.
"""

import argparse
import html as htmlmod
import json
import math
import re
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "ddstore"
NAME = "DDStore"
BASE = "https://ddstore.mk"
LOCALE = "/mk"                      # Macedonian store view (store code "mk"); /en and /al also exist
GRAPHQL = BASE + "/graphql"
ROOT_CATEGORY_ID = 2                # "Default Category"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

SELLS = ("IT and electronics shop (~13,800 products, 15 departments): PC components (cases, "
         "motherboards, GPUs, CPUs, PSUs, cooling, RAM, SSD/HDD, UPS, cables), printer consumables "
         "(toners/ink, ~2,300), peripherals (mice, keyboards, headsets, webcams, speakers), monitors, "
         "TVs, projectors, laptops, tablets, smartphones, smartwatches, networking, gaming, "
         "smart home and cameras, small home appliances (vacuums, kitchen, personal care), "
         "air conditioners and air care, office furniture, new and refurbished PCs. "
         "Large appliances are a token range (7 products).")
NOTES = ("price_mkd = VAT-inclusive final price of the default (winning) offer, rounded like the "
         "site; regular_price_mkd = struck-through price during a promotion. in_stock: true = "
         "'Во ДДСтор магацин' (own warehouse) / '1-3 дена' (at distributor) and similar; null = "
         "'Прашај за залиха' (ask; ~30% of the catalogue) or conflicting signals; false = 'Нема "
         "залиха' / 'По нарачка' (supplier order, not orderable online). Every order is confirmed "
         "for stock and lead time before payment. Many products have several supplier offers "
         "('N понуди'); listings show only the winning one, `detail` lists all in extra.offers. "
         "Facets = the site's layered navigation (attribute option ids). The price filter/facet is "
         "NET of VAT (5% IT, 18% TV/appliances); use price_mkd=LO-HI for the real price. Search "
         "(GraphQL) ANDs words with fuzzy/transliterated matching over title+description and is "
         "broader than the site's search box; it does not index EAN/part numbers. ean = Part "
         "Number when it is a valid GTIN. shipping_mkd: free from 3,000 MKD, else 150 (parcels "
         "up to 5 kg). Cloudflare challenges non-browser user agents.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing",
                "ean_in_detail", "warranty", "delivery_estimate"]

PAGE_SIZE = 300            # GraphQL page size (server accepts at least 600; 300 items ~0.5 s)
PACE_SECONDS = 0.5         # delay between consecutive requests
MAX_RETRIES = 4
MAX_ALIASES = 10           # server limit: "Max Aliases in query should be 10"
FREE_SHIPPING_FROM = 3000  # FAQ: free delivery for orders over 3,000 MKD (parcels <= 5 kg, zone 1)
SHIPPING_FEE = 150

# availability labels (attributes if_in_stock / if_out_stock); compared lower-cased
IN_STOCK_LABELS = {"1-3 дена", "3-5 дена", "мала залиха", "нови количини", "во ддстор магацин",
                   "последно парче"}
ASK_LABEL = "прашај за залиха"
OUT_LABELS = {"нема залиха", "наскоро", "еол продукт", "по нарачка", "продадено"}
CANON_LABEL = {x.lower(): x for x in (
    "1-3 дена", "3-5 дена", "Мала залиха", "Нови количини", "Во ДДСтор магацин", "Последно парче",
    "Прашај за залиха", "Нема залиха", "Наскоро", "ЕОЛ продукт", "По нарачка", "Продадено")}
DELIVERY = {
    "во ддстор магацин": "own warehouse (Skopje): ready the same working day if paid by 15:00, "
                         "then courier 1-5 working days",
    "1-3 дена": "at distributor: 1-3 working days, then courier 1-5 working days",
    "3-5 дена": "3-5 working days, then courier 1-5 working days",
    "по нарачка": "supplier order on request: typically 1-6 weeks, confirmed by the shop",
    "прашај за залиха": "unconfirmed: the shop confirms stock and lead time before payment",
}
# Magnetix offer-list stock words (product pages) -> label used by the rest of the site
# ("Yes" was seen only on offers whose GraphQL label is "Во ДДСтор магацин", 2026-10-03)
OFFER_STOCK = {"call": "Прашај за залиха", "48 hours": "1-3 дена", "yes": "Во ДДСтор магацин"}

OPTION_ATTRS = ("brand", "if_in_stock", "if_out_stock", "warranty", "promotion_type", "condition")
FACET_SKIP = {"category_uid", "category_id", "importer_locked"}   # not storefront filters
SPEC_SKIP = {"importer_locked"}

# Markers of a Cloudflare challenge / block page. Deliberately NOT "captcha" or
# "challenge-platform": every normal page embeds Magento's captcha config and Cloudflare's
# passive /cdn-cgi/challenge-platform/scripts/jsd/main.js beacon.
CHALLENGE_MARKERS = ("<title>just a moment", "cf-chl", "cf_chl", "cf-turnstile",
                     "challenges.cloudflare.com/turnstile", "attention required! | cloudflare",
                     "cf-error-details", "sorry, you have been blocked")

LIST_FIELDS = """
  id sku name url_key url_suffix canonical_url stock_status part_number
  price_range { minimum_price { regular_price { value } final_price { value } } }
  categories { id name url_path level }
  brand if_in_stock if_out_stock warranty promotion_type condition
"""
DETAIL_FIELDS = LIST_FIELDS + """
  description { html }
  custom_attributesV2(filters: {is_visible_on_front: true}) { items { code
    ... on AttributeValue { value }
    ... on AttributeSelectedOptions { selected_options { label value } } } }
"""
OFFER_FIELDS = ("id sku name canonical_url url_key url_suffix stock_status part_number "
                "if_in_stock if_out_stock warranty "
                "price_range { minimum_price { regular_price { value } final_price { value } } }")

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}


class StoreError(RuntimeError):
    """Unexpected failure (exit 1)."""


class Blocked(StoreError):
    """Cloudflare challenge / block page instead of data (exit 3)."""


class NotFound(StoreError):
    """Unknown category or product (exit 2)."""


class Usage(StoreError):
    """Bad usage (exit 2)."""


class BackendUnavailable(StoreError):
    """GraphQL is gone / changed shape -> callers may fall back to HTML."""


class GraphQLQueryError(BackendUnavailable):
    """The server rejected the query (bad filter field, schema change)."""


# --------------------------------------------------------------------------- helpers

def _collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def _mkd(value):
    """6129.9 -> 6130 (half-up, like the site's Magecko_PriceRounder); '6.130 ден.' -> 6130."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        d = Decimal(str(value))
    else:
        s = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):          # machine format: 6129.90
            d = Decimal(s)
        else:                       # display format: 6.130 ден. / 6,130 / 5990,00 / 1.234,56
            s2 = re.sub(r"[^\d.,]", "", s).strip(".,")
            if not re.search(r"\d", s2):
                return None
            m = re.fullmatch(r"(.*?)[.,](\d{1,2})", s2)      # trailing 1-2 decimals
            whole, frac = (m.group(1), m.group(2)) if m else (s2, "")
            d = Decimal(f"{re.sub(r'[.,]', '', whole) or 0}.{frac or 0}")
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _is_gtin(code):
    if not re.fullmatch(r"\d{8}|\d{12,14}", code):
        return False
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def _gtin(part_number):
    """First valid GTIN-8/12/13/14 in the Part Number field (it sometimes holds 'EAN1,EAN2'),
    else None. The field often holds an MPN instead."""
    for tok in re.split(r"[\s,;/]+", (part_number or "").strip()):
        if tok and _is_gtin(tok):
            return tok
    return None


def _mpn(part_number):
    """Part Number when it looks like a manufacturer code (letters and digits, not a GTIN)."""
    pn = _collapse(part_number)
    if not pn or pn.upper() == "N/A" or _gtin(pn):
        return None
    if re.search(r"[A-Za-z]", pn) and re.search(r"\d", pn) and len(pn) <= 40:
        return pn
    return None


def _looks_like_challenge(resp):
    if resp.headers.get("cf-mitigated"):
        return True
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text/plain" not in ctype:
        return False
    head = resp.text[:30000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def _stock(in_label, out_label, salable):
    """-> (in_stock, stock_note). salable: True/False from Magento stock_status / add-to-cart,
    or None when unknown."""
    il = _collapse(in_label).lower()
    ol = _collapse(out_label).lower()
    note = _collapse(in_label) or _collapse(out_label) or None
    note = CANON_LABEL.get(note.lower(), note) if note else None   # listing cards lower-case some
    if salable is False:
        return False, note or "Нема залиха"
    if il == ASK_LABEL:
        return None, note          # orderable, but the shop says availability must be confirmed
    if il:
        return (True if il in IN_STOCK_LABELS or salable else None), note
    if ol:
        # e.g. "Продадено" while Magento still says IN_STOCK -> conflicting signals
        return (None if salable or ol not in OUT_LABELS else False), note
    return (True if salable else None), note


def _shipping(price):
    if not price:
        return None
    return 0 if price >= FREE_SHIPPING_FROM else SHIPPING_FEE


def _warranty_text(w):
    w = _collapse(w)
    if not w or w in ("0", " "):
        return None
    if w in ("9999", "999"):
        return "по компоненти (per component)" if w == "9999" else f"{w} денови"
    return f"{w} денови" if w.isdigit() else w


def _clean_url(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _strip_html(text):
    if not text:
        return ""
    return _collapse(htmlmod.unescape(BeautifulSoup(text, "html.parser").get_text(" ")))


# --------------------------------------------------------------------------- client

class DDStore:
    def __init__(self, backend="auto", pace=PACE_SECONDS, verbose=False, quiet=False):
        self.backend = backend
        self.pace = pace
        self.verbose = verbose
        self.quiet = quiet
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "mk,en;q=0.8"})
        self._last = 0.0
        self._options = None        # {attr_code: {option_id(int): label}}
        self._attr_labels = {}      # {attr_code: frontend label}
        self._nodes = None          # flat category list (GraphQL tree)
        self.requests_made = 0

    def log(self, msg):
        if not self.quiet:
            print(f"[{STORE}] {msg}", file=sys.stderr)

    def warn(self, msg):
        print(f"[{STORE}] WARNING: {msg}", file=sys.stderr)

    # ------------------------------------------------------------------ http
    def _request(self, method, url, **kw):
        kw.setdefault("timeout", 90)
        r = None
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                self._last = time.monotonic()
                if attempt == MAX_RETRIES:
                    raise StoreError(f"{method} {url} failed: {e}") from e
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                self.log(f"{method} {url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s "
                         f"cf-cache={r.headers.get('cf-cache-status')}")
            if _looks_like_challenge(r) or r.status_code == 403:
                title = re.search(r"<title>([^<]*)", r.text[:5000], re.I)
                raise Blocked(
                    f"HTTP {r.status_code} on {method} {url}, cf-mitigated={r.headers.get('cf-mitigated')}, "
                    f"title={title.group(1).strip() if title else None!r}, cf-ray={r.headers.get('cf-ray')} "
                    f"(Cloudflare challenge/block page instead of data)")
            if r.status_code in (429, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** attempt
                self.log(f"HTTP {r.status_code} on {url}, retrying in {delay}s")
                time.sleep(min(delay, 60))
                continue
            return r
        raise StoreError(f"{method} {url}: still HTTP {r.status_code if r is not None else '?'} "
                         f"after {MAX_RETRIES} attempts")

    # --------------------------------------------------------------- graphql
    def gql(self, query, variables=None):
        r = self._request("POST", GRAPHQL, json={"query": query, "variables": variables or {}},
                          headers={"Store": "mk", "Content-Type": "application/json"})
        ctype = r.headers.get("content-type", "")
        if r.status_code not in (200, 400, 500) or "json" not in ctype:
            raise BackendUnavailable(f"GraphQL: HTTP {r.status_code} ({ctype}); body {r.text[:150]!r}")
        try:
            data = r.json()
        except ValueError as e:
            raise BackendUnavailable(f"GraphQL: invalid JSON: {e}") from e
        errs = data.get("errors") or []
        if errs:
            msgs = "; ".join(e.get("message", "?") for e in errs)
            if data.get("data") is None:
                raise GraphQLQueryError(f"GraphQL errors: {msgs}")
            self.log(f"GraphQL partial errors (ignored): {msgs}")
        if r.status_code != 200 or data.get("data") is None:
            raise BackendUnavailable(f"GraphQL: HTTP {r.status_code}, no data")
        return data["data"]

    def options(self):
        """Option-id -> label maps for the select attributes used in records (one request)."""
        if self._options is None:
            q = ("query($a:[AttributeInput!]){ customAttributeMetadataV2(attributes:$a){ "
                 "items{ code label options{ label value } } } }")
            d = self.gql(q, {"a": [{"attribute_code": c, "entity_type": "catalog_product"}
                                   for c in OPTION_ATTRS]})
            self._options = {}
            for it in d["customAttributeMetadataV2"]["items"]:
                self._attr_labels[it["code"]] = it.get("label")
                self._options[it["code"]] = {int(o["value"]): _collapse(o["label"])
                                             for o in it.get("options") or [] if str(o["value"]).isdigit()}
        return self._options

    def attr_labels(self, codes):
        missing = [c for c in codes if c not in self._attr_labels]
        if missing:
            q = ("query($a:[AttributeInput!]){ customAttributeMetadataV2(attributes:$a){ "
                 "items{ code label } } }")
            d = self.gql(q, {"a": [{"attribute_code": c, "entity_type": "catalog_product"} for c in missing]})
            for it in d["customAttributeMetadataV2"]["items"]:
                self._attr_labels[it["code"]] = it.get("label")
            for c in missing:
                self._attr_labels.setdefault(c, c)
        return {c: self._attr_labels.get(c) or c for c in codes}

    def _opt(self, code, value):
        """Label of a select/multiselect value (multiselects come back as '3721,2918')."""
        if value is None or value == "":
            return None
        labels = []
        for v in str(value).split(","):
            try:
                lab = self.options().get(code, {}).get(int(v))
            except (TypeError, ValueError):
                lab = None
            if lab and lab.strip():
                labels.append(lab)
        return ", ".join(labels) or None

    def stock_option_ids(self):
        """if_in_stock option ids that mean 'in stock' (server-side --in-stock narrowing)."""
        return [str(k) for k, v in self.options().get("if_in_stock", {}).items()
                if v.lower() in IN_STOCK_LABELS]

    def gql_products(self, flt=None, search=None, limit=None, fields=LIST_FIELDS, want_aggs=False):
        """Walk every page of products(...). Returns (items, meta). Verifies total_count."""
        aggs = "aggregations { attribute_code label options { label value count } }"
        q_tpl = ("query($f: ProductAttributeFilterInput, $s: String, $ps: Int, $cp: Int) {"
                 " products(filter: $f, search: $s, pageSize: $ps, currentPage: $cp%s) {"
                 " total_count page_info { total_pages current_page } %s items { %s } } }")
        sort = "" if search else ", sort: {name: ASC}"
        items, seen, page, meta = [], set(), 1, {}
        ps = min(PAGE_SIZE, limit) if limit else PAGE_SIZE
        while True:
            q = q_tpl % (sort, aggs if (want_aggs and page == 1) else "", fields)
            v = {"f": flt or {}, "ps": ps, "cp": page}
            if search:
                v["s"] = search
            p = self.gql(q, v)["products"]
            if page == 1:
                meta = {"total_count": p["total_count"],
                        "total_pages": p["page_info"]["total_pages"],
                        "aggregations": p.get("aggregations")}
            for it in p["items"]:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
            if limit and len(items) >= limit:
                meta["pages_fetched"] = page
                return items[:limit], meta
            if page >= (p["page_info"]["total_pages"] or 0) or not p["items"]:
                break
            page += 1
        meta["pages_fetched"] = page
        if len(items) < meta["total_count"] and not limit and meta["total_count"] <= 1000:
            # unstable ordering across pages -> refetch everything in one page
            self.log(f"collected {len(items)}/{meta['total_count']} unique; refetching in one page")
            q = q_tpl % (sort, "", fields)
            v = {"f": flt or {}, "ps": meta["total_count"], "cp": 1}
            if search:
                v["s"] = search
            for it in self.gql(q, v)["products"]["items"]:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
        if len(items) != meta["total_count"] and not limit:
            self.warn(f"collected {len(items)} unique products but GraphQL total_count is "
                      f"{meta['total_count']}")
        return items, meta

    @staticmethod
    def _category_path(cats, prefer_path=None):
        if not cats:
            return None
        pool = cats
        if prefer_path:
            under = [c for c in cats if (c.get("url_path") or "") == prefer_path
                     or (c.get("url_path") or "").startswith(prefer_path + "/")]
            pool = under or cats
        leaf = max(pool, key=lambda c: (c.get("level") or 0, len(c.get("url_path") or "")))
        lp = leaf.get("url_path") or ""
        chain = [c for c in cats if c.get("url_path") and (lp + "/").startswith(c["url_path"] + "/")]
        chain.sort(key=lambda c: (c.get("level") or 0, len(c["url_path"])))
        names = []
        for c in chain:
            if c["name"] not in names:
                names.append(c["name"])
        return " > ".join(names) or leaf.get("name")

    @staticmethod
    def _gql_url(it):
        # canonical_url is "<url_key>.html" or, for products without a store-view URL rewrite,
        # "pid/<sku>" -- building from url_key alone 404s for those.
        path = it.get("canonical_url") or f"{it['url_key']}{it.get('url_suffix') or '.html'}"
        return f"{BASE}{LOCALE}/{path.lstrip('/')}"

    def gql_record(self, it, prefer_path=None):
        mp = (it.get("price_range") or {}).get("minimum_price") or {}
        price = _mkd((mp.get("final_price") or {}).get("value")) or None
        reg = _mkd((mp.get("regular_price") or {}).get("value")) or None
        in_label = self._opt("if_in_stock", it.get("if_in_stock"))
        out_label = self._opt("if_out_stock", it.get("if_out_stock"))
        salable = {"IN_STOCK": True, "OUT_OF_STOCK": False}.get(it.get("stock_status"))
        in_stock, note = _stock(in_label, out_label, salable)
        brand = self._opt("brand", it.get("brand"))
        attrs = {}
        cond = self._opt("condition", it.get("condition"))
        if cond and cond.lower() != "new":
            attrs["condition"] = cond
        promo = self._opt("promotion_type", it.get("promotion_type"))
        if promo:
            attrs["promotion"] = promo
        rec = {
            "store": STORE,
            "id": str(it["id"]),
            "sku": it.get("sku") or None,
            "title": _collapse(it.get("name")),
            "url": self._gql_url(it),
            "brand": brand if brand and brand.upper() != "N/A" else None,
            "price_mkd": price,
            "regular_price_mkd": reg if (reg and price and reg > price) else None,
            "in_stock": in_stock,
            "stock_note": note,
            "category": self._category_path(it.get("categories") or [], prefer_path),
            "ean": _gtin(it.get("part_number")),
            "mpn": _mpn(it.get("part_number")),
            "delivery_estimate": DELIVERY.get((note or "").lower()),
            "shipping_mkd": _shipping(price),
        }
        if attrs:
            rec["attributes"] = attrs
        if price is None:
            rec["stock_note"] = "; ".join(x for x in (note, "price on request") if x)
        return rec

    # -------------------------------------------------------------- categories
    def nodes(self):
        """Flat category list from one categoryList call (levels 2-6; the tree uses 2-4)."""
        if self._nodes is None:
            f = "id name url_path product_count level include_in_menu position"
            q = "{ categoryList(filters:{ids:{eq:\"%d\"}}) { %s %s } }"
            inner = f
            for _ in range(3):        # root children + 4 levels (the tree is 3 deep below them)
                inner = f"{f} children {{ {inner} }}"
            d = self.gql(q % (ROOT_CATEGORY_ID, f, f"children {{ {inner} }}"))
            roots = d.get("categoryList") or []
            if not roots:
                raise BackendUnavailable("categoryList returned no root category")
            out = []

            def walk(n, names, parent):
                chain = names + [_collapse(n["name"])]
                out.append({
                    "id": str(n["id"]), "slug": n.get("url_path"), "name": _collapse(n["name"]),
                    "path": " > ".join(chain),
                    "url": f"{BASE}{LOCALE}/{n['url_path']}.html" if n.get("url_path") else None,
                    "parent": parent, "count": n.get("product_count"), "level": n.get("level"),
                    "in_menu": bool(n.get("include_in_menu")),
                })
                for c in sorted(n.get("children") or [], key=lambda c: c.get("position") or 0):
                    walk(c, chain, str(n["id"]))

            for c in sorted(roots[0].get("children") or [], key=lambda c: c.get("position") or 0):
                walk(c, [], None)
            self._nodes = out
        return self._nodes

    def html_nodes(self):
        """Degraded category list from the HTML mega-menu (no counts, ~75% of the tree)."""
        r = self.html_get(f"{BASE}{LOCALE}/")
        soup = BeautifulSoup(r.text, "html.parser")
        found = {}                  # url_path -> (name, count)
        for a in soup.select("a[href]"):
            m = re.match(r"https://ddstore\.mk/mk/([A-Za-z0-9_/-]+)\.html$", a["href"])
            if not m or not a.find_parent(class_=re.compile("sm_megamenu")):
                continue
            text = _collapse(a.get_text(" "))
            mc = re.fullmatch(r"(.*?)\s*\((\d+)\)", text)       # menu labels read "Телевизори (65)"
            name, count = (mc.group(1), int(mc.group(2))) if mc else (text, None)
            if name and (m.group(1) not in found or found[m.group(1)][1] is None):
                found[m.group(1)] = (name, count)
        out = []
        for path in sorted(found, key=lambda p: (p.count("/"), p)):
            parent = path.rsplit("/", 1)[0] if "/" in path else None
            pnode = next((n for n in out if n["slug"] == parent), None)
            name, count = found[path]
            out.append({"id": path, "slug": path, "name": name,
                        "path": (pnode["path"] + " > " if pnode else "") + name,
                        "url": f"{BASE}{LOCALE}/{path}.html", "parent": pnode["id"] if pnode else None,
                        "count": count, "level": path.count("/") + 2, "in_menu": True})
        self.warn(f"HTML fallback: {len(out)} categories from the site menu (may be incomplete; "
                  f"ids are url_paths)")
        if not out:
            raise StoreError("HTML fallback: no categories found in the site menu (markup changed?)")
        return out

    def categories(self, grep=None):
        nodes = self._run(self.nodes, self.html_nodes)
        if not grep:
            return nodes
        rx = re.compile(grep, re.I)
        rx_lat = re.compile(translit(grep), re.I)
        out = []
        for n in nodes:            # field by field, so ^...$ anchors work on one field
            fields = [x for x in (n["name"], n["path"], n["slug"]) if x]
            fields += [translit(x) for x in fields[:2]]
            if any(rx.search(x) or rx_lat.search(x) for x in fields):
                out.append(n)
        return out

    @staticmethod
    def _split_category_arg(arg):
        """-> (kind, value, params); kind in {'id','path','name'}; params = layered-nav pairs."""
        arg = arg.strip()
        params = []
        if arg.startswith("http") or arg.startswith("/"):
            u = urlparse(arg if arg.startswith("http") else BASE + arg)
            params = [(k, v) for k, v in parse_qsl(u.query) if k not in ("p", "q")]
            m = re.search(r"/catalog/category/view/(?:.*/)?id/(\d+)", u.path)
            if m:
                return "id", m.group(1), params
            path = re.sub(r"^/(mk|en|al)(/|$)", "/", u.path).strip("/")
            return "path", re.sub(r"\.html$", "", path), params
        if arg.isdigit():
            return "id", arg, params
        if re.fullmatch(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*(\.html)?", arg):
            return "path", re.sub(r"\.html$", "", arg.strip("/")), params
        return "name", arg, params

    def resolve_category(self, arg):
        """-> (node, params). Raises NotFound / Usage."""
        kind, value, params = self._split_category_arg(arg)
        nodes = self.nodes()
        if kind == "id":
            hit = [n for n in nodes if n["id"] == value]
        elif kind == "path":
            hit = [n for n in nodes if (n["slug"] or "").lower() == value.lower()]
            if not hit:   # a bare Latin word may also be a name
                hit = [n for n in nodes if n["name"].lower() == value.lower()]
        else:
            hit = [n for n in nodes if n["name"].lower() == value.lower()] or \
                  [n for n in nodes if n["path"].lower() == value.lower()]
        if not hit:
            raise NotFound(f"unknown category {arg!r} (not an id, url_path, URL or exact name in the "
                           f"{len(nodes)}-node tree; try `categories --grep`)")
        if len(hit) > 1:
            raise Usage(f"category name {arg!r} is ambiguous: " +
                        "; ".join(f"{n['id']} = {n['path']}" for n in hit))
        return hit[0], params

    # ------------------------------------------------------------------ html
    def html_get(self, url, allow_404=False):
        r = self._request("GET", url)
        if r.status_code == 404:
            if allow_404:
                return None
            raise NotFound(f"{url}: HTTP 404")
        if r.status_code != 200:
            raise StoreError(f"{url}: HTTP {r.status_code}")
        return r

    @staticmethod
    def parse_listing(text):
        soup = BeautifulSoup(text, "html.parser")
        total = None
        amt = soup.select_one(".toolbar-amount")
        if amt:
            t = amt.get_text(" ", strip=True)
            m = re.search(r"од\s+([\d.]+)", t) or re.search(r"([\d.]+)\s+продукт", t, re.I)
            if m:
                total = int(m.group(1).replace(".", ""))
        cards = []
        for li in soup.select("li.product-item"):
            if not li.select_one(".product-item-info"):
                continue          # knockout templates (compare sidebar etc.)
            a = li.select_one("a.product-item-link")
            if not a or not a.get("href"):
                continue
            pb = li.select_one("[data-role=priceBox]")
            form = li.select_one("form[data-role=tocart-form]")
            inp = form.select_one("input[name=product]") if form else None
            pid = (pb.get("data-product-id") if pb else None) or (inp["value"] if inp else None)
            if not pid:
                # "Прашај нѐ за цена" cards have no price box and no cart form; the entity id is
                # still in the compare/wishlist data-post and the image container class.
                for el in li.select("a.tocompare[data-post], a.towishlist[data-post]"):
                    m = re.search(r'"product"\s*:\s*"?(\d+)', el.get("data-post") or "")
                    if m:
                        pid = m.group(1)
                        break
            if not pid:
                img = li.select_one("[class*=product-image-container-]")
                m = re.search(r"product-image-container-(\d+)", " ".join(img.get("class") or [])) if img else None
                pid = m.group(1) if m else None
            fin = li.select_one("[data-price-type=finalPrice]")
            old = li.select_one("[data-price-type=oldPrice]")
            sku_el = li.select_one(".mpi-sku[data-sku]")
            st = li.select_one(".product-info-stock-label")
            cat = li.select_one(".category_in_listing a")
            cards.append({
                "id": pid,
                "sku": (sku_el.get("data-sku") if sku_el else None) or (form.get("data-product-sku") if form else None),
                "title": _collapse(a.get_text()),
                "url": _clean_url(a["href"]),
                "final": fin.get("data-price-amount") if fin else None,
                "old": old.get("data-price-amount") if old else None,
                "stock_label": _collapse(st.get_text(" ")) if st else "",
                "salable": bool(form),
                "ask_price": bool(li.select_one(".ask-for-price")),
                "category": _collapse(cat.get_text()) if cat else None,
            })
        return soup, cards, total

    def html_card_record(self, c):
        price = _mkd(c["final"]) or None
        reg = _mkd(c["old"]) or None
        label = c["stock_label"]
        if label.lower() in OUT_LABELS:
            in_stock, note = _stock("", label, None if c["salable"] else False)
        else:
            in_stock, note = _stock(label, "", True if c["salable"] else None)
        if price is None:           # "Прашај нѐ за цена" (same wording as the GraphQL backend)
            note = "; ".join(x for x in (note, "price on request") if x)
        return {
            "store": STORE, "id": c["id"], "sku": c["sku"], "title": c["title"], "url": c["url"],
            "brand": None,          # not on listing cards (only on product pages)
            "price_mkd": price,
            "regular_price_mkd": reg if (reg and price and reg > price) else None,
            "in_stock": in_stock, "stock_note": note, "category": c["category"],
            "ean": None,            # not on listing cards
            "delivery_estimate": DELIVERY.get(label.lower()) if label else None,
            "shipping_mkd": _shipping(price),
        }

    def html_walk(self, url, params=None, limit=None):
        """Walk ?p=1..N of a category or search page. Returns (cards, total, pages, first_soup)."""
        params = [(k, v) for k, v in (params or []) if k != "p"]
        cards, seen, page, total, first = [], set(), 1, None, None
        while True:
            qs = urlencode(params + [("p", page)])
            r = self.html_get(f"{url}?{qs}")
            soup, page_cards, t = self.parse_listing(r.text)
            if page == 1:
                total, first = t, soup
            new = []
            for c in page_cards:
                key = c["id"] or c["url"]       # never dedupe on a missing id
                if key not in seen:
                    seen.add(key)
                    new.append(c)
            cards += new
            if limit and len(cards) >= limit:
                return cards[:limit], total, page, first
            if not new or total is None or len(cards) >= total:
                break
            page += 1
        if total is not None and len(cards) != total and not limit:
            self.warn(f"collected {len(cards)} unique products but the page says {total}")
        return cards, total, page, first

    @staticmethod
    def parse_offers(soup):
        """Magnetix offer list on a product page -> list of offers (empty when single-offer)."""
        out = []
        box = soup.select_one("#magnetix-offers")
        if not box:
            return out
        winner = box.get("data-winner")
        for li in box.select(".magnetix-offers__item"):
            cls = li.get("class") or []
            amount = li.get("data-amount")
            price = _mkd(amount) if amount not in (None, "", "0") else None
            stock = _collapse(htmlmod.unescape(li.get("data-stock") or ""))
            note = OFFER_STOCK.get(stock.lower(), CANON_LABEL.get(stock.lower(), stock)) or None
            lab = (note or "").lower()
            available = "is-unavailable" not in cls
            out.append({
                "id": li.get("data-product-id"),
                "winner": li.get("data-product-id") == winner or "is-winner" in cls,
                "price_mkd": price or None,
                "regular_price_mkd": _mkd(li.get("data-regular-amount")) or None,
                "warranty": _warranty_text(li.get("data-warranty")),
                "in_stock": (False if not available or lab in OUT_LABELS else True if lab in IN_STOCK_LABELS
                             else None),
                "stock_note": note,
                "available": available,
            })
        return out

    def html_detail(self, url):
        r = self.html_get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.select_one(".product-info-main")
        if main is None:
            raise NotFound(f"{url}: no product block (not a product page?)")
        ld = {}
        for sc in soup.select('script[type="application/ld+json"]'):
            try:
                d = json.loads(sc.get_text())
            except ValueError:
                continue
            for x in (d if isinstance(d, list) else [d]):
                if isinstance(x, dict) and x.get("@type") == "Product":
                    ld = x
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        table = {}
        for tr in soup.select("#product-attribute-specs-table tr"):
            th, td = tr.select_one("th"), tr.select_one("td")
            if th and td:
                table[_collapse(th.get_text())] = _collapse(td.get_text(" "))
        pid_el = main.select_one("input[name=product]") or main.select_one("[data-product-id]")
        pid = (pid_el.get("value") or pid_el.get("data-product-id")) if pid_el else None
        fin = main.select_one(".product-info-price [data-price-type=finalPrice]")
        old = main.select_one(".product-info-price [data-price-type=oldPrice]")
        price = _mkd(offers.get("price")) or _mkd(fin.get("data-price-amount") if fin else None) or None
        reg = _mkd(old.get("data-price-amount")) if old else None
        st = main.select_one(".product-info-stock-label")
        label = _collapse(st.get_text(" ")) if st else ""
        avail = (offers.get("availability") or "").rsplit("/", 1)[-1]
        salable = {"InStock": True, "OutOfStock": False}.get(avail)
        if label.lower() in OUT_LABELS:
            in_stock, note = _stock("", label, salable)
        else:
            in_stock, note = _stock(label, "", salable)
        brand = (ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else ld.get("brand")
        brand = brand or table.get("Бренд")
        w = table.get("Warranty") or table.get("Гаранција")
        desc = soup.select_one("#description")
        attrs = {k: v for k, v in table.items() if v and v.upper() != "N/A"}
        spec_rows = "; ".join(f"{k}: {v}" for k, v in attrs.items())
        specs = _collapse(" | ".join(x for x in (spec_rows, desc.get_text(" ") if desc else "") if x))
        can = soup.select_one("link[rel=canonical]")
        pn = table.get("Part Number")
        sku = ld.get("sku")
        sku_el = main.select_one(".product-info-sku")
        if not sku and sku_el:
            m = re.search(r"КОД:\s*(\S+)", sku_el.get_text())
            sku = m.group(1) if m else None
        offer_list = self.parse_offers(soup)
        rec = {
            "store": STORE,
            "id": pid,
            "sku": sku,
            "title": _collapse(ld.get("name") or (main.select_one(".page-title") or main).get_text()),
            "url": (can.get("href") if can else None) or _clean_url(url),
            "brand": _collapse(brand) or None,
            "price_mkd": price,
            "regular_price_mkd": reg if (reg and price and reg > price) else None,
            "in_stock": in_stock,
            "stock_note": note,
            "category": table.get("Категорија"),
            "ean": _gtin(pn),
            "mpn": _mpn(pn),
            "delivery_estimate": DELIVERY.get((note or "").lower()),
            "shipping_mkd": _shipping(price),
            "warranty": _warranty_text(w),
            "specs": specs,
            "per_location_stock": None,
            "attributes": attrs or None,
            "extra": {"part_number": pn, "offers": offer_list or None},
        }
        if price is None:           # "Прашај нѐ за цена" (same wording as the GraphQL backend)
            rec["stock_note"] = "; ".join(x for x in (note, "price on request") if x)
        self._annotate_offers(rec)
        return rec

    def _annotate_offers(self, rec):
        offers = (rec.get("extra") or {}).get("offers") or []
        if len(offers) < 2:
            return
        alt = [o for o in offers if not o.get("winner") and o.get("available")
               and o.get("in_stock", (o.get("stock_note") or "").lower() in IN_STOCK_LABELS)]
        msg = f"{len(offers)} offers"
        if rec.get("in_stock") is not True and alt:
            best = min(alt, key=lambda o: o.get("price_mkd") or 10 ** 9)
            msg += (f"; another offer is in stock: {best.get('stock_note')} at "
                    f"{best.get('price_mkd')} MKD (see extra.offers)")
        rec["stock_note"] = "; ".join(x for x in (rec.get("stock_note"), msg) if x)

    # -------------------------------------------------------------- dispatch
    def _run(self, gql_fn, html_fn):
        if self.backend == "html":
            return html_fn()
        try:
            return gql_fn()
        except BackendUnavailable as e:
            if self.backend == "graphql":
                raise StoreError(str(e)) from e
            self.warn(f"GraphQL unavailable ({e}); falling back to HTML pages")
            return html_fn()

    # ---------------------------------------------------------------- search
    def search(self, query, limit=None, in_stock=False, category=None):
        query = _collapse(query)
        if not query:
            raise Usage("empty search query")

        def g():
            self.options()
            flt = {}
            if category:
                node, _ = self.resolve_category(category)
                flt["category_id"] = {"eq": node["id"]}
            if in_stock:
                flt["if_in_stock"] = {"in": self.stock_option_ids()}
            items, meta = self.gql_products(flt or None, search=query, limit=limit)
            recs = [self.gql_record(i) for i in items]
            self.log(f"search {query!r}{' in category ' + category if category else ''}"
                     f"{' (in stock)' if in_stock else ''}: {meta['total_count']} hits, returning "
                     f"{len(recs)} ({meta['pages_fetched']} page(s)). GraphQL full-text: words ANDed "
                     f"with fuzzy/transliterated matching over title+description, relevance order; "
                     f"broader than the site's search box")
            if not meta["total_count"]:
                self._zero_hit_hint(query)
            return recs

        def h():
            cards, total, pages, _ = self.html_walk(f"{BASE}{LOCALE}/catalogsearch/result/",
                                                   [("q", query)], limit)
            self.log(f"search[html] {query!r}: {total} hits, returning {len(cards)} ({pages} page(s)); "
                     f"site search box semantics (strict AND)")
            if category:
                self.warn("--category is ignored by the HTML search fallback")
            recs = [self.html_card_record(c) for c in cards]
            if not cards:
                self._zero_hit_hint(query)
            return recs

        recs = self._run(g, h)
        if in_stock:
            recs = [r for r in recs if r["in_stock"] is True]
        return recs

    def _zero_hit_hint(self, query):
        if re.fullmatch(r"\d{8,14}", query) or re.fullmatch(r"[A-Za-z0-9]+[-/][A-Za-z0-9/-]+", query):
            self.log(f"0 hits for {query!r}: DDStore search does not index EAN / Part Number "
                     f"(model codes match only when they are in the title); the 'КОД' (sku) is "
                     f"indexed. Match EANs/MPNs via listings (`ean`/`mpn` fields) instead")
        elif len(query.split()) > 1:
            self.log(f"0 hits for {query!r}: every word must match; try fewer or more general words")
        else:
            self.log(f"0 hits for {query!r}")

    # ------------------------------------------------------------ filtering
    def _parse_tokens(self, tokens, params, node):
        """Filter tokens + URL params -> (graphql filter dict, gross price range, html params)."""
        flt, gross, html_params, seen = {}, None, [], {}
        pairs = list(params)
        for tok in tokens or []:
            if "=" not in tok:
                raise Usage(f"bad filter token {tok!r}: expected CODE=VALUE (see `facets`)")
            k, v = tok.split("=", 1)
            pairs.append((k.strip(), v.strip()))
        need_labels = []
        for k, v in pairs:
            if k.startswith("product_list_") or k in ("shopbyAjax", "_", "isAjax", "p", "q") or not v:
                continue
            if not re.fullmatch(r"[a-z0-9_]+", k):
                raise Usage(f"bad filter code {k!r}")
            if k in seen:
                raise Usage(f"filter {k!r} given twice; put alternatives in one token "
                            f"({k}={seen[k]},{v}) to OR them")
            seen[k] = v
            if k in ("price", "price_mkd"):
                m = re.fullmatch(r"\s*([\d.]*)\s*[-_]\s*([\d.*]*)\s*", v)
                if not m or not (m.group(1) or m.group(2).strip("*")):
                    raise Usage(f"bad {k} range {v!r}: expected FROM-TO, FROM- or -TO")
                lo = float(m.group(1)) if m.group(1) else None
                hi = float(m.group(2)) if m.group(2).strip("*") else None
                if k == "price":
                    rng = {}
                    if lo is not None:
                        rng["from"] = str(int(lo))
                    if hi is not None:
                        rng["to"] = str(int(hi))
                    flt["price"] = rng
                    html_params.append(("price", f"{int(lo or 0)}-{int(hi) if hi is not None else ''}"))
                else:
                    gross = (lo, hi)
                continue
            vals = [x.strip() for x in v.split(",") if x.strip()]
            if not all(x.isdigit() for x in vals):
                need_labels.append(k)
            flt[k] = vals
            html_params.append((k, ",".join(vals)))
        if gross:
            # server-side narrowing: net = gross / (1 + VAT), VAT is 5% (IT) or 18% (TV/appliances)
            lo, hi = gross
            rng = dict(flt.get("price") or {})
            if lo is not None:
                rng["from"] = str(max(int(rng.get("from", 0)), int(math.floor(lo / 1.18))))
            if hi is not None:
                cap = int(math.ceil(hi))
                rng["to"] = str(min(int(rng["to"]), cap) if "to" in rng else cap)
            flt["price"] = rng
        if need_labels:
            if node is None:
                raise Usage(f"option labels in filters ({', '.join(need_labels)}) need GraphQL; "
                            f"the HTML fallback accepts option ids only (see `facets`)")
            labels = self._facet_label_map(node)
            for k in need_labels:
                ids = []
                for x in flt[k]:
                    if x.isdigit():
                        ids.append(x)
                        continue
                    hit = labels.get(k, {}).get(x.lower())
                    if not hit:
                        avail = ", ".join(sorted({lab for lab in labels.get(k, {})}))[:400]
                        raise Usage(f"no {k} option labelled {x!r} in this category"
                                    + (f" (available: {avail})" if avail else
                                       f" (attribute {k!r} has no facet here; see `facets`)"))
                    ids += hit
                flt[k] = ids
                html_params = [(a, b) if a != k else (a, ",".join(ids)) for a, b in html_params]
        for k, vals in list(flt.items()):
            if k != "price":
                flt[k] = {"in": vals} if len(vals) > 1 else {"eq": vals[0]}
        return flt, gross, html_params

    def _facet_label_map(self, node):
        """{code: {label.lower(): [option ids]}} from the category's aggregations (1 request)."""
        q = ("query($f: ProductAttributeFilterInput) { products(filter: $f, pageSize: 1) {"
             " aggregations { attribute_code options { label value } } } }")
        d = self.gql(q, {"f": {"category_id": {"eq": node["id"]}}})
        out = {}
        for a in d["products"].get("aggregations") or []:
            for o in a.get("options") or []:
                out.setdefault(a["attribute_code"], {}).setdefault(
                    _collapse(o["label"]).lower(), []).append(str(o["value"]))
        return out

    # ------------------------------------------------------------------ list
    def list_category(self, arg, in_stock=False, limit=None, filters=None):
        def g():
            self.options()
            node, params = self.resolve_category(arg)
            flt, gross, _ = self._parse_tokens(filters, params, node)
            user_stock_filter = "if_in_stock" in flt
            flt["category_id"] = {"eq": node["id"]}
            if in_stock and not user_stock_filter:
                flt["if_in_stock"] = {"in": self.stock_option_ids()}
            fetch_limit = None if gross else limit
            try:
                items, meta = self.gql_products(flt, limit=fetch_limit)
            except GraphQLQueryError as e:
                m = re.search(r'Field "([a-z0-9_]+)" is not defined by type "ProductAttributeFilterInput"', str(e))
                if m:
                    raise Usage(f"{m.group(1)!r} is not a filterable attribute on DDStore (see `facets`)")
                raise
            recs = [self.gql_record(i, prefer_path=node["slug"]) for i in items]
            if gross:
                lo, hi = gross
                recs = [r for r in recs if r["price_mkd"] is not None
                        and (lo is None or r["price_mkd"] >= lo) and (hi is None or r["price_mkd"] <= hi)]
                if limit:
                    recs = recs[:limit]
            extra = {k: v for k, v in flt.items() if k != "category_id"}
            self.log(f"category {node['id']} {node['path']!r}{' filters=' + json.dumps(extra, ensure_ascii=False) if extra else ''}: "
                     f"{meta['total_count']} matching (category total {node['count']}), fetched "
                     f"{len(items)} over {meta['pages_fetched']} GraphQL page(s) of {PAGE_SIZE}"
                     f"{f', {len(recs)} within price_mkd range' if gross else ''}")
            if items and not recs:
                self.warn(f"{len(items)} products pass the server-side filters but none is within "
                          f"price_mkd={'' if gross[0] is None else int(gross[0])}-"
                          f"{'' if gross[1] is None else int(gross[1])} (genuine zero)")
            if not items:
                if extra:
                    # Filters / --in-stock matched nothing: a genuine zero (exit 0) when the
                    # category itself still answers with products, else empty / soft block (exit 2).
                    q = ("query($f: ProductAttributeFilterInput) { products(filter: $f, pageSize: 1)"
                         " { total_count } }")
                    n_all = self.gql(q, {"f": {"category_id": {"eq": node["id"]}}})["products"]["total_count"]
                    if n_all:
                        self.warn(f"0 of {n_all} products in {node['path']!r} match "
                                  f"{'--in-stock and ' if in_stock and not user_stock_filter else ''}"
                                  f"the filters (genuine zero)")
                        return []
                if not node["count"]:
                    raise NotFound(f"category {node['id']} {node['path']!r} is empty (0 products)")
                raise NotFound(f"category {node['id']} {node['path']!r} returned 0 products although "
                               f"the tree says {node['count']} (soft block or schema change?)")
            return recs

        def h():
            kind, value, params = self._split_category_arg(arg)
            if kind == "id":
                url = f"{BASE}{LOCALE}/catalog/category/view/id/{value}/"
            elif kind == "path":
                url = f"{BASE}{LOCALE}/{value}.html"
            else:
                raise Usage("the HTML fallback needs a category id, url_path or URL, not a name")
            _, gross, hparams = self._parse_tokens(filters, params, None)
            cards, total, pages, _ = self.html_walk(url, hparams, None if gross else limit)
            self.log(f"category[html] {url} {hparams or ''}: page says {total}, fetched {len(cards)} "
                     f"over {pages} HTML page(s) of 24")
            recs = [self.html_card_record(c) for c in cards]
            if gross:
                lo, hi = gross
                recs = [r for r in recs if r["price_mkd"] is not None
                        and (lo is None or r["price_mkd"] >= lo) and (hi is None or r["price_mkd"] <= hi)]
                recs = recs[:limit] if limit else recs
            if not cards:
                if hparams:
                    _, _, n_all = self.parse_listing(self.html_get(url).text)
                    if n_all:
                        self.warn(f"0 of {n_all} products at {url} match the filters (genuine zero)")
                        return []
                raise NotFound(f"0 products at {url} (unknown or empty category, or markup changed)")
            return recs

        recs = self._run(g, h)
        if in_stock:
            recs = [r for r in recs if r["in_stock"] is True]
        if limit:
            recs = recs[:limit]
        return recs

    # ---------------------------------------------------------------- facets
    def facets(self, arg):
        def g():
            node, params = self.resolve_category(arg)
            flt, _, _ = self._parse_tokens([], params, node)
            flt["category_id"] = {"eq": node["id"]}
            q = ("query($f: ProductAttributeFilterInput) { products(filter: $f, pageSize: 1) {"
                 " total_count aggregations { attribute_code label position options { label value count } } } }")
            p = self.gql(q, {"f": flt})["products"]
            if not p.get("total_count"):
                if not node["count"]:
                    raise NotFound(f"category {node['id']} {node['path']!r} is empty (0 products), "
                                   f"so it has no facets")
                if not params:
                    raise NotFound(f"category {node['id']} {node['path']!r} returned 0 products although "
                                   f"the tree says {node['count']} (soft block or schema change?)")
            out = []
            for a in sorted(p.get("aggregations") or [], key=lambda a: (a.get("position") is None,
                                                                         a.get("position") or 0)):
                code = a["attribute_code"]
                if code in FACET_SKIP:
                    continue
                name = _collapse(a.get("label")) or code
                if code == "price":
                    name = "Цена (без ДДВ / net of VAT)"
                for o in a.get("options") or []:
                    label = _collapse(o.get("label"))
                    if code == "price":
                        lo, _, hi = str(o["value"]).partition("_")
                        hi = "" if hi in ("*", "") else hi
                        token = f"price={lo}-{hi}"
                    else:
                        token = f"{code}={o['value']}"
                    if not label or str(o["value"]) in ("0", ""):
                        continue          # "no value" buckets (e.g. if_out_stock 0)
                    out.append({"name": name, "value": label, "count": o.get("count"),
                                "token": token, "code": code})
            self.log(f"facets for {node['id']} {node['path']!r}: {p['total_count']} products, "
                     f"{len({r['code'] for r in out})} attributes, {len(out)} values. Values OR inside "
                     f"one token (brand=1,2), --filter flags AND; add price_mkd=LO-HI for the real price")
            if not out:
                self.warn("no facets returned for this category")
            return out

        def h():
            kind, value, params = self._split_category_arg(arg)
            url = (f"{BASE}{LOCALE}/catalog/category/view/id/{value}/" if kind == "id"
                   else f"{BASE}{LOCALE}/{value}.html")
            r = self.html_get(url + (("?" + urlencode(params)) if params else ""))
            soup = BeautifulSoup(r.text, "html.parser")
            out = []
            for item in soup.select(".filter-options-item"):
                t = item.select_one(".filter-options-title")
                name = _collapse(t.get_text()) if t else "?"
                for a in item.select("li.item a[href]"):
                    qs = parse_qsl(urlparse(a["href"]).query)
                    new = [(k, v) for k, v in qs if (k, v) not in params and k != "p"]
                    if not new:
                        continue
                    k, v = new[0]
                    lab = a.select_one(".label")
                    cnt = a.select_one(".count")
                    m = re.match(r"\s*(\d+)", cnt.get_text()) if cnt else None
                    out.append({"name": name, "value": _collapse((lab or a).get_text(" ")),
                                "count": int(m.group(1)) if m else None, "token": f"{k}={v}", "code": k})
            self.log(f"facets[html] {url}: {len(out)} values (price slider not included)")
            if not out:
                raise NotFound(f"no layered-navigation filters found at {url}")
            return out

        return self._run(g, h)

    # --------------------------------------------------------------- detail
    @staticmethod
    def _detail_key(arg):
        """-> ('path', 'url-key.html' | 'pid/SKU' | 'catalog/product/view/id/N') | ('sku', code)
        | ('num', digits) (SKU first, then entity id)."""
        a = arg.strip()
        if a.startswith("http") or a.startswith("/"):
            u = urlparse(a if a.startswith("http") else BASE + a)
            if u.netloc and "ddstore.mk" not in u.netloc:
                raise Usage(f"{arg!r} is not a ddstore.mk URL")
            path = re.sub(r"^/(mk|en|al)(/|$)", "/", u.path).strip("/")
            m = re.search(r"catalog/product/view/(?:.*/)?id/(\d+)", path)
            if m:
                return "path", f"catalog/product/view/id/{m.group(1)}"
            if not path:
                raise Usage(f"{arg!r} is not a product URL")
            return "path", path
        if re.fullmatch(r"id:\d+", a):
            return "path", f"catalog/product/view/id/{a[3:]}"
        if re.fullmatch(r"sku:\S+", a):
            return "sku", a[4:]
        if a.startswith("pid/") or a.endswith(".html"):
            return "path", a
        if a.isdigit():
            return "num", a
        return "sku", a

    def gql_route_product(self, path):
        q = ("query($u:String!){ route(url:$u){ __typename ... on ProductInterface { %s } } }"
             % DETAIL_FIELDS)
        r = self.gql(q, {"u": path}).get("route")
        return r if r and r.get("sku") else None

    def detail(self, args, offers=True):
        """-> (records, exit_code). One record per input, in input order; failures become
        {"input", "error"} rows. A block aborts the rest."""
        keys = []
        for a in args:
            try:
                keys.append(self._detail_key(a))
            except Usage as e:
                keys.append(("bad", str(e)))
        out, code = [None] * len(args), 0

        def fail(i, msg, c):
            nonlocal code
            self.log(f"detail {args[i]!r}: {msg}")
            out[i] = {"input": args[i], "error": msg}
            code = 1 if (c == 1 or code == 1) else c

        def g():
            self.options()
            found = {}
            skus = sorted({v for k, v in keys if k in ("sku", "num")})
            if skus:
                # NB: sku filter is exact; url_key filter is IGNORED by this store (returns the
                # whole catalogue), so URLs go through route() instead.
                items, _ = self.gql_products({"sku": {"in": skus}}, fields=DETAIL_FIELDS, limit=len(skus))
                found = {it.get("sku"): it for it in items}
            for i, ((kind, val), raw) in enumerate(zip(keys, args)):
                if kind == "bad":
                    fail(i, f"bad input: {val}", 2)
                    continue
                try:
                    it = found.get(val) if kind in ("sku", "num") else None
                    if it is None:
                        if kind == "num":
                            path = f"catalog/product/view/id/{val}"
                        elif kind == "sku":
                            path = f"pid/{val}"
                        else:
                            path = val
                        it = self.gql_route_product(path)
                    if it is None:
                        fail(i, "not found (no КОД/sku, URL or entity-id match)", 2)
                        continue
                    rec = self.gql_detail_record(it)
                    if offers:
                        self._add_offers(rec)
                    out[i] = rec
                except Blocked:
                    raise
                except NotFound as e:
                    fail(i, f"not found: {e}", 2)
                except BackendUnavailable:
                    raise
                except (StoreError, requests.RequestException, ValueError, KeyError) as e:
                    fail(i, f"{type(e).__name__}: {e}", 1)
            return out

        def h():
            for i, ((kind, val), raw) in enumerate(zip(keys, args)):
                if kind == "bad":
                    fail(i, f"bad input: {val}", 2)
                    continue
                try:
                    if kind == "path":
                        out[i] = self.html_detail(f"{BASE}{LOCALE}/{val}")
                        continue
                    # bare value: try it as a SKU ("КОД"), then as the entity id (the records' `id`)
                    try:
                        out[i] = self.html_detail(f"{BASE}{LOCALE}/pid/{val}")
                    except NotFound:
                        if kind != "num":
                            raise
                        out[i] = self.html_detail(f"{BASE}{LOCALE}/catalog/product/view/id/{val}/")
                except Blocked:
                    raise
                except NotFound as e:
                    fail(i, f"not found: {e}", 2)
                except (StoreError, requests.RequestException, ValueError, KeyError) as e:
                    fail(i, f"{type(e).__name__}: {e}", 1)
            return out

        try:
            recs = self._run(g, h)
        except Blocked as e:
            done = [r if r is not None else {"input": a, "error": f"blocked: {e}"}
                    for r, a in zip(out, args)]
            raise _BatchBlocked(done, e)
        return recs, code

    def gql_detail_record(self, it):
        rec = self.gql_record(it)
        attrs = []
        for a in (it.get("custom_attributesV2") or {}).get("items") or []:
            if a["code"] in SPEC_SKIP:
                continue
            if "selected_options" in a:
                val = ", ".join(_collapse(o["label"]) for o in a["selected_options"] if _collapse(o["label"]))
            else:
                val = _collapse(str(a.get("value") or ""))
            attrs.append((a["code"], val))
        labels = self.attr_labels([c for c, _ in attrs])
        amap = dict(attrs)
        shown = {labels[c]: v for c, v in attrs if v and v.upper() != "N/A"}
        spec_rows = "; ".join(f"{k}: {v}" for k, v in shown.items())
        desc = _strip_html((it.get("description") or {}).get("html"))
        w = amap.get("warranty") or self._opt("warranty", it.get("warranty"))
        rec.update({
            "warranty": _warranty_text(w),
            "specs": _collapse(" | ".join(x for x in (spec_rows, desc) if x)),
            "per_location_stock": None,   # not exposed; "Во ДДСтор магацин" is the only location hint
            "attributes": shown or None,
            "extra": {"part_number": it.get("part_number"),
                      "warranty_days": int(w) if w and w.isdigit() and w not in ("9999", "999") else None,
                      "offers": None},
        })
        return rec

    def _add_offers(self, rec):
        """Read the product page's Magnetix offer list (GraphQL does not expose it); enrich each
        offer from GraphQL (one aliased request per 10 offers)."""
        try:
            r = self.html_get(rec["url"])
        except Blocked:
            raise
        except (StoreError, requests.RequestException) as e:
            self.warn(f"product page {rec['url']} unavailable ({e}); offers unknown")
            return
        offers = self.parse_offers(BeautifulSoup(r.text, "html.parser"))
        if len(offers) < 2:
            rec["extra"]["offers"] = None
            return
        ids = [o["id"] for o in offers if o.get("id")]
        info = {}
        for start in range(0, len(ids), MAX_ALIASES):
            chunk = ids[start:start + MAX_ALIASES]
            q = "{ " + " ".join(
                f'o{n}: route(url:"catalog/product/view/id/{pid}"){{ __typename ... on ProductInterface {{ {OFFER_FIELDS} }} }}'
                for n, pid in enumerate(chunk) if pid.isdigit()) + " }"
            try:
                d = self.gql(q)
            except BackendUnavailable as e:
                self.warn(f"offer enrichment failed: {e}")
                break
            for v in d.values():
                if v and v.get("id"):
                    info[str(v["id"])] = v
        for o in offers:
            v = info.get(o["id"])
            if v:
                in_label = self._opt("if_in_stock", v.get("if_in_stock"))
                out_label = self._opt("if_out_stock", v.get("if_out_stock"))
                salable = {"IN_STOCK": True, "OUT_OF_STOCK": False}.get(v.get("stock_status"))
                o["in_stock"], o["stock_note"] = _stock(in_label, out_label, salable)
                o["sku"] = v.get("sku")
                o["title"] = _collapse(v.get("name"))
                o["ean"] = _gtin(v.get("part_number"))
                o["url"] = self._gql_url(v)
        rec["extra"]["offers"] = offers
        if not rec.get("ean"):
            eans = {o.get("ean") for o in offers if o.get("ean")}
            if len(eans) == 1:
                rec["ean"] = eans.pop()
                rec["extra"]["ean_from_offer"] = True
        self._annotate_offers(rec)


class _BatchBlocked(Exception):
    def __init__(self, recs, err):
        super().__init__(str(err))
        self.recs, self.err = recs, err


# --------------------------------------------------------------------------- CLI

def _print_products(recs):
    for r in recs:
        if "error" in r:
            print(f"      !  ERROR  {r['input']}: {r['error']}")
            continue
        price = f"{r['price_mkd']:>7,}" if r.get("price_mkd") is not None else "      ?"
        if r.get("regular_price_mkd"):
            price += f" (was {r['regular_price_mkd']:,})"
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        note = (r.get("stock_note") or "")[:18]
        title = (r.get("title") or "")[:90]
        print(f"{price}  {stock} {note:<18}  {title}  {r['url']}")


def _print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>6}" if r.get("count") is not None else "     -"
        hidden = "" if r.get("in_menu", True) else "  (not in site menu)"
        print(f"{r['id']:>5} {cnt}  {r['path']}  [{r['slug']}]{hidden}")


def _print_facets(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{cnt}  {r['name']}: {r['value']}   --filter '{r['token']}'")


def _emit(recs, path, printer=_print_products, what="products", quiet=False):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        print(len(recs))
    else:
        printer(recs)
        sys.stdout.flush()
        if not quiet:
            print(f"-- {len(recs)} {what}", file=sys.stderr)


def info():
    return {"store": STORE, "name": NAME, "base_url": BASE, "capabilities": CAPABILITIES,
            "sells": SELLS, "notes": NOTES}


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("--backend", choices=("auto", "graphql", "html"), default="auto",
                        help="auto = GraphQL, falling back to HTML pages if GraphQL is unavailable")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    ap = argparse.ArgumentParser(description="DDStore (ddstore.mk) catalogue client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--category", help="restrict to a category (id, url_path, URL or exact name)")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep", help="case-insensitive regex over name, path, slug and a Latin "
                                  "transliteration (monitor finds Монитори)")
    p = sub.add_parser("list", parents=[common])
    p.add_argument("category")
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", action="append", default=[], metavar="TOKEN",
                   help="facet token, e.g. brand=2215 or price_mkd=10000-20000 (repeatable, AND)")
    p = sub.add_parser("detail", parents=[common])
    p.add_argument("refs", nargs="+", metavar="url-or-id")
    p.add_argument("--no-offers", action="store_true",
                   help="skip the product page (Magnetix offer list): one request instead of 2-3")
    p = sub.add_parser("facets", parents=[common])
    p.add_argument("category")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0

    if a.cmd == "info":
        print(json.dumps(info(), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print(f"[{STORE}] ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    d = DDStore(backend=a.backend, verbose=a.verbose, quiet=a.quiet)
    code = 0
    try:
        if a.cmd == "search":
            _emit(d.search(a.query, limit=a.limit, in_stock=a.in_stock, category=a.category), a.json,
                  quiet=a.quiet)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = d.categories(grep=a.grep)
            if a.grep and not recs:
                d.log(f"no category matches {a.grep!r} (matched against name, path, slug and a "
                      f"Latin transliteration)")
            _emit(recs, a.json, _print_categories, "categories", quiet=a.quiet)
        elif a.cmd == "list":
            _emit(d.list_category(a.category, in_stock=a.in_stock, limit=a.limit, filters=a.filter),
                  a.json, quiet=a.quiet)
        elif a.cmd == "facets":
            _emit(d.facets(a.category), a.json, _print_facets, "facet values", quiet=a.quiet)
        elif a.cmd == "detail":
            try:
                recs, code = d.detail(a.refs, offers=not a.no_offers)
            except _BatchBlocked as b:
                _emit(b.recs, a.json, quiet=a.quiet)
                raise b.err
            _emit(recs, a.json, quiet=a.quiet)
        d.log(f"{d.requests_made} HTTP request(s)")
        return code
    except Blocked as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 3
    except (NotFound, Usage) as e:
        print(f"[{STORE}] ERROR: {e}", file=sys.stderr)
        return 2
    except (StoreError, requests.RequestException, ValueError, KeyError) as e:
        print(f"[{STORE}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
