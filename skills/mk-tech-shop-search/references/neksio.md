# Neksio (g.store.neksio.mk): store reference

Client: `scripts/stores/neksio.py` (key `neksio`). Verified live 2026-10-03, then re-checked by an independent verifier on the same day
(list counts in 6 categories against single-call walks, 11 product pages parsed separately, search, grep, block stubs).

## What it sells and when it is useful

Neksio Computers (Skopje) is an IT **distributor** with its own webshop. The menu holds about **4,330 products**
in 12 groups: peripherals 1,301 (keyboards 436, mice 430, headphones 258); components 1,142 (coolers 427,
cases 191, PSUs 141, RAM 109, HDD/SSD 94); cables/networking/adapters 690 (cables 280, networking 227,
converters 108); gaming gear 329 (chairs, desks, consoles, controllers, wheels, VR); home & office 326 (small
appliances 160: vacuums, fans, purifiers, e-scooters, TV mounts); computers & laptops 169 (mostly Dell);
external storage 110; monitors 95; printers & consumables 66 (only 15 printers); smartwatches 56; projectors 48.
There are effectively **no TVs**: ТВ И ДОДАТОЦИ (`9/88`) holds 16 wall mounts and stands plus one out-of-stock 50" Metz set. There are also no large
kitchen or laundry appliances (`--grep 'перални|washing|фрижидер'` finds nothing).

Use it for peripherals, PC parts, cables, networking (TP-Link, Cudy, MikroTik) and monitors. It is the best
shop here for **exact stock quantities** and **EANs in nearly every listing** (about 97%), which makes cross-store matching easy.
The same catalogue is mirrored by IT.mk, SuperMart and the "Нексио" seller on Ananas (mkshop marks the
Ananas listings `mirror_of: neksio`). Count those offers once.

## Access

- **Listing and search:** `POST /FilterAndPaginateProducts`, JSON, the endpoint the shop's own `/js/shop.js` calls.
  The body **must have every key** (a partial body gets HTTP 500 HTML):
  `{categoryId, manufacturerIds[], subCategoryIds[], page, pageSize, description, selectedMinMaxPrice{minPrice,maxPrice}, orderBy, quantityStock}`.
  `categoryId` or `description` must be set (both null gives 500). `orderBy` 7 = price ascending (the client
  uses this), 8 = price descending, 9/10 = name. `quantityStock` 1 = all, 2 = in stock. There is no
  `pageSize` cap, but latency is roughly linear (100 rows about 2.4 s, 500 about 8.5 s). The client uses 100.
- **Response:** `noOfProducts`, `noOfPages`, `allManufacturers[]`, `allSubCategories[]`, `productMinMaxPrice`,
  and `productCards[]`. Each card has `productId`, `productName`, `productCode` (Шифра), `barCode`, `manufacturer(Id)`,
  `category(Id)`, `subCategory(Id)`, `priceWTax`, `old_PriceWTax`, `isOnSale`, `discountPercentage`, `tax`, `quantity`,
  `guaranteePeriodInDays`, `comingSoon` and `futureDocumentDate`.
- **Category tree:** the server-rendered side menu on `GET /` (334 KB). On the first visit `/Shop` redirects to `/`.
- **Detail:** `GET /Product/Details/{id}` (HTML, about 260 KB) gives the specs box, breadcrumb, "Гаранција" and
  the stock text. An **unknown id returns HTTP 500**, not 404. The client makes one more listing call by Шифра to get the
  card (qty, VAT, discount). It cross-checks the JSON price against the page and uses the page price if they differ (they matched in every test).
- Typical cost: `list monitori` (95) takes 2 requests, about 4.5 s. `list gluvchinja` (430) takes 6 requests, about 15 s.
  `detail <id>` takes 2 requests, and `ean:`/`sku:` inputs take 3. `categories --counts` with no grep takes 52 requests, about 43 s.

## Category model

