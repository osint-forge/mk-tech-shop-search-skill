#!/usr/bin/env python3
"""Neksio (https://g.store.neksio.mk) catalogue client. See references/client-contract.md
and references/neksio.md.

Neksio is a custom ASP.NET Core MVC shop (IIS, no CDN/WAF). Its own frontend (/js/shop.js)
fills every category and search page from one JSON endpoint, which this client calls directly:

    POST /FilterAndPaginateProducts   (Content-Type: application/json; every key required)
        {"categoryId": 6|null, "manufacturerIds": [], "subCategoryIds": [], "page": 1,
         "pageSize": 100, "description": "<free text>"|null,
         "selectedMinMaxPrice": {"minPrice": null, "maxPrice": null},
         "orderBy": 7, "quantityStock": 1}
        -> {noOfProducts, noOfPages, productCards[...], allManufacturers[...], ...}

The category tree (3 levels: menu group > CategoryId > SubCategoryId) is parsed from the
server-rendered side menu on the home page. Product detail is the HTML page
/Product/Details/{id} (specs, warranty text, breadcrumb).

    neksio.py info
    neksio.py search "<query>" [--limit N] [--in-stock] [--category REF] [--json PATH]
    neksio.py categories [--grep REGEX] [--counts] [--json PATH]
    neksio.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    neksio.py detail <url|id|ean:EAN|sku:CODE> [...] [--json PATH]
    neksio.py facets <category> [--json PATH]
    common options: --quiet, -v/--verbose (log every request)

Category refs: an id printed by `categories` ("6", "6/279", "g212"), its slug ("monitori",
"monitori/gejming-monitor", "g:monitori"), a /Shop?CategoryId=..&SubCategoryId=.. URL,
"sub:279", or an exact category name.
Filter tokens: brand=<id|name>[,<id|name>...]   sub=<id|name>[,...]   price=MIN-MAX
(commas OR inside a token; several --filter flags AND).

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
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "neksio"
NAME = "Neksio"
BASE = "https://g.store.neksio.mk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PAGE_SIZE = 100         # no server cap, but latency grows ~linearly with size (100 rows ~1-3 s)
PACE_S = 0.6            # pause between consecutive requests: the origin has very little capacity
MAX_RETRIES = 4
REFILL_MAX = 1500       # gap-fill a short walk with one big page only up to this many rows
MAX_COUNT_REQUESTS = 60  # `categories --counts` budget (one cheap request per node)
ORDER_PRICE_ASC = 7     # 7 price asc, 8 price desc, 9 name A-Z, 10 name Z-A
STOCK_ALL, STOCK_IN_STOCK_ONLY = 1, 2
DELIVERY_POLICY = "up to 72 h (working days) after order confirmation (store policy)"

SELLS = ("IT distributor's webshop (~4,300 products): peripherals (keyboards, mice, headsets, speakers, "
         "webcams, microphones, barcode scanners), PC components (coolers, cases, PSUs, RAM, HDD/SSD, GPUs, "
         "CPUs, motherboards), monitors and monitor arms, cables/adapters/converters/KVM, networking "
         "(routers, switches, access points, passive), gaming gear (chairs, desks, consoles, controllers, "
         "wheels, VR), laptops (mostly Dell), tablets, desktop PCs, a few phones, smartwatches, external "
         "storage (USB sticks, memory cards, external SSD/HDD, NAS), a few printers plus toners/paper/3D "
         "filament, projectors, UPS/AVR, batteries, LED lighting, office chairs, tools, small home "
         "appliances (vacuums, fans, air purifiers, humidifiers, hair dryers), e-scooters, TV mounts. "
         "Effectively no TVs and no large kitchen/laundry appliances.")
NOTES = ("price_mkd = priceWTax, the anonymous price incl. VAT (5% on computers, parts, peripherals, "
         "monitors; 18% on networking, cables, appliances, consumables, some accessories); "
         "regular_price_mkd only when the item is on sale. Exact stock quantity per item (one web "
         "figure, no per-store split). EAN (barcode) in listings (missing on ~3%). Search = every word as a "
         "substring (AND, any order) of title, Шифра or barcode; titles are English and the server "
         "transliterates Cyrillic letter by letter, so 'монитор' works but 'тастатура' finds nothing. "
         "No search cap. Facets = brand and subcategory (+ price range filter). Ordering is by "
         "inquiry ('Прашај/Нарачај' form, phone, e-mail), no online checkout; delivery up to 72 h "
         "(working days) after confirmation, no published delivery fee. Slow origin: sequential only. "
         "detail: a bare number is a product id; pass a Шифра as sku:CODE.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter",
                "ean_in_listing", "ean_in_detail", "stock_qty", "warranty",
                "search_ean", "search_codes"]

CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "turnstile",
                     "g-recaptcha", "h-captcha", "attention required", "are you a robot",
                     "ddos-guard", "access denied")

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}

# English words appended to a category's grep haystack when its (Macedonian) name or path
# contains the stem, so `categories --grep mouse` finds ГЛУВЧИЊА. Lowercase stems.
_EN_TAGS = {
    "глувч": "mouse mice", "тастатур": "keyboard", "слушалк": "headphones headset earphones",
    "звучници": "speakers", "звучни картич": "sound card audio", "монитор": "monitor display screen",
    "графичк": "graphics card gpu video card", "процесор": "cpu processor",
    "матичн": "motherboard mainboard", "напојув": "power supply psu", "рам мемор": "ram memory dimm",
    "куќишт": "pc case chassis", "кулер": "cooler cooling fan", "вентилатор": "fan",
    "хард диск": "hdd hard disk drive storage", "режач": "optical drive dvd",
    "веб камер": "webcam web camera", "микрофон": "microphone mic", "баркод": "barcode scanner",
    "мемориски картич": "memory card sd card", "усб мемор": "usb flash drive pendrive stick",
    "читач на картич": "card reader", "нас уред": "nas", "кутии за": "enclosure",
    "компјутерски систем": "desktop pc computer", "компјутерски конфигурац": "desktop pc build",
    "лаптоп": "laptop notebook", "таблет": "tablet", "телефон": "phone smartphone mobile",
    "чанти": "bag", "ранци": "backpack", "софтвер": "software license", "кабли": "cable cables",
    "камери": "camera", "двоглед": "binoculars", "конвертор": "converter adapter hub",
    "мрежна опрема": "network networking router switch wifi lan", "пасивна мрежна": "patch rack",
    "принтер": "printer", "потрошен материјал": "toner ink cartridge paper consumables",
    "филамент": "filament", "проектор": "projector", "презентер": "presenter pointer",
    "паметни часовни": "smartwatch smart watch", "часовни": "watch", "акумулатор": "battery",
    "алат": "tools", "апарати за домаќинство": "home appliances household",
    "аудио опрема": "audio", "батерии": "batteries", "канцелариск": "office supplies",
    "лед осветлув": "led lighting light", "лед ламп": "led lamp bulb", "упс": "ups",
    "стабилизатор": "voltage stabilizer avr", "гејминг": "gaming",
    "виртуелна реалност": "vr virtual reality", "волани": "steering wheel racing",
    "столици": "chair", "конзол": "console", "контролер": "controller gamepad", "игри": "games",
    "маси": "desk table", "безжичн": "wireless", "жичан": "wired", "надворешн": "external",
    "додатоци": "accessories", "портабил": "portable",
    "тв и": "tv television", "правосмукалк": "vacuum cleaner", "тротинет": "e-scooter scooter",
    "овлажнув": "humidifier", "прочистувач": "air purifier", "фен за коса": "hair dryer",
    "фитнес": "fitness", "паметни уреди": "smart home", "андроид": "android tv box",
    "акциони камер": "action camera", "размножувач": "usb hub", "разделувач": "kvm switch splitter",
    "ласерск": "laser", "термалн": "thermal label", "тонери": "toner", "хартија": "paper",
    "сет тастатура": "combo keyboard mouse set", "докинг": "docking station",
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


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", translit(text)).strip("-") or "x"


def has_cyrillic(text):
    return bool(re.search(r"[Ѐ-ӿ]", text or ""))


def to_int(value):
    """'6.130 ден.' -> 6130; '5990,00' -> 5990; 6130.0 -> 6130; None/'' -> None"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    s = re.sub(r"[^\d,.]", "", str(value)).strip(".,")
    if not s:
        return None
    if re.search(r",\d{1,2}$", s):          # 5.990,00 -> decimal comma
        s = s.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", s) and s.count(".") == 1 and "," not in s:
        pass                                 # 5990.5 -> decimal point
    else:                                    # 6.130 / 6,130 / 1.234.567 -> thousands separators
        s = s.replace(".", "").replace(",", "")
    return int(round(float(s)))


