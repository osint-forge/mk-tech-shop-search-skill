#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.28", "beautifulsoup4>=4.11"]
# ///
"""Tehnomarket (https://www.tehnomarket.com.mk) catalogue client. See references/client-contract.md
and references/tehnomarket.md.

Tehnomarket's shop is a CodeIgniter (PHP) site behind LiteSpeed with a jQuery frontend. Listings are
loaded by the page itself through one AJAX endpoint per category or search; this client calls it the
same way (form-encoded POST, no cookies or tokens needed). The answer is JSON (served as text/html)
whose `products_list` is the server-rendered product grid, including each product's per-store
availability.

    POST /category/<id>/<slug>/page/<n>       page=<n> offset=<page size> orderby=id-desc
         [stock=1|2] [manufs=<brandId>] [pricerange=MIN-MAX]        category listing (all levels)
    POST /products/search/<query>/page/<n>    same form fields                       full-text search
    GET  /product/<id>[/<slug>]               product page (301 to the canonical slug)
    GET  /products/search?search=<nothing>    zero-hit search page: the mega-menu (category ids and
                                              slugs, 4 levels) plus a commented-out department list

The browser builds these from the URL hash (#page/2/manufs/1156/pricerange/..); a /page/N path on a
plain GET is ignored, which is why plain page fetches always show page 1.

    tehnomarket.py info
    tehnomarket.py search "<query>" [--limit N] [--in-stock] [--json PATH]
    tehnomarket.py categories [--grep REGEX] [--counts] [--json PATH]
    tehnomarket.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
    tehnomarket.py detail <url|id> [...] [--json PATH]
    tehnomarket.py facets <category> [--json PATH]
    common options: --quiet, -v/--verbose (log every request)

Category refs: a numeric id ("4335"), a slug ("televizori"), a category URL (hash filters such as
#page/1/manufs/1156/pricerange/// are honoured) or an exact menu name ("ТЕЛЕВИЗОРИ").
Filter tokens: brand=<id|name>[,...]  sub=<id|name>[,...]  price=MIN-MAX  stock=1
               (commas OR inside a token; several --filter flags AND)

Exit codes: 0 ok (incl. a genuine zero-hit search), 1 unexpected error,
2 bad usage / unknown category or product, 3 blocked (stderr line starting "BLOCKED:").
"""

import argparse
import html as htmlmod
import json
import re
import sys
import time
from urllib.parse import quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

STORE = "tehnomarket"
NAME = "Tehnomarket"
BASE = "https://www.tehnomarket.com.mk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PAGE_SIZE = 200          # the UI offers 8..64; 200 is honoured (~1.7 MB of HTML per page)
PACE_S = 0.5             # pause between consecutive requests
MAX_RETRIES = 5
SORT = "id-desc"         # unique key: stable pagination (the default price sort has ties)
MAX_COUNT_REQUESTS = 60  # budget for `categories --counts`
MAX_FACET_SUB_REQUESTS = 40
TREE_PAGE = "/products/search?search=zqxjvw"   # zero-hit search page: menu + department ids
# Department slugs (not in the menu; seen in the server's 301s on 2026-10-03). Only a first guess:
# a wrong slug costs one 301, which the client follows.
DEPT_SLUGS = {"3792": "kompjuteri-i-gejming", "3823": "tv-audio-video", "3824": "telefoni-i-chasovnici",
              "3776": "bela-tehnika", "4348": "ladenje-greenje-prochistuvachi", "3794": "mali-aparati",
              "4094": "nega-i-ubavina", "4135": "sport-i-gradina", "3803": "ostanato",
              "3812": "rezervni-delovi"}
DELIVERY_POLICY = ("store policy: basic home delivery 199 MKD, 2-7 working days; "
                   "pickup in store possible")
SHIPPING_MKD = 199

SELLS = ("Consumer-electronics and appliance chain (~24 stores, ~5,800 products online in 8 "
         "departments): computers and gaming (laptops, desktops plus a few PC parts, monitors, "
         "printers, tablets, "
         "consoles, storage, networking, peripherals, office chairs, UPS); TV/audio/video (TVs, "
         "soundbars, audio systems, car audio, cameras, batteries); phones, smartwatches, landline "
         "phones and PBX; large appliances (washing machines, dryers, fridges, freezers, cookers, "
         "built-in ovens/hobs/dishwashers, hoods, freestanding dishwashers, microwaves, boilers, water "
         "dispensers); cooling/heating (air conditioners, heaters, stoves, radiators, air "
         "purifiers, fans); small appliances (kitchen appliances, coffee machines, cookware, irons, "
         "vacuums incl. robot and stick); personal care; sport and garden (garden furniture and "
         "tools, bikes, e-scooters, toys, fitness, lighting and tools) and an OUTLET category.")
NOTES = ("price_mkd = the 'SMART цена', which the online cart charges every buyer (no card "
         "needed; verified in the cart); regular_price_mkd = the higher 'Редовна Цена' shown "
         "above it. No EAN anywhere; Шифра (sku) equals the product id. Listings carry "
         "per-store availability (yes/no for all 24 locations, no quantities). "
         "in_stock false = not orderable online. Search is AND over substrings of title AND "
         "description (noisy), no Cyrillic/Latin transliteration, and brackets or quotes in the "
         "query give 0 hits (the client retries without them); an exact product id jumps "
         "to that product. Facets: brand, sub-category, price, stock. No warranty field. "
         "Delivery 199 MKD, 2-7 working days. LiteSpeed blocks 'python-requests' user agents.")
CAPABILITIES = ["search", "categories", "list", "detail", "facets", "filter",
                "per_location_stock", "delivery_estimate"]

CHALLENGE_RE = re.compile(
    r"captcha|recaptcha|challenge|Just a moment|Attention Required|cf-chl|turnstile|"
    r"Access to this resource on the server is denied|403 Forbidden|Too Many Requests|"
    r"verify you are (a )?human", re.I)
# A 429/503 is a block straight away only when it carries a real challenge; a plain
# "Too Many Requests" page is backed off and retried first (contract: backoff on 429/503).
HARD_CHALLENGE_RE = re.compile(
    r"captcha|challenge|Just a moment|Attention Required|cf-chl|turnstile|"
    r"verify you are (a )?human", re.I)
SITE_MARKER_RE = re.compile(r"cat-nav|products-range|section[^>]+id=\"product\"|Техномаркет")
RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*од\s*(\d+)")

_MK_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "gj", "е": "e", "ж": "zh",
           "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
           "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ќ": "kj",
           "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "џ": "dj", "ш": "sh"}

