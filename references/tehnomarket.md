# Tehnomarket (tehnomarket.com.mk): store reference

Client: `scripts/stores/tehnomarket.py` (key `tehnomarket`). Verified live 2026-10-03, then
re-verified independently the same day (see the end of this file).

## What it sells and when it is useful

Tehnomarket is a national consumer-electronics and appliance chain: 24 locations appear in its stock
lists. 19 stores plus the central warehouse ("ДИРЕКЦИЈА Скопје") held stock in samples; 4 never did.
The webshop has about **5,815 products in 8 departments**:

- computers & gaming 1,692: laptops 48, desktops, monitors, printers, tablets, consoles, storage,
  networking, peripherals, chairs, UPS
- small appliances 1,114: kitchen, coffee, cookware, irons, vacuums including robot and stick models
- phones & watches 833: smartphones 224
- TV/audio/video 667: 183 TV sets
- large appliances 587: washers 72, fridges, freezers, cookers, built-in, dishwashers, boilers
- personal care 383
- cooling/heating/air 277: ACs, heaters, stoves, purifiers, fans
- sport & garden 262: bikes, e-scooters, toys, garden, fitness, OUTLET

Use it for TVs, large and small appliances, ACs, phones and mainstream laptops. **PC parts are a
sideline.** About 37 items share one category with desktop PCs: КОМПЈУТЕРИ И ГЕЈМИНГ > КОНФИГУРАЦИИ
(3995). They are mostly CPU coolers, plus DDR4/DDR5 RAM, PSUs, motherboards, SSDs and a single GPU,
and only 4 were in stock on 2026-10-03. There are no CPU, GPU or motherboard categories of their own.
`categories --grep 'motherboard|gpu|psu|ram'` finds 3995. Other SSDs, external HDDs, USB sticks and
memory cards have their own categories under КОМПЈУТЕРИ И ГЕЈМИНГ > МЕМОРИИ (4336; ССД is 4018).

It also sells through Ananas (`mirror_of: tehnomarket`). **No EAN anywhere**, so cross-store
matching must use the model codes in titles.

## Access

The site is CodeIgniter/PHP behind LiteSpeed with a jQuery frontend. A category page loads its grid
by AJAX, driven by the URL hash (`#page/2/manufs/1156/pricerange/..`). That is why a plain GET with
`/page/N` always shows page 1. The client calls the same endpoint (no cookies or tokens needed):

- **Listing:** `POST /category/<id>/<slug>/page/<n>`, form fields `page=<n> offset=<page size>
  orderby=id-desc stock=1|2 [manufs=<brandId>] [pricerange=MIN-MAX]`. It answers JSON served as
  text/html: `products_range` ("201 - 400 од 1692 производи"), `products_list` (the grid HTML),
  `manuf_filter` (brand `<option>`s with ids), `min_price`/`max_price` (regular prices). `offset` up to
  300 was honoured; the client uses 200 (~1.7 MB of HTML per page). `orderby` options are `price-asc`
  (site default), `price-desc`, `name-asc/desc` and `id-desc`; the client uses `id-desc` for stable
  paging.
- **Search:** `POST /products/search/<quote_plus(query)>/page/<n>` with the same fields.
- **Detail:** `GET /product/<id>` (301 to `/product/<id>/<slug>`), server-rendered HTML.
- **Tree:** the zero-hit search page `/products/search?search=zqxjvw` (~107 KB) carries the mega-menu
  (250 nodes, 4 levels, ids and slugs) and a commented-out category picker with the department ids.
  There is no `sitemap.xml` (404), no JSON product API and no structured data.

**Cost:** `categories` takes 1 request; `list` takes 1 plus 1 per 200 products (computers, 1,692
products: 10 requests, 30 s, mostly parsing). `detail` takes 1 per item. `search` takes 1 per 200 hits.

## Category model

- **Ids** are integers, mostly 4 digits (`4335` TVs, `4109` smartphones, `4003` laptops, `3787`
  washers, `3791` ACs, `433716` vacuums, `15843710` MiniLED TVs). Slugs are Latin transliterations
  (`televizori`, `mashini-za-perenje`).