- Three levels. **Menu groups** have ids `g209`–`g221` and slugs like `g:komponenti`. They are not a server
  concept: the client lists their categories one by one. **Categories** use `CategoryId` (`6`, slug `monitori`).
  **Subcategories** use `CategoryId/SubCategoryId` (`6/279`, slug `monitori/gejming-monitor`). In total there are 12 groups,
  51 categories and 191 subcategories. The site has no slugs of its own (its URLs are `/Shop?CategoryId=6&SubCategoryId=279`),
  so the client builds slugs by transliterating the names.
- `list` accepts an id, a slug, a `/Shop?...` URL, `sub:279`, or an exact name. A category wins over a group of
  the same name (МОНИТОРИ is both). Ambiguous names such as `AMD` exit 2 and list the candidates.
  URLs may be scheme-less, in any case, and may end in `/` or `#...`. A query value that is not a number exits 2 and is
  never dropped, because dropping a `SubCategoryId` would silently widen the list to the whole parent category.
- A category includes its subcategories **plus products that have no subcategory**: ПРИНТЕРИ has 15 products, 13 in subcategories and 2 in none.
  Subcategories are often spec buckets (monitor size, SSD form factor, DDR generation).
- The site shows no counts (it uses infinite scroll). `noOfProducts` is what the frontend receives. `categories` returns
  `count: null`, and `--counts` fetches counts with one cheap request per node. The cap is 60 requests, so the
  whole tree gets category counts only; narrow `--grep` to get subcategory counts.
- `--grep` is a case-insensitive regex that tests each field on its own: name, path, slug, the Latin transliteration of name and path,
  and a small set of English department words (`mouse`, `ssd`, `printer`, `router`, `headset`, `gpu`, `ups`, `chair`, `vacuum`,
  `television`...). Anchors apply within one field (`^monitor`). Literal path matching means a Cyrillic group name pulls in its whole
  subtree (`мрежна` returns 30 nodes, including cables and KVM). The English words come only from a node's own name and, for a
  subcategory, its category's name, never from the menu group. So `router` returns the 15 networking nodes, and `gpu` returns ГРАФИЧКИ КАРТИ
  with its AMD and NVIDIA subcategories.
- Short Latin terms hit transliterated substrings: `tv` matches "sof**tv**er" and "domakjins**tv**o". Use word boundaries (`\btv\b`).
- An unknown id gets HTTP 200 with 0 products from the endpoint. The client checks it against the menu and exits 2.

## Facets and filters

- The server filters by brand (`manufacturerIds`, OR), subcategory (`subCategoryIds`, OR), price (inclusive,
  on `priceWTax`) and stock. It has **no spec attributes**, so parse the spec-dense titles or the detail `specs`.
- `facets <cat>` walks the category and counts brand, subcategory and price range from the cards (exact, plus
  in-stock counts). Example: HDD/SSD (category 4) has 94 products, 27 brands (WD 17, Samsung 9...), and 40 of them are in `sub=103` (NVMe).
- `--filter` tokens are `brand=<id|name>`, `sub=<id|name>` and `price=MIN-MAX` (`price=-7000`, `price=5000-`).
  Commas OR inside a token, and several flags AND. Two different `brand=` flags AND to nothing and exit 2.

## Search semantics (verified)

- Every whitespace-separated word must appear as a **substring** (AND, in any order) of the **title, Шифра or barcode**.
  `monitor 27` and `27 monitor` both return 41. Descriptions, specs and category names are **not** searched.
- **Cyrillic is transliterated letter by letter on the server**: `монитор` returns the same 168 as `monitor`, and `самсунг ссд`
  returns 12. Macedonian nouns therefore find nothing (`тастатура`, `кабел`, `фрижидер` all return 0). Titles are
  English with a type prefix, for example `MONITOR 27" ...`, `MOUSE WIRELESS USB ...`, `SSD M.2 1TB ...`,
  `NET ROUTER WIRELESS ...`, `PRINTER LASER ...` and `CABLES ...`. Laptops are titled **`NOTEBOOK`**: `laptop` returns 19 hits, mostly bags.
