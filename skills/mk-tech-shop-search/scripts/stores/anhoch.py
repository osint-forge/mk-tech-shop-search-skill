#!/usr/bin/env python3
"""Anhoch (www.anhoch.com) catalogue client. See references/client-contract.md.

Anhoch runs FleetCart (Laravel + Vue storefront, LiteSpeed origin behind Cloudflare).
The storefront's product grid calls a JSON endpoint, which this client uses directly:

    GET /products?categories[0]=<slug>&query=<q>&brand[0]=<brand id>&inStockOnly=1|2
                 &sort=<sort>&perPage=<n>&page=<p>
        header X-Requested-With: XMLHttpRequest   (without it you get the HTML shell)
        -> {"products": <Laravel paginator>, "brands": [...], "attributes": [...]}

The category tree (all nodes, no counts) is embedded in every category page as
:initial-categories. Product detail has no JSON route: /products/<slug> embeds the
full product model in <product-show :product="..."> (MPN, brand, warranty, description,
per-store stock). Numeric ids are resolved to slugs through the anonymous compare list.

    anhoch.py info
    anhoch.py search "<query>" [--limit N] [--in-stock] [--category REF] [--all-words] [--json PATH]
    anhoch.py categories [--grep REGEX] [--counts] [--json PATH]
    anhoch.py list <category> [--in-stock] [--limit N] [--filter brand=<slug>[,<slug>]] [--json PATH]
    anhoch.py detail <url|slug|id> [...] [--json PATH]
    anhoch.py facets <category> [--json PATH]
    common options: --quiet, --no-brands (skip brand enrichment: one request per brand)

Exit codes: 0 ok (incl. a genuine zero-hit search), 1 unexpected error,
2 bad usage / unknown category or product, 3 blocked (stderr line starting "BLOCKED:").
"""

import argparse
import datetime as _dt
import html as htmllib
import json
import re
import sys
import time
from urllib.parse import quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "anhoch"
NAME = "Anhoch"
BASE = "https://www.anhoch.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PER_PAGE = 250           # server accepts >= 1000; 250 rows ~ 0.9 MB, ~1 s server time
BRAND_PER_PAGE = 500     # brand-enrichment walks only need ids
PACE_S = 0.5             # pause between consecutive requests (origin queues concurrency)
MAX_LIST_BRANDS = 60     # brand enrichment costs ~one request per brand
MAX_SEARCH_BRANDS = 25
MAX_FACET_COUNTS = 80    # per-value count requests in `facets`
MAX_CATEGORY_COUNTS = 40 # per-category count requests in `categories --counts`
SEARCH_CAP = 1000        # the site's search never reports more than 1000 hits
# category pages that embed the full tree (first one that answers is used);
# the search page /products?query= is the slug-independent fallback (1.2 MB)
TREE_SOURCES = ("monitori", "site-laptopi", "vouchers")
# delivery terms (/faq, /uslovi-i-pravila-za-internet-prodazba, 2026-10):
# 230 MKD per order up to 5,000 MKD, 179 MKD above; pickup in any Anhoch store free
SHIP_LOW, SHIP_HIGH, SHIP_THRESHOLD = 230, 179, 5000

SELLS = ("IT and consumer-electronics chain (~9,970 products): laptops, desktops, monitors, "
         "tablets, PC components (CPU, GPU, RAM, SSD/HDD, motherboards, cases, PSUs, cooling), "
         "keyboards/mice/headsets, gaming (consoles, games, chairs, controllers), phones, "
         "smartwatches, chargers/power banks, TVs, soundbars/speakers/headphones, projectors, "
         "cameras/drones, memory cards/USB/external drives, NAS, networking, cables/adapters, "
         "printers/toners, smart home, air conditioners, UPS, e-scooters, power stations, "
         "software, vouchers. No large kitchen/laundry appliances.")
NOTES = ("price_mkd = selling price (special price applied); regular_price_mkd only when a "
         "special price is active. qty is capped at 10 (10 = 10+). No EAN anywhere. "
         "Search matches multi-word queries as an ORDERED phrase (--all-words = AND in any "
         "order), silently ORs the words when the phrase matches nothing, caps at 1000 hits; it "
         "indexes title and description only, so part numbers (MPN) that are not in the title "
         "return 0 or OR-fallback junk. "
         "Listings carry no brand: it is filled by one extra query per brand (--no-brands skips). "
         "Facets = brand only (the site exposes no attribute filters). shipping_mkd = home "
         "delivery for a one-item order (230 MKD up to 5,000 MKD, 179 above); store pickup is free. "
         "Detail by numeric id costs 3 requests. python-requests / Python-urllib UAs are "
         "blocked (403 at the origin / Cloudflare 1010).")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter",
                "stock_qty", "per_location_stock", "warranty"]

# Cloudflare / bot-wall markers. Deliberately NOT bare "captcha": every normal page
# contains the Ziggy route name "bone.captcha.image".
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "just a moment", "attention required",
                     "turnstile", "g-recaptcha", "h-captcha")
LITESPEED_DENY = "access to this resource on the server is denied"
CF_DENY = "used cloudflare to restrict access"  # Cloudflare 1010/1020 HTML block page title
HOSTS = ("anhoch.com", "www.anhoch.com")

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}


class Blocked(RuntimeError):
    """Response is a bot challenge / block page / login wall rather than data."""


class NotFound(RuntimeError):
    """Unknown category or product (exit 2)."""


class Usage(RuntimeError):
    """Bad usage (exit 2)."""


class GaveUp(RuntimeError):
    """Retries on 5xx exhausted (exit 1). Never retried by a caller."""


# --------------------------------------------------------------------------- helpers

