# Neksio (g.store.neksio.mk): store reference

Client: `scripts/stores/neksio.py` (key `neksio`). Verified live 2026-10-03, then re-checked by an independent verifier on the same day
(list counts in 6 categories against single-call walks, 11 product pages parsed separately, search, grep, block stubs).

## What it sells and when it is useful

Neksio Computers (Skopje) is an IT **distributor** with its own webshop. The menu holds a few thousand products
in these groups, roughly largest first: peripherals (keyboards, mice, headphones); components (coolers, cases,
PSUs, RAM, HDD/SSD); cables/networking/adapters (cables, networking, converters); gaming gear (chairs, desks,
consoles, controllers, wheels, VR); home & office (small appliances: vacuums, fans, purifiers, e-scooters, TV
mounts); computers & laptops (mostly Dell); external storage; monitors; printers & consumables (few printers);
smartwatches; projectors.
There are effectively **no TVs**: ТВ И ДОДАТОЦИ (`9/88`) holds wall mounts and stands and hardly any TV sets. There are also no large
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
  `pageSize` cap, but latency grows roughly linearly with it. The client uses 100.
- **Response:** `noOfProducts`, `noOfPages`, `allManufacturers[]`, `allSubCategories[]`, `productMinMaxPrice`,
  and `productCards[]`. Each card has `productId`, `productName`, `productCode` (Шифра), `barCode`, `manufacturer(Id)`,
  `category(Id)`, `subCategory(Id)`, `priceWTax`, `old_PriceWTax`, `isOnSale`, `discountPercentage`, `tax`, `quantity`,
  `guaranteePeriodInDays`, `comingSoon` and `futureDocumentDate`.
- **Category tree:** the server-rendered side menu on `GET /`. On the first visit `/Shop` redirects to `/`.
- **Detail:** `GET /Product/Details/{id}` (HTML) gives the specs box, breadcrumb, "Гаранција" and
  the stock text. An **unknown id returns HTTP 500**, not 404. The client makes one more listing call by Шифра to get the
  card (qty, VAT, discount). It cross-checks the JSON price against the page and uses the page price if they differ (they matched in every test).
- Typical cost: `list` takes 1 request for the menu plus 1 per 100 products, so a category of a few hundred products
  takes well under half a minute. `detail <id>` takes 2 requests, and `ean:`/`sku:` inputs take 3. `categories --counts`
  with no grep makes one request per category and takes under a minute.

## Category model

- Three levels. **Menu groups** have ids `g209`–`g221` and slugs like `g:komponenti`. They are not a server
  concept: the client lists their categories one by one. **Categories** use `CategoryId` (`6`, slug `monitori`).
  **Subcategories** use `CategoryId/SubCategoryId` (`6/279`, slug `monitori/gejming-monitor`). The site has no slugs
  of its own (its URLs are `/Shop?CategoryId=6&SubCategoryId=279`), so the client builds slugs by transliterating the names.
- `list` accepts an id, a slug, a `/Shop?...` URL, `sub:279`, or an exact name. A category wins over a group of
  the same name (МОНИТОРИ is both). Ambiguous names such as `AMD` exit 2 and list the candidates.
  URLs may be scheme-less, in any case, and may end in `/` or `#...`. A query value that is not a number exits 2 and is
  never dropped, because dropping a `SubCategoryId` would silently widen the list to the whole parent category.
- A category includes its subcategories **plus products that have no subcategory** (ПРИНТЕРИ has some in none).
  Subcategories are often spec buckets (monitor size, SSD form factor, DDR generation).
- The site shows no counts (it uses infinite scroll). `noOfProducts` is what the frontend receives. `categories` returns
  `count: null`, and `--counts` fetches counts with one cheap request per node. The cap is 60 requests, so the
  whole tree gets category counts only; narrow `--grep` to get subcategory counts.