- **Departments** have no menu link. Their ids come from the search page (3792 computers, 3823 TV,
  3824 phones, 3776 large appliances, 4348 cooling/heating, 3794 small appliances, 4094 care, 4135
  sport). They list fine. Two hidden departments exist: ОСТАНАТО 3803 (bags, vouchers, detergents,
  "Гаранции" 3817, which holds extended-warranty cards) and РЕЗЕРВНИ ДЕЛОВИ 3812.
- **Parents include all descendants**: washers 3787 = 7 top-load + 65 front-load = 72. **TVs 4335
  (290) includes 107 accessories** (ТВ ДОДАТОЦИ 3820: mounts, antennas, boxes). For TV sets, use
  `--filter sub=4329,4332,4306,15843710,15843711` (183).
- **Server quirks:** a wrong slug answers 301 to the canonical one, which the client follows. An
  unknown id 301s to `/category`, so the client exits 2. `list` accepts an id, slug, URL (hash filters
  `manufs`/`pricerange`/`stock` honoured) or exact name.
- **`--grep`** matches name, path, slug, a Latin transliteration and English words (`fridge`,
  `washing`, `tv`, `vacuum`, `air cond`, `laptop`, `phone`). `phone` also hits "headphones"; anchor
  with `\b`. `--counts` costs 1 request per category (max 60).
- Listed counts equal the site's own "1 - 32 од N производи" (checked: TVs 290, washers 72,
  computers 1,692, smartphones 224, laptops 48). An independent re-walk with another page size and
  sort matched id for id, with the same prices and stock: LED TVs 46-85" 59, washers/dryers 79,
  phones & accessories 618, monitors 39, coffee machines 107.
- `--grep` also knows colloquial names: `перална`/`перални` (washers), `фрижидер`, `рерна`
  (cookers, built-in ovens), `веш машина`.

## Facets and filters

`facets <cat>` walks the category once and returns brand counts (exact), the in-stock count, the
SMART price range and child categories (1 request each, max 40). Tokens: `brand=<id|name>[,...]`,
`sub=<id|name>[,...]`, `price=MIN-MAX` and `stock=1`. Commas OR within a token; repeated flags AND.
`sub=` must name a category under the one being listed (`list 3833 --filter sub=4335` exits 2).
- **The server takes one brand id per request** (a comma gives an empty body), so the client runs
  one walk per brand and merges them.
- **The server's `pricerange` filters the regular price**, which is ≥ the SMART price. The client
  sends only the lower bound and applies the exact range to `price_mkd`.
- There are no attribute facets (size, capacity, energy class). Use titles or `detail` specs.

## Search semantics

- **AND of substrings over the title and the description.** `samsung xyzzyq` gives 0; `Dolby Vision`
  gives 60 TVs and boxes through their spec text; `машина за перење` gives 78, including mixers and
  juicers. Description matches make results noisy, so prefer `list` for membership.
- **No transliteration:** `телевизор` 21 vs `televizor` 0; `frizider` 0. Titles are mostly Latin
  brand + model (+ a Cyrillic product noun on appliances). TV titles say "TV", ACs rarely say "клима":
  `клима инвертер` gives 2, while the inverter AC category holds dozens.
- **No cap, no relevance sort:** `samsung` 430, `55` 530 (substring: matches "4550"), `100%` 1,001.
  Results come newest first. `--in-stock` is client-side, because search ignores `stock`.
- **The reported total counts rows, and a product can come back twice:** `100%` reports 1,001
  but holds 1,000 distinct products (one product repeated, and which one varies between requests).
  The client pages until it has received every row and logs how many duplicates it dropped. It
  warns "partial" only when rows are really missing.
- **Brackets, quotes and apostrophes kill a query:** `(demo)` gives 0 while `demo` finds the
  "(Demo)" units. On 0 hits the client retries once with that punctuation removed, then with the
  model-code hyphen toggled (`(QE55S90)` → `QE55S90` → `QE-55S90`, 3 requests).