def en_tags(text):
    low = (text or "").lower()
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def product_url(pid):
    return f"{BASE}/Product/Details/{pid}"


# --------------------------------------------------------------------------- client

class Neksio:
    def __init__(self, pace=PACE_S, page_size=PAGE_SIZE, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "mk,en;q=0.8"})
        self.pace = pace
        self.page_size = page_size
        self.verbose = verbose
        self._last = 0.0
        self._nodes = None
        self.requests_made = 0

    # ---- transport
    def _request(self, method, path, expect_json=False, allow_500=False, **kw):
        url = path if path.startswith("http") else BASE + path
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
                    raise RuntimeError(f"{method} {url} failed: {e}") from e
                log(f"  network error ({e.__class__.__name__}), retry {attempt} in {2 ** attempt}s")
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            self.requests_made += 1
            if self.verbose:
                log(f"  {method} {url} -> {r.status_code} {r.elapsed.total_seconds():.2f}s")
            if r.status_code in (429, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 2 ** attempt
                warn(f"HTTP {r.status_code} from {url}, retrying in {min(delay, 60)}s")
                time.sleep(min(delay, 60))
                continue
            self._check_block(r, expect_json)
            if r.status_code == 500 and allow_500:
                return r
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
                          f"retry-after={r.headers.get('Retry-After')}")
        raise RuntimeError(f"{method} {url}: still HTTP {r.status_code if r is not None else '?'} "
                           f"after {MAX_RETRIES} attempts (store down?)")

    @staticmethod
    def _check_block(r, expect_json):
        ctype = r.headers.get("content-type", "")
        if urlparse(r.url).path.lower().startswith("/login"):
            raise Blocked(f"HTTP {r.status_code}: redirected to the login page {r.url} (login wall)")
        if r.status_code in (401, 403):
            title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
            raise Blocked(f"HTTP {r.status_code} from {r.url}; server={r.headers.get('server')} "
                          f"cf-ray={r.headers.get('cf-ray')} "
                          f"title={collapse(title.group(1)) if title else None!r}")
        if "html" in ctype:
            low = (r.text or "").lower()
            hit = next((m for m in CHALLENGE_MARKERS if m in low[:30000]), None)
            # real pages carry the shop's own markup (side menu / product block); a challenge does not
            if hit and (expect_json or ("side-menu-category" not in low
                                        and 'id="productid"' not in low)):
                title = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
                raise Blocked(f"HTTP {r.status_code} challenge/block page from {r.url} "
                              f"(marker {hit!r}, title={collapse(title.group(1)) if title else None!r}, "
                              f"cf-ray={r.headers.get('cf-ray')})")

    # ---- listing endpoint
    def filter_products(self, category_id=None, sub_ids=(), page=1, page_size=None,
                        description=None, in_stock=False, manufacturer_ids=(),
                        min_price=None, max_price=None, order_by=ORDER_PRICE_ASC):
        """One call to the frontend's listing endpoint. Every key must be present (a partial
        body -> HTTP 500), and categoryId or description is required (both null -> 500)."""
        body = {
            "categoryId": category_id,
            "manufacturerIds": list(manufacturer_ids),
            "subCategoryIds": list(sub_ids),
            "page": page,
            "pageSize": page_size or self.page_size,
            "description": description,
            "selectedMinMaxPrice": {"minPrice": min_price, "maxPrice": max_price},
            "orderBy": order_by,
            "quantityStock": STOCK_IN_STOCK_ONLY if in_stock else STOCK_ALL,
        }
        data = self._request("POST", "/FilterAndPaginateProducts", expect_json=True, json=body,
                             headers={"Referer": BASE + "/Shop",
                                      "X-Requested-With": "XMLHttpRequest"})
        if not isinstance(data, dict) or "productCards" not in data or "noOfProducts" not in data:
            raise RuntimeError("unexpected FilterAndPaginateProducts payload "
                               f"(keys {list(data)[:12] if isinstance(data, dict) else type(data)}); "
                               "layout change?")
        return data

    def count(self, **filters):
        return self.filter_products(page=1, page_size=1, **filters)["noOfProducts"] or 0

    def walk(self, limit=None, label="", keep=None, **filters):
        """Every page of one query; returns (cards, total). Verifies the number of unique cards
        against noOfProducts. `keep` is an optional client-side predicate (applied after the
        completeness check); `limit` counts kept cards."""
        cards, seen, page, total = [], set(), 1, 0
        size = min(self.page_size, limit) if (limit and keep is None) else self.page_size
        kept = 0
        while True:
            data = self.filter_products(page=page, page_size=size, **filters)
            if page == 1:
                total = data["noOfProducts"] or 0
                pages = data["noOfPages"] or 0
                if total > size:
                    log(f"  {label or 'query'}: {total} products, {pages} page(s) of {size}")
            for c in data["productCards"]:
                if c["productId"] not in seen:
                    seen.add(c["productId"])
                    cards.append(c)
                    kept += keep is None or bool(keep(c))
            if limit and kept >= limit:
                out = [c for c in cards if keep is None or keep(c)]
                return out[:limit], total
            if page >= (data["noOfPages"] or 0) or not data["productCards"]:
                break
            page += 1
        if len(cards) < total <= REFILL_MAX:
            # Page boundaries shifted mid-walk (catalogue edit): fetch the whole set in one call
            # and merge, rather than silently returning a short list.
            warn(f"{label}: collected {len(cards)} of {total}; re-fetching as one page to fill the gap")
            data = self.filter_products(page=1, page_size=total, **filters)
            for c in data["productCards"]:
                if c["productId"] not in seen:
                    seen.add(c["productId"])
                    cards.append(c)
        if len(cards) != total:
            warn(f"{label}: collected {len(cards)} unique products but the site reports "
                 f"noOfProducts={total} (incomplete)")
        if keep is not None:
            cards = [c for c in cards if keep(c)]
        return (cards[:limit] if limit else cards), total

    # ---- records
    @staticmethod
    def stock_of(c):
        qty = c.get("quantity")
        if qty is None:
            return None, None
        if qty > 0:
            return True, f"има на залиха (qty {qty})"
        if c.get("comingSoon"):
            when = f", expected {str(c['futureDocumentDate'])[:10]}" if c.get("futureDocumentDate") else ""
            return False, f"наскоро / coming soon (qty 0{when})"
        return False, "нема на залиха (qty 0)"

    def listing_record(self, c):
        in_stock, note = self.stock_of(c)
        cat = " > ".join(collapse(x) for x in (c.get("category"), c.get("subCategory")) if x) or None
        price = to_int(c.get("priceWTax"))
        regular = to_int(c.get("old_PriceWTax")) if c.get("isOnSale") else None
        if regular is not None and price is not None and regular <= price:
            regular = None
        return {
            "store": STORE,
            "id": str(c["productId"]),
            "sku": collapse(c.get("productCode")) or None,
            "title": collapse(c.get("productName")),
            "url": product_url(c["productId"]),
            "brand": collapse(c.get("manufacturer")) or None,
            "price_mkd": price,
            "regular_price_mkd": regular,
            "in_stock": in_stock,
            "stock_note": note,
            "category": cat,
            "ean": collapse(c.get("barCode")) or None,
        }

    # ---- category tree
    def nodes(self):
        """Flat list of the side-menu tree: groups (gNNN) > categories (CategoryId)
        > subcategories (CategoryId/SubCategoryId)."""
        if self._nodes is not None:
            return self._nodes
        r = self._request("GET", "/")
        soup = BeautifulSoup(r.text, "html.parser")
        nodes = []
        for tg in soup.select('[data-bs-target^="#cat_"]'):
            gid = tg["data-bs-target"][len("#cat_"):]
            gname = collapse(tg.get_text(" "))
            box = soup.select_one(f"#cat_{gid}")
            if not box or not gname:
                continue
            g = {"id": f"g{gid}", "slug": f"g:{slugify(gname)}", "name": gname, "path": gname,
                 "url": None, "parent": None, "count": None, "kind": "group", "cat": None,
                 "sub": None, "children": []}
            nodes.append(g)
            for ct in box.select('[data-bs-target^="#subcat_"]'):
                cid = int(ct["data-bs-target"][len("#subcat_"):])
                cname = collapse(ct.get_text(" "))
                cslug = slugify(cname)
                c = {"id": str(cid), "slug": cslug, "name": cname, "path": f"{gname} > {cname}",
                     "url": f"{BASE}/Shop?CategoryId={cid}", "parent": g["id"], "count": None,
                     "kind": "category", "cat": cid, "sub": None, "children": []}
                g["children"].append(c["id"])
                nodes.append(c)
                sbox = soup.select_one(f"#subcat_{cid}")
                for a in (sbox.select("a[href]") if sbox else []):
                    href = htmlmod.unescape(a["href"])
                    m = re.search(r"CategoryId=(\d+)&SubCategoryId=(\d+)", href)
                    if not m:
                        continue
                    sid = int(m.group(2))
                    sname = collapse(a.get_text(" "))
                    s = {"id": f"{cid}/{sid}", "slug": f"{cslug}/{slugify(sname)}", "name": sname,
                         "path": f"{gname} > {cname} > {sname}",
                         "url": f"{BASE}/Shop?CategoryId={cid}&SubCategoryId={sid}",
                         "parent": c["id"], "count": None, "kind": "sub", "cat": cid, "sub": sid,
                         "children": []}
                    c["children"].append(s["id"])
                    nodes.append(s)
        if not any(n["kind"] == "category" for n in nodes):
            raise RuntimeError("no categories found in the home page side menu (layout change?)")
        self._nodes = nodes
        return nodes

    def node_by_id(self, nid):
        return next((n for n in self.nodes() if n["id"] == nid), None)

    def categories(self, grep=None, counts=False):
        nodes = self.nodes()
        if grep:
            rx = re.compile(grep, re.I)

            def tags(n):
                # English words from the node's own name, plus its category's name for a
                # subcategory (so `gpu` finds ГРАФИЧКИ КАРТИ > AMD). Never from the menu group:
                # broad group names ("МРЕЖНА ОПРЕМА, АДАПТЕРИ И ДОДАТОЦИ") would tag every cable,
                # KVM and camera node with "router". The literal path still matches (contract).
                parent = self.node_by_id(n["parent"]) if n["kind"] == "sub" else None
                return en_tags(n["name"] + (" / " + parent["name"] if parent else ""))
            # each field separately, so ^/$ anchor within one field (e.g. '^monitor' = name starts)
            sel = [n for n in nodes
                   if any(rx.search(f) for f in (n["name"], n["path"], n["slug"], translit(n["name"]),
                                                 translit(n["path"]), tags(n)) if f)]
        else:
            sel = list(nodes)
        if counts:
            self._fill_counts(sel)
        return [{k: n[k] for k in ("id", "slug", "name", "path", "url", "parent", "count")}
                for n in sel]

    def _fill_counts(self, sel):
        want = []
        for n in sel:
            if n["kind"] == "group":
                want += [self.node_by_id(c) for c in n["children"]]
            else:
                want.append(n)
        uniq = list({n["id"]: n for n in want}.values())
        if len(uniq) > MAX_COUNT_REQUESTS:
            cats = [n for n in uniq if n["kind"] == "category"]
            warn(f"--counts would need {len(uniq)} requests (cap {MAX_COUNT_REQUESTS}); subcategory "
                 f"counts skipped, narrow --grep to get them")
            uniq = cats[:MAX_COUNT_REQUESTS]
        log(f"  fetching {len(uniq)} product counts (one request each)")
        for n in uniq:
            n["count"] = self.count(category_id=n["cat"], sub_ids=[n["sub"]] if n["sub"] else [])
        for n in sel:
            if n["kind"] == "group":
                kids = [self.node_by_id(c) for c in n["children"]]
                if kids and all(k["count"] is not None for k in kids):
                    n["count"] = sum(k["count"] for k in kids)

    # ---- category resolution
    def _parent_of_sub(self, sid):
        hits = [n for n in self.nodes() if n["kind"] == "sub" and n["sub"] == sid]
        if not hits:
            raise NotFound(f"subcategory {sid} is not in the site menu; pass CATEGORY/{sid}")
        return hits[0]["cat"]

    def resolve(self, ref):
        """-> target dict {label, scopes: [(cat_id, [sub_ids])], node or None}."""
        raw = ref
        ref = (ref or "").strip()
        if not ref:
            raise Usage("empty category reference")
        low = ref.lower().rstrip("/")
        if low.startswith(("http://", "https://")) or re.search(r"(^|/)shop\?", low):
            url = ref.rstrip("/")
            if not url.lower().startswith(("http://", "https://", "/")):
                url = "https://" + url          # scheme-less paste: g.store.neksio.mk/Shop?...
            q = {k.lower(): v for k, v in parse_qs(urlparse(url).query).items()}

            def ids(vals, what):
                # leading digits only, so a pasted "...SubCategoryId=101/" or "101#top" still
                # counts; an unparsable value is an error, never silently dropped (that would
                # widen a subcategory URL to its whole parent category)
                out = []
                for v in vals:
                    for x in v.split(","):
                        m = re.match(r"\s*(\d+)", x)
                        if m:
                            out.append(int(m.group(1)))
                        elif x.strip():
                            raise Usage(f"cannot read {what} {x!r} in {raw!r}")
                return out
            cats = ids(q.get("categoryid", []), "CategoryId")
            subs = ids(q.get("subcategoryid", []) + q.get("subcategoryids", []), "SubCategoryId")
            if cats:
                return self._target(cats[0], subs)
            if subs:
                return self._target(self._parent_of_sub(subs[0]), subs)
            raise Usage(f"no CategoryId / SubCategoryId in {raw!r}")
        m = re.fullmatch(r"(\d+)\s*[/:]\s*(\d+(?:\s*,\s*\d+)*)", low)
        if m:
            return self._target(int(m.group(1)), [int(x) for x in m.group(2).split(",")])
        if low.isdigit():
            return self._target(int(low), [])
        m = re.fullmatch(r"(?:sub:|s)(\d+)", low)
        if m:
            sid = int(m.group(1))
            return self._target(self._parent_of_sub(sid), [sid])
        m = re.fullmatch(r"g(\d+)", low)
        if m:
            node = self.node_by_id(low)
            if not node:
                raise NotFound(f"no menu group {ref!r}; run `categories` for the ids")
            return self._group_target(node)
        nodes = self.nodes()
        hit = next((n for n in nodes if n["slug"] == low), None)
        if not hit:
            name = collapse(ref).lower()
            named = [n for n in nodes if n["name"].lower() == name]
            for kind in ("category", "group", "sub"):
                same = [n for n in named if n["kind"] == kind]
                if len(same) == 1:
                    hit = same[0]
                    break
                if len(same) > 1:
                    raise Usage(f"category name {ref!r} is ambiguous: "
                                + "; ".join(f"{n['id']} ({n['path']})" for n in same))
        if not hit:
            raise NotFound(f"unknown category {ref!r}; run `categories --grep ...` for ids/slugs")
        if hit["kind"] == "group":
            return self._group_target(hit)
        return {"label": hit["path"], "node": hit,
                "scopes": [(hit["cat"], [hit["sub"]] if hit["sub"] else [])]}

    def _target(self, cat, subs):
        nid = f"{cat}/{subs[0]}" if len(subs) == 1 else (str(cat) if not subs else None)
        node = None
        if self._nodes is not None and nid:
            node = self.node_by_id(nid)
        label = node["path"] if node else (f"{cat}/{','.join(map(str, subs))}" if subs else str(cat))
        return {"label": label, "node": node, "scopes": [(cat, subs)], "numeric": node is None}

    def _group_target(self, g):
        kids = [self.node_by_id(c) for c in g["children"]]
        return {"label": g["path"], "node": g, "scopes": [(k["cat"], []) for k in kids]}

    def _check_known(self, target):
        """NotFound if a numeric target names a category/subcategory the side menu lacks (the
        endpoint answers such ids with 200 and 0 products, so this is the only check)."""
        cat, subs = target["scopes"][0]
        if len(target["scopes"]) == 1:
            known = self.node_by_id(str(cat))
            if not known:
                raise NotFound(f"unknown category {target['label']!r}: CategoryId {cat} is not in the "
                               f"site menu and the endpoint returned 0 products")
            bad = [s for s in subs if not self.node_by_id(f"{cat}/{s}")]
            if bad:
                raise NotFound(f"subcategory {bad} is not under CategoryId {cat} ({known['path']}) in "
                               f"the site menu; the endpoint returned 0 products")

    def _explain_empty(self, target):
        """Called when a category query returned 0 products: NotFound with the reason."""
        self._check_known(target)
        raise NotFound(f"category {target['label']!r} returned 0 products (empty category, or a "
                       f"soft block / layout change)")

    # ---- filters and facets
    def _brand_index(self, target):
        """manufacturerId -> name for the target's categories (one request per scope)."""
        idx = {}
        for cat, subs in target["scopes"]:
            d = self.filter_products(category_id=cat, sub_ids=subs, page=1, page_size=1)
            for m in d.get("allManufacturers") or []:
                if m.get("manufacturerId") is not None:
                    idx[int(m["manufacturerId"])] = collapse(m.get("manufacturer"))
        return idx

    def parse_filters(self, tokens, target):
        """-> dict(manufacturer_ids, sub_ids, min_price, max_price); several tokens AND."""
        out = {"manufacturer_ids": None, "sub_ids": None, "min_price": None, "max_price": None}
        brand_idx = None
        for tok in tokens or []:
            m = re.fullmatch(r"\s*(brand|manufacturer|sub|subcategory|price)\s*=\s*(.+?)\s*", tok, re.I)
            if not m:
                raise Usage(f"bad --filter {tok!r}; use brand=<id|name>, sub=<id|name> or price=MIN-MAX")
            key, val = m.group(1).lower(), m.group(2)
            if key == "price":
                pm = re.fullmatch(r"(\d*)\s*-\s*(\d*)", val)
                if not pm or not (pm.group(1) or pm.group(2)):
                    raise Usage(f"bad price filter {val!r}; use price=MIN-MAX, price=MIN- or price=-MAX")
                lo, hi = (int(x) if x else None for x in pm.groups())
                if lo is not None:
                    out["min_price"] = max(lo, out["min_price"] or lo)
                if hi is not None:
                    out["max_price"] = min(hi, out["max_price"] or hi)
                continue
            vals = [v.strip() for v in val.split(",") if v.strip()]
            if key in ("brand", "manufacturer"):
                ids = set()
                for v in vals:
                    if v.isdigit():
                        ids.add(int(v))
                        continue
                    if brand_idx is None:
                        brand_idx = self._brand_index(target)
                    hit = [i for i, n in brand_idx.items() if n.lower() == v.lower()
                           or slugify(n) == slugify(v)]
                    if not hit:
                        raise NotFound(f"no brand {v!r} in {target['label']}; see `facets`")
                    ids.update(hit)
                out["manufacturer_ids"] = ids if out["manufacturer_ids"] is None \
                    else out["manufacturer_ids"] & ids
            else:
                if len(target["scopes"]) != 1:
                    raise Usage("sub= filters need a single category, not a menu group")
                cat = target["scopes"][0][0]
                kids = [n for n in self.nodes() if n["kind"] == "sub" and n["cat"] == cat]
                ids = set()
                for v in vals:
                    if v.isdigit():
                        ids.add(int(v))
                        continue
                    hit = [n["sub"] for n in kids if n["name"].lower() == v.lower()
                           or n["slug"].split("/")[-1] == slugify(v)]
                    if not hit:
                        raise NotFound(f"no subcategory {v!r} under {target['label']}; see `facets`")
                    ids.update(hit)
                out["sub_ids"] = ids if out["sub_ids"] is None else out["sub_ids"] & ids
        for k in ("manufacturer_ids", "sub_ids"):
            if out[k] is not None and not out[k]:
                raise Usage(f"the --filter tokens AND to an empty {k.split('_')[0]} set")
        if out["min_price"] is not None and out["max_price"] is not None \
                and out["min_price"] > out["max_price"]:
            raise Usage("price filters AND to an empty range")
        return out

    def _scoped(self, target, flt):
        """Apply filters to the target's scopes -> list of (cat, subs) to query."""
        scopes = []
        for cat, subs in target["scopes"]:
            if flt and flt["sub_ids"] is not None:
                subs = sorted(flt["sub_ids"] & set(subs)) if subs else sorted(flt["sub_ids"])
                if not subs:
                    raise Usage("sub= filter does not intersect the requested subcategory")
            scopes.append((cat, subs))
        return scopes

    def facets(self, ref):
        target = self.resolve(ref)
        brands, subs, total, lo, hi = {}, {}, 0, None, None
        for cat, sub_ids in target["scopes"]:
            cards, _ = self.walk(label=target["label"], category_id=cat, sub_ids=sub_ids)
            total += len(cards)
            for c in cards:
                if c.get("manufacturerId") is not None:
                    k = int(c["manufacturerId"])
                    name = collapse(c.get("manufacturer")) or str(k)
                    brands.setdefault(k, [name, 0, 0])
                    brands[k][1] += 1
                    brands[k][2] += (c.get("quantity") or 0) > 0
                if c.get("subCategoryId") is not None and len(target["scopes"]) == 1:
                    k = int(c["subCategoryId"])
                    subs.setdefault(k, [collapse(c.get("subCategory")) or str(k), 0, 0])
                    subs[k][1] += 1
                    subs[k][2] += (c.get("quantity") or 0) > 0
                p = to_int(c.get("priceWTax"))
                if p is not None:
                    lo = p if lo is None else min(lo, p)
                    hi = p if hi is None else max(hi, p)
        if not total:
            self._explain_empty(target)
        out = [{"name": "brand", "value": v[0], "count": v[1], "in_stock": v[2], "token": f"brand={k}"}
               for k, v in sorted(brands.items(), key=lambda x: (-x[1][1], x[1][0]))]
        out += [{"name": "subcategory", "value": v[0], "count": v[1], "in_stock": v[2],
                 "token": f"sub={k}"}
                for k, v in sorted(subs.items(), key=lambda x: (-x[1][1], x[1][0]))]
        if lo is not None:
            out.append({"name": "price", "value": f"{lo}-{hi}", "count": total, "in_stock": None,
                        "token": f"price={lo}-{hi}"})
        log(f"  {target['label']}: {total} products, {len(brands)} brands, {len(subs)} subcategories")
        return out

    # ---- commands
    def search(self, query, limit=None, in_stock=False, category=None):
        query = collapse(query)
        if not query:
            raise Usage("empty search query (the endpoint answers 500 with no filter at all)")
        short = [w for w in query.split() if len(w) <= 2 and not w.isdigit()]
        if short:
            log(f"  note: short word(s) {short} match as substrings of almost any title")
        label = f"search {query!r}"
        keep, sub_ids = None, []
        if category:
            # With a description the server IGNORES categoryId (it still honours subCategoryIds,
            # manufacturerIds, price and stock), so a category scope is applied client-side.
            target = self.resolve(category)
            label += f" in {target['label']}"
            scopes = target["scopes"]
            cats = {cat for cat, _ in scopes}
            if len(scopes) == 1 and scopes[0][1]:
                sub_ids = scopes[0][1]
            keep = lambda c: c.get("categoryId") in cats  # noqa: E731
        cards, total = self.walk(limit=limit, label=label, keep=keep, description=query,
                                 sub_ids=sub_ids, in_stock=in_stock)
        recs = [self.listing_record(c) for c in cards]
        if category and not recs:
            # 0 hits in a category is a genuine result only if the menu knows the category;
            # an unknown id would otherwise come back as a silent empty list (exit 2 instead)
            self._check_known(target)
        stock = " in stock" if in_stock else ""
        if keep and not sub_ids:
            log(f"  {label}: site reports {total} hit(s){stock} store-wide, {len(recs)} of them in "
                f"the category{' (stopped at --limit)' if limit and len(recs) >= limit else ''}")
        else:
            log(f"  {label}: site reports {total} hit(s){stock}, returning {len(recs)}")
        if not recs and has_cyrillic(query):
            log("  note: titles are English and the server transliterates Cyrillic letter by letter "
                f"({query!r} -> {translit(query)!r}); use the English word (e.g. 'keyboard' not "
                "'тастатура') or `list` the category")
        return recs

    def list_category(self, ref, in_stock=False, limit=None, filters=None):
        target = self.resolve(ref)
        flt = self.parse_filters(filters, target) if filters else None
        scopes = self._scoped(target, flt)
        extra = {}
        if flt:
            extra = {"manufacturer_ids": sorted(flt["manufacturer_ids"] or []),
                     "min_price": flt["min_price"], "max_price": flt["max_price"]}
        cards, total = [], 0
        for cat, subs in scopes:
            left = (limit - len(cards)) if limit else None
            if left is not None and left <= 0:
                break
            got, n = self.walk(limit=left, label=target["label"], category_id=cat, sub_ids=subs,
                               in_stock=in_stock, **extra)
            cards += got
            total += n
        if not cards:
            if not (in_stock or flt):
                self._explain_empty(target)
            base = sum(self.count(category_id=c, sub_ids=s) for c, s in target["scopes"])
            if not base:
                self._explain_empty(target)
            log(f"  {target['label']}: {base} products, none match the stock/filter constraints")
            return []
        recs = [self.listing_record(c) for c in cards]
        log(f"  {target['label']}: site reports {total}"
            f"{' in stock' if in_stock else ''}{' (filtered)' if flt else ''}, fetched {len(recs)}")
        return recs

    # ---- detail
    @staticmethod
    def parse_ref(ref):
        """-> ('id', int) | ('ean', str) | ('sku', str)"""
        s = (ref or "").strip()
        m = re.search(r"/Product/Details/(\d+)", s, re.I)
        if m:
            return "id", int(m.group(1))
        m = re.fullmatch(r"(ean|sku|code):\s*(\S+)", s, re.I)
        if m:
            return ("ean" if m.group(1).lower() == "ean" else "sku"), m.group(2)
        if re.fullmatch(r"\d{8,14}", s):
            return "ean", s                  # product ids are <= 6 digits; 8-14 digits = barcode
        if re.fullmatch(r"0\d{3,7}", s):
            return "sku", s                  # ids never start with 0; many Шифра do ("09625"),
                                             # but over half are plain 5-digit numbers: use sku:
        if re.fullmatch(r"\d{1,7}/?", s):
            return "id", int(s.rstrip("/"))
        raise Usage(f"cannot parse a product from {ref!r}; give a /Product/Details/<id> URL, an id, "
                    f"ean:<barcode> or sku:<Шифра>")

    def _find_card(self, pid, *needles):
        for n in needles:
            if not n:
                continue
            data = self.filter_products(description=n, page=1, page_size=20)
            for c in data["productCards"]:
                if c["productId"] == pid:
                    return c
        return None

    def _id_for_code(self, kind, code):
        data = self.filter_products(description=code, page=1, page_size=50)
        field = "barCode" if kind == "ean" else "productCode"
        exact = [c for c in data["productCards"] if collapse(c.get(field)).lower() == code.lower()]
        if not exact:
            raise NotFound(f"no product with {kind} {code!r} (search returned "
                           f"{data['noOfProducts']} non-matching hit(s))")
        if len(exact) > 1:
            log(f"  note: {kind} {code} matches {len(exact)} products "
                f"({', '.join(str(c['productId']) for c in exact)}); using the first")
        return exact[0]["productId"]

    def detail_one(self, ref):
        kind, val = self.parse_ref(ref)
        if kind == "id" and re.fullmatch(r"\d{5,6}/?", (ref or "").strip()):
            # over half of all Шифра are 5-digit numbers without a leading zero (17477, 35370)
            # that overlap the product-id range, so a bare number is ambiguous
            log(f"  note: {ref.strip()!r} read as a product id; if it is a Шифра pass sku:{ref.strip()}")
        pid = val if kind == "id" else self._id_for_code(kind, val)
        url = product_url(pid)
        r = self._request("GET", url, allow_500=True)
        if r.status_code == 500:
            raise NotFound(f"product {pid}: HTTP 500 (the site answers 500 for unknown ids)")
        if 'id="productId"' not in r.text:
            raise RuntimeError(f"{url}: product block not found in page (layout change?)")
        soup = BeautifulSoup(r.text, "html.parser")
        wrap = soup.select_one("#product-details-wrapper")
        title_el = wrap.select_one("h4") if wrap else None
        title = collapse(title_el.get_text()) if title_el else None

        fields = {}
        for h6 in (wrap.select("h6") if wrap else []):
            m = re.match(r"([^:]+):\s*(.*)", collapse(h6.get_text(" ")))
            if m:
                fields[m.group(1).strip().lower()] = m.group(2).strip()
        price_span = wrap.select_one(".product-details-price-wrapper span.h3") if wrap else None
        old_el = price_span.select_one(".old-details-price") if price_span else None
        old_txt = collapse(old_el.get_text()) if old_el else ""
        if old_el:
            old_el.extract()
        html_price = to_int(price_span.get_text()) if price_span else None
        html_old = to_int(old_txt) if old_txt else None

        crumbs = [collapse(li.get_text()) for li in soup.select("ol.breadcrumb li.breadcrumb-item")]
        crumbs = [c for c in crumbs[1:-1] if c]  # drop "Почетна" and the product itself
        box = soup.select_one(".product-details-boxlayout .box-container")
        specs = collapse(box.get_text(" ")) if box else None

        sku = fields.get("шифра") or None
        ean = fields.get("баркод") or None
        stock_txt = fields.get("залиха") or None
        warranty_txt = fields.get("гаранција") or None

        card = self._find_card(pid, sku, ean, title)
        if card:
            rec = self.listing_record(card)
            if html_price is not None and rec["price_mkd"] != html_price:
                log(f"  note: {pid} JSON price {rec['price_mkd']} != page price {html_price}; "
                    f"using the page price")
                rec["price_mkd"] = html_price
                rec["regular_price_mkd"] = html_old if html_old and html_old > html_price else None
            if stock_txt:
                rec["stock_note"] = f"{stock_txt} (qty {card.get('quantity')})"
        else:
            log(f"  note: {pid} not found through the listing endpoint; page data only (no qty)")
            low = (stock_txt or "").lower()
            rec = {
                "store": STORE, "id": str(pid), "sku": sku, "title": title, "url": url,
                "brand": fields.get("производител") or None, "price_mkd": html_price,
                "regular_price_mkd": html_old if html_old and html_price and html_old > html_price else None,
                "in_stock": False if "нема" in low else (True if "има" in low else None),
                "stock_note": stock_txt, "category": None, "ean": ean,
            }
        rec["title"] = title or rec["title"]
        if crumbs:
            rec["category"] = " > ".join(crumbs)
        days = card.get("guaranteePeriodInDays") if card else None
        if days is None and warranty_txt:
            m = re.search(r"\d+", warranty_txt)
            days = int(m.group()) if m else None
        warranty = warranty_txt or (f"{days} дена" if days else None)
        if days == 0:
            warranty = None   # "0 дена" = not entered by the store, not "no warranty"
        rec.update({
            "input": ref,
            "ean": ean or rec.get("ean"),
            "warranty": warranty,
            "specs": specs or None,
            "per_location_stock": None,  # one web stock figure; no per-store breakdown exposed
            "delivery_estimate": DELIVERY_POLICY if rec.get("in_stock") else None,
            "extra": {
                "qty": card.get("quantity") if card else None,
                "warranty_days": days,
                "warranty_raw": warranty_txt,
                "vat_pct": card.get("tax") if card else None,
                "discount_pct": card.get("discountPercentage") if card else None,
                "coming_soon": bool(card.get("comingSoon")) if card else None,
                "expected_date": (card.get("futureDocumentDate") if card else None),
                "manufacturer_id": card.get("manufacturerId") if card else None,
                "category_id": card.get("categoryId") if card else None,
                "subcategory_id": card.get("subCategoryId") if card else None,
            },
        })
        return rec

    def detail(self, refs):
        """-> (records in input order, exit code). Failures become {"input","error"} rows.
        Exit 0 if at least one product resolved, else 2 (all not found) or 1."""
        out, ok, hard = [], 0, False
        for i, ref in enumerate(refs):
            try:
                out.append(self.detail_one(ref))
                ok += 1
            except Blocked as e:
                out.append({"input": ref, "error": f"blocked: {e}"})
                out.extend({"input": r, "error": "skipped: store blocked the client"}
                           for r in refs[i + 1:])
                raise _BatchBlocked(out, e)
            except NotFound as e:
                log(f"  not found: {ref} ({e})")
                out.append({"input": ref, "error": f"not found: {e}"})
            except Usage as e:
                log(f"  bad input: {ref} ({e})")
                out.append({"input": ref, "error": f"bad input: {e}"})
            except (RuntimeError, requests.RequestException, ValueError) as e:
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
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        qty = re.search(r"qty (\d+)", r.get("stock_note") or "")
        stock += f" {qty.group(1):>3}" if qty else "    "
        brand = f"[{r['brand']}] " if r.get("brand") else ""
        print(f"{price} MKD{reg}  {stock}  {brand}{(r.get('title') or '')[:80]}  {r['url']}")
        if "warranty" in r:
            print(f"        warranty {r.get('warranty')} | Шифра {r.get('sku')} | EAN {r.get('ean')} | "
                  f"{r.get('category')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        print(f"{r['id']:>8} {cnt}  {r['path']}  [{r['slug']}]")


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
    ap = argparse.ArgumentParser(description="Neksio (g.store.neksio.mk) catalogue client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", parents=[common])
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--category", help="restrict to a category (id, slug, url or name)")
    p = sub.add_parser("categories", parents=[common])
    p.add_argument("--grep")
    p.add_argument("--counts", action="store_true",
                   help=f"fetch product counts (one request per node, max {MAX_COUNT_REQUESTS})")
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
    c = Neksio(verbose=a.verbose)
    code = 0
    try:
        if a.cmd == "search":
            emit(c.search(a.query, limit=a.limit, in_stock=a.in_stock, category=a.category), a.json)
        elif a.cmd == "categories":
            try:
                re.compile(a.grep or "")
            except re.error as e:
                raise Usage(f"bad --grep regex: {e}")
            recs = c.categories(grep=a.grep, counts=a.counts)
            if a.grep and not recs:
                log(f"  no category matches {a.grep!r} (matched against name, path, slug, a Latin "
                    f"transliteration and English department words)")
            emit(recs, a.json, print_categories, "categories")
        elif a.cmd == "list":
            emit(c.list_category(a.category, in_stock=a.in_stock, limit=a.limit,
                                 filters=a.filter), a.json)
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