- `--grep` is a case-insensitive regex that tests each field on its own: name, path, slug, the Latin transliteration of name and path,
  and a small set of English department words (`mouse`, `ssd`, `printer`, `router`, `headset`, `gpu`, `ups`, `chair`, `vacuum`,
  `television`...). Anchors apply within one field (`^monitor`). Literal path matching means a Cyrillic group name pulls in its whole
  subtree (`мрежна` returns the whole group, including cables and KVM). The English words come only from a node's own name and, for a
  subcategory, its category's name, never from the menu group. So `router` returns the networking nodes, and `gpu` returns ГРАФИЧКИ КАРТИ
  with its AMD and NVIDIA subcategories.
- Short Latin terms hit transliterated substrings: `tv` matches "sof**tv**er" and "domakjins**tv**o". Use word boundaries (`\btv\b`).
- An unknown id gets HTTP 200 with 0 products from the endpoint. The client checks it against the menu and exits 2.

## Facets and filters

- The server filters by brand (`manufacturerIds`, OR), subcategory (`subCategoryIds`, OR), price (inclusive,
  on `priceWTax`) and stock. It has **no spec attributes**, so parse the spec-dense titles or the detail `specs`.
- `facets <cat>` walks the category and counts brand, subcategory and price range from the cards (exact, plus
  in-stock counts). Example: for HDD/SSD (category 4) it gives the per-brand counts and how many products are in `sub=103` (NVMe).
- `--filter` tokens are `brand=<id|name>`, `sub=<id|name>` and `price=MIN-MAX` (`price=-7000`, `price=5000-`).
  Commas OR inside a token, and several flags AND. Two different `brand=` flags AND to nothing and exit 2.

## Search semantics (verified)

- Every whitespace-separated word must appear as a **substring** (AND, in any order) of the **title, Шифра or barcode**.
  `monitor 27` and `27 monitor` return the same set. Descriptions, specs and category names are **not** searched.
- **Cyrillic is transliterated letter by letter on the server**: `монитор` returns the same as `monitor`, and `самсунг ссд`
  the same as `samsung ssd`. Macedonian nouns therefore find nothing (`тастатура`, `кабел`, `фрижидер` all return 0). Titles are
  English with a type prefix, for example `MONITOR 27" ...`, `MOUSE WIRELESS USB ...`, `SSD M.2 1TB ...`,
  `NET ROUTER WIRELESS ...`, `PRINTER LASER ...` and `CABLES ...`. Laptops are titled **`NOTEBOOK`**: `laptop` returns a few hits, mostly bags.
- Substring traps: `monitor` also hits `CABLES MONITOR` and monitor arms, and `ssd` hits mounting kits. Words of 1–2 letters
  match almost every title (the client warns).