- **An exact product id redirects to the product** (search `29536627` returns that one item).
- **Model codes are filed inconsistently** ("QE-55S90HAEXXH" vs "QE55S90"). On 0 hits the client
  retries with the hyphen toggled and logs `fallback:`. A `/`, `+`, `%` or `&` inside a query works.
  Codes in the description match too (`UE98DU9072UXXH` finds the "UE-98DU9072UXXH" TV).
- **EANs never match:** no page carries one (`8806095467405` gives 0).

## Price semantics

- **Two prices are shown:** "Редовна Цена" (regular) and "SMART цена". SMART is nominally a
  loyalty-card price, but **the anonymous online cart charges the SMART price**: verified 2026-10-03,
  a 899/749 item went into a fresh session cart at 749. A second check by the verifier gave the same
  result: AEG LWR73864O (69,999/49,998, cart POST carrying price=69999) showed 49,998 in the cart.
  The cart takes its price from the server, not from the posted value. So `price_mkd` = SMART, and
  `regular_price_mkd` = Редовна when it is higher. When the two are equal the detail page shows only
  "Редовна Цена"; the client then returns it as `price_mkd`.
- **The regular price is mostly an anchor:** 2,449 of 3,393 products sampled (72%) are "discounted",
  median 20% (Samsung S95F 55" OLED 139,999 → 69,991, -50%). Phone cases reach -95% (999 → 50).
  The red "-NN%" badge (`discount_badge`) is just that gap; it matched the computed gap on all 1,507
  discounted items re-checked. Do not read it as a deal on its own; compare across shops.
- **Prices are whole MKD with VAT.** Instalments: "На 24 рати x N" (`extra.instalments`) is
  computed on the SMART price and is not available with online card payment.
- **No price validity window is exposed** (checked 2026-10-03), so records carry no
  `price_valid_until`. Listings, product pages and the frontend JS show no end date or countdown for the SMART
  price. The only dated data is the listing answer's `actions` list (`start_date`/`end_date` plus model codes),
  and those are overlay stickers such as "2+3 years warranty" or gift bundles, not price windows.
- **Delivery** (`/shipping-policy`): basic home delivery is 199 MKD within 2–7 working days, to the
  building entrance. Carry-in, upper floors without a lift and similar options cost extra and are
  paid in a store. Pickup in a store is possible. Detail records carry `shipping_mkd: 199`. No
  free-delivery threshold was found.

## Stock semantics

- `in_stock` true = green "залиха", an orderable add-to-cart. False = red "залиха": not orderable,
  but still listed with prices. 80% of 3,683 sampled products were in stock (TVs 217/290,
  computers 1,270/1,692, small appliances 930/1,114, large appliances 533/587).
- **Per-store availability is in every listing row** (`per_location_stock`, yes/no, no quantities):
  a modal listing all 24 locations. The detail page lists only the stores that carry the line (e.g. 16),
  with the same yes-count. Every orderable item was in at least one location (warehouse included).
  Out-of-stock items carry no store list (`null`).
- Four locations never showed stock in 3,900 items (НОВА БИТОЛА, ТИНЕКС-ГОДЕЛ, КАВАДАРЦИ, CAPITOL
  MALL); treat them as inactive. No lead times, supplier-order or pre-order states were seen.

## Warranty

There is no warranty field. `warranty` is filled only when the description text states one (31 of
~5,800 products mention "гаранција"); otherwise it is null. Recognised forms: "Гаранција: 1 година",
"2+3 ГОДИНИ ГАРАНЦИЈА", "2-годишна гаранција". Part warranties ("инвертер мотор со 10години
гаранција", "... на компресорот") are skipped. Extended-warranty cards are sold as products
(category 3817).

## Data-quality traps

- **Шифра = product id** (checked on 14 products, old ids such as 785700 and 735667 included);
  `sku` = id.
  Brands are as filed: `DRUGI PROIZVODITELI` ("other makers"), `ARISTON THERMO S.P.A.`.
- **Outlet units** ("(OUTLET)GORENJE TEG-5 O БОЈЛЕР") live only in СПОРТ И ГРАДИНА > OUTLET (4360, 20
  items), not in their real category. Demo units carry "(Demo)" in the title.
- Titles mix languages and formats; specs are free text (`extra.attributes` is a best-effort split on
  ":"/tab). The detail page's second store list is an HTML comment; it is not real data.

## Bot protection and politeness (evidence 2026-10-03)

- **LiteSpeed blocks the `python-requests` user agent** (GET and POST): 403, `server: LiteSpeed`,
  title "403 Forbidden", "Access to this resource on the server is denied!", 787 bytes. It is a
  case-insensitive substring match: `PYTHON-REQUESTS/2.0` and `Mozilla/5.0 python-requests/2.31` are
  blocked too. Empty UA, `curl/8.5.0`, `Python-urllib/3.14`, a Googlebot UA, `python-httpx`, `Wget`,
  `Scrapy`, `aiohttp` and `Go-http-client` all pass. There is no CAPTCHA, JS challenge, cookie wall
  or login wall (reCAPTCHA exists only on account forms).
- **No rate limiting seen:** 10 unpaced sequential listing POSTs all returned 200 in ~0.25 s, and 12
  unpaced product-page GETs all returned 200 in ~1.3 s each (product pages are slow, 115–145 KB).
  There were no `Retry-After` or `X-RateLimit` headers. robots.txt disallows only admin paths.
- **The client** uses a desktop Chrome UA, strictly sequential requests 0.5 s apart, and backoff on
  429/5xx (2, 4, 8, 16 s). On a block it exits 3 with `BLOCKED: HTTP 403 ... server=LiteSpeed
  title='403 Forbidden'` (tested by forcing the python-requests UA). A 429 or 503 is a block at once
  only when it carries a challenge (captcha, "Just a moment", cf-chl). A plain 429 is retried and
  becomes a block only after 5 attempts; a plain 503 becomes exit 1 ("store down?").

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Exit 3 `BLOCKED: HTTP 403 ... LiteSpeed` | The UA contains "python-requests", or an IP ban. Keep the Chrome UA; retry later. |
| Search returns 0 | No transliteration. Switch script, try the model code with or without hyphens, or `list` the category. |
| Search returns irrelevant items | Descriptions are searched. Filter by title or use `list`. |
| "TVs" includes mounts and antennas | 4335 includes ТВ ДОДАТОЦИ. Use the sub-ids above. |
| `list` exit 2 "unknown category id" | The id 301s to `/category`. Re-grep: ids change when the menu is rebuilt. |
| `ERROR ... empty answer` | The server rejected a filter combination (e.g. several brand ids in one request). Report it. |
| Price filter gives fewer items than the site | The site filters on the regular price; the client filters on the SMART price. |
| `detail` "no product" | Delisted: the id 302s to a search page. Search the model code. |
| `detail` "not a tehnomarket.com.mk URL" | Another shop's link; `mkshop.py detail` routes links by domain. |
| Search returns 0 for `(...)`, quotes or `l'...` | Brackets and quotes break the shop's search; the client already retried without them. |
| `WARNING ... partial` on search or list | Rows were really missing (the duplicate-row case is handled). Re-run; report if it persists. |

## Independent verification (2026-10-03)

- **Completeness:** for 5 categories the client's `list` matched an independent urllib walk (page
  size 64, name sort) id for id, 902 products in total. Parents include their children (3833 = 72 +
  7). `--in-stock` matched the green-button count exactly (618 → 559, 107 → 92). Paging was checked
  at page sizes 13, 20, 39 and 40 against a 39-item category, and `--limit` at page boundaries.
- **Fields:** 9 product pages parsed independently gave the same title, brand, Шифра, SMART and
  regular price and stock state as `detail`. The sample covered an expensive in-stock TV, out-of-stock
  items with and without a discount, a non-discounted washer, a 29-MKD case and a 143,999-MKD phone.
  The per-store yes-sets in listings equal those on the detail pages.
- **Search totals** equal the site's own search page (`xiaomi redmi` 90, `ладилник` 106, `bosch` 82).
