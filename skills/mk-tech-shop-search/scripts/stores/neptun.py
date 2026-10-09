#!/usr/bin/env python3
"""Neptun (https://www.neptun.mk) catalogue client. See references/client-contract.md and
references/neptun.md.

Neptun's shop is ASP.NET MVC with an AngularJS frontend behind Cloudflare. This client calls
the JSON endpoints that frontend calls (no cookies, tokens or browser needed):

    POST /NeptunCategories/LoadProductsForCategory  {"model": {CategoryId, Manufacturers[],
         PriceRange, BoolFeatures[], DropdownFeatures[], MultiSelectFeatures[],
         ShowAllProducts, Sort, ItemsPerPage, CurrentPage}}      category listing (paged)
    POST /Product/SearchProductsAutocomplete  {"term", "page", "itemsPerPage"}   search (paged)
    POST /Product/GetProduct                  {"id"}            full product model
    POST /Product/GetShopsForProduct          {"productid"}     stores holding the item

The category tree comes from the server-rendered mega-menu on the home page (names, *.nspx
slugs, and the numeric ids listed in each department's brand links). A category page
(/<Slug>.nspx) embeds data-categorydetails (id, NumberOfProducts, brands, feature filters,
sub-categories) and data-initialSearchModel (the exact listing request the page makes,
including its per-category ShowAllProducts flag and any ?brands=/?multi= URL filters).

    neptun.py info
    neptun.py search "<query>" [--limit N] [--in-stock] [--json PATH]
    neptun.py categories [--grep REGEX] [--counts] [--json PATH]
    neptun.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    neptun.py detail <url|id|code|ean> [...] [--json PATH]
    neptun.py facets <category> [--json PATH]
    common options: --quiet, -v/--verbose (log every request)

Category refs: a numeric id ("173"), a slug ("televizori", case-sensitive on the server but
matched case-insensitively here), a category URL (query-string filters such as
?brands=61_41&multi=390:55 are honoured), or an exact menu name ("ТЕЛЕВИЗОРИ").
Filter tokens: brand=<id|name>[,...]  price=MIN-MAX  sub=<id|name>[,...]
               f<featureId>=<value>[,...]   (commas OR inside a token; several flags AND)

Exit codes: 0 ok (incl. a genuine zero-hit search), 1 unexpected error,
2 bad usage / unknown category or product, 3 blocked (stderr line starting "BLOCKED:").
"""

import argparse
import datetime as _dt
import html as htmlmod
import json
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "neptun"
NAME = "Neptun"
BASE = "https://www.neptun.mk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PAGE_SIZE = 200          # honoured up to 200; 250+ is silently clamped to 120 (paging uses Config)
SEARCH_PAGE_SIZE = 100
PACE_S = 0.5             # pause between consecutive requests (bursts trigger multi-second stalls)
MAX_RETRIES = 5
DEFAULT_SORT = 7         # appconfig.productSort: newest first (the site default)
MAX_CATEGORY_LOOKUPS = 15   # per search: extra 1-item listing calls to name categories not in the menu
MAX_COUNT_REQUESTS = 60     # budget for `categories --counts`
MAX_FACET_REQUESTS = 40     # budget for per-value counts in `facets`
MEMBER_PRICE_TYPES = {3, 4}  # DiscountPriceType of the "haPPy" loyalty-card price
MEMBER_CARD_TERMS = "150 MKD online / 299 MKD in store, one-off"  # what the haPPy card costs
DELIVERY_POLICY = ("standard delivery within 7 working days, 300 MKD (store policy); "
                   "pickup in store also offered")
INACTIVE_NOTE = ("inactive: sold out or delisted (hidden from listings and search; the product page "
                 "is a soft 404)")

SELLS = ("Consumer-electronics and appliance chain (~5,600 products online in 12 departments): "
         "TVs, home and personal audio, car gadgets, smart-home devices; gaming (consoles, games, "
         "gaming laptops, monitors and peripherals); computers (laptops, desktops, PC components, "
         "monitors, projectors, printers, tablets, software, Starlink); phones, smartwatches and "
         "accessories; cameras; air conditioners, heat pumps, heaters, fans; large appliances "
         "(fridges, freezers, washing machines and dryers, dishwashers, cookers, ovens, hobs, "
         "boilers, built-in); small appliances (vacuums, coffee machines, kitchen appliances, "
         "irons, air and water purifiers, cookware); personal care; sport and outdoors "
         "(e-scooters, bikes, fitness, drones, garden tools, pet gear); vouchers and services; "
         "an adult-toys department.")
NOTES = ("price_mkd = the price a buyer without the haPPy loyalty card pays (RegularPrice, or the "
         "'Онлајн цена' WebshopDiscountPrice when set); member_price_mkd = the lower 'haPPy цена' "
         "(card costs " + MEMBER_CARD_TERMS + "; the anonymous cart charges price_mkd). "
         "detail adds member_price_valid_until = the end of the haPPy campaign the product is filed "
         "under (GetProduct PromotionId, e.g. 'HAPPY WEEKS'), null when no promotion is attached at "
         "all (type-4 haPPy prices), left out when the product has promotions but GetProduct names "
         "none of them; listings cannot tell. "
         "Listings and search contain only products orderable online (sold-out items are hidden). "
         "Шифра in every record; EAN on ~95% (missing on most adult items; FUEGO/HOOBART private "
         "labels carry store-internal 20-29 prefix codes); mpn only when the search title carries it "
         "in parentheses. Search is "
         "AND over substrings of title, tags, Шифра and EAN (no Cyrillic/Latin transliteration). "
         "detail adds per-store availability (yes/no, no quantities), warranty in months and specs. "
         "Facets = brand, sub-category, the category's feature filters (sparse) and price. Delivery "
         "300 MKD within 7 working days (adult items 160). Cloudflare blocks empty/urllib/fake-bot/"
         "AI-crawler user agents.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter",
                "ean_in_listing", "ean_in_detail", "per_location_stock", "warranty",
                "delivery_estimate", "search_ean", "search_codes"]

CHALLENGE_RE = re.compile(
    r"Just a moment|cf-chl|cf_chl_opt|challenge-platform|turnstile|captcha|"
    r"Attention Required|Sorry, you have been blocked|"
    r"cf-error-details|used Cloudflare to restrict access|error code: 1\d{3}", re.I)

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}