def log(*a):
    """Progress / detail lines (indented, suppressed by --quiet)."""
    if not QUIET:
        print(*a, file=sys.stderr)


def warn(msg):
    """Result-affecting warnings: unindented 'WARNING:' lines, never suppressed. mkshop.py
    only lifts stderr lines that start at column 0 into its envelope's warnings."""
    print(f"WARNING: {msg}", file=sys.stderr)


def note(msg):
    """Unindented NOTE line (search semantics hints), suppressed by --quiet."""
    if not QUIET:
        print(f"NOTE: {msg}", file=sys.stderr)


def check_host(url, what):
    host = (urlparse(url).hostname or "").lower()
    if host and host not in HOSTS:
        raise NotFound(f"not an Anhoch {what} URL (host {host}): {url}")


QUIET = False


def to_int(amount):
    if amount is None or amount == "":
        return None
    if isinstance(amount, str):  # API sends "5980.0000"; tolerate "5.980,00 ден." too
        s = re.sub(r"[^\d,.]", "", amount).strip(".,")
        if re.search(r",\d{1,2}$", s):
            s = s.replace(".", "").replace(",", ".")
        elif re.search(r"\.\d{3}($|\.)", s) and not re.search(r"\.\d{1,2}$|\.\d{4}$", s):
            s = s.replace(".", "")
        s = s.replace(",", "")
        amount = s
    return int(round(float(amount)))


def collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text):
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    return collapse(htmllib.unescape(text))


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def product_url(slug):
    return f"{BASE}/products/{quote(slug)}"


def category_url(slug):
    return f"{BASE}/categories/{quote(slug)}/products"


def stock_note_from(p):
    qty = p.get("qty")
    parts = ["На залиха" if p.get("is_in_stock") else "Нема на залиха"]
    if p.get("manage_stock") and qty is not None:
        parts.append(f"qty {qty}+" if qty >= 10 else f"qty {qty}")  # store caps qty at 10
    if p.get("in_stock_date"):
        parts.append(f"expected {p['in_stock_date']}")
    return "; ".join(parts)


def prices_from(p):
    """selling_price = what the buyer pays (special price applied); price = regular price,
    shown struck through only while a special price is active."""
    selling = to_int((p.get("selling_price") or {}).get("amount"))
    regular = to_int((p.get("price") or {}).get("amount"))
    if selling is None:
        selling = regular
    shown = regular if (regular is not None and selling is not None and regular > selling) else None
    return selling, shown


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


def special_until(p, price, regular):
    """price_valid_until for a FleetCart product (listing row or detail model), or NO_WINDOW when
    the record cannot tell. FleetCart charges the special price while today <= special_price_end,
    so it ends at the end of that calendar day in Skopje. None (standing) only on positive
    evidence: no markdown (selling_price == price, the list price) or the markdown IS the special
    price and it has no end date (open-ended). NO_WINDOW: no price, a markdown that is not the
    special price (no special price, or another amount: some other source set it), or an
    unreadable special_price_end."""
    if price is None:
        return NO_WINDOW
    if regular is None:                             # no markdown is being charged
        listed = to_int((p.get("price") or {}).get("amount"))
        return None if listed == price else NO_WINDOW
    sp = p.get("special_price")
    amount = to_int(sp.get("amount")) if isinstance(sp, dict) else None
    if amount is None:
        return NO_WINDOW                            # a markdown with no special price behind it
    if p.get("special_price_type") == "percent":    # FleetCart: price - price * special / 100
        if abs(regular * (100 - amount) / 100 - price) > 1:
            return NO_WINDOW
    elif amount != price:
        return NO_WINDOW                            # the charged price is not the special price
    end = p.get("special_price_end")
    if end is None or end == "":
        return None                                 # open-ended special price
    iso = skopje_iso(end)
    return skopje_iso(iso[:10]) if iso else NO_WINDOW


def _with_window(rec, window):
    """Set rec["price_valid_until"], or leave the key out for NO_WINDOW."""
    if window is NO_WINDOW:
        rec.pop("price_valid_until", None)
    else:
        rec["price_valid_until"] = window
    return rec


def shipping_for(price):
    if price is None:
        return None
    return SHIP_LOW if price <= SHIP_THRESHOLD else SHIP_HIGH


# --------------------------------------------------------------------------- client