- There is **no cap** (`e` returns nearly the whole catalogue) and no relevance ranking. Results come back sorted by price.
- **With search text the server ignores `categoryId`**. `monitor` returns the same hits with categoryId null, 6 or 13 (re-checked).
  The server still honours subcategory, brand, price and stock filters (`monitor` with subcategory 6/283 returns only
  that subcategory's hits). `search --category` therefore filters by category on the client side and by subcategory
  on the server side, and logs "N store-wide, M in the category".
  A zero-hit `search --category` checks the id against the menu, so an unknown category exits 2 instead of returning an empty list.
- Code lookup: an exact Шифра (`09625`, `17477`) or EAN returns exactly that product. `detail` also accepts `ean:<code>`,
  `sku:<code>`, a bare 8–14 digit EAN, or a Шифра with a leading zero (ids never start with 0). **Over half of all Шифра are plain
  5-digit numbers** (e.g. `17477`, `35370`) in the same range as product ids. A bare `17477` is read as product id 17477,
  which may not exist (HTTP 500) or may be a different product. Pass a Шифра as `sku:17477`.
  The client prints a note on stderr for any bare 5–6 digit input.
- Special queries: `#discount` returns the items on sale (`%` returns the same set); `#new` and `#commingSoon` also exist.

## Prices

- `price_mkd` = `priceWTax`, an integer in MKD with VAT included. This is the anonymous "Цена со ДДВ". VAT (`extra.vat_pct`) is per item:
  5% on most monitors and almost all HDD/SSD, and 18% on all networking gear, cables, small appliances and consumables.
  Mice are mixed (mostly 5%). The VAT is already included in the price, so it only matters for invoices.
  `retailPriceWTax` is always 0 for anonymous users. A partner login may see other prices (not checked).
- `regular_price_mkd` = `old_PriceWTax` (a formatted string, "15.660 ден.") and is set only when `isOnSale`. It is the
  struck-through price on the page, a real temporary markdown.
- **No price validity window is exposed** (checked 2026-10-03 on every `#discount` card, product pages and `/js/shop.js`), so
  records carry no `price_valid_until`. A card has no date field except `futureDocumentDate`, which is the expected restock date.
- **There is no online checkout** for anonymous buyers. The "Прашај/Нарачај" button opens an inquiry form (e-mail,
  phone, comment), or the buyer orders by phone or at sales@neksio.mk. Registration is optional.
- **No delivery fee is published** (Policies/PurchaseAndShipping), so `shipping_mkd` is not set. Ask the shop.

## Stock

- `in_stock` = `quantity > 0`. `stock_note` is "има на залиха (qty N)", "нема на залиха (qty 0)" or
  "наскоро / coming soon" (none on 2026-10-03). `quantity` is an **exact unit count** and a single
  web figure: `per_location_stock` is always null. The `--in-stock` server filter returned the same set as `qty > 0` in every check.
- Delivery policy (as of 2026-10-03): **up to 72 h (working days) after order confirmation**. Orders after 16:00 are confirmed the next
  working day, and the shop phones before delivering (its own service or Ин Пошта Радески). `detail` sets
  `delivery_estimate` to this for in-stock items. No supplier lead times are shown for out-of-stock items.

## Warranty

`detail.warranty` is the page text ("1080 дена"), and `extra.warranty_days` is the integer. Values seen on 2026-10-03 are 0, 180, 360, 600, 690, 720,
1050 and 1080 (36 months is common on monitors, 24 on Logitech mice and the Samsung 990 PRO). **0 means not entered** (seen on accessories such as
monitor arms, and every item in ПОТРОШЕН МАТЕРИЈАЛ), so `warranty` is null and the raw "0 дена" is kept in `extra.warranty_raw`.
Unused goods can be exchanged within 15 days (policy as of 2026-10-03).

## Data-quality traps

- The brand is as the shop files it, and it can be wrong or missing. An ASRock monitor is filed as GIGABYTE (20389). The Xerox 3025NI
  and Zebra ZD220DT have no brand at all. `LOGITECH` and `LOGITECH G` are separate brands. Match on the title.
- Some products are misfiled: a TP-Link T2U USB Wi-Fi adapter sits under ROUTER (17/127), and the projector categories open with desk
  mounts and a laser pointer (because results are sorted by price). Filter by title.
- Colour variants are separate products with the same price (STAR SOLUTIONS F4 black and white).
- The EAN is almost always present (nearly every monitor; the gaps cluster in networking), but it can be null.
- Expect occasional price typos. Sanity-check outliers.
- `/sitemap.xml` is stale (a small fraction of the catalogue). Do not use it.

## Bot protection and politeness (checked 2026-10-03)

- **No bot protection was found**: no CAPTCHA, JS challenge, rate-limit headers or login wall, and `robots.txt`
  returns 404. The antiforgery cookie is set but not enforced. A page past the end returns 200 JSON with no cards:
  an empty page, not a block.
- **The origin has very little capacity.** A burst of concurrent detail requests slowed each from under a second
  to many seconds (2026-10-01). Never parallelise. The client is sequential, waits 0.6 s between requests, retries
  429/502/503/504 with backoff, and exits 3 on a 401/403, challenge markup, a redirect to `/Login`, or a
  persistent 429.

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
| Slow runs | Normal: a few seconds per 100-row page. Do not raise the page size or parallelise. Use `--limit` or a subcategory. |
| Exit 3 `BLOCKED` | Not seen as of 2026-10-03. Check the logged status, title and cf-ray, then retry a single request later. |