# English words added to a category's grep haystack when its Macedonian name contains the stem,
# so `categories --grep fridge` finds ФРИЖИДЕРИ. Lowercase stems.
_EN_TAGS = {
    "телевизор": "tv television", "домашно аудио": "home audio", "домашни кина": "home cinema",
    "музички систем": "music system hifi", "soundbar": "soundbar", "персонално аудио": "personal audio",
    "слушалки": "headphones headset earphones earbuds", "звучници": "speakers bluetooth speaker",
    "радио": "radio alarm clock", "антен": "antenna", "далечински": "remote control",
    "држачи за телевизори": "tv mount bracket", "кабли": "cable cables", "микрофон": "microphone",
    "батерии": "batteries", "навигаци": "gps navigation", "автомобил": "car",
    "паметни уреди": "smart home", "камери за надзор": "security camera cctv",
    "приклучоц": "smart plug socket", "конзол": "console playstation xbox nintendo",
    "игри": "games", "games": "games", "лаптоп": "laptop notebook", "десктоп": "desktop pc computer",
    "компоненти": "pc components parts", "монитор": "monitor display screen",
    "проектор": "projector", "печатари": "printer printers", "таблет": "tablet",
    "софтвер": "software",
    "мобилни телефони": "phone phones smartphone mobile смартфон паметен телефон паметни телефони",
    "часовници": "smartwatch watch", "алки": "fitness band tracker", "фото апарат": "camera photo",
    "акциони камери": "action camera gopro", "клима": "air conditioner ac aircon",
    "инвертер": "inverter air conditioner", "топлотни пумпи": "heat pump", "греење": "heating heater",
    "вентилатор": "fan", "фрижидер": "fridge refrigerator", "замрзнувач": "freezer",
    "микробранов": "microwave", "шпорет": "cooker stove oven",
    "бојлер": "boiler water heater", "вградна": "built-in", "плотни": "hob cooktop",
    "фурни": "oven", "аспиратор": "cooker hood extractor",
    # colloquial Macedonian names the menu does not use (Cyrillic + Latin spelling)
    "машини за перење": "washing machine washer laundry перална перални peralna peralni",
    "сушење": "dryer tumble dryer сушара сушари susara susari",
    "машини за садови": "dishwasher садомијалка sadomijalka",
    "правосмукалк": "vacuum cleaner hoover", "пегл": "iron steam", "нега на облека": "iron clothes care",
    "кујнски апарати": "kitchen appliances", "кафе": "coffee", "кафемат": "coffee machine espresso",
    "блендер": "blender", "миксер": "mixer", "тостер": "toaster", "фритез": "fryer air fryer",
    "кујнски прибор": "cookware kitchenware", "квалитет на воздух": "air purifier humidifier",
    "квалитет на вода": "water filter", "чистење": "cleaning", "нега на лице": "face care",
    "нега на коса": "hair care dryer straightener", "нега на заби": "toothbrush dental",
    "нега на тело": "body care shaver epilator", "бричење": "shaver razor", "фитнес": "fitness",
    "тротинет": "e-scooter scooter", "велосипед": "bike bicycle e-bike", "дронови": "drone",
    "миленици": "pet", "двор": "garden outdoor", "косилк": "lawn mower", "ваучер": "voucher gift card",
    "галантерија": "accessories", "додатоци": "accessories", "gaming": "gaming",
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


def card_name(p):
    """The loyalty card's name, spelled as the site shows it ('haPPy'), so the
    condition text is the same everywhere: DiscountPriceName is 'HaPPy' in
    category listings but 'haPPy' in search and detail."""
    return re.sub(r"(?i)happy", "haPPy", collapse(str(p.get("DiscountPriceName") or ""))) or "haPPy"


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def en_tags(text):
    low = (text or "").lower()
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def to_int(value):
    """6130.0 -> 6130; '6.130' -> 6130; '5990,00' -> 5990; None/''/0 -> None"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value else None
    s = re.sub(r"[^\d,.]", "", str(value)).strip(".,")
    if not s:
        return None
    if re.search(r",\d{1,2}$", s):
        s = s.replace(".", "").replace(",", ".")
    elif not (re.search(r"\.\d{1,2}$", s) and s.count(".") == 1 and "," not in s):
        s = s.replace(".", "").replace(",", "")
    v = int(round(float(s)))
    return v or None


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



def html_to_text(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>|</(?:div|p|li)>", "\n", s, flags=re.I)
    s = BeautifulSoup(s, "html.parser").get_text(" ")
    return collapse(htmlmod.unescape(s))


def product_url(url_field):
    if not url_field:
        return None
    u = url_field.strip()
    if u.startswith("http"):
        return u
    u = u.lstrip("/")
    if not u.startswith("categories/"):
        u = "categories/" + u
    return BASE + "/" + u


def norm_slug(path):
    """'/Gaming_tastaturi.nspx/' -> 'Gaming_tastaturi' (case kept: the server is case-sensitive)."""
    p = unquote(path or "").strip().strip("/")
    if p.lower().endswith(".nspx"):
        p = p[:-5]
    return p


# --------------------------------------------------------------------------- client

class Neptun:
    def __init__(self, pace=PACE_S, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "mk,en;q=0.8",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": BASE + "/",
        })
        self.pace = pace
        self.verbose = verbose
        self._last = 0.0
        self._nodes = None
        self._cat_names = {}
        self.requests_made = 0

    # ---- transport
    @staticmethod
    def _block_evidence(r):
        title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
        code = re.search(r"error code: (\d{4})", r.text or "")
        return (f"HTTP {r.status_code} from {r.url}; server={r.headers.get('server')} "
                f"cf-ray={r.headers.get('cf-ray')} cf-mitigated={r.headers.get('cf-mitigated')} "
                f"title={collapse(title.group(1)) if title else None!r}"
                + (f" cloudflare-error={code.group(1)}" if code else ""))

    def _check_block(self, r):
        if r.headers.get("cf-mitigated"):
            raise Blocked("Cloudflare challenge: " + self._block_evidence(r))
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            return
        head = (r.text or "")[:20000]
        if r.status_code in (401, 403):
            raise Blocked(self._block_evidence(r))
        if r.status_code in (429, 503) and CHALLENGE_RE.search(head):
            raise Blocked("challenge page: " + self._block_evidence(r))
        # Real Neptun pages are large AngularJS shells; an interstitial is small and carries markers.
        if CHALLENGE_RE.search(head) and len(r.content) < 60000 and "ng-controller" not in head:
            raise Blocked("challenge/block page: " + self._block_evidence(r))

    def _request(self, method, url, expect_json=False, **kw):
        url = url if url.startswith("http") else BASE + "/" + url.lstrip("/")
        kw.setdefault("timeout", 45)
        delay = 2.0
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
                    raise RuntimeError(f"{method} {url}: network error after {attempt} attempts: {e}")
                log(f"  network error ({e.__class__.__name__}), retry {attempt} in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                log(f"  {method} {url} -> {r.status_code} {len(r.content)}B "
                    f"{r.elapsed.total_seconds():.2f}s")
            self._check_block(r)
            if r.status_code in (429, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("retry-after")
                sleep_for = max(float(ra), delay) if ra and ra.isdigit() else delay
                warn(f"HTTP {r.status_code} from {url}, retrying in {min(sleep_for, 60):.0f}s")
                time.sleep(min(sleep_for, 60))
                delay *= 2
                continue
            if expect_json:
                ctype = r.headers.get("content-type", "")
                if r.status_code != 200 or "json" not in ctype:
                    raise RuntimeError(f"{method} {url}: expected JSON, got HTTP {r.status_code} "
                                       f"({ctype}); body starts {r.text[:120]!r}")
                return r.json()
            if r.status_code != 200:
                raise RuntimeError(f"{method} {url}: HTTP {r.status_code}")
            return r
        if r is not None and r.status_code == 429:
            raise Blocked(f"HTTP 429 (rate limited) from {url} after {MAX_RETRIES} attempts; "
                          f"retry-after={r.headers.get('retry-after')} cf-ray={r.headers.get('cf-ray')}")
        raise RuntimeError(f"{method} {url}: still HTTP {r.status_code if r is not None else '?'} "
                           f"after {MAX_RETRIES} attempts (store down?)")

    def post(self, path, body):
        return self._request("POST", path, expect_json=True, json=body)

    def get_page(self, url):
        return self._request("GET", url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    # ---- category tree (home page mega-menu)
    def nodes(self):
        """Flat list of menu nodes: 12 departments > categories > sub-categories (> a few lvl4)."""
        if self._nodes is not None:
            return self._nodes
        html = self.get_page(BASE + "/").text
        main = re.search(r'<nav id="mainNMenu">.*?</nav>', html, re.S)
        if not main:
            raise RuntimeError("home page has no #mainNMenu mega-menu (layout change?)")
        soup = BeautifulSoup(main.group(0), "html.parser")
        mob = re.search(r'<nav id="menu">.*?</nav>', html, re.S)
        dept_urls = {}
        if mob:
            msoup = BeautifulSoup(mob.group(0), "html.parser")
            root = msoup.find("ul")
            prod = root.find("li") if root else None   # "Производи" -> departments
            sub = prod.find("ul") if prod else None
            for li in (sub.find_all("li", recursive=False) if sub else []):
                a = li.find("a", recursive=False)
                if a is not None and a.get("href"):
                    dept_urls[collapse(a.get_text(" ")).lower()] = a["href"]

        nodes = []

        def node(name, href, level, parent, dept, kw):
            slug = norm_slug(urlparse(href).path) if href else None
            n = {"id": None, "slug": slug, "name": name, "path": None,
                 "url": (BASE + "/" + href.lstrip("/")) if href and not href.startswith("http") else href,
                 "parent": parent, "count": None, "level": level, "dept": dept,
                 "kw": kw, "children": []}
            nodes.append(n)
            return n

        lvl1 = soup.select_one("ul.lvl1")
        for dli in (lvl1.find_all("li", recursive=False) if lvl1 else []):
            da = dli.find("a", recursive=False)
            if da is None:
                continue
            dname = collapse(da.get_text(" "))
            d = node(dname, dept_urls.get(dname.lower()), 1, None, dname, " ".join(dli.get("class") or []))
            d["path"] = dname
            if not d["slug"]:
                d["slug"] = re.sub(r"[^a-z0-9]+", "_", translit(dname)).strip("_")
            holder = dli.find("div", class_="menu-holder")
            ul2 = holder.find("ul", class_="lvl2") if holder else None
            order2, order3 = [], []
            for li2 in (ul2.find_all("li", recursive=False) if ul2 else []):
                a2 = li2.find("a", recursive=False)
                if a2 is None:
                    continue
                n2 = node(collapse(a2.get_text(" ")), a2.get("href"), 2, d, dname,
                          " ".join(li2.get("class") or []))
                order2.append(n2)
                u3 = li2.find("ul", recursive=False)
                for li3 in (u3.find_all("li", recursive=False) if u3 else []):
                    a3 = li3.find("a", recursive=False)
                    if a3 is None:
                        continue
                    n3 = node(collapse(a3.get_text(" ")), a3.get("href"), 3, n2, dname,
                              " ".join(li3.get("class") or []))
                    order3.append(n3)
                    u4 = li3.find("ul", recursive=False)
                    for li4 in (u4.find_all("li", recursive=False) if u4 else []):
                        a4 = li4.find("a", recursive=False)
                        if a4 is not None:
                            node(collapse(a4.get_text(" ")), a4.get("href"), 4, n3, dname,
                                 " ".join(li4.get("class") or []))
            # Each department's brand links carry ?categories=<lvl2 ids>_<lvl3 ids> in menu
            # order, with blanks for CMS links (redirects / landing pages) that are not categories.
            ids = None
            for ma in dli.select('a[href*="manufacturer-products"]'):
                q = parse_qs(urlparse(htmlmod.unescape(ma["href"])).query, keep_blank_values=True)
                if "categories" in q:
                    ids = q["categories"][0].split("_")
                    break
            if ids is not None:
                ordered = order2 + order3
                if len(ids) == len(ordered):
                    for n, cid in zip(ordered, ids):
                        n["id"] = cid if cid.isdigit() else ""
                else:
                    warn(f"menu id list for {dname!r} has {len(ids)} ids for {len(ordered)} links; "
                         "ids left unresolved (resolved per page on `list`)")

        # Drop CMS/service links (blank id) that have no category below them, deepest first.
        # Departments stay: each has a landing category page (the services department's page
        # lists vouchers and the haPPy card although all its menu links are CMS pages).
        for n in nodes:
            if n["parent"] is not None:
                n["parent"]["children"].append(n)
        dropped = set()
        for n in sorted(nodes, key=lambda x: -x["level"]):
            n["children"] = [c for c in n["children"] if id(c) not in dropped]
            if not n["children"] and n["id"] == "":
                dropped.add(id(n))
        keep = [n for n in nodes if id(n) not in dropped]
        for n in keep:
            if n["id"] == "":
                n["id"] = None
            if n["level"] > 1:
                n["path"] = f"{n['parent']['path']} > {n['name']}"
        if not any(n["level"] == 2 for n in keep):
            raise RuntimeError("no categories found in the home page menu (layout change?)")
        self._nodes = keep
        return keep

    @staticmethod
    def _ref(n):
        return n["id"] if n["id"] else n["slug"]

    def public_node(self, n):
        return {"id": n["id"], "slug": n["slug"], "name": n["name"], "path": n["path"],
                "url": n["url"], "parent": self._ref(n["parent"]) if n["parent"] else None,
                "count": n["count"]}

    def categories(self, grep=None, counts=False):
        nodes = self.nodes()
        if grep:
            rx = re.compile(grep, re.I)
            sel = [n for n in nodes
                   if any(rx.search(f) for f in (n["name"], n["path"], n["slug"], translit(n["name"]),
                                                 translit(n["path"]), n["kw"], en_tags(n["name"]),
                                                 en_tags(n["path"])) if f)]
        else:
            sel = list(nodes)
        if counts:
            self._fill_counts(sel)
        return [self.public_node(n) for n in sel]

    def _fill_counts(self, sel):
        """One request per node: a 1-item listing call for leaves with a known id, the category page
        (exact NumberOfProducts, id, ShowAllProducts flag) for parents and id-less nodes."""
        todo = sorted(sel, key=lambda n: n["level"])
        budget = MAX_COUNT_REQUESTS
        skipped = 0
        log(f"  fetching counts for {len(todo)} categories (budget {budget} requests)")
        for n in todo:
            if budget <= 0:
                skipped += 1
                continue
            if n["id"] and not n["children"]:
                n["count"] = self.listing_total(int(n["id"]), False)
                budget -= 1
            elif n["url"]:
                budget -= 1
                try:
                    pg = self.category_page(n["url"])
                except (NotFound, RuntimeError) as e:
                    log(f"  no count for {n['path']}: {e}")
                    continue
                if pg is None:
                    continue
                n["id"] = n["id"] or str(pg["id"])
                n["count"] = pg["count"]
                if not pg["count"] and n["children"] and budget > 0:
                    n["count"] = self.listing_total(pg["id"], True)
                    budget -= 1
        if skipped:
            warn(f"--counts budget exhausted; {skipped} categories left without a count "
                 "(narrow --grep to get them)")

    # ---- category pages and resolution
    def category_page(self, url):
        """GET a category page -> dict(id, name, count, show_all, model, cd, url) or None if the
        page is not a product category (CMS/landing page)."""
        r = self.get_page(url)
        html = r.text
        m = re.search(r'data-categorydetails="([^"]*)"', html)
        if not m:
            return None
        cd = json.loads(htmlmod.unescape(m.group(1)))
        m2 = re.search(r'data-initialSearchModel="([^"]*)"', html, re.I)
        ism = json.loads(htmlmod.unescape(m2.group(1))) if m2 else {}
        final_q = {k.lower(): v for k, v in parse_qs(urlparse(r.url).query).items()}
        model = {
            "Manufacturers": ism.get("Manufacturers") or [],
            "BoolFeatures": ism.get("BoolFeatures") or [],
            "DropdownFeatures": ism.get("DropdownFeatures") or [],
            "MultiSelectFeatures": ism.get("MultiSelectFeatures") or [],
            # the page always fills PriceRange with the category bounds; keep it only when the URL
            # asked for a price range
            "PriceRange": ism.get("PriceRange") if "pricerange" in final_q else None,
        }
        filtered = bool(model["Manufacturers"] or model["BoolFeatures"] or model["DropdownFeatures"]
                        or model["MultiSelectFeatures"] or model["PriceRange"])
        return {"id": int(cd["Id"]), "name": cd.get("Name"), "count": cd.get("NumberOfProducts"),
                "show_all": bool(ism.get("ShowAllProducts")), "model": model, "filtered": filtered,
                "cd": cd, "url": r.url}

    def _node_lookup(self, ref):
        nodes = self.nodes()
        low = ref.lower()
        if low.isdigit():
            return [n for n in nodes if n["id"] == low]
        slug = norm_slug(urlparse(ref).path if ref.lower().startswith("http") else ref).lower()
        hit = [n for n in nodes if n["slug"] and n["slug"].lower() == slug]
        if hit:
            return hit
        name = collapse(ref).lower()
        return [n for n in nodes if n["name"].lower() == name]

    def resolve(self, ref):
        """-> target {label, scopes: [{id, name, show_all, model, count, cd}], node}"""
        raw = ref
        ref = (ref or "").strip()
        if not ref:
            raise Usage("empty category reference")
        if "/categories/" in ref:
            raise Usage(f"{raw!r} is a product URL; use `detail`")
        is_url = ref.lower().startswith(("http://", "https://", "/")) or ".nspx" in ref.lower()
        if ref.isdigit():
            hits = self._node_lookup(ref)
            if not hits:
                return self._resolve_bare_id(int(ref))
            return self._resolve_node(hits[0])
        if is_url:
            u = urlparse(ref if ref.lower().startswith("http") else BASE + "/" + ref.lstrip("/"))
            host = (u.netloc or "www.neptun.mk").lower()
            if host not in ("www.neptun.mk", "neptun.mk"):
                raise Usage(f"{raw!r} is not a neptun.mk URL")
            slug = norm_slug(u.path)
            if not slug:
                raise Usage(f"no category path in {raw!r}")
            url = f"{BASE}/{slug}.nspx" + (f"?{u.query}" if u.query else "")
            pg = self.category_page(url)
            if pg is None:
                # Case-sensitive slugs: a wrong case gives an empty 200. Retry with the menu's case.
                hits = self._node_lookup(slug)
                if hits and hits[0]["slug"] != slug:
                    url = f"{BASE}/{hits[0]['slug']}.nspx" + (f"?{u.query}" if u.query else "")
                    pg = self.category_page(url)
                if pg is None:
                    if hits and hits[0]["children"]:
                        return self._group_target(hits[0])
                    raise NotFound(f"{raw!r} is not a product category page (unknown slug, wrong "
                                   "case, or a CMS page); run `categories --grep ...`")
            return self._page_target(pg)
        hits = self._node_lookup(ref)
        if len(hits) > 1 and len({h["id"] or h["slug"] for h in hits}) > 1:
            raise Usage(f"category name {raw!r} is ambiguous: "
                        + "; ".join(f"{self._ref(h)} ({h['path']})" for h in hits))
        if hits:
            return self._resolve_node(hits[0])
        # Not in the menu: maybe a hidden category slug.
        pg = self.category_page(f"{BASE}/{norm_slug(ref)}.nspx")
        if pg is None:
            raise NotFound(f"unknown category {raw!r}; run `categories --grep ...` for ids/slugs")
        return self._page_target(pg)

    def _resolve_node(self, n):
        pg = self.category_page(n["url"]) if n["url"] else None
        if pg is None:
            if n["children"]:
                return self._group_target(n)
            raise NotFound(f"menu entry {n['path']!r} ({n['url']}) is not a product category page")
        t = self._page_target(pg)
        t["label"] = n["path"]
        return t

    def _page_target(self, pg):
        show_all = pg["show_all"]
        note = None
        kids = pg["cd"].get("WebChildren") or []
        if not show_all and not pg["count"] and kids and not pg["filtered"]:
            # The page shows only sub-category tiles (0 products of its own): list the children.
            show_all = True
            note = "the category page shows only sub-category tiles; listing its sub-categories' products"
        scope = {"id": pg["id"], "name": pg["name"], "show_all": show_all, "model": pg["model"],
                 "count": pg["count"], "cd": pg["cd"], "filtered": pg["filtered"]}
        label = pg["name"] or str(pg["id"])
        if pg["filtered"]:
            label += " (URL filters applied)"
        return {"label": label, "scopes": [scope], "note": note}

    def _group_target(self, n):
        """A menu entry without a category page (CMS grouping): its children, one scope each."""
        scopes = []
        for c in n["children"]:
            if c["id"]:
                scopes.append({"id": int(c["id"]), "name": c["name"], "show_all": True,
                               "model": {}, "count": None, "cd": None, "filtered": False})
            elif c["url"]:
                pg = self.category_page(c["url"])
                if pg:
                    scopes.append(self._page_target(pg)["scopes"][0])
        if not scopes:
            raise NotFound(f"menu entry {n['path']!r} has no product categories")
        return {"label": n["path"], "scopes": scopes,
                "note": f"{n['path']!r} is a menu grouping without its own page; listing its "
                        f"{len(scopes)} sub-categories"}

    def _resolve_bare_id(self, cid):
        """An id not in the menu (hidden, lvl4 or adult category): no page URL, so the
        ShowAllProducts flag is unknown. Own products; children's only when it has none."""
        own = self.listing_total(cid, False)
        allp = self.listing_total(cid, True)
        if not own and not allp:
            raise NotFound(f"unknown category id {cid}: not in the site menu and the listing "
                           "endpoint returned 0 products")
        show_all = own == 0
        note = None
        if own and allp > own:
            note = (f"category {cid} has {allp} products including sub-categories; listing its own "
                    f"{own}. Pass the category URL to mirror exactly what the site page shows.")
        return {"label": f"category {cid}", "note": note,
                "scopes": [{"id": cid, "name": None, "show_all": show_all, "model": {},
                            "count": None, "cd": None, "filtered": False}]}

    # ---- listing
    def listing_total(self, cid, show_all, **filters):
        data = self._load(cid, show_all, 1, 1, filters)
        return int(((data.get("Batch") or {}).get("Config") or {}).get("TotalItems") or 0)

    def _load(self, cid, show_all, page, per_page, model):
        body = {"CategoryId": cid, "Sort": DEFAULT_SORT, "Manufacturers": [], "Recomended": False,
                "PriceRange": None, "BoolFeatures": [], "DropdownFeatures": [],
                "MultiSelectFeatures": [], "ShowAllProducts": bool(show_all),
                "ItemsPerPage": per_page, "CurrentPage": page}
        for k, v in (model or {}).items():
            if v:
                body[k] = v
        data = self.post("NeptunCategories/LoadProductsForCategory", {"model": body})
        if not isinstance(data, dict) or "Batch" not in data:
            raise RuntimeError("unexpected LoadProductsForCategory payload (keys "
                               f"{list(data)[:10] if isinstance(data, dict) else type(data)}); layout change?")
        return data

    def walk(self, scope, limit=None, extra=None, label=""):
        """Every page of one category scope -> (items, total). Stops at TotalItems (an out-of-range
        page silently returns page 1 again, so never loop until empty)."""
        model = dict(scope.get("model") or {})
        for k, v in (extra or {}).items():
            if v:
                model[k] = v
        out, seen, page, total, first = [], set(), 1, 0, None
        size = min(PAGE_SIZE, limit) if limit else PAGE_SIZE
        while True:
            data = self._load(scope["id"], scope["show_all"], page, size, model)
            batch = data.get("Batch") or {}
            cfg = batch.get("Config") or {}
            items = batch.get("Items") or []
            total = int(cfg.get("TotalItems") or 0)
            per = int(cfg.get("ItemsPerPage") or size)   # the server may clamp the page size
            ids = [p.get("Id") for p in items]
            if page == 1:
                first = ids
                if total > per and not limit:
                    log(f"  {label or scope['id']}: {total} products, {-(-total // per)} page(s) of {per}")
            elif ids and ids == first:
                break
            for p in items:
                if p.get("Id") not in seen:
                    seen.add(p.get("Id"))
                    out.append(p)
            if limit and len(out) >= limit:
                return out[:limit], total
            if not items or page * per >= total:
                break
            page += 1
        if len(out) < total:
            warn(f"{label or scope['id']}: collected {len(out)} unique products but the site reports "
                 f"TotalItems={total} (catalogue changed mid-walk?)")
        return out, total

    # ---- filters and facets
    @staticmethod
    def _features(cd):
        out = {}
        for g in (cd or {}).get("FeatureGroups") or []:
            for f in g.get("Features") or []:
                out[int(f["Id"])] = dict(f, Group=g.get("Title"))
        return out

    def parse_filters(self, tokens, target):
        """-> dict(Manufacturers, PriceRange, MultiSelectFeatures, DropdownFeatures, BoolFeatures,
        subs). Several tokens AND; commas OR inside a token."""
        if len(target["scopes"]) != 1 or target["scopes"][0]["cd"] is None:
            raise Usage("--filter needs a single category with its own page (pass its URL or slug)")
        cd = target["scopes"][0]["cd"]
        feats = self._features(cd)
        brands = {int(m["Key"]): collapse(m.get("Value")) for m in cd.get("Manufacturers") or []}
        kids = {int(c["Id"]): collapse(c.get("Name")) for c in cd.get("WebChildren") or []}
        out = {"Manufacturers": None, "PriceRange": None, "subs": None, "features": {}}
        lo = hi = None
        for tok in tokens or []:
            m = re.fullmatch(r"\s*([^=]+?)\s*=\s*(.+?)\s*", tok)
            if not m:
                raise Usage(f"bad --filter {tok!r}; use brand=<id|name>, price=MIN-MAX, sub=<id|name> "
                            "or f<featureId>=<value> (see `facets`)")
            key, val = m.group(1).lower(), m.group(2)
            if key == "price":
                pm = re.fullmatch(r"(\d*)\s*-\s*(\d*)", val)
                if not pm or not (pm.group(1) or pm.group(2)):
                    raise Usage(f"bad price filter {val!r}; use price=MIN-MAX, price=MIN- or price=-MAX")
                a, b = (int(x) if x else None for x in pm.groups())
                lo = a if a is not None and (lo is None or a > lo) else lo
                hi = b if b is not None and (hi is None or b < hi) else hi
                continue
            vals = [v.strip() for v in val.split(",") if v.strip()]
            if key in ("brand", "manufacturer"):
                ids = set()
                for v in vals:
                    if v.isdigit():
                        ids.add(int(v))
                        continue
                    hit = [k for k, nm in brands.items()
                           if nm.lower() == v.lower() or translit(nm) == translit(v)]
                    if not hit:
                        raise NotFound(f"no brand {v!r} in {target['label']}; brands: "
                                       + ", ".join(sorted(brands.values())))
                    ids.update(hit)
                out["Manufacturers"] = ids if out["Manufacturers"] is None else out["Manufacturers"] & ids
                if not out["Manufacturers"]:
                    raise Usage("the brand= filters AND to an empty set")
            elif key in ("sub", "subcategory"):
                ids = set()
                for v in vals:
                    if v.isdigit():
                        ids.add(int(v))
                        continue
                    hit = [k for k, nm in kids.items() if nm.lower() == v.lower()]
                    if not hit:
                        raise NotFound(f"no sub-category {v!r} under {target['label']}; see `facets`")
                    ids.update(hit)
                bad = ids - set(kids)
                if bad:
                    raise NotFound(f"sub-category id(s) {sorted(bad)} are not children of "
                                   f"{target['label']} (children: {sorted(kids)})")
                out["subs"] = ids if out["subs"] is None else out["subs"] & ids
                if not out["subs"]:
                    raise Usage("the sub= filters AND to an empty set")
            else:
                fm = re.fullmatch(r"f?(\d+)", key)
                fid = int(fm.group(1)) if fm else next(
                    (i for i, f in feats.items() if collapse(f.get("Title")).lower() == key), None)
                if fid is None or fid not in feats:
                    raise NotFound(f"no feature {m.group(1)!r} in {target['label']}; see `facets` "
                                   f"(features: {', '.join(f'f{i}' for i in feats) or 'none'})")
                f = feats[fid]
                allowed = {}
                for dv in f.get("DropDownValues") or []:
                    for k in ("ValueEn", "Value"):
                        if dv.get(k) not in (None, ""):
                            allowed[str(dv[k]).lower()] = dv.get("ValueEn") or dv.get("Value")
                chosen = set()
                if val.strip().lower() in allowed:          # a value that itself contains a comma
                    vals = [val.strip()]
                for v in vals:
                    if f.get("ValueType") == "y_n":
                        yn = {"y": "Y", "yes": "Y", "да": "Y", "n": "N", "no": "N", "не": "N"}.get(v.lower())
                        if not yn:
                            raise Usage(f"f{fid} is a yes/no feature; use f{fid}=Y or f{fid}=N")
                        chosen.add(yn)
                    elif allowed:
                        if v.lower() not in allowed:
                            raise NotFound(f"f{fid} ({f.get('Title')}) has no value {v!r}; values: "
                                           + ", ".join(sorted(set(allowed.values()))))
                        chosen.add(allowed[v.lower()])
                    else:
                        chosen.add(v)
                prev = out["features"].get(fid)
                out["features"][fid] = chosen if prev is None else prev & chosen
                if not out["features"][fid]:
                    raise Usage(f"the f{fid} filters AND to an empty set")
        if lo is not None or hi is not None:
            if lo is not None and hi is not None and lo > hi:
                raise Usage("price filters AND to an empty range")
            out["PriceRange"] = {"MinPriceValue": lo if lo is not None else 0,
                                 "MaxPriceValue": hi if hi is not None else 100000000}
        model = {"Manufacturers": sorted(out["Manufacturers"]) if out["Manufacturers"] else None,
                 "PriceRange": out["PriceRange"], "MultiSelectFeatures": [], "DropdownFeatures": [],
                 "BoolFeatures": []}
        for fid, values in out["features"].items():
            vt = feats[fid].get("ValueType")
            key = {"multi_select": "MultiSelectFeatures", "dropdown": "DropdownFeatures"}.get(vt, "BoolFeatures")
            for v in sorted(values):
                entry = {"Id": fid, "FilterValue": v}
                if vt == "text":
                    entry["Type"] = "text"
                model[key].append(entry)
        return model, out["subs"], ((lo, hi) if (lo is not None or hi is not None) else None)

    def _apply_filters(self, target, tokens):
        """-> (list of (scope, extra-model) to walk, (lo, hi) price bounds or None)."""
        if not tokens:
            return [(s, None) for s in target["scopes"]], None
        extra, subs, bounds = self.parse_filters(tokens, target)
        base = target["scopes"][0]
        for k in ("Manufacturers", "PriceRange"):
            if base["model"].get(k) and extra.get(k):
                log(f"  note: --filter {k} overrides the URL's own {k}")
        for k in ("MultiSelectFeatures", "DropdownFeatures", "BoolFeatures"):
            if base["model"].get(k):
                extra[k] = list(base["model"][k]) + list(extra.get(k) or [])
        if subs:
            return [({"id": sid, "name": None, "show_all": True, "model": {}, "count": None,
                      "cd": None, "filtered": False}, extra) for sid in sorted(subs)], bounds
        return [(base, extra)], bounds

    def facets(self, ref):
        target = self.resolve(ref)
        if len(target["scopes"]) != 1 or target["scopes"][0]["cd"] is None:
            raise Usage("facets needs a single category with its own page (pass its URL or slug)")
        scope = target["scopes"][0]
        cd = scope["cd"]
        items, total = self.walk(scope, label=target["label"])
        if not total:
            raise NotFound(f"category {target['label']!r} returned 0 products")
        out = []
        brands = {}
        lo = hi = None
        for p in items:
            man = p.get("Manufacturer") or {}
            if man.get("Id") is not None:
                b = brands.setdefault(int(man["Id"]), [collapse(man.get("Name")), 0])
                b[1] += 1
            price = self.prices(p)[0]
            if price is not None:
                lo = price if lo is None else min(lo, price)
                hi = price if hi is None else max(hi, price)
        out += [{"name": "brand", "value": v[0], "count": v[1], "token": f"brand={k}"}
                for k, v in sorted(brands.items(), key=lambda x: (-x[1][1], x[1][0]))]
        budget = MAX_FACET_REQUESTS
        for c in cd.get("WebChildren") or []:
            cnt = None
            if budget > 0:
                cnt = self.listing_total(int(c["Id"]), True)
                budget -= 1
            out.append({"name": "subcategory", "value": collapse(c.get("Name")), "count": cnt,
                        "token": f"sub={c['Id']}"})
        skipped = 0
        for fid, f in self._features(cd).items():
            values = [dv.get("ValueEn") or dv.get("Value") for dv in f.get("DropDownValues") or []]
            if f.get("ValueType") == "y_n":
                values = ["Y", "N"]
            for v in values:
                if v in (None, ""):
                    continue
                cnt = None
                if budget > 0:
                    model = self.parse_filters([f"f{fid}={v}"], target)[0]
                    model = {k: x for k, x in model.items() if x}
                    for k in ("MultiSelectFeatures", "DropdownFeatures", "BoolFeatures"):
                        if scope["model"].get(k):
                            model[k] = list(scope["model"][k]) + list(model.get(k) or [])
                    cnt = self.listing_total(scope["id"], scope["show_all"],
                                             **dict(scope["model"], **model))
                    budget -= 1
                else:
                    skipped += 1
                out.append({"name": collapse(f.get("Title")) or f"f{fid}", "value": str(v),
                            "count": cnt, "token": f"f{fid}={v}"})
        if skipped:
            warn(f"facet count budget ({MAX_FACET_REQUESTS} requests) exhausted; {skipped} values have "
                 "count null")
        if lo is not None:
            out.append({"name": "price", "value": f"{lo}-{hi}", "count": total,
                        "token": f"price={lo}-{hi}"})
        nfeat = sum(1 for r in out if r["name"] not in ("brand", "subcategory", "price"))
        log(f"  {target['label']}: {total} products, {len(brands)} brands, "
            f"{len(cd.get('WebChildren') or [])} sub-categories, {nfeat} feature values")
        return out

    # ---- records
    @staticmethod
    def member_discount(p):
        """True when DiscountPrice is the haPPy loyalty-card price (type 3/4 or named haPPy)."""
        try:
            dtype = int(p.get("DiscountPriceType") or 0)
        except (TypeError, ValueError):
            dtype = 0
        return dtype in MEMBER_PRICE_TYPES or "happy" in str(p.get("DiscountPriceName") or "").lower()

    @staticmethod
    def promotion_windows(d):
        """GetProduct.Promotions -> [{id, name, valid_from, valid_to}] (ISO 8601, Skopje offset).
        ValidFrom/ValidTo are .NET '/Date(ms)/' instants; names often repeat them as text
        ('... HAPPY WEEKS #2 ... 21.09-04.10.2026' -> valid_to 2026-10-04T23:59:00+02:00)."""
        return [{"id": p.get("Id"),
                 "name": collapse(p.get("CustomPromotionName") or p.get("PromotionName")) or None,
                 "valid_from": skopje_iso(p.get("ValidFrom")), "valid_to": skopje_iso(p.get("ValidTo"))}
                for p in d.get("Promotions") or []]

    @classmethod
    def price_windows(cls, d):
        """GetProduct model -> {"price_valid_until", ["member_price_valid_until"]}; a key the
        model cannot tell is left out (unknown), never null.

        DiscountPrice belongs to the promotion GetProduct names in PromotionId (the haPPy
        campaign, e.g. 4343 / -3282 'HAPPY WEEKS #2 ... 21.09-04.10.2026'), WebshopDiscountPrice
        to WebPromotionId; the window is that promotion's ValidTo. Category listings and search
        carry neither the ids nor the promotions (PromotionId 0, PromotionStart/End 0001-01-01).

        null (standing) only on positive evidence: price_mkd is RegularPrice ("Редовна цена"), or
        a haPPy member price with no promotion attached at all (PromotionId 0, Promotions empty).
        The latter is DiscountPriceType 4: 17 of 19 sampled on 2026-10-04 (new LG TVs, Galaxy
        A27, Honor 600 Lite, laptops) carried no promotion, no eyecatcher and placeholder
        PromotionStart/End, and the page shows the haPPy price with no campaign or end. Left out:
        a named promotion that is not listed or has a placeholder ValidTo; PromotionId 0 while
        the product has promotions (the shop does not say which one sets the price: TCL 55T69C,
        Honor 600 Pro); an online or public discount price whose promotion is not named (no
        haPPy-style evidence for those, none seen in the full scan); a price from any other
        source (e.g. only ActualPrice). extra.promotion_windows lists every promotion's dates."""
        listed = [p for p in d.get("Promotions") or [] if isinstance(p, dict)]
        promos = {p.get("Id"): p for p in listed if p.get("Id")}
        unknown = object()

        def end(pid, unattached_is_standing=False):
            pr = promos.get(pid) if pid else None
            if pr:   # a placeholder / unreadable ValidTo cannot tell
                return skopje_iso(pr.get("ValidTo")) or unknown
            if unattached_is_standing and not pid and not listed:
                return None
            return unknown

        price, _reg, member = cls.prices(d)
        regular, web = to_int(d.get("RegularPrice")), to_int(d.get("WebshopDiscountPrice"))
        disc = to_int(d.get("DiscountPrice"))
        if price is None:
            until = unknown
        elif price == regular:
            until = None
        elif web and price == web:
            until = end(d.get("WebPromotionId"))
        elif disc and price == disc and not cls.member_discount(d):
            until = end(d.get("PromotionId"))
        else:   # a discount from some other source
            until = unknown
        out = {} if until is unknown else {"price_valid_until": until}
        if member:
            m_until = end(d.get("PromotionId"), unattached_is_standing=True)
            if m_until is not unknown:
                out["member_price_valid_until"] = m_until
        return out

    @staticmethod
    def prices(p):
        """-> (price_mkd, regular_price_mkd, member_price_mkd).

        RegularPrice = "Редовна цена"; WebshopDiscountPrice = "Онлајн цена" (0 = none);
        DiscountPrice with DiscountPriceType 3/4 (name "haPPy") = loyalty-card price shown next to
        the regular price when 0 < DiscountPrice < RegularPrice; ActualPrice = the lowest of these.
        price_mkd excludes the card price (the card costs 150 MKD online), member_price_mkd keeps it."""
        regular = to_int(p.get("RegularPrice"))
        web = to_int(p.get("WebshopDiscountPrice"))
        disc = to_int(p.get("DiscountPrice"))
        member = Neptun.member_discount(p)
        cands = [x for x in (regular, web) if x]
        if disc and regular and disc < regular and not member:
            cands.append(disc)
        if not cands:
            actual = to_int(p.get("ActualPrice"))
            return actual, None, None
        price = min(cands)
        reg = regular if regular and regular > price else None
        member_price = disc if member and disc and disc < price else None
        return price, reg, member_price

    @staticmethod
    def stock_of(p):
        if p.get("Active") is False:
            return False, INACTIVE_NOTE
        if p.get("Preorder"):
            return False, "preorder"
        avail = p.get("AvailableWebshop")
        if avail is None:
            return None, None
        return (True, "available for online order") if avail else (False, "not available online")

    def listing_record(self, p, category=None):
        price, reg, member = self.prices(p)
        in_stock, note = self.stock_of(p)
        man = p.get("Manufacturer")
        brand = man.get("Name") if isinstance(man, dict) else man
        cat = p.get("Category")
        bc = collapse(p.get("Barcode"))
        rec = {
            "store": STORE,
            "id": str(p["Id"]),
            "sku": collapse(p.get("CodeNumber")) or None,
            "title": collapse(p.get("Title")),
            "url": product_url(p.get("Url")),
            "brand": collapse(brand) or None,
            "price_mkd": price,
            "regular_price_mkd": reg,
            "in_stock": in_stock,
            "stock_note": note,
            "category": (cat.get("Name") if isinstance(cat, dict) else None) or category,
            "ean": bc if re.fullmatch(r"\d{8,14}", bc) else None,
            "member_price_mkd": member,
            "member_price_condition": f"{card_name(p)} loyalty card, {MEMBER_CARD_TERMS}" if member else None,
        }
        # Listings carry no promotion dates: a RegularPrice is standing (null); for an online or
        # discount price_mkd and for member prices the window is only in detail (key left out).
        if price is not None and price == to_int(p.get("RegularPrice")):
            rec["price_valid_until"] = None
        return rec

    def search_record(self, p, cat_names):
        rec = self.listing_record(p)
        # Search hits carry the listing title in ShortTitle; Title is the ERP name, which often
        # ends in the manufacturer part number in parentheses: "... (SM-F966BZKCEUC) JETBLACK".
        rec["title"] = collapse(p.get("ShortTitle") or p.get("Title"))
        rec["url"] = product_url(p.get("Url") or p.get("Link"))
        rec["category"] = cat_names.get(str(p.get("CategoryId"))) or None
        mpn = re.search(r"\(([A-Z0-9][A-Z0-9./+-]{4,})\)", p.get("Title") or "")
        if mpn and re.search(r"\d", mpn.group(1)) and re.search(r"[A-Z]", mpn.group(1)):
            rec["mpn"] = mpn.group(1)
        return rec

    # ---- commands
    def list_category(self, ref, in_stock=False, limit=None, filters=None):
        target = self.resolve(ref)
        if target.get("note"):
            log(f"  note: {target['note']}")
        runs, bounds = self._apply_filters(target, filters)
        # The server's PriceRange keeps a product when RegularPrice >= min and ActualPrice (the
        # haPPy price when lower) <= max, i.e. any overlap. --filter price= is applied exactly to
        # price_mkd on top of it.
        exact = (lambda r: (bounds[0] is None or (r["price_mkd"] or 0) >= bounds[0])
                 and (bounds[1] is None or (r["price_mkd"] or 0) <= bounds[1])) if bounds else None
        recs, seen, total_all, dropped = [], set(), 0, 0
        for scope, extra in runs:
            left = (limit - len(recs)) if limit else None
            if left is not None and left <= 0:
                break
            items, total = self.walk(scope, limit=left if not (in_stock or exact) else None,
                                     extra=extra, label=target["label"])
            total_all += total
            for p in items:
                if str(p["Id"]) in seen:
                    continue
                seen.add(str(p["Id"]))
                r = self.listing_record(p, scope.get("name"))
                if exact and not exact(r):
                    dropped += 1
                    continue
                if in_stock and not r["in_stock"]:
                    continue
                recs.append(r)
            if (len(runs) == 1 and not filters and not target.get("note") and not limit
                    and scope.get("count") is not None and scope["count"] != total):
                log(f"  note: the category page says {scope['count']} products, the API {total}")
        if limit:
            recs = recs[:limit]
        if not total_all:
            if filters or any(s.get("filtered") for s in target["scopes"]):
                log(f"  {target['label']}: no products match the filters")
                return []
            raise NotFound(f"category {target['label']!r} returned 0 products (empty category, "
                           "or a soft block / layout change)")
        if dropped:
            rng = (f"{bounds[0]}-{bounds[1]}" if bounds[0] is not None and bounds[1] is not None
                   else f">= {bounds[0]}" if bounds[0] is not None else f"<= {bounds[1]}")
            log(f"  {dropped} server hit(s) dropped: price_mkd not {rng} MKD (the server range also "
                "matches when only the haPPy member price falls inside; see member_price_mkd)")
        log(f"  {target['label']}: site reports {total_all}{' (filtered)' if filters else ''}, "
            f"returning {len(recs)}{' in stock' if in_stock else ''}")
        return recs

    def _category_name(self, cid):
        if cid not in self._cat_names:
            name = None
            try:
                data = self._load(int(cid), False, 1, 1, None)
                for p in (data.get("Batch") or {}).get("Items") or []:
                    c = p.get("Category") or {}
                    if str(c.get("Id")) == str(cid):
                        name = c.get("Name")
            except (RuntimeError, ValueError) as e:
                log(f"  could not resolve category {cid}: {e}")
            self._cat_names[cid] = name
        return self._cat_names[cid]

    def search(self, query, limit=None, in_stock=False):
        query = collapse(query)
        if not query:
            raise Usage("empty search query")
        hits, seen, cat_names = [], set(), {}
        page, total = 1, 0
        while True:
            data = self.post("Product/SearchProductsAutocomplete",
                             {"term": query, "page": page, "itemsPerPage": SEARCH_PAGE_SIZE})
            if not isinstance(data, dict) or "ProductsResult" not in data:
                raise RuntimeError("unexpected SearchProductsAutocomplete payload; layout change?")
            for c in data.get("list") or []:
                if c.get("Type") == 2 and c.get("Id") is not None:   # category suggestions
                    cat_names[str(c["Id"])] = c.get("Name") or c.get("Title")
            pr = data.get("ProductsResult") or {}
            res = pr.get("results") or []
            total = int(pr.get("total") or 0)
            for p in res:
                if p.get("Id") in seen:
                    continue
                seen.add(p.get("Id"))
                if in_stock and self.stock_of(p)[0] is not True:
                    continue
                hits.append(p)
            if not res or len(seen) >= total or (limit and len(hits) >= limit) \
                    or page * SEARCH_PAGE_SIZE >= total:
                break
            page += 1
        if limit:
            hits = hits[:limit]
        missing = sorted({str(p.get("CategoryId")) for p in hits
                          if str(p.get("CategoryId") or "").isdigit()} - set(cat_names))
        if missing:
            try:
                for n in self.nodes():
                    if n["id"] and n["id"] in missing:
                        cat_names[n["id"]] = n["name"]
            except (RuntimeError, NotFound) as e:
                log(f"  menu unavailable for category names: {e}")
            rest = [c for c in missing if c not in cat_names]
            for cid in rest[:MAX_CATEGORY_LOOKUPS]:
                cat_names[cid] = self._category_name(cid)
        recs = [self.search_record(p, cat_names) for p in hits]
        log(f"  search {query!r}: site reports {total} hit(s), returning {len(recs)}"
            f"{' in stock' if in_stock else ''}")
        if not total:
            log("  note: search is AND over substrings of title, tags, Шифра and EAN, with no "
                "Cyrillic/Latin transliteration: try the other script ('televizor'/'телевизор'), the "
                "brand or model code, or `list` the category")
        return recs

    # ---- detail
    def _get_product(self, pid):
        d = self.post("Product/GetProduct", {"id": int(pid)})
        if not isinstance(d, dict) or not d.get("Id") or not d.get("Title"):
            return None
        return d

    def _id_from_code(self, code, field):
        data = self.post("Product/SearchProductsAutocomplete", {"term": code, "page": 1, "itemsPerPage": 20})
        res = (data.get("ProductsResult") or {}).get("results") or []
        if field == "Barcode":
            # UPC-A / EAN-13 / GTIN-14 differ only in leading zeros: 195951415772 == 0195951415772
            # (the store's substring search finds both; the exact check must not reject either).
            exact = [p for p in res
                     if collapse(str(p.get(field) or "")).lstrip("0") == code.lstrip("0") != ""]
        else:
            exact = [p for p in res if collapse(str(p.get(field) or "")) == code]
        if len(exact) > 1:
            log(f"  note: {field} {code} matches {len(exact)} products; using the first")
        return exact[0]["Id"] if exact else None

    def _model_from_page(self, url):
        r = self.get_page(url)
        m = re.search(r'data-productModel="([^"]*)"', r.text, re.I)
        if not m:
            return None
        d = json.loads(htmlmod.unescape(m.group(1)))
        return d if d.get("Id") else None

    def detail_one(self, ref):
        a = (ref or "").strip()
        d = None
        m = re.fullmatch(r"(ean|sku|code|id):\s*(\S+)", a, re.I)
        if m:
            kind, val = m.group(1).lower(), m.group(2)
            if kind == "id":
                d = self._get_product(val) if val.isdigit() else None
            else:
                pid = self._id_from_code(val, "Barcode" if kind == "ean" else "CodeNumber")
                d = self._get_product(pid) if pid else None
        elif a.isdigit():
            if len(a) >= 12:                                  # EAN-13 / UPC / GTIN-14
                pid = self._id_from_code(a, "Barcode")
                d = self._get_product(pid) if pid else None
            elif len(a) == 8:                                 # Шифра (84xxxxxx), else maybe EAN-8
                pid = self._id_from_code(a, "CodeNumber") or self._id_from_code(a, "Barcode")
                d = self._get_product(pid) if pid else self._get_product(a)
            else:                                             # internal id (6 digits today)
                d = self._get_product(a)
                if d is None:
                    pid = self._id_from_code(a, "CodeNumber")
                    d = self._get_product(pid) if pid else None
        elif "neptun.mk" in a.lower() or a.startswith("/categories/") or a.startswith("categories/"):
            url = a if a.lower().startswith("http") else BASE + "/" + a.lstrip("/")
            path = urlparse(url).path.rstrip("/")
            if "/categories/" not in path:
                raise Usage(f"{ref!r} is not a product URL (product URLs contain /categories/)")
            url = BASE + path                                 # drop query / trailing slash
            try:
                d = self._model_from_page(url)
            except RuntimeError:
                d = None
            if d is None:
                cm = re.match(r"(\d{8})(?:-|$)", path.rsplit("/", 1)[-1])
                if cm:
                    pid = self._id_from_code(cm.group(1), "CodeNumber")
                    d = self._get_product(pid) if pid else None
        else:
            raise Usage(f"cannot parse a product from {ref!r}; give a product URL, an id, a Шифра "
                        "(8 digits), an EAN, or ean:/sku:/id: prefixed values")
        if d is None:
            raise NotFound(f"product not found: {ref} (unknown id/code, or the product is inactive - sold "
                           "out or delisted: search drops it and its page is a soft 404, so only its "
                           "internal id still resolves; treat it as not orderable)")
        shops = None
        try:
            shops = self.post("Product/GetShopsForProduct", {"productid": d["Id"]})
            shops = [s for s in shops if s.get("HasStock", True)]
        except RuntimeError as e:
            log(f"  GetShopsForProduct failed for {d['Id']}: {e}; using the embedded shop list")
        return self.detail_record(d, shops, ref)

    def detail_record(self, d, shops, ref):
        rec = self.listing_record(d)
        rec["input"] = ref
        nav = [n for n in (d.get("NavigationPath") or []) if n.get("Name")]
        if nav:
            rec["category"] = " > ".join(collapse(n["Name"]) for n in nav)
        if shops is None:
            shops = d.get("Shops") or []
        per_loc = [{"location": collapse(s.get("Name")), "city": s.get("City"), "in_stock": True}
                   for s in shops]
        inactive = d.get("Active") is False
        web_ok = bool(d.get("AvailableWebshop")) and not d.get("Preorder") and not inactive
        rec["in_stock"] = False if (inactive or d.get("Preorder")) else (web_ok or bool(per_loc))
        bits = []
        if inactive:
            bits.append(INACTIVE_NOTE)
        if d.get("Preorder"):
            bits.append("preorder")
        bits.append("online order: " + ("yes" if web_ok else "no"))
        bits.append(f"in stock in {len(per_loc)} store(s): " + ", ".join(x["location"] for x in per_loc)
                    if per_loc else "no store stock listed")
        rec["stock_note"] = "; ".join(bits)
        w = d.get("Warranty")
        rec["warranty"] = f"{int(w)} months" if isinstance(w, (int, float)) and w > 0 else None
        parts = []
        desc = html_to_text(d.get("Description"))
        short = html_to_text(d.get("ShortDescription"))
        if desc:
            parts.append(desc)
        bullets = [collapse(b) for b in short.split("•") if collapse(b)]
        if short and not all(b in desc for b in bullets):
            parts.append(short)
        feats = []
        for g in d.get("CategoryFeatureGroups") or []:
            for f in g.get("Features") or []:
                val = f.get("PresentationValue") or f.get("Value") or f.get("FilterValue")
                if val not in (None, "", "n/a"):
                    feats.append(f"{collapse(f.get('Title'))}: {collapse(str(val))}")
        if feats:
            parts.append("; ".join(feats))
        rec["specs"] = " | ".join(parts) or None
        rec["per_location_stock"] = per_loc
        rec["delivery_estimate"] = DELIVERY_POLICY if web_ok else None
        promos = [collapse(p.get("CustomPromotionName") or p.get("PromotionName"))
                  for p in d.get("Promotions") or [] if p.get("CustomPromotionName") or p.get("PromotionName")]
        rec.pop("price_valid_until", None)
        rec.update(self.price_windows(d))
        rec["extra"] = {
            "member_price_mkd": rec.get("member_price_mkd"),
            "member_price_name": card_name(d) if rec.get("member_price_mkd") else None,
            "actual_price_mkd": to_int(d.get("ActualPrice")),
            "online_price_mkd": to_int(d.get("WebshopDiscountPrice")),
            "warranty_months": w if isinstance(w, (int, float)) and w > 0 else None,
            "active": d.get("Active"),
            "available_webshop": d.get("AvailableWebshop"),
            "preorder": bool(d.get("Preorder")),
            "category_ids": [n.get("Id") for n in nav],
            "promotions": promos,
            "promotion_windows": self.promotion_windows(d) or None,
            "instalments": (f"{d.get('NumberOfRates')} x {to_int(d.get('RatePrice'))} MKD"
                            if d.get("NumberOfRates") and d.get("RatePrice") else None),
        }
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
            except NotFound as e:
                log(f"  not found: {ref} ({e})")
                out.append({"input": ref, "error": f"not found: {e}"})
            except Usage as e:
                log(f"  bad input: {ref} ({e})")
                out.append({"input": ref, "error": f"bad input: {e}"})
            except (RuntimeError, requests.RequestException, ValueError, KeyError) as e:
                log(f"  error: {ref} ({e})")
                out.append({"input": ref, "error": str(e)})
                hard = True
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
        mem = f" [haPPy {r['member_price_mkd']}]" if r.get("member_price_mkd") else ""
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        brand = f"[{r['brand']}] " if r.get("brand") else ""
        print(f"{price} MKD{reg}{mem}  {stock}  {brand}{(r.get('title') or '')[:80]}  {r['url']}")
        if "warranty" in r:
            print(f"        warranty {r.get('warranty')} | Шифра {r.get('sku')} | EAN {r.get('ean')} | "
                  f"{r.get('category')} | {r.get('stock_note')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        rid = r["id"] if r.get("id") else "-"
        print(f"{rid:>5} {cnt}  {r['path']}  [{r['slug']}]")


def print_facets(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
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
    for _stream in (sys.stdout, sys.stderr):   # UTF-8 output on every platform, even when piped
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress on stderr")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request to stderr")
    common.add_argument("--json", metavar="PATH", help="write a JSON list to PATH")
    ap = argparse.ArgumentParser(description="Neptun (neptun.mk) catalogue client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p.add_argument("--counts", action="store_true",
                   help=f"fetch product counts (one request per category, max {MAX_COUNT_REQUESTS})")
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

    if a.cmd == "info":
        print(json.dumps(info(), ensure_ascii=False))
        return 0
    if getattr(a, "limit", None) is not None and a.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2
    c = Neptun(verbose=a.verbose)
    code = 0
    try:
        if a.cmd == "search":
            emit(c.search(a.query, limit=a.limit, in_stock=a.in_stock), a.json)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = c.categories(grep=a.grep, counts=a.counts)
            if a.grep and not recs:
                log(f"  no category matches {a.grep!r} (matched against name, path, slug, a Latin "
                    "transliteration, the menu's keywords and English department words)")
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
    except (RuntimeError, requests.RequestException, ValueError, KeyError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