class Anhoch:
    def __init__(self, pace=PACE_S):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "mk,en;q=0.8"})
        self.pace = pace
        self._last = 0.0
        self._tree = None
        self._nodes = None
        self._csrf = None
        self.requests_made = 0

    # ---- transport
    def _request(self, method, path_or_url, params=None, data=None, xhr=False, allow_404=False,
                 headers=None):
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        h = {"X-Requested-With": "XMLHttpRequest",
             "Accept": "application/json, text/javascript, */*; q=0.01"} if xhr else \
            {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        h.update(headers or {})
        delay = 2.0
        last = None
        for attempt in range(6):
            wait = self.pace - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, params=params, data=data, headers=h, timeout=90)
            except requests.RequestException as e:
                self._last = time.time()
                if attempt == 5:
                    raise
                log(f"  network error {e!r}; retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            self._last = time.time()
            self.requests_made += 1
            if r.status_code in (429, 502, 503, 504, 520, 521, 522, 523, 524):
                self._check_block(r)
                last = r
                if attempt == 5:
                    break
                ra = r.headers.get("Retry-After")
                sleep_for = min(float(ra), 120.0) if ra and ra.isdigit() else delay
                log(f"  HTTP {r.status_code} on {r.url}; retry in {sleep_for:.0f}s")
                time.sleep(sleep_for)
                delay *= 2
                continue
            self._check_block(r)
            if r.status_code == 404 and allow_404:
                raise NotFound(url)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} for {r.url}")
            return r
        if last is not None and last.status_code == 429:
            # a rate limiter that never lets up is a block for the caller: stop, don't hammer
            raise Blocked(f"HTTP 429 rate-limited on all 6 attempts (backoff up to {delay / 2:.0f}s) "
                          f"cf-ray={last.headers.get('cf-ray', '-')} on {last.url}")
        raise GaveUp(f"giving up after 6 attempts (last HTTP "
                     f"{last.status_code if last is not None else 'network error'}): {url}")

    def _get(self, path_or_url, params=None, xhr=False, allow_404=False):
        return self._request("GET", path_or_url, params=params, xhr=xhr, allow_404=allow_404)

    @staticmethod
    def _check_block(r):
        ctype = r.headers.get("content-type", "")
        ray = r.headers.get("cf-ray", "-")
        if r.headers.get("cf-mitigated"):
            raise Blocked(f"HTTP {r.status_code} cf-mitigated={r.headers['cf-mitigated']} "
                          f"cf-ray={ray} on {r.url}")
        if re.search(r"/login/?$", urlparse(r.url).path):
            raise Blocked(f"HTTP {r.status_code} redirected to login wall {r.url} cf-ray={ray}")
        if "json" in ctype:
            if r.status_code < 400:
                return
            # Cloudflare answers its own 1xxx errors as JSON (RFC 9457) when the request
            # prefers JSON, as our XHR calls do: e.g. 403 error 1010 browser_signature_banned
            try:
                j = r.json()
            except ValueError:
                j = None
            if isinstance(j, dict) and (j.get("cloudflare_error") or
                                        re.fullmatch(r"1\d{3}", str(j.get("error_code", "")))):
                if r.status_code == 429:  # 1015 rate limit: let the caller back off first
                    return
                raise Blocked(f"HTTP {r.status_code} Cloudflare error {j.get('error_code')} "
                              f"{j.get('error_name') or ''} ({collapse(j.get('title'))[:60]!r}) "
                              f"cf-ray={ray} on {r.url}")
            if r.status_code == 403:
                raise Blocked(f"HTTP 403 (JSON body) cf-ray={ray} on {r.url}: {r.text[:120]!r}")
            return
        head = r.text[:20000].lower()
        title = re.search(r"<title[^>]*>(.*?)</title>", r.text[:20000], re.S | re.I)
        title = collapse(title.group(1))[:80] if title else "-"
        hit = [m for m in CHALLENGE_MARKERS if m in head]
        if hit:
            raise Blocked(f"HTTP {r.status_code} challenge page markers={hit} title={title!r} "
                          f"cf-ray={ray} on {r.url}")
        if r.status_code == 403:
            if LITESPEED_DENY in head:
                why = ("LiteSpeed origin 'Access ... denied' (User-Agent blocklist: "
                       "python-requests substring)")
            elif CF_DENY in head:
                why = ("Cloudflare edge 'Access denied' (error 1010/1020: browser signature "
                       "or WAF rule, e.g. a Python-urllib User-Agent)")
            else:
                why = "403 Forbidden"
            raise Blocked(f"HTTP 403 {why} title={title!r} cf-ray={ray} on {r.url}")

    def _json(self, params):
        r = self._get("/products", params=params, xhr=True)
        if "json" not in r.headers.get("content-type", ""):
            title = re.search(r"<title[^>]*>(.*?)</title>", r.text[:20000], re.S | re.I)
            raise Blocked(f"HTTP {r.status_code} expected JSON from {r.url}, got "
                          f"{r.headers.get('content-type')} title="
                          f"{collapse(title.group(1))[:80] if title else '-'!r} "
                          f"cf-ray={r.headers.get('cf-ray', '-')}")
        d = r.json()
        if not isinstance(d, dict) or "products" not in d:
            raise RuntimeError(f"unexpected JSON shape from {r.url}: "
                               f"{list(d) if isinstance(d, dict) else type(d).__name__}")
        if isinstance(d["products"], list):  # FleetCart returns [] instead of a paginator at 0
            d["products"] = {"data": [], "total": 0, "last_page": 1}
        return d

    def total(self, params):
        d = self._json(dict(params, perPage=1, page=1))
        return int(d["products"].get("total") or 0), d

    # ---- listing (JSON endpoint, all pages)
    def walk(self, base_params, limit=None, label="", per_page=PER_PAGE):
        """Every page of /products for the filter. Returns (rows, first_response, total)."""
        params = dict(base_params)
        params["perPage"] = min(per_page, limit) if limit else per_page
        params["page"] = 1
        first = self._json(params)
        pg = first["products"]
        total = int(pg.get("total") or 0)
        last = int(pg.get("last_page") or 1)
        items = list(pg.get("data") or [])
        if label:
            log(f"  {label}: total={total} pages={last} (perPage {params['perPage']})")
        page = 1
        while page < last and (limit is None or len(items) < limit):
            page += 1
            params["page"] = page
            items.extend(self._json(params)["products"].get("data") or [])
        seen, uniq = set(), []
        for x in items:
            if x["id"] not in seen:
                seen.add(x["id"])
                uniq.append(x)
        if limit is None and len(uniq) != total and last > 1:
            # unstable ordering between pages or a stock sync mid-walk; one big page is exact
            log(f"  page walk gave {len(uniq)} unique of {total}; re-fetching as one page")
            d = self._json(dict(base_params, perPage=max(total, 1), page=1))
            uniq = d["products"].get("data") or []
            total = int(d["products"].get("total") or 0)
            if len(uniq) != total:
                raise RuntimeError(f"could not obtain all {total} products ({len(uniq)} returned)")
        return (uniq[:limit] if limit else uniq), first, total

    def brand_map(self, base_params, brands, cap):
        """product id -> brand name, by re-querying the same filter per brand id
        (listing rows carry no brand). Returns {} when skipped."""
        if not brands:
            return {}
        if len(brands) > cap:
            log(f"  {len(brands)} brands > {cap}: brand left null (narrow the query or use detail)")
            return {}
        log(f"  brand enrichment: {len(brands)} brand queries")
        out = {}
        for b in brands:
            items, _, _ = self.walk(dict(base_params, **{"brand[0]": b["id"]}),
                                    per_page=BRAND_PER_PAGE)
            for x in items:
                out.setdefault(x["id"], collapse(b.get("name")))
        return out

    # ---- category tree
    def tree(self):
        if self._tree is not None:
            return self._tree
        for slug in TREE_SOURCES:
            try:
                self._tree = self._category_page(slug)["tree"] or None
                if self._tree:
                    break
            except (Blocked, GaveUp):  # the next source would fail the same way
                raise
            except (NotFound, RuntimeError):  # slug renamed / layout change: try the next
                continue
        if self._tree is None:
            r = self._get("/products", params={"query": "zzqx"})
            m = re.search(r':initial-categories="([^"]*)"', r.text)
            tree = json.loads(htmllib.unescape(m.group(1))) if m else None
            if not tree:
                raise Blocked(f"HTTP {r.status_code} no category tree on {r.url} (layout change "
                              f"or soft block) cf-ray={r.headers.get('cf-ray', '-')}")
            self._tree = tree
        return self._tree

    def _category_page(self, slug):
        r = self._get(category_url(slug), allow_404=True)
        m = re.search(r':initial-categories="([^"]*)"', r.text)
        if not m:
            raise RuntimeError(f"category page {r.url} lacks the product-index component")
        tree = json.loads(htmllib.unescape(m.group(1)))
        name = re.search(r'initial-category-name="([^"]*)"', r.text)
        cid = re.search(r'initial-category-id="([^"]*)"', r.text)
        return {"tree": tree, "name": htmllib.unescape(name.group(1)).strip() if name else None,
                "id": cid.group(1) if cid else None}

    def nodes(self):
        """Flat list of category nodes in tree order."""
        if self._nodes is not None:
            return self._nodes
        out = []

        def walk(ns, path, parent):
            for n in ns:
                name = collapse(n.get("name"))
                p = path + [name]
                kids = n.get("items") or []
                out.append({"id": str(n["id"]), "slug": n["slug"], "name": name, "path": p,
                            "parent": parent, "leaf": not kids})
                walk(kids, p, str(n["id"]))
        walk(self.tree(), [], None)
        self._nodes = out
        return out

    def categories(self, grep=None, counts=False):
        rx = re.compile(grep, re.I | re.M) if grep else None  # ^/$ anchor per field
        recs = []
        for n in self.nodes():
            path = " > ".join(n["path"])
            if rx:
                hay = f"{n['name']}\n{path}\n{n['slug']}\n{translit(path)}"
                if not rx.search(hay):
                    continue
            recs.append({"id": n["id"], "slug": n["slug"], "name": n["name"], "path": path,
                         "url": category_url(n["slug"]), "parent": n["parent"], "count": None,
                         "leaf": n["leaf"]})
        if counts:
            if len(recs) > MAX_CATEGORY_COUNTS:
                warn(f"--counts skipped: {len(recs)} categories > {MAX_CATEGORY_COUNTS} (one request "
                     f"each); count left null. Narrow --grep.")
            else:
                for r in recs:
                    r["count"], _ = self.total({"categories[0]": r["slug"]})
                dup = self._dup_slugs()
                for r in recs:
                    if r["slug"].lower() in dup:
                        r["count_note"] = "slug shared by several categories; count is their union"
        return recs

    def _dup_slugs(self):
        seen = {}
        for n in self.nodes():
            seen.setdefault(n["slug"].lower(), []).append(n)
        return {k: v for k, v in seen.items() if len(v) > 1}

    def resolve_category(self, ref):
        """-> (node, by_id). node has id, slug, name, path, parent, leaf."""
        ref = (ref or "").strip()
        if ref.startswith("http"):
            check_host(ref, "category")
            m = re.search(r"/categories/([^/?#]+)", urlparse(ref).path)
            if not m:
                raise Usage(f"not an Anhoch category URL: {ref}")
            ref = unquote(m.group(1))
        ref = ref.strip().strip("/")  # "slug/" made the site answer HTTP 500
        if not ref:
            raise Usage("empty category reference")
        nodes = self.nodes()
        if ref.isdigit():
            hit = [n for n in nodes if n["id"] == ref]
            if not hit:
                raise NotFound(f"unknown category id {ref!r} (run `categories --grep ...`)")
            return hit[0], True
        hit = [n for n in nodes if n["slug"] == ref] or \
            [n for n in nodes if n["slug"].lower() == ref.lower()] or \
            [n for n in nodes if n["name"].lower() == ref.lower()]
        if hit:
            if len(hit) > 1:
                warn(f"{ref!r} names {len(hit)} categories "
                     f"({', '.join(n['id'] + ' ' + ' > '.join(n['path']) for n in hit)}); the store "
                     f"merges them, exactly as its own page does. Pass an id to get just one.")
            return hit[0], False
        # not in the tree (hidden category?): the category page itself is authoritative
        try:
            page = self._category_page(ref)
        except NotFound:
            raise NotFound(f"unknown category {ref!r} (HTTP 404; run `categories --grep ...`)")
        log(f"  NOTE: {ref!r} is not in the menu tree but its page exists ({page['name']})")
        return {"id": page["id"] or ref, "slug": ref, "name": page["name"] or ref,
                "path": [page["name"] or ref], "parent": None, "leaf": True}, False

    def _ancestors(self, node):
        by_id = {n["id"]: n for n in self.nodes()}
        out, cur = [], node
        while cur.get("parent"):
            cur = by_id.get(cur["parent"])
            if not cur:
                break
            out.append(cur)
        return out

    def _disambiguator(self, node):
        """For an id whose slug is shared with other categories: the nearest ancestor slug
        whose subtree contains none of the other same-slug categories (to intersect with)."""
        dup = self._dup_slugs().get(node["slug"].lower())
        if not dup:
            return None
        others = [n for n in dup if n["id"] != node["id"]]
        dup_slugs = self._dup_slugs()
        for anc in self._ancestors(node):
            if anc["slug"].lower() in dup_slugs:
                continue
            if not any(anc["id"] in [a["id"] for a in self._ancestors(o)] for o in others):
                return anc
        return False

    # ---- records
    def listing_record(self, x, category=None, brand=None):
        price, regular = prices_from(x)
        return _with_window({
            "store": STORE,
            "id": str(x["id"]),
            "sku": str(x["id"]),  # the on-page "Шифра" IS the internal id; MPN is detail-only
            "title": collapse(x.get("name")),
            "url": product_url(x["slug"]),
            "brand": brand,
            "price_mkd": price,
            "regular_price_mkd": regular,
            "price_valid_until": special_until(x, price, regular),
            "in_stock": bool(x["is_in_stock"]) if x.get("is_in_stock") is not None else None,
            "stock_note": stock_note_from(x),
            "category": category,
            "ean": None,  # Anhoch publishes no EAN/GTIN anywhere
            "shipping_mkd": shipping_for(price),
        }, special_until(x, price, regular))

    # ---- filters / facets
    def _parse_filters(self, tokens, brands):
        """--filter tokens -> extra query params. brand=<slug|id>[,<slug|id>] (OR inside a
        token), attr:<slug>=<value> (only if the store ever exposes attributes)."""
        params, brand_sets = {}, []
        by_key = {}
        for b in brands or []:
            by_key[str(b["id"])] = b
            by_key[str(b.get("slug", "")).lower()] = b
            by_key[collapse(b.get("name")).lower()] = b
        attr_i = {}
        for tok in tokens or []:
            m = re.match(r"^\s*brand\s*[=:]\s*(.+)$", tok, re.I)
            if m:
                ids = set()
                for v in m.group(1).split(","):
                    b = by_key.get(v.strip().lower())
                    if not b:
                        valid = ", ".join(sorted(str(x.get("slug")) for x in brands or []))
                        raise Usage(f"brand {v.strip()!r} not present in this listing; valid: {valid}")
                    ids.add(str(b["id"]))
                brand_sets.append(ids)
                continue
            m = re.match(r"^\s*attr:([^=]+)=(.+)$", tok)
            if m:
                slug = m.group(1).strip()
                i = attr_i.get(slug, 0)
                params[f"attribute[{slug}][{i}]"] = m.group(2).strip()
                attr_i[slug] = i + 1
                continue
            raise Usage(f"unsupported --filter token {tok!r}; use a token printed by `facets` "
                        f"(brand=<slug>[,<slug>])")
        if brand_sets:
            ids = set.intersection(*brand_sets)
            if not ids:
                raise Usage("several brand= filters AND together and a product has one brand; "
                            "use one token brand=a,b for either")
            for i, bid in enumerate(sorted(ids)):
                params[f"brand[{i}]"] = bid
        return params

    def facets(self, ref):
        node, by_id = self.resolve_category(ref)
        base = {"categories[0]": node["slug"]}
        total, first = self.total(base)
        if total == 0:
            raise NotFound(f"category {node['slug']!r} returned 0 products (empty, soft block "
                           f"or layout change)")
        if self._disambiguator(node) is not None:
            warn("this slug is shared by several categories; facet counts cover their union")
        brands = first.get("brands") or []
        attrs = first.get("attributes") or []
        n_req = len(brands) + sum(len(a.get("values") or []) for a in attrs)
        do_counts = n_req <= MAX_FACET_COUNTS
        if not do_counts:
            log(f"  {n_req} facet values > {MAX_FACET_COUNTS}: counts left null")
        else:
            log(f"  {total} products; counting {n_req} facet values (one request each)")
        out = []
        for b in brands:
            cnt = self.total(dict(base, **{"brand[0]": b["id"]}))[0] if do_counts else None
            out.append({"name": "brand", "value": collapse(b.get("name")), "count": cnt,
                        "token": f"brand={b.get('slug') or b['id']}"})
        if attrs:
            log(f"  NOTE: the store now returns attribute facets {[a.get('name') for a in attrs]}; "
                f"attr: tokens are untested")
        for a in attrs:
            for v in a.get("values") or []:
                val = v.get("value") if isinstance(v, dict) else v
                tok = f"attr:{a.get('slug')}={val}"
                cnt = self.total(dict(base, **self._parse_filters([tok], brands)))[0] \
                    if do_counts else None
                out.append({"name": collapse(a.get("name")), "value": val, "count": cnt,
                            "token": tok})
        out.sort(key=lambda f: (f["name"], -(f["count"] or 0), f["value"]))
        return out

    # ---- commands
    def search(self, query, limit=None, in_stock=False, category=None, brands=True,
               all_words=False):
        """The site's search. Semantics (verified 2026-10): the whole query is matched as an
        ORDERED PHRASE of word prefixes over title and description ("1tb ssd" 34 hits, "ssd 1tb"
        5). Words are split on any non-alphanumeric character ("EP-T4511" = "ep t4511",
        "/BacklitKB" matches "backlitkb"). When the phrase matches nothing the site silently
        falls back to OR of the words; totals cap at 1000; queries under 3 chars return 0.
        The manufacturer part number (detail `mpn`) is NOT indexed unless it is in the title.
        all_words=True instead returns products whose TITLE contains every word (any order),
        built from the rarest word's hits."""
        if len(query.strip()) < 3:
            raise Usage("query must be at least 3 characters (the site returns 0 hits for "
                        "shorter queries, e.g. '4k' or '27')")
        base = {"query": query, "sort": "relevance", "inStockOnly": 1 if in_stock else 2}
        cpath = None
        if category:
            node, by_id = self.resolve_category(category)
            base["categories[0]"] = node["slug"]
            cpath = " > ".join(node["path"])
            if by_id and node["slug"].lower() in self._dup_slugs():  # (a slug ref warned already)
                warn(f"category slug {node['slug']!r} is shared by several categories; the "
                     f"search covers all of them")
        # the site tokenises on every non-alphanumeric character, not only on spaces
        words = [t for t in re.split(r"[^\w]+|_", query.strip()) if t]
        # words under 3 chars alone return 0 (site minimum) but still filter inside a phrase
        terms = [t for t in words if len(t) >= 3]
        code_like = bool(re.search(r"\d", query) and re.search(r"[^\W\d_]", query)
                         and " " not in query.strip())
        mpn_hint = ("Anhoch's search does not index manufacturer part numbers unless they appear "
                    "in the title; search the model name (e.g. 'galaxy s25', 'yoga 7') instead")
        bbase = base  # filter the per-brand enrichment queries repeat
        if all_words and len(words) > 1 and not terms:
            # e.g. 'lg 55': no word long enough to walk on its own; the phrase is all there is
            note("--all-words needs a word of 3+ characters; matching the query as a phrase")
        if all_words and len(words) > 1 and terms:
            items, first, total, rare = self._search_all_words(base, words, terms, limit)
            bbase = dict(base, query=rare)
        else:
            items, first, total = self.walk(base, limit=limit, label=f"search {query!r}")
            if total >= SEARCH_CAP - 1:
                warn(f"{total} hits = the site's search cap ({SEARCH_CAP}); results are "
                     f"truncated. Refine the query, add --category, or use `list <category>`.")
            if len(words) > 1 and 1 <= len(terms) <= 5 and total:
                # a phrase match can never exceed any of its words' own counts; OR fallback can
                tt = {t: self.total(dict(base, query=t))[0] for t in terms}
                if total > min(tt.values()):
                    warn(f"{query!r}: {total} hits > the {min(tt.values())} hits of its rarest "
                         f"word ({tt}): no product matched the phrase, so the site fell back to "
                         f"OR-matching; results are probably irrelevant. "
                         + (mpn_hint + "." if code_like else "Try --all-words."))
                elif total < min(tt.values()):
                    note(f"the site matched {query!r} as an ordered phrase ({total} hits; "
                         f"rarest word alone: {min(tt.values())}). --all-words finds titles with "
                         f"every word in any order.")
            elif len(words) > 1 and total:
                note("multi-word queries are matched as an ordered phrase; --all-words "
                     "matches every word in any order")
        if total == 0:
            alive, _ = self.total({})
            if alive == 0:
                raise Blocked("search and the unfiltered catalogue both returned 0 products "
                              "(soft block or site change)")
            log(f"  0 hits for {query!r} (catalogue answers normally: {alive} products)")
            if code_like:
                note(mpn_hint)
        bmap = {}
        if brands and items:
            bmap = self.brand_map(bbase, first.get("brands") or [], MAX_SEARCH_BRANDS)
        recs = [self.listing_record(x, cpath, bmap.get(x["id"])) for x in items]
        if in_stock:
            recs = [r for r in recs if r["in_stock"]]
        return recs

    def _search_all_words(self, base, words, terms, limit):
        """AND of all words in any order: phrase hits plus the rarest word's hit set filtered
        to titles that contain every word as a word prefix."""
        if not terms:
            raise Usage("--all-words needs at least one word of 3+ characters")
        tt = {t: self.total(dict(base, query=t))[0] for t in terms}
        rare = min(tt, key=tt.get)
        log(f"  --all-words: word counts {tt}; walking the rarest {rare!r}")
        if tt[rare] >= SEARCH_CAP - 1:
            warn(f"even the rarest word {rare!r} hits the {SEARCH_CAP} cap; results can be "
                 f"incomplete. Add --category or a more specific word.")
        rows, first, _ = self.walk(dict(base, query=rare), label=f"search {rare!r}")
        pats = [re.compile(r"(?<![^\W_])" + re.escape(w), re.I) for w in words]
        keep = [x for x in rows if all(p.search(x.get("name") or "") for p in pats)]
        ptotal, _ = self.total(base)  # cheap probe: skip walking an OR-fallback hit set
        if ptotal and ptotal <= min(tt.values()):  # a real phrase match, not the OR fallback
            phrase, _, _ = self.walk(base, label=f"phrase {base['query']!r}")
            seen = {x["id"] for x in phrase}
            keep = phrase + [x for x in keep if x["id"] not in seen]
        log(f"  --all-words: {len(keep)} products contain every word")
        return (keep[:limit] if limit else keep), first, len(keep), rare

    def list_category(self, ref, in_stock=False, limit=None, filters=None, brands=True):
        node, by_id = self.resolve_category(ref)
        cpath = " > ".join(node["path"])
        base = {"categories[0]": node["slug"], "sort": "latest",
                "inStockOnly": 1 if in_stock else 2}
        first_brands = None
        if filters:
            _, probe = self.total({"categories[0]": node["slug"]})
            first_brands = probe.get("brands") or []
            base.update(self._parse_filters(filters, first_brands))
        anc = self._disambiguator(node) if by_id else None
        items, first, total = self.walk(base, limit=None if anc else limit,
                                        label=f"list {node['slug']} ({cpath})")
        if anc:
            # the endpoint filters by slug only, and this slug names several categories
            log(f"  slug {node['slug']!r} is shared by several categories; intersecting with "
                f"ancestor {anc['slug']!r} to keep only id {node['id']}")
            anc_items, _, _ = self.walk(dict(base, **{"categories[0]": anc["slug"]}))
            keep_ids = {x["id"] for x in anc_items}
            items = [x for x in items if x["id"] in keep_ids][:limit]
        elif anc is False:
            warn(f"slug {node['slug']!r} is shared and cannot be separated; "
                 f"results include the other same-slug categories")
        if not items:
            if in_stock or filters:
                alive, _ = self.total({"categories[0]": node["slug"]})
                if alive:
                    log(f"  0 products match the filters ({alive} in the category overall)")
                    return []
            raise NotFound(f"category {node['slug']!r} ({cpath}) returned 0 products "
                           f"(empty category, soft block or layout change)")
        bmap = {}
        if brands:
            bl = first.get("brands") or first_brands or []
            fb = {str(v) for k, v in base.items() if k.startswith("brand[")}
            if fb:  # a brand filter already says which brand(s) the rows belong to
                bl = [b for b in (first_brands or bl) if str(b["id"]) in fb]
            if len(fb) == 1 and bl:
                bmap = {x["id"]: collapse(bl[0].get("name")) for x in items}
            else:
                bmap = self.brand_map({k: v for k, v in base.items() if not k.startswith("brand[")},
                                      bl, MAX_LIST_BRANDS)
        recs = [self.listing_record(x, cpath, bmap.get(x["id"])) for x in items]
        if in_stock:
            recs = [r for r in recs if r["in_stock"]]
        return recs

    # ---- detail
    def resolve_slug(self, ref):
        ref = ref.strip()
        if ref.startswith("http"):
            check_host(ref, "product")
            m = re.search(r"/products/([^/?#]+)", urlparse(ref).path)
            if not m:
                raise NotFound(f"not an Anhoch product URL: {ref}")
            return unquote(m.group(1))
        ref = ref.strip("/")
        if not ref:
            raise NotFound("empty product reference")
        if ref.isdigit():
            return self._slug_for_id(ref)
        return ref

    def _slug_for_id(self, pid):
        """No id route exists and search does not index ids; the anonymous, session-scoped
        compare list renders the full product model (incl. slug) for an id."""
        if not self._csrf:
            r = self._get("/compare")
            tok = re.search(r"csrfToken: '([^']+)'", r.text)
            if not tok:
                raise RuntimeError("could not find the CSRF token needed for id lookup")
            self._csrf = tok.group(1)
        try:  # an unknown id answers 404; any other failure is a real error, not "not found"
            self._request("POST", "/compare", data={"productId": pid}, xhr=True,
                          headers={"X-CSRF-TOKEN": self._csrf}, allow_404=True)
        except NotFound:
            raise NotFound(f"product id {pid} not found (HTTP 404 from the compare list)")
        r = self._get("/compare")
        m = re.search(r':compare="([^"]*)"', r.text)
        prod = None
        if m:
            data = json.loads(htmllib.unescape(m.group(1)))
            prods = data.get("products") or {}
            if isinstance(prods, dict):
                prod = prods.get(str(pid))
            else:
                prod = next((p for p in prods if str(p.get("id")) == str(pid)), None)
        if not prod:
            raise NotFound(f"product id {pid} not found")
        return prod["slug"]

    def detail_one(self, slug):
        r = self._get(product_url(slug), allow_404=True)
        page = r.text
        m = re.search(r'<product-show[^>]*?\s:product="([^"]*)"', page, re.S)
        if not m:
            raise RuntimeError(f"product page {r.url} lacks <product-show> data (layout change?)")
        p = json.loads(htmllib.unescape(m.group(1)))
        soup = BeautifulSoup(page, "html.parser")

        crumbs = [collapse(a.get_text()) for a in soup.select(".breadcrumb li a")][1:]  # drop "Дома"
        category = " > ".join(crumbs) or None
        avail = soup.select_one(".availability")
        avail_txt = collapse(avail.get_text()) if avail else None
        w = soup.select_one("li.warranty span")
        warranty = collapse(w.get_text()) if w else \
            (f"{p['warranty']} месеци" if p.get("warranty") else None)

        brand = p.get("brand")
        brand = collapse(brand.get("name")) if isinstance(brand, dict) and brand.get("name") else None
        if not brand:
            for ld in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
                try:
                    j = json.loads(ld)
                except ValueError:
                    continue
                if isinstance(j, dict) and j.get("@type") == "Product" and \
                        isinstance(j.get("brand"), dict):
                    brand = j["brand"].get("name") or None

        attrs = {}
        for a in p.get("attributes") or []:
            name = collapse(a.get("name") or (a.get("attribute") or {}).get("name"))
            vals = ", ".join(v.get("value", "") for v in a.get("values") or [] if isinstance(v, dict))
            if name:
                attrs[name] = vals
        specs = collapse(" ".join(filter(None, [
            strip_html(p.get("short_description")), strip_html(p.get("description")),
            "; ".join(f"{k}: {v}" for k, v in attrs.items())])))

        region = {2: "Скопје", 3: "Македонија"}
        locs = sorted(p.get("locations") or [], key=lambda l: (l.get("position") or 0))
        per_loc = [{"location": collapse(l.get("name")),
                    "in_stock": bool((l.get("pivot") or {}).get("in_stock")),
                    "region": region.get(l.get("location_group_id")),
                    "walk_in": bool(l.get("show"))} for l in locs] or None

        price, regular = prices_from(p)
        note = stock_note_from(p)
        if avail_txt and not note.startswith(avail_txt):
            note = f"{avail_txt}; {note}"
        sifra = soup.select_one("li.sku")
        sifra = re.sub(r"^\s*Шифра:\s*", "", collapse(sifra.get_text())) if sifra else ""
        inst = p.get("installments") or {}
        return _with_window({
            "store": STORE,
            "id": str(p["id"]),
            "sku": sifra or str(p["id"]),
            "mpn": collapse(p.get("sku")) or None,
            "title": collapse(p.get("name")),
            "url": product_url(p["slug"]),
            "brand": brand,
            "price_mkd": price,
            "regular_price_mkd": regular,
            "price_valid_until": special_until(p, price, regular),
            "in_stock": bool(p.get("is_in_stock")),
            "stock_note": note,
            "category": category,
            "ean": None,
            "shipping_mkd": shipping_for(price),
            "warranty": warranty,
            "specs": specs,
            "attributes": attrs or None,
            "per_location_stock": per_loc,
            "extra": {
                "qty": p.get("qty"),
                "in_stock_date": p.get("in_stock_date"),
                "warranty_months": p.get("warranty"),
                "special_price_window": [p.get("special_price_start"), p.get("special_price_end")]
                if p.get("special_price_start") or p.get("special_price_end") else None,
                "installments": {"months": inst.get("period"),
                                 "per_month_mkd": to_int((inst.get("price") or {}).get("amount"))}
                if inst.get("period") else None,
                # badge campaigns (FleetCart flash-sale records without a price): their window is
                # the campaign's, not the price's, so it never feeds price_valid_until
                "campaigns": [collapse(a.get("campaign_name")) + (
                    f" ({a.get('start_date') or '?'} .. {a['end_date']})" if a.get("end_date") else "")
                              for a in p.get("actions") or [] if isinstance(a, dict)] or None,
                "options": len(p.get("options") or []) or None,
            },
        }, special_until(p, price, regular))

    def detail(self, refs):
        """-> (records, worst_exit). One record per input, in order; failures become
        {"input", "error"} rows. A block aborts the rest (they would all fail)."""
        out, code = [], 0
        for i, ref in enumerate(refs):
            try:
                out.append(self.detail_one(self.resolve_slug(ref)))
            except Blocked as e:
                out.append({"input": ref, "error": f"blocked: {e}"})
                out.extend({"input": r, "error": "skipped: store blocked the client"}
                           for r in refs[i + 1:])
                raise _BatchBlocked(out, e)
            except NotFound as e:
                log(f"  not found: {ref} ({e})")
                out.append({"input": ref, "error": f"not found: {e}"})
                code = code or 2
            except (RuntimeError, requests.RequestException, ValueError) as e:
                log(f"  error: {ref} ({e})")
                out.append({"input": ref, "error": str(e)})
                code = 1
        return out, code


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
        stock = "IN " if r["in_stock"] else ("OUT" if r["in_stock"] is False else " ? ")
        brand = f"[{r['brand']}] " if r.get("brand") else ""
        print(f"{price} MKD{reg}  {stock}  {brand}{r['title'][:80]}  {r['url']}")