- Substring traps: `monitor` also hits `CABLES MONITOR` and monitor arms, and `ssd` hits mounting kits. Words of 1–2 letters
  match almost every title (the client warns).
- There is **no cap** (`e` returns 4,233) and no relevance ranking. Results come back sorted by price.
- **With search text the server ignores `categoryId`**. `monitor` returns the same 168 hits with categoryId null, 6 or 13 (re-checked).
  The server still honours subcategory, brand, price and stock filters (`monitor` with subcategory 6/283 returns 18). `search --category`
  therefore filters by category on the client side and by subcategory on the server side, and logs "N store-wide, M in the category".
  A zero-hit `search --category` checks the id against the menu, so an unknown category exits 2 instead of returning an empty list.
- Code lookup: an exact Шифра (`09625`, `17477`) or EAN returns exactly that product. `detail` also accepts `ean:<code>`,
  `sku:<code>`, a bare 8–14 digit EAN, or a Шифра with a leading zero (ids never start with 0). **Over half of all Шифра are plain
  5-digit numbers** (279 of 498 sampled, e.g. `17477`, `35370`) in the same range as product ids (up to about 20,500). A bare `17477` is
  read as product id 17477, which here does not exist (HTTP 500); others resolve to a different product. Pass a Шифра as `sku:17477`.
  The client prints a note on stderr for any bare 5–6 digit input.
- Special queries: `#discount` returns the items on sale (41 today; `%` returns the same set), `#new` returns 54 and `#commingSoon` returns 0.

## Prices

- `price_mkd` = `priceWTax`, an integer in MKD with VAT included. This is the anonymous "Цена со ДДВ". VAT (`extra.vat_pct`) is per item:
  5% on most monitors (79 of 95) and HDD/SSD (93 of 94), and 18% on all networking gear, cables, small appliances and consumables.
  Mice are mixed (321 at 5%, 109 at 18%). The VAT is already included in the price, so it only matters for invoices.
  `retailPriceWTax` is always 0 for anonymous users. A partner login may see other prices (not checked).
- `regular_price_mkd` = `old_PriceWTax` (a formatted string, "15.660 ден.") and is set only when `isOnSale`. It is the
  struck-through price on the page, a real temporary markdown (Samsung 9100 PRO 1TB: 14,950, was 15,660, 5%).
- **No price validity window is exposed** (checked 2026-10-03 on all 41 `#discount` cards, product pages and `/js/shop.js`), so
  records carry no `price_valid_until`. A card has no date field except `futureDocumentDate`, which is the expected restock date.
- **There is no online checkout** for anonymous buyers. The "Прашај/Нарачај" button opens an inquiry form (e-mail,
  phone, comment), or the buyer orders by phone or at sales@neksio.mk. Registration is optional.
- **No delivery fee is published** (Policies/PurchaseAndShipping), so `shipping_mkd` is not set. Ask the shop.

## Stock

- `in_stock` = `quantity > 0`. `stock_note` is "има на залиха (qty N)", "нема на залиха (qty 0)" or
  "наскоро / coming soon" (none on 2026-10-03). `quantity` is an **exact unit count** (up to 301 seen) and a single
  web figure: `per_location_stock` is always null. The `--in-stock` server filter returned the same set as `qty > 0` in every check
  (monitors: 80 of 95).
- Delivery policy: **up to 72 h (working days) after order confirmation**. Orders after 16:00 are confirmed the next
  working day, and the shop phones before delivering (its own service or Ин Пошта Радески). `detail` sets
  `delivery_estimate` to this for in-stock items. No supplier lead times are shown for out-of-stock items.

## Warranty

`detail.warranty` is the page text ("1080 дена"), and `extra.warranty_days` is the integer. Values seen are 0, 180, 360, 600, 690, 720,
1050 and 1080 (36 months is common on monitors, 24 on Logitech mice and the Samsung 990 PRO). **0 means not entered** (seen on accessories such as
monitor arms, and every item in ПОТРОШЕН МАТЕРИЈАЛ), so `warranty` is null and the raw "0 дена" is kept in `extra.warranty_raw`. Unused goods can be exchanged within 15 days.