# English words added to a category's grep haystack when its Macedonian name contains the stem,
# so `categories --grep fridge` finds ЛАДИЛНИЦИ. Lowercase stems.
_EN_TAGS = {
    "компјутер": "computer pc", "гејминг": "gaming", "лаптоп": "laptop notebook",
    "торби и ранци": "bag backpack", "вентилациони подлоги": "laptop cooler cooling pad",
    "адаптери и полначи": "adapter charger power supply", "конфигурации": "desktop pc computer components parts motherboard graphics card gpu ram memory "
                    "psu power supply cpu cooler",
    "тастатур": "keyboard", "глувч": "mouse mice mousepad", "веб камера": "webcam",
    "пц камери": "webcam", "контролер": "controller gamepad joystick", "монитор": "monitor display",
    "проектор": "projector", "софтвер": "software", "принтер": "printer", "тонер": "toner ink",
    "скенер": "scanner", "хартија": "paper", "таблет": "tablet", "е-читач": "e-reader ereader",
    "конзол": "console playstation xbox nintendo", "игри": "games", "мемори": "memory storage",
    "усб флеш": "usb flash drive", "мемориски картици": "memory card sd", "хдд": "hdd hard drive",
    "ссд": "ssd", "мрежн": "network networking", "безжична мрежа": "wifi wireless router",
    "жична мрежа": "wired network switch", "слушалки": "headphones headset earphones",
    "микрофон": "microphone", "звучници": "speakers", "столици": "chair gaming chair",
    "бироа": "desk", "кабли": "cable cables", "упс": "ups power", "заштита": "protection",
    "дискови": "disc media", "калкулатор": "calculator", "гпс": "gps navigation",
    "телевизор": "tv television", "лед тв": "led tv television", "олед": "oled tv television",
    "qled": "qled tv television", "миниled": "miniled mini led tv", "минилед": "miniled tv",
    "микроргб": "micro rgb tv", "тв ": "tv", "андроид уреди": "android tv box streaming",
    "антени": "antenna", "држачи": "mount bracket holder", "далечински": "remote control",
    "тв комоди": "tv stand cabinet", "дигитални приемници": "set-top box receiver dvb",
    "дом.кино": "home cinema soundbar player", "домашно кино": "home cinema theater",
    "саундбар": "soundbar", "блу-реј": "blu-ray bluray", "двд": "dvd", "аудио систем": "hifi audio system",
    "мини систем": "mini hifi system", "миди систем": "midi hifi system", "радио": "radio",
    "диктафон": "dictaphone voice recorder", "преносни звучници": "portable speaker bluetooth",
    "авто": "car", "автомобилск": "car", "појачала": "amplifier", "фотоапарат": "camera photo",
    "дслр": "dslr camera", "акциона камера": "action camera gopro", "камери": "camera camcorder",
    "трипод": "tripod", "батерии": "batteries battery", "полначи": "charger",
    "мобилни телефони": "phone phones smartphone mobile", "мобилен": "phone mobile",
    "заштита за екрани": "screen protector", "смартфон": "smartphone phone",
    "футроли": "case cover", "паметни часовници": "smartwatch watch", "нараквици": "fitness band",
    "фиксни телефони": "landline phone", "безжичен телефон": "cordless phone dect",
    "централи": "pbx", "пбх": "pbx", "факс": "fax", "бела техника": "large appliances white goods",
    "машини за перење": "washing machine washer laundry веш машина перална перални ves masina peralna",
    "машина за перење": "washing machine washer перална перални",
    "сушење": "dryer tumble dryer", "сушари за алишта": "tumble dryer clothes dryer сушара",
    "ладилни": "fridge refrigerator фрижидер frizider", "замрзнувач": "freezer", "шпорет": "cooker stove range oven рерна",
    "вградлива": "built-in", "вградување": "built-in", "фурни": "oven рерна", "плотни": "hob cooktop",
    "аспиратор": "cooker hood extractor", "машина за садови": "dishwasher",
    "машини за садови": "dishwasher", "микробранов": "microwave", "микровални": "microwave",
    "бојлер": "boiler water heater", "автомати за вода": "water dispenser cooler",
    "клима": "air conditioner ac aircon", "инвертер": "inverter air conditioner",
    "стандард сплит": "split air conditioner", "греење": "heating heater", "греалки": "heater",
    "камини": "fireplace", "печки": "stove", "пелети": "pellet stove", "радијатор": "radiator",
    "пепел": "ash vacuum", "квалитет на воздух": "air quality purifier", "прочистувач": "air purifier",
    "навлажнувачи": "humidifier", "одвлажнувачи": "dehumidifier", "вентилатор": "fan",
    "мали апарати": "small appliances", "кујнски апарати": "kitchen appliances",
    "апарати за леб": "bread maker", "мулти готвачи": "multicooker", "готвење": "cooking",
    "фритез": "fryer air fryer", "соковниц": "juicer", "блендер": "blender", "тостер": "toaster",
    "грил": "grill", "скари": "grill", "бокали": "kettle", "мелница за месо": "meat grinder mincer",
    "миксер": "mixer", "роботи": "food processor kitchen robot", "пасатор": "hand blender",
    "кујнски ваги": "kitchen scale", "кеси": "vacuum sealer", "ледомат": "ice maker",
    "кафе": "coffee", "еспресо": "espresso coffee machine", "кафемат": "coffee machine espresso",
    "мелница за кафе": "coffee grinder", "кујнски садови": "cookware kitchenware pots pans",
    "пегл": "iron steam iron", "даски за пеглање": "ironing board", "решоа": "hot plate",
    "правосмукал": "vacuum cleaner hoover", "стик": "stick vacuum cordless",
    "робот правосмукалки": "robot vacuum", "нега и убавина": "personal care beauty",
    "фенови": "hair dryer", "преси за коса": "hair straightener", "депилатор": "epilator",
    "бричење": "shaver razor", "потстрижување": "trimmer clipper", "четки за заби": "toothbrush",
    "лична нега": "personal care", "вага за телесна": "bathroom scale", "масажери": "massager",
    "спорт": "sport", "градина": "garden", "градинарски": "garden tools", "мебел": "furniture",
    "велосипед": "bike bicycle", "тротинет": "e-scooter scooter", "играчки": "toys",
    "фитнес": "fitness", "осветлување": "lighting", "алати": "tools", "гаранции": "warranty",
    "ваучери": "voucher gift card", "резервни делови": "spare parts", "додатоци": "accessories",
}


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