def emit(recs, path, printer=print_products, what="records"):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        print(len(recs))
        log(f"  {len(recs)} {what} -> {path}")
    else:
        printer(recs)
        log(f"-- {len(recs)} {what}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{r['id']:>8} {cnt}  {r['path']}  [{r['slug']}]")


def print_facets(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{cnt}  {r['name']}: {r['value']}   --filter '{r['token']}'")


def info():
    return {"store": STORE, "name": NAME, "base_url": BASE, "capabilities": CAPABILITIES,
            "sells": SELLS, "notes": NOTES}


def main(argv=None):
    global QUIET
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("--no-brands", action="store_true",
                        help="skip brand enrichment (saves one request per brand)")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    ap = argparse.ArgumentParser(description="Anhoch (anhoch.com) catalogue client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--category", help="restrict to a category (id, slug or url)")
    p.add_argument("--all-words", action="store_true",
                   help="AND every word in any order (title match) instead of the site's "
                        "ordered-phrase matching")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p.add_argument("--counts", action="store_true",
                   help=f"fetch product counts (one request per category, max {MAX_CATEGORY_COUNTS})")
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
    QUIET = a.quiet
    brands = not a.no_brands

    if a.cmd == "info":
        print(json.dumps(info(), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    c = Anhoch()
    code = 0
    try:
        if a.cmd == "search":
            emit(c.search(a.query, limit=a.limit, in_stock=a.in_stock, category=a.category,
                          brands=brands, all_words=a.all_words), a.json)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = c.categories(grep=a.grep, counts=a.counts)
            if a.grep and not recs:
                log(f"  no category matches {a.grep!r} (matched against name, path, slug and a "
                    f"Latin transliteration)")
            emit(recs, a.json, print_categories, "categories")
        elif a.cmd == "list":
            emit(c.list_category(a.category, in_stock=a.in_stock, limit=a.limit,
                                 filters=a.filter, brands=brands), a.json)
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