## Data-quality traps

- The brand is as the shop files it, and it can be wrong or missing. An ASRock monitor is filed as GIGABYTE (20389). The Xerox 3025NI
  and Zebra ZD220DT have no brand at all. `LOGITECH` and `LOGITECH G` are separate brands. Match on the title.
- Some products are misfiled: a TP-Link T2U USB Wi-Fi adapter sits under ROUTER (17/127), and the projector categories open with desk
  mounts and a laser pointer (because results are sorted by price). Filter by title.
- Colour variants are separate products with the same price (STAR SOLUTIONS F4 black and white).
- The EAN is almost always present (94 of 95 monitors; 13 of 498 sampled cards lacked one, 8 of them in networking), but it can be null.
- Expect occasional price typos. An earlier example was a keyboard combo at 80,850 MKD (id 2238). Sanity-check outliers.
- `/sitemap.xml` is stale (250 products). Do not use it.

## Bot protection and politeness (evidence 2026-10-03)

- **No bot protection was found.** The `python-requests/2.32.5` UA and an empty UA got HTTP 200 JSON on the POST and a full 261 KB product
  page. 12 back-to-back POSTs all returned 200 in 0.23–0.28 s, with no `Retry-After`, `X-RateLimit-*` or `cf-*` headers (only
  `Server: Microsoft-IIS/10.0`, `X-Powered-By: ASP.NET` and HSTS). `robots.txt` returns 404. There is no CAPTCHA, JS challenge or login
  wall. The antiforgery cookie is set but not enforced. About 200 requests were made on this date with no errors.
- Verifier re-probe (same day, 19 requests, different pattern): `ClaudeBot`, `GPTBot` and `curl` UAs all got the full 265 KB product
  page. 10 zero-delay sequential product GETs on one python-requests session were all 200 in 0.54–0.65 s with no drift. 3 concurrent
  20-row POSTs took 0.73–0.79 s each, and a page past the end (99) returned 200 JSON with 0 cards. An `en-US` Accept-Language gets the same
  Macedonian page and prices. Only `Server: Microsoft-IIS/10.0` and the antiforgery `Set-Cookie` came back, with no challenge or rate-limit headers.
- **The origin has very little capacity.** In earlier probes (2026-10-01), 25 concurrent detail GETs each took 16–17 s,
  against 0.6 s for a single request. Never parallelise. The client is sequential, waits 0.6 s between requests, retries
  429/502/503/504 with backoff, and exits 3 on a 401/403, challenge markup, a redirect to `/Login`, or a persistent 429.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Cyrillic search returns 0 | The query is transliterated, not translated. Use the English title word (`keyboard`, `cable`, `notebook`) or `list` the category. |
| `search --category` count differs from `list` | The server ignores categoryId in search, so the client filters. The count shown is "in the category". |
| Search returns cables or stands | Substring matching. Add a type word (`monitor 27 ips`), use `--category`, or `list` the subcategory. |
| `list` or `search --category` exit 2 "not in the site menu" | Wrong id, or the menu changed. Re-run `categories --grep`. |
| `list` 0 rows with exit 0 | The category has products, but none match `--in-stock` or `--filter`. |
| `detail` "HTTP 500" | Unknown or removed product id (the site answers 500, not 404), or a Шифра passed as a bare number. Retry with `sku:<code>`. |
| `detail <number>` returns an unrelated product | The number was a Шифра and was read as a product id. Pass `sku:<code>`. |
| `detail` note "page data only (no qty)" | The listing call could not find the card. Price and stock come from the HTML. |
| Exit 1 "expected JSON ... 500" | The endpoint body changed (a missing key gives 500). Compare with `/js/shop.js`. |
| Slow runs | Normal: about 2.4 s per 100-row page. Do not raise the page size or parallelise. Use `--limit` or a subcategory. |
| Exit 3 `BLOCKED` | Not seen so far. Check the logged status, title and cf-ray, then retry a single request later. |