def translit(text):
    return "".join(_MK_LAT.get(ch, ch) for ch in (text or "").lower())


def en_tags(text):
    low = (text or "").lower() + " "
    return " ".join(v for k, v in _EN_TAGS.items() if k in low)


def to_int(value):
    """'22,999' -> 22999; '6.130' -> 6130; '5990,00' -> 5990; None/'' -> None"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    s = re.sub(r"[^\d,.]", "", str(value)).strip(".,")
    if not s:
        return None
    m = re.fullmatch(r"(.*?)[.,](\d{1,2})", s)
    if m:                                   # '5990,00', '1.234,56', '12.5' -> decimal part
        whole, frac = re.sub(r"[.,]", "", m.group(1)), m.group(2)
        s = f"{whole or 0}.{frac}"
    else:                                   # '22,999', '6.130', '1,234,567' -> separators
        s = re.sub(r"[.,]", "", s)
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def soup(html_text):
    return BeautifulSoup(html_text or "", "html.parser")


def norm_name(s):
    return collapse(s).upper()


# --------------------------------------------------------------------------- client

class Tehnomarket:
    def __init__(self, pace=PACE_S, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "mk,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.pace = pace
        self.verbose = verbose
        self._last = 0.0
        self._nodes = None
        self._slug_cache = {}
        self.requests_made = 0
        self._unknown_labels = set()

    # ---- transport
    @staticmethod
    def _evidence(r):
        title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
        return (f"HTTP {r.status_code} from {r.url}; server={r.headers.get('server')} "
                f"content-type={r.headers.get('content-type')} "
                f"title={collapse(title.group(1)) if title else None!r} bytes={len(r.content)}")

    def _check_block(self, r):
        head = (r.text or "")[:30000]
        if r.status_code in (401, 403):
            raise Blocked(self._evidence(r))
        if r.status_code in (429, 503) and HARD_CHALLENGE_RE.search(head):
            raise Blocked("challenge/limit page: " + self._evidence(r))
        if r.status_code == 200 and head.lstrip().startswith("<") and len(r.content) < 30000 \
                and CHALLENGE_RE.search(head) and not SITE_MARKER_RE.search(head):
            raise Blocked("interstitial instead of a shop page: " + self._evidence(r))

    def _request(self, method, url, allow_redirects=True, **kw):
        url = url if url.startswith("http") else BASE + "/" + url.lstrip("/")
        kw.setdefault("timeout", 60)
        delay = 2.0
        r = None
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, allow_redirects=allow_redirects, **kw)
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
                    f"{r.elapsed.total_seconds():.2f}s"
                    + (f" -> {r.headers.get('location')}" if r.is_redirect else ""))
            self._check_block(r)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    break
                ra = r.headers.get("retry-after")
                sleep_for = max(float(ra), delay) if ra and ra.isdigit() else delay
                warn(f"HTTP {r.status_code} from {url}, retrying in {min(sleep_for, 60):.0f}s")
                time.sleep(min(sleep_for, 60))
                delay *= 2
                continue
            return r
        if r is not None and r.status_code == 429:
            raise Blocked(f"HTTP 429 (rate limited) from {url} after {MAX_RETRIES} attempts; "
                          f"retry-after={r.headers.get('retry-after')} server={r.headers.get('server')}")
        raise RuntimeError(f"{method} {url}: still HTTP {r.status_code if r is not None else '?'} "
                           f"after {MAX_RETRIES} attempts (store down?)")

    def get(self, url, allow_redirects=True):
        return self._request("GET", url, allow_redirects=allow_redirects)

    def ajax(self, url, data):
        """POST a listing request the way the page's jQuery does. Returns (json|None, response).
        Redirects are not followed: a 301/302 means a wrong slug, an unknown id or (for search) a
        jump to a product page, and the caller decides."""
        r = self._request("POST", url, allow_redirects=False, data=data,
                          headers={"X-Requested-With": "XMLHttpRequest",
                                   "Accept": "application/json, text/javascript, */*; q=0.01"})
        if r.is_redirect:
            return None, r
        if r.status_code != 200:
            raise RuntimeError(f"POST {url}: HTTP {r.status_code}")
        body = (r.text or "").strip()
        if not body:
            raise RuntimeError(f"POST {url}: empty answer (the server rejects this filter "
                               f"combination; data={data})")
        try:
            d = json.loads(body)
        except ValueError:
            if CHALLENGE_RE.search(body[:20000]) and not SITE_MARKER_RE.search(body[:20000]):
                raise Blocked("challenge page instead of listing JSON: " + self._evidence(r))
            raise RuntimeError(f"POST {url}: expected JSON, got {body[:120]!r} (layout change?)")
        if not isinstance(d, dict) or "products_range" not in d:
            raise RuntimeError(f"POST {url}: unexpected listing payload keys "
                               f"{list(d)[:8] if isinstance(d, dict) else type(d).__name__}")
        return d, r

    # ---- category tree
    def nodes(self):
        """Flat list of category nodes: 8 menu departments (+2 hidden ones) > categories > up to
        two more levels. Menu ids/slugs from the mega-menu; department ids from the search page's
        commented-out category picker."""
        if self._nodes is not None:
            return self._nodes
        h = self.get(TREE_PAGE).text
        sp = soup(h)
        nav = sp.select_one("ul.cat-nav")
        if nav is None:
            raise RuntimeError("category menu (ul.cat-nav) not found on the search page; layout change?")
        # department ids (top-level inputs without a parentNNN class) and their direct children
        picker = re.findall(r'<input class="search-category\s*(?:parent(\d+))?"[^>]*value="(\d+)">'
                            r'(?:<b>)?<span[^>]*>([^<]*)<', h)
        dept_ids, hidden = {}, []
        for parent, cid, name in picker:
            name = collapse(htmlmod.unescape(name))
            if not parent:
                dept_ids[norm_name(name)] = cid
            hidden.append((parent or None, cid, name))
        nodes, by_id = [], {}

        def add(name, cid, slug, parent, level):
            path = (parent["path"] + " > " if parent else "") + name
            n = {"id": cid, "slug": slug, "name": name, "path": path,
                 "url": (f"{BASE}/category/{cid}/{slug}" if slug else f"{BASE}/category/{cid}")
                 if cid else None,
                 "parent": parent, "level": level, "children": [], "count": None}
            nodes.append(n)
            if parent:
                parent["children"].append(n)
            if cid and cid not in by_id:
                by_id[cid] = n
            return n

        def walk(ul, parent, level):
            for li in ul.find_all("li", recursive=False):
                a = li.find("a", recursive=False)
                if a is None:
                    continue
                name = collapse(a.get_text(" ", strip=True))
                m = re.search(r"/category/(\d+)/([^#?/\s]*)", a.get("href") or "")
                if m:
                    cid, slug = m.group(1), unquote(m.group(2)) or None
                else:
                    cid = dept_ids.get(norm_name(name))
                    slug = DEPT_SLUGS.get(cid)
                n = add(name, cid, slug, parent, level)
                sub = li.find("ul", recursive=False)
                if sub is not None:
                    walk(sub, n, level + 1)

        walk(nav, None, 0)
        # departments that exist only in the picker (ОСТАНАТО, РЕЗЕРВНИ ДЕЛОВИ) and their children
        for parent, cid, name in hidden:
            if cid in by_id:
                continue
            p = by_id.get(parent) if parent else None
            if parent and p is None:
                continue
            add(name, cid, DEPT_SLUGS.get(cid), p, (p["level"] + 1) if p else 0)
        if len(nodes) < 50:
            raise RuntimeError(f"only {len(nodes)} categories parsed from the menu; layout change?")
        self._nodes = nodes
        return nodes

    @staticmethod
    def public_node(n):
        return {"id": n["id"], "slug": n["slug"], "name": n["name"], "path": n["path"],
                "url": n["url"], "parent": n["parent"]["id"] if n["parent"] else None,
                "count": n["count"]}

    def categories(self, grep=None, counts=False):
        nodes = self.nodes()
        if grep:
            rx = re.compile(grep, re.I)
            sel = [n for n in nodes
                   if any(rx.search(f) for f in (n["name"], n["path"], n["slug"], translit(n["name"]),
                                                 translit(n["path"]), en_tags(n["name"]),
                                                 en_tags(n["path"])) if f)]
        else:
            sel = list(nodes)
        if counts:
            todo = [n for n in sel if n["id"]]
            if len(todo) > MAX_COUNT_REQUESTS:
                warn(f"--counts budget is {MAX_COUNT_REQUESTS} requests; {len(todo) - MAX_COUNT_REQUESTS} "
                     "categories left without a count (narrow --grep to get them)")
                todo = todo[:MAX_COUNT_REQUESTS]
            log(f"  fetching counts for {len(todo)} categories")
            for n in todo:
                try:
                    n["count"] = self.category_total(n)
                except NotFound as e:
                    log(f"  no count for {n['path']}: {e}")
        return [self.public_node(n) for n in sel]

    # ---- category resolution
    def _node_by_id(self, cid):
        for n in self.nodes():
            if n["id"] == str(cid):
                return n
        return None

    def resolve(self, ref):
        """-> dict(id, slug, name, path, node, extra) for a category reference."""
        ref = (ref or "").strip()
        if not ref:
            raise Usage("empty category reference")
        extra = {}
        cid, slug = None, None
        if re.match(r"(?i)^(https?://)?(www\.)?tehnomarket\.com\.mk/", ref) or ref.startswith("/category/"):
            u = urlparse(ref if "://" in ref or ref.startswith("/") else "https://" + ref)
            m = re.search(r"/category/(\d+)(?:/([^/#?]+))?", u.path)
            if not m:
                raise NotFound(f"not a category URL: {ref}")
            cid, slug = m.group(1), unquote(m.group(2)) if m.group(2) else None
            if u.fragment:
                segs = [x for x in u.fragment.strip("/").split("/")]
                hp = {segs[i]: segs[i + 1] for i in range(0, len(segs) - 1, 2)}
                for k in ("manufs", "pricerange", "stock"):
                    if hp.get(k):
                        extra[k] = hp[k]
                if extra:
                    log(f"  honouring URL filters {extra} (the site's pricerange filters the "
                        "regular price, not the SMART price)")
        elif re.fullmatch(r"\d+", ref):
            cid = ref
        else:
            low = ref.lower().strip("/")
            hits = [n for n in self.nodes() if n["slug"] and n["slug"].lower() == low]
            if not hits:
                hits = [n for n in self.nodes() if n["name"].lower() == ref.lower()
                        or translit(n["name"]) == translit(ref) or n["path"].lower() == ref.lower()]
            ids = {n["id"] for n in hits if n["id"]}
            if len(ids) > 1:
                raise NotFound(f"ambiguous category {ref!r}: "
                               + "; ".join(f"{n['id']} = {n['path']}" for n in hits) + " (pass the id)")
            if not ids:
                raise NotFound(f"unknown category {ref!r} (use `categories --grep` to find its id)")
            cid = ids.pop()
        node = self._node_by_id(cid)
        if node is None and not slug:
            slug = self._slug_cache.get(cid)
        if node is not None:
            slug = node["slug"] or slug or self._slug_cache.get(cid)
        name = node["name"] if node else None
        return {"id": cid, "slug": slug, "name": name, "path": node["path"] if node else None,
                "node": node, "extra": extra}

    def _listing_url(self, target, page):
        slug = target.get("slug") or self._slug_cache.get(target["id"]) or "x"
        return f"{BASE}/category/{target['id']}/{slug}/page/{page}"

    def _post_listing(self, target, page, size, params):
        """POST one listing page of a category, fixing the slug via the server's 301."""
        data = {"page": page, "offset": size, "orderby": SORT, "stock": 2}
        data.update(params or {})
        for _ in range(3):
            url = self._listing_url(target, page)
            d, r = self.ajax(url, data)
            if d is not None:
                return d
            loc = r.headers.get("location") or ""
            m = re.search(r"/category/(\d+)/([^/#?]+)", loc)
            if m and m.group(1) == str(target["id"]):
                target["slug"] = unquote(m.group(2))
                self._slug_cache[target["id"]] = target["slug"]
                continue
            raise NotFound(f"unknown category id {target['id']} (the shop redirects to "
                           f"{loc or 'nowhere'})")
        raise RuntimeError(f"category {target['id']}: redirect loop")

    @staticmethod
    def _range(d):
        m = RANGE_RE.search(collapse(d.get("products_range") or ""))
        return int(m.group(3)) if m else None

    def category_total(self, node_or_target, params=None):
        t = node_or_target
        target = {"id": t["id"], "slug": t.get("slug")}
        d = self._post_listing(target, 1, 1, params)
        if t.get("slug") is None and target.get("slug"):
            t["slug"] = target["slug"]
            if "url" in t:
                t["url"] = f"{BASE}/category/{t['id']}/{target['slug']}"
        return self._range(d) or 0

    def walk(self, target, params=None, limit=None):
        """All products of one category listing (server-side params applied). -> (lis, total, meta)"""
        # `rows` counts every grid row, duplicates included: the shop's total counts rows, and its SQL
        # sometimes returns one product twice (seen in search: 1001 rows = 1000 products).
        out, seen, page, total, meta, rows = [], set(), 1, None, {}, 0
        while True:
            d = self._post_listing(target, page, PAGE_SIZE, params)
            if page == 1:
                meta = {"manuf_filter": d.get("manuf_filter"), "min_price": d.get("min_price"),
                        "max_price": d.get("max_price")}
            total = self._range(d)
            if total is None:
                raise RuntimeError(f"no 'N од M производи' range in the listing answer for category "
                                   f"{target['id']} (layout change?)")
            size = to_int(d.get("offset")) or PAGE_SIZE
            if size != PAGE_SIZE and page == 1:
                warn(f"server clamped the page size to {size}")
            new = 0
            lis = soup(d.get("products_list")).select("ul.products > li[data-id]")
            rows += len(lis)
            for li in lis:
                pid = li.get("data-id")
                if pid in seen:
                    continue
                seen.add(pid)
                out.append(li)
                new += 1
            if limit and len(out) >= limit:
                break
            if rows >= total or new == 0 or page * size >= total:
                break
            page += 1
        if total and rows < total and not limit:
            warn(f"category {target['id']}: the site reports {total} products but only {rows} rows "
                 f"({len(seen)} distinct) came back over the pages (partial)")
        elif rows > len(seen) and not limit:
            log(f"  category {target['id']}: dropped {rows - len(seen)} duplicate row(s) the shop "
                "returned twice")
        return out, total or 0, meta

    # ---- parsing
    def _price_pairs(self, blocks):
        """[(label, value)] from price blocks such as 'Редовна Цена: 22,999 ден.'"""
        pairs = []
        for b in blocks:
            nm = b.select_one("span.nm")
            if nm is None:
                continue
            text = collapse(b.get_text(" ", strip=True))
            label = collapse(text.split(":")[0]) if ":" in text else ""
            if "рати" in text.lower():
                continue
            pairs.append((label, to_int(nm.get_text())))
        return pairs

    def _prices(self, pairs):
        regular, smart, other = None, None, {}
        for label, value in pairs:
            low = label.lower()
            if value is None:
                continue
            if "редовна" in low:
                regular = value
            elif "smart" in low or "смарт" in low:
                smart = value
            else:
                other[label or "?"] = value
                if label not in self._unknown_labels:
                    self._unknown_labels.add(label)
                    warn(f"unknown price label {label!r} ({value}); layout change? using the lowest "
                         "non-regular price")
        if smart is not None:
            price = smart
        elif other:
            price = min(other.values())
        else:
            price = regular
        reg = regular if (regular is not None and price is not None and regular > price) else None
        return price, reg, {"regular": regular, "smart": smart, **({"other": other} if other else {})}

    @staticmethod
    def _stock(container):
        """-> (in_stock, per_location_stock, stock_note)"""
        btn = container.select_one(".stock-green, .stock-red, [class*='stock-']")
        cls = " ".join(btn.get("class") or []) if btn else ""
        locs = []
        for dl in container.select("div[id^=stocks_dialog_] dl"):
            name = collapse(dl.get_text(" ", strip=True))
            icon = dl.select_one("dt i")
            ic = " ".join(icon.get("class") or []) if icon else ""
            ok = True if "icon-ok" in ic else (False if "icon-remove" in ic else None)
            if name:
                locs.append({"location": name, "in_stock": ok})
        if "stock-green" in cls:
            n_ok = sum(1 for x in locs if x["in_stock"])
            note = (f"orderable online; in stock in {n_ok} of {len(locs)} store(s) listed"
                    if locs else "orderable online")
            return True, locs or None, note
        if "stock-red" in cls:
            return False, None, "out of stock (not orderable online)"
        raw = collapse(btn.get_text(" ", strip=True)) if btn else None
        return None, locs or None, (f"unknown availability marker {cls!r}: {raw}" if btn
                                    else "no availability marker")

    def listing_record(self, li, category=None):
        pid = li.get("data-id")
        name_el = li.select_one(".product-name")
        a = (name_el.select_one("a[href]") if name_el else None) or li.select_one("a[href*='/product/']")
        title = collapse((name_el.get("title") if name_el else None) or (a.get_text(" ") if a else ""))
        url = a.get("href") if a else f"{BASE}/product/{pid}"
        if url and not url.startswith("http"):
            url = BASE + "/" + url.lstrip("/")
        pp = li.select_one(".product-price .pull-left") or li.select_one(".product-price")
        blocks = pp.find_all("div", recursive=False) if pp else []
        price, reg, raw = self._prices(self._price_pairs(blocks))
        if price is None:
            price = to_int(li.get("data-price"))
        brand = None
        for b in blocks:
            if "Производител" in b.get_text():
                st = b.select_one("strong")
                brand = collapse(st.get_text()) if st else None
        in_stock, locs, note = self._stock(li)
        badge = None
        m = re.search(r"-\d{1,2}%", collapse(li.get_text(" ")))
        if m:
            badge = m.group(0)
        rec = {"store": STORE, "id": pid, "sku": pid, "title": title, "url": url, "brand": brand,
               "price_mkd": price, "regular_price_mkd": reg, "in_stock": in_stock,
               "stock_note": note, "category": category, "ean": None,
               "per_location_stock": locs}
        if badge:
            rec["discount_badge"] = badge
        return rec

    @staticmethod
    def _warranty(specs):
        """Warranty stated in the free-text description ('Гаранција: 1 година', '2+3 ГОДИНИ
        ГАРАНЦИЈА', '2-годишна гаранција'). Part warranties ('мотор со 10 години гаранција') are
        skipped. None when the text states none (the usual case)."""
        if not specs:
            return None
        unit = r"(?:години|година|годишна|год\.?|месеци|месец|месечна|мес\.?|years?|months?)"
        num = r"\d{1,2}(?:\s*\+\s*\d{1,2})?"
        pats = (rf"(?i)(?:гаранциј\w*|warranty)\W{{0,4}}({num})\s*-?\s*({unit})",
                rf"(?i)({num})\s*-?\s*({unit})\s+(?:\w+\s+)?(?:гаранциј\w*|warranty)")
        for pat in pats:
            for m in re.finditer(pat, specs):
                part = r"мотор|motor|компресор|compressor|панел|panel|батериј|battery"
                before = specs[max(0, m.start() - 40):m.start()].lower()
                after = specs[m.end():m.end() + 30].lower()
                if re.search(part, before) or re.match(rf"\W*(?:на|за|on|for)\s+(?:the\s+)?(?:{part})",
                                                       after):
                    continue
                n, u = re.sub(r"\s+", "", m.group(1)), m.group(2).lower()
                u = {"годишна": "години", "месечна": "месеци"}.get(u, u)
                return f"{n} {u}"
        return None

    def detail_from_page(self, html_text, final_url, ref):
        sp = soup(html_text)
        sec = sp.select_one("section#product")
        if sec is None:
            raise NotFound(f"no product section at {final_url} (delisted product or layout change)")
        pid = sec.get("data-id")
        url = sec.get("data-url") or final_url
        h = sec.select_one(".box-heading h3") or sec.select_one("h3")
        title = collapse(h.get_text(" ")) if h else None
        crumbs = sp.select("ul.breadcrumbs li")
        names, cat_id = [], None
        for li in crumbs[1:]:
            if "active" in (li.get("class") or []):
                continue
            names.append(collapse(li.get_text(" ")).rstrip(" /").strip())
            a = li.select_one("a[href*='/category/']")
            if a:
                m = re.search(r"/category/(\d+)", a["href"])
                cat_id = m.group(1) if m else cat_id
        desc = sec.select_one(".product-desc")
        brand = sku = None
        if desc is not None:
            txt = desc.get_text("\n")
            m = re.search(r"Производител:\s*\n?\s*([^\n]+)", txt)
            brand = collapse(m.group(1)) if m else None
            m = re.search(r"Шифра:\s*\n?\s*([^\n]+)", txt)
            sku = collapse(m.group(1)) if m else None
        in_stock, locs, note = self._stock(desc if desc is not None else sec)
        blocks = sec.select("div.price.product-price")
        price, reg, raw = self._prices(self._price_pairs(blocks))
        instal = None
        for b in blocks:
            t = collapse(b.get_text(" ", strip=True))
            if "рати" in t.lower():
                m = re.search(r"(\d+)\s*рати\s*x\s*([\d.,]+)", t)
                instal = f"{m.group(1)} x {to_int(m.group(2))} MKD" if m else t
        if price is None:
            btn = sec.select_one("button.add-to-cart[data-price]")
            price = to_int(btn.get("data-price")) if btn else None
        badge = None
        rel = sec.select_one(".display-relative")
        if rel is not None:
            m = re.search(r"-\d{1,2}%", rel.get_text(" "))
            badge = m.group(0) if m else None
        dpane = sec.select_one("#description")
        lines = []
        if dpane is not None:
            infos = dpane.select("span.info")
            if infos:
                lines = [collapse(x.get_text(" ")) for x in infos]
            else:
                lines = [collapse(x) for x in dpane.get_text("\n").split("\n")]
            lines = [x for x in lines if x]
        specs = "; ".join(lines) or None
        attrs = {}
        for x in (dpane.select("span.info") if dpane is not None else []):
            raw_t = x.get_text().strip()
            m = re.match(r"\s*([^:\t]{2,60}?)\s*(?::|\t)\s*(.+)$", raw_t, re.S)
            if m and collapse(m.group(2)):
                k = collapse(m.group(1))
                if k and k not in attrs:
                    attrs[k] = collapse(m.group(2))
        warranty = self._warranty(specs)
        ean = None
        if specs:
            m = re.search(r"(?i)\b(?:EAN|GTIN|баркод|barcode)\W{0,4}(\d{8,14})\b", specs)
            ean = m.group(1) if m else None
        img = sp.select_one("#product_gallery img")
        rec = {"store": STORE, "id": pid, "sku": sku, "title": title, "url": url, "brand": brand,
               "price_mkd": price, "regular_price_mkd": reg, "in_stock": in_stock,
               "stock_note": note, "category": " > ".join(names) or None, "ean": ean,
               "warranty": warranty, "specs": specs, "per_location_stock": locs,
               "delivery_estimate": DELIVERY_POLICY if in_stock else None,
               "shipping_mkd": SHIPPING_MKD,
               "extra": {k: v for k, v in {
                   "category_id": cat_id, "prices_shown": raw, "instalments": instal,
                   "discount_badge": badge, "attributes": attrs or None,
                   "image": img.get("src") if img else None,
                   "price_note": ("SMART цена = the price the online cart charges (no card needed)"
                                  if raw.get("smart") is not None else None)}.items() if v}}
        if ref is not None:
            rec["input"] = ref
        return rec

    # ---- listing / filters
    def _brand_map(self, meta):
        out = {}
        for m in re.finditer(r'<option value="(\d+)"[^>]*>([^<]+)</option>', (meta or {}).get("manuf_filter") or ""):
            out[m.group(1)] = collapse(htmlmod.unescape(m.group(2)))
        return out

    def parse_filters(self, tokens, target):
        """-> dict(brands=[ids]|None, subs=[targets]|None, price=(lo,hi)|None, stock=bool)"""
        f = {"brands": None, "subs": None, "price": None, "stock": False}
        brand_tokens = []
        for tok in tokens or []:
            if "=" not in tok:
                raise Usage(f"bad --filter {tok!r}: expected brand=, sub=, price= or stock= "
                            "(see `facets`)")
            k, v = tok.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k in ("brand", "manuf", "manufs"):
                brand_tokens.append([x.strip() for x in v.split(",") if x.strip()])
            elif k in ("sub", "category", "cat"):
                vals = [x.strip() for x in v.split(",") if x.strip()]
                subs = []
                base = target.get("node")
                pool = []
                if base is not None:
                    stack = list(base["children"])
                    while stack:
                        n = stack.pop()
                        pool.append(n)
                        stack.extend(n["children"])
                for x in vals:
                    if re.fullmatch(r"\d+", x):
                        # a sub-category must lie under the listed category, or `list 3833
                        # --filter sub=4335` would silently list TVs under the washers' label
                        if base is not None and x != str(target["id"]) \
                                and x not in {n["id"] for n in pool}:
                            raise Usage(f"sub={x}: not a sub-category of "
                                        f"{target.get('path') or target['id']} (see `facets` or "
                                        "`categories --grep`)")
                        subs.append(x)
                        continue
                    hits = [n for n in pool if n["name"].lower() == x.lower() or
                            (n["slug"] or "").lower() == x.lower() or translit(n["name"]) == translit(x)]
                    if len({n["id"] for n in hits}) != 1:
                        raise Usage(f"sub={x!r}: no unique sub-category of {target.get('path') or target['id']} "
                                    "by that name (use the id from `facets`)")
                    subs.append(hits[0]["id"])
                f["subs"] = subs if f["subs"] is None else [s for s in f["subs"] if s in subs]
            elif k == "price":
                m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", v.replace(" ", ""))
                if not m or not (m.group(1) or m.group(2)):
                    raise Usage(f"bad price filter {v!r}: use price=MIN-MAX, price=MIN- or price=-MAX")
                f["price"] = (int(m.group(1)) if m.group(1) else None, int(m.group(2)) if m.group(2) else None)
            elif k in ("stock", "in_stock", "instock"):
                f["stock"] = v.lower() in ("1", "true", "yes", "y")
            else:
                raise Usage(f"unknown filter key {k!r}: use brand=, sub=, price= or stock=")
        if brand_tokens:
            need_names = any(not re.fullmatch(r"\d+", x) for grp in brand_tokens for x in grp)
            bmap = {}
            if need_names:
                d = self._post_listing({"id": target["id"], "slug": target.get("slug")}, 1, 1, None)
                bmap = self._brand_map(d)
            ids = None
            for grp in brand_tokens:
                gids = set()
                for x in grp:
                    if re.fullmatch(r"\d+", x):
                        gids.add(x)
                        continue
                    hit = [i for i, n in bmap.items() if n.lower() == x.lower()]
                    if not hit:
                        raise Usage(f"brand {x!r} not offered in this category; brands: "
                                    + ", ".join(sorted(bmap.values())[:60]))
                    gids.update(hit)
                ids = gids if ids is None else ids & gids
            f["brands"] = sorted(ids or [])
            if not f["brands"]:
                f["brands"] = ["__none__"]
        return f

    def list_category(self, ref, in_stock=False, limit=None, filters=None):
        target = self.resolve(ref)
        f = self.parse_filters(filters, target)
        in_stock = in_stock or f["stock"]
        label = target["path"] or f"category {target['id']}"
        scopes = []
        if f["subs"] is not None:
            for sid in f["subs"]:
                t = self.resolve(sid)
                scopes.append(t)
        else:
            scopes.append(target)
        lo, hi = f["price"] or (None, None)
        params = dict(target.get("extra") or {})
        if in_stock:
            params["stock"] = 1
        if lo:
            # the server filters (and sorts) on the regular price; regular >= SMART, so only the lower
            # bound can be pushed to the server. The exact range is applied to price_mkd below.
            params["pricerange"] = f"{lo}-999999999"
        brand_runs = f["brands"] or [None]
        recs, seen, total_all = [], set(), 0
        for scope in scopes:
            cat_label = scope["path"] or scope.get("name") or label
            for b in brand_runs:
                if b == "__none__":
                    continue
                p = dict(params)
                if b:
                    p["manufs"] = b
                left = None if (limit is None or lo or hi) else limit - len(recs)
                if left is not None and left <= 0:
                    break
                lis, total, _meta = self.walk(scope, p, limit=left)
                total_all += total
                for li in lis:
                    pid = li.get("data-id")
                    if pid in seen:
                        continue
                    seen.add(pid)
                    r = self.listing_record(li, cat_label)
                    if (lo is not None and (r["price_mkd"] or 0) < lo) or \
                            (hi is not None and (r["price_mkd"] or 0) > hi):
                        continue
                    if in_stock and r["in_stock"] is not True:
                        continue
                    recs.append(r)
        if limit:
            recs = recs[:limit]
        if not total_all:
            if filters or in_stock or target.get("extra"):
                unfiltered = self.category_total(target)
                if unfiltered:
                    log(f"  {label}: no products match the filters (the category has {unfiltered})")
                    return []
            raise NotFound(f"category {label!r} returned 0 products (empty category, or a soft "
                           "block / layout change)")
        log(f"  {label}: site reports {total_all}{' (server-filtered)' if params or f['brands'] else ''}, "
            f"returning {len(recs)}{' in stock' if in_stock else ''}")
        if not recs and (lo is not None or hi is not None):
            log(f"  {label}: no products with a SMART price in {lo or 0}-{hi or 'max'} MKD")
        return recs

    def facets(self, ref):
        target = self.resolve(ref)
        label = target["path"] or f"category {target['id']}"
        lis, total, meta = self.walk(target, {})
        if not total:
            raise NotFound(f"category {label!r} returned 0 products")
        bmap = self._brand_map(meta)
        recs = [self.listing_record(li) for li in lis]
        counts, stock_n = {}, 0
        for r in recs:
            counts[r["brand"] or "?"] = counts.get(r["brand"] or "?", 0) + 1
            stock_n += 1 if r["in_stock"] else 0
        out = []
        name_to_id = {}
        for i, n in bmap.items():
            name_to_id.setdefault(n.upper(), i)
        for bname, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            bid = name_to_id.get(bname.upper())
            out.append({"name": "brand", "value": bname, "count": c,
                        "token": f"brand={bid}" if bid else f"brand={bname}"})
        out.append({"name": "stock", "value": "in stock (orderable online)", "count": stock_n,
                    "token": "stock=1"})
        prices = [r["price_mkd"] for r in recs if r["price_mkd"] is not None]
        if prices:
            out.append({"name": "price", "value": f"{min(prices)}-{max(prices)} MKD (SMART price)",
                        "count": len(prices), "token": f"price={min(prices)}-{max(prices)}"})
        node = target.get("node")
        if node is not None and node["children"]:
            kids = node["children"]
            if len(kids) > MAX_FACET_SUB_REQUESTS:
                warn(f"{len(kids)} sub-categories; counting only the first {MAX_FACET_SUB_REQUESTS}")
            for k in kids:
                cnt = None
                if k["id"] and kids.index(k) < MAX_FACET_SUB_REQUESTS:
                    try:
                        cnt = self.category_total(k)
                    except NotFound:
                        cnt = 0
                out.append({"name": "sub", "value": k["name"], "count": cnt,
                            "token": f"sub={k['id']}" if k["id"] else f"sub={k['name']}"})
        log(f"  {label}: {total} products, {len(counts)} brands, {stock_n} in stock")
        return out

    # ---- search
    @staticmethod
    def _code_variant(query):
        """Model codes are filed inconsistently ('QE-55S90HAEXXH' vs 'QE55S90HAEXXH'): toggle the
        hyphen after a short letter prefix, or drop hyphens inside a code. None if nothing changes."""
        out, changed = [], False
        for tok in query.split():
            if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):
                if "-" in tok.strip("-"):
                    tok, changed = tok.replace("-", ""), True
                else:
                    m = re.fullmatch(r"([A-Za-z]{1,3})(\d\w{2,})", tok)
                    if m:
                        tok, changed = f"{m.group(1)}-{m.group(2)}", True
            out.append(tok)
        return " ".join(out) if changed else None

    @staticmethod
    def _punct_variant(query):
        """The shop's search finds nothing once brackets, quotes or similar punctuation are in the
        query ('(demo)' gives 0 while 'demo' finds the '(Demo)' units). None if nothing changes."""
        alt = collapse(re.sub(r"[^\w\s\-/.+%]", " ", query))
        return alt if alt and alt != query else None

    def search(self, query, limit=None, in_stock=False, _fallback=True):
        query = collapse(query)
        if not query:
            raise Usage("empty search query")
        recs = self._search(query, limit, in_stock)
        if recs is not None:
            return recs
        if _fallback:
            cur = query
            alt = self._punct_variant(cur)
            if alt:
                log(f"  fallback: no hits for {cur!r}; retrying as {alt!r} (the shop's search "
                    "finds nothing when brackets, quotes and similar punctuation are in the query)")
                recs = self._search(alt, limit, in_stock)
                if recs is not None:
                    return recs
                cur = alt
            alt = self._code_variant(cur)
            if alt:
                log(f"  fallback: no hits for {cur!r}; retrying as {alt!r} (model codes are "
                    "filed with and without hyphens)")
                recs = self._search(alt, limit, in_stock)
                if recs is not None:
                    return recs
        log("  note: search is AND over substrings of title and description, with no Cyrillic/Latin "
            "transliteration: try the other script ('televizor'/'телевизор'), the brand or "
            "model code, or `list` the category")
        return []

    def _search(self, query, limit, in_stock):
        """-> records, or None for a genuine zero-hit answer."""
        path = f"{BASE}/products/search/{quote_plus(query)}/page/"
        recs, seen, page, total, rows = [], set(), 1, 0, 0
        while True:
            d, r = self.ajax(path + str(page), {"page": page, "offset": PAGE_SIZE, "orderby": SORT})
            if d is None:
                loc = r.headers.get("location") or ""
                m = re.search(r"/product/(\d+)", loc)
                if m and page == 1:
                    log(f"  search {query!r}: the shop jumps straight to product {m.group(1)} "
                        "(exact Шифра/id match)")
                    rec = self.detail_one(m.group(1))
                    rec.pop("input", None)
                    if in_stock and rec.get("in_stock") is not True:
                        return []
                    return [rec]
                raise RuntimeError(f"search {query!r}: unexpected redirect to {loc!r}")
            total = self._range(d)
            if total is None:
                raise RuntimeError("no 'N од M производи' range in the search answer (layout change?)")
            new = 0
            lis = soup(d.get("products_list")).select("ul.products > li[data-id]")
            rows += len(lis)
            for li in lis:
                pid = li.get("data-id")
                if pid in seen:
                    continue
                seen.add(pid)
                new += 1
                rec = self.listing_record(li, None)
                if in_stock and rec["in_stock"] is not True:
                    continue
                recs.append(rec)
            if (limit and len(recs) >= limit) or rows >= total or new == 0 \
                    or page * PAGE_SIZE >= total:
                break
            page += 1
        if limit:
            recs = recs[:limit]
        if total and rows < total and not (limit and len(recs) >= limit):
            warn(f"search {query!r}: site reports {total} hits but only {rows} rows ({len(seen)} "
                 "distinct) came back (partial)")
        dup = (f"; {rows - len(seen)} duplicate row(s) dropped, so {len(seen)} distinct products"
               if rows > len(seen) else "")
        log(f"  search {query!r}: site reports {total} hit(s){dup}, returning {len(recs)}"
            f"{' in stock' if in_stock else ''} (newest first; matches title AND description text)")
        return recs if total else None

    # ---- detail
    def detail_one(self, ref):
        ref = str(ref).strip()
        if re.fullmatch(r"\d+", ref):
            url = f"{BASE}/product/{ref}"
        elif "tehnomarket.com.mk" in ref or ref.startswith("/product/"):
            u = urlparse(ref if "://" in ref or ref.startswith("/") else "https://" + ref)
            m = re.search(r"/product/(\d+)", u.path)
            if not m:
                if re.search(r"/category/\d+", u.path):
                    raise Usage(f"{ref} is a category URL; use `list`")
                raise Usage(f"not a product URL: {ref}")
            url = f"{BASE}/product/{m.group(1)}" + (u.path[m.end():] if u.path[m.end():] else "")
        elif re.match(r"(?i)^(https?://|www\.)", ref) or re.match(r"(?i)^[\w.-]+\.[a-z]{2,}/", ref):
            raise Usage(f"{ref} is not a tehnomarket.com.mk URL (route other shops' links to their "
                        "own client)")
        else:
            hits = self.search(ref, limit=3)
            if len(hits) == 1:
                rec = self.detail_one(hits[0]["id"])
                rec["input"] = ref
                return rec
            raise NotFound(f"{ref!r} is neither a product URL nor an id"
                           + (f"; the search found {len(hits)}+ candidates" if hits else ""))
        r = self.get(url)
        final = r.url
        if "/products/search" in final or "/product/" not in final:
            raise NotFound(f"no product {ref} (the shop redirects to {final})")
        if r.status_code == 404:
            raise NotFound(f"no product {ref} (HTTP 404)")
        if r.status_code != 200:
            raise RuntimeError(f"GET {url}: HTTP {r.status_code}")
        return self.detail_from_page(r.text, final, ref)

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
        stock = {True: "IN ", False: "OUT", None: " ? "}[r.get("in_stock")]
        brand = f"[{r['brand']}] " if r.get("brand") else ""
        print(f"{price} MKD{reg}  {stock}  {brand}{(r.get('title') or '')[:80]}  {r['url']}")
        if "specs" in r:
            print(f"        Шифра {r.get('sku')} | {r.get('category')} | {r.get('stock_note')} | "
                  f"warranty {r.get('warranty')}")


def print_categories(recs):
    for r in recs:
        cnt = f"{r['count']:>5}" if r.get("count") is not None else "    -"
        rid = r["id"] or "-"
        print(f"{rid:>8} {cnt}  {r['path']}  [{r['slug'] or '-'}]")


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
    ap = argparse.ArgumentParser(description="Tehnomarket (tehnomarket.com.mk) catalogue client")
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
    c = Tehnomarket(verbose=a.verbose)
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
                    "transliteration and English words for common departments)")
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
