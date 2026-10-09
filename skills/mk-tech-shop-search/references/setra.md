# Setra (setra.mk): store reference

Client: `scripts/stores/setra.py` (key `setra`). Verified live 2026-10-03, then independently re-verified the same day
(different categories and products). Counts drift.

## What it sells and when it is useful

Setra (СЕТРА ДООЕЛ, Skopje; one shop) is an IT, gaming and small-electronics retailer with **1,635 products**
(1,364 in stock, 269 out of stock, 2 on backorder) in 85 categories. Prices run from 150 to 339,990 MKD (median 4,350).
Top-level departments, with category counts (a product sits in 1–6 categories, so they overlap):
- **Peripherals:** Галантерија 607 (mice 167, headphones 113, keyboards 112, combos 34, speakers 45, bags 33, mouse pads 30)
  and Гејминг 584 (gaming keyboards 97, mice 95, PCs 83, headsets 63, coolers 63, monitors 59, cases 52, chairs 25).
- **PC parts:** Комјутерски Компоненти 255 (coolers 92, cases 70, PSUs 46, water cooling 40) and the brand category
  `be quiet!` 105. There are only 4 GPUs, 2 motherboards and 1 CPU.
- **Computers:** Компјутерски системи 105 (PowerCube prebuilt PCs 90, desktops 15), laptops 61 (Lenovo 49, Gigabyte 7,
  Dell 4), gaming laptops 7, tablets 10.
- **Monitors 93** (Dell 39, AOC 36). Printers 38 plus toners and cartridges 73. Networking 62 (routers, switches,
  extenders, adapters). UPS and stabilisers 57 (nJoy). Video surveillance 37 (IMOU, Xiaomi). Small appliances 56 and
  air purifiers 12 (mostly Xiaomi). Inverter air conditioners 21 (Vivax, Tesla, OZON). E-scooters 13, smartwatches 21.
- **Not sold:** phones (1 Nokia feature phone), TVs (TV boxes only), large appliances.

Best for Logitech (255), be quiet!/NZXT/Arctic parts, Dell/AOC monitors, Lenovo laptops, PowerCube PCs, nJoy UPSs.

## Access

WordPress + WooCommerce (Porto theme) on a bare Apache origin (no CDN; Wordfence installed). All anonymous: no cookies,
nonces or login.
- **Products:** Store API `GET /wp-json/wc/store/v1/products` with `category=<ids>` (comma = OR; parents include
  children), `search=`, `include=<ids>`, `stock_status=instock`, `per_page` (max 100), `page`, `orderby=id&order=asc`,
  `_fields`; totals in `X-WP-Total`/`X-WP-TotalPages`. `/products/<id|slug>` (404 JSON if unknown), `/products/categories`
  (85 terms, 1 page), `/products/collection-data?category=<id>&calculate_price_range=true&calculate_stock_status_counts=true`.
- **HTML (filter widget only):** the category pages `/product-category/<path>/` and `/shop/` carry the HUSKY/WOOF filter
  widget. `?swoof=1&<tax>=<slug,slug>&count=100` filters the listing; `count` goes above the 12/24/36 menu (700 worked).
  Pages are `/page/N/`. The main loop is the `ul.products` that does **not** have `is-shortcode`; the other is
  "Препорачани производи". WP Super Cache serves repeat HTML in 0.3–0.5 s.
- **Terms page:** `/wp-json/wp/v2/pages?slug=pravila-i-uslovi-na-prodazhba` gives the delivery wording (once per `detail` run).
- **Latency is set by the server.** A Store API call takes ≥2.5 s (2.8 s for 1 row, 5–7 s for 100) and uncached HTML takes 3–5 s.
  `list peripherals` (607) takes 9 requests and 43 s. A search takes 2–7 requests and 10–25 s; `detail` takes 4 plus 1 per slug.
- Rejected: `wc/v3`, `portowc/v1`, YITH `ywcas/v1` (401); `products/attributes`, `products/brands` (empty); HTML search (40–60 s).

## Category model

- **Identifiers.** Ids are numeric (`252`). Slugs are mostly English (`monitors`, `notebooks`, `peripherals`,
  `computer-components`, `psus`), with a few Latin transliterations (`gejming-masi`, `vodeni-ladilnitsi`). URLs are
  `/product-category/<parent>/<child>/`. `list` takes an id, slug, URL (`/page/N/` and `/en/` are ignored), exact name,
  or a comma list (union). An unknown category exits 2 with near matches; the API itself would return `200 []`.
- **Counts.** Parent counts include their descendants and equal `X-WP-Total` (Галантерија 607, components 255). Checked
  against the site's own listing (`?count=700`, every tile id): monitors 93, laptops 61, components 255 and
  peripherals 607 had **identical id sets and identical out-of-stock counts** (13/28/18/105). Re-verified at `?count=200`:
  Гејминг 584 (63 out) and Компјутерски системи 105 (15 out, 3 levels deep) identical; coolers `--in-stock` 88 = the
  page's 88 `instock` tiles. The site prints no "N results" text.
- **`--grep`** matches the name, path, slug, transliterations and English department words (`laptop`→Лаптопи,
  `klima`→Клима уреди, `component`→Комјутерски Компоненти, `smartphone`→Мобилни Телефони). The component department
  is misspelt **"Комјутерски"** on the site; the client adds the correct spelling as an alias, so `--grep компјутер`
  finds it along with Компјутерски системи. Not sold, so 0 matches: `перални`/`washing`, `телевизор`.

## Facets and filters

- `facets <cat>` reads the category page's filter widget (1 HTML request) and adds subcategories (`cat=`), the price
  range and stock counts (1 API request). Widget taxonomies: **brand** (`Brend`, "Производител"), `Display`
  ("Дисплеј", sizes), `Osvezuvanje` (refresh Hz), `cpu` (i5, Ryzen 7, Ultra 7...), `ram`, `Memory` (internal storage).
  Monitors show brand, Display and Osvezuvanje. Laptops add cpu and ram. Most other departments show brand only.
- **Tokens:** `brand=dell` (or `Brend=`), `Display=27-2`, `Osvezuvanje=144,180`, `cpu=ryzen-7`, `ram=32gb`,
  `price=MIN-MAX` (either end optional), `stock=in|out|backorder` and `cat=<id|slug>` (a parent matches through its
  descendants: products often carry only the leaf term, e.g. none of the 90 PowerCube PCs carries `powercube` itself,
  so `list computer-systems --filter cat=powercube` = 90). Commas OR within a token
  (`brand=dell,aoc` = 39+36 = 75), and repeated flags AND. Widget tokens need **one** category. Keys also match the
  labels and values match the term id, slug or name. An unknown key or value exits 2 and lists the valid ones.
- **How it runs:** the client fetches the filtered HTML listing (all pages) for ids, then the records through `include=`.
  Price, stock and `cat` are applied client-side. Checked: `brand=dell` on monitors gives 39 (= widget count),
  `brand=logitech` on peripherals gives 228 over 3 HTML pages (= widget count), and laptops `cpu=ryzen-7 ram=32gb
  price=-90000` gives 5. Two widget taxonomies AND on the site: Гејминг `brand=powercube cpu=ryzen-7` gives 24, a subset
  of the 27 PowerCube titles naming Ryzen 7 (the missing ones lack the cpu tag).
- **Widget tags are incomplete.** Display covers 85 of 93 monitors, cpu 60 of 61 laptops; `ram` also holds storage sizes
  (2TB, 512GB). `Display=27"` + 144/180 Hz gives 4 where titles show 6 (AOC 27G42E has no Display tag), and gaming monitors
  `Osvezuvanje=240,280` gives 13 where 17 titles say 240/280 Hz. Cross-check titles.

## Search semantics (verified)

- **The server matches the whole query as one case-insensitive substring of the title.** There is no word AND, and it
  does not search descriptions, the short description, SKUs or EANs. `gaming monitor` gives 54, `monitor gaming` 0,
  `nitor` = `monitor` = 94, `Гаранција` (in every short description) 0, and an EAN printed in a description 0.
- **The client emulates word-AND.** It counts each word (up to 4), walks the rarest and keeps titles containing every word.
  1–2 letter words are whole tokens. `dell monitor` gives 34 where the raw search gives 0; `rtx 5070` gives 9 where the raw
  search gives 3 (it adds the `RTX5070` PCs). `--phrase` sends the query verbatim, and `--category` restricts on the server.
- **Cyrillic.** Only 48 titles contain Cyrillic (AC units "ИНВЕРТЕР", PCs "+ ПОДАРОК"). A Cyrillic word with 0 hits
  is retried as Latin: `монитор` → monitor, 94; `самсунг монитор` → 1. `клима` finds 5 directly.
- **Titles often omit the product type.** `laptop`/`лаптоп` finds 2 bags (laptops are "Lenovo ThinkPad E14 …"). Every
  search prints matching categories on stderr; for a department use `categories` + `list`. No cap, no ranking (id order).
  Model codes in titles work (`S25BG400EU` 1, `27G4X` 2, `9800X3D` 3). `sku` is empty except on 1 product. EANs and part
  numbers printed only in descriptions cannot be searched (`943-000094` = Logitech G240's PN gives 0, with a stderr note).
  A nonsense query returns 0 with exit 0.

## Prices

- `price_mkd` = `prices.price / 10^currency_minor_unit` (minor unit **2**: `"279000"` = 2,790). VAT is included ("Цените се со
  пресметан ДДВ"), and it matches the page and JSON-LD. There is no card, cash or club price; the iute widget only shows instalments.
- WOOCS (currency switcher, EUR offered) is installed; the client refuses any `currency_code` other than MKD.
- Spot check (re-verification): 10 of 10 `detail` records matched the live page's visible price, JSON-LD offer price and
  availability, and `<h1>` (170 MKD cooler kit, 164,990 PowerCube, Nokia 105, three out-of-stock items including a gaming
  laptop, the backorder OZON AC = JSON-LD `BackOrder`, 27072 at 5,990, and two NZXT EANs printed on the page).
- **Strike-through prices are essentially unused:** 0 of 1,635 products have `regular_price > price` (`on_sale` false
  everywhere). **Trap:** 27072 NZXT H5 Flow carries a stale `sale_price` 4,990 while the page charges **5,990**.
  Never read `sale_price`; `detail` shows it as `extra.stale_sale_price_mkd`.
- **Sale window: not exposed**, so `price_valid_until` is never set. The Store API has no `date_on_sale_*` keys, `wc/v3`
  is 401, `wp/v2/product` `meta` holds only slider settings, and the Porto product page shows no countdown. The stale
  27072 `sale_price` is the only product on `on_sale=true`, which means a scheduled sale that is not running now (checked
  2026-10-03; site clock is a fixed UTC+2).
- **Delivery** (terms of sale): courier, **from 150 MKD** by size and weight; no free-delivery threshold is published, so
  `shipping_mkd` is unset. `extra.max_per_order` shows quantity limits (Ryzen 7 9800X3D: 1).

## Stock and delivery

- `in_stock` = `is_in_stock`, with one exception: **backorder** ("Достапно по нарачка"; 2 products, e.g. Epson L14150 and
  OZON 18k BTU AC) is `in_stock: null`. Those items are orderable but come from a supplier with no lead time.
  `stock_note` is the raw text ("Нема на залиха" for out of stock), or "in stock (no quantity shown)".
- There are **no quantities** (`low_stock_remaining` is always null) and no per-store split, so `per_location_stock` is null.
  Out-of-stock items stay listed. `--in-stock` uses the server's `stock_status=instock`, which excludes backorder.
- `delivery_estimate` (in-stock items, store-wide): "maximum 4 working days from the order confirmation", from 150 MKD.

## Warranty

`detail.warranty` is the text after "Гаранција:" in the short description (description as fallback);
`extra.warranty_months` normalises it. **~1,362 of 1,635 (83%)** have one: 12 months (550), 24 (359), 36 (218), 6 (139),
60 (32). 270 are empty (many Dell monitors). Spellings vary ("Гаранација", "24месеци", "360 дена", "5 години", "23M");
"0 месеци" = not entered (null). One model line can carry 12 or 24 months: confirm for big buys.

## Data-quality traps

- **Brand** is not in the API. The shop files brands in an HTML-only `Brend` taxonomy (85 brands). The client takes the
  earliest brand from that list that the title names, falling back to the "Бренд:" spec line. This gives 1,613 of 1,635,
  and it matched the widget counts exactly on monitors and laptops (on Гејминг it is within 1–2 of every widget count).
  Titles without a known brand ("PSU 700W PURE POWER 11", Brother toners, Kolink) give null. A brand right after
  "Compatible for"/"for" is the printer a third-party toner fits, not the maker, so those 24 toners give null (or the
  trailing maker, "… Compatible for HP 17A MS" → MS); "Genuine charger for ACER" stays Acer. The shop's own tag is
  inconsistent here (it files the compatible W1106A under HP). `list --filter brand=X` fills a null brand from the shop's tag.
- **EAN** appears only where a vendor description prints "EAN"/"UPC"/"GTIN" or nJoy's "GS1" (**101 products**, e.g.
  NZXT, Chieftec, Targus, nJoy UPS/PSUs). It must pass the checksum. The shop copies one description to every colour,
  so a label is often followed by a variant list: "EAN: X (White) / Y (Black)" and "EAN Code Black: X Black/Cyan: Y" are
  resolved by the variant the title names (longest tag wins); untagged multi-SKU lists (NZXT H3/H9 Flow, Kraken Plus)
  give null. All candidates are in `extra.ean_candidates`. **Shop copy-paste errors the client cannot see:** 19894
  Kraken 240 carries the Kraken 280's EAN and UPC, and 28308 nJoy 1000W carries the 850W's GS1 and PN. Confirm an
  EAN match by title before trusting it.
  **`mpn`** comes from the first "PN:" of the spec table (240 products: monitors, laptops, UPS, Logitech). It can carry
  shop typos (PN `W3225QF` for Alienware AW3225QF).
- **Gift bundles.** 29 titles end "+ ПОДАРОК <gift>" (PowerCube PCs, one Dell laptop), so searches for AOC (5), MARVO,
  "keyboard" (10) or "mouse" (10) also hit those PCs. The PowerCube Gaming Combo category is all bundles.
- **Duplicates.** The same title can appear under two ids with different stock; colour variants are separate products.

## Bot protection and politeness (evidence)

- **None triggered** on 2026-10-01/02/03. Today: 12 back-to-back **unpaced** Store API calls all returned 200 at 2.75–2.96 s
  with no drift. The python-requests default, empty, `curl/8.5.0` and `Python-urllib` UAs all got 200 on the API and on
  `/shop/`. 5 back-to-back uncached filtered HTML pages got 200 (3.3–4.0 s). There were no `Retry-After`,
  `X-RateLimit-*`, `cf-*` or Wordfence cookies; the only cookie is `language=mk`. About 30 probe requests in total.
- **Independent re-probe, 2026-10-03 (21 requests, sequential, unpaced):** 6 cache-busted product pages (`?nc=<random>`)
  with a `Scrapy/2.11.2` UA and no Accept-Language: all 200 in 3.0–3.3 s, full pages, no markers. 8 uncached Store API
  searches with the bare python-requests UA: all 200 in 3.3–4.7 s. `/wp-json/`, `/wp-json/wordfence/v1` and a HEAD on
  `/shop/` answered 200; a Chrome-UA API call afterwards took 3.1 s (no throttling). No `Retry-After`, rate-limit or `cf-*`
  headers; cookies only `language=mk` and `woocommerce_recently_viewed`. `wp-login.php` carries Wordfence and CAPTCHA
  strings, which is why bare "wordfence"/"captcha" are not block markers.
- **Concurrency hurts the origin.** Earlier: a 10-way burst queued to 3.4–4.2 s each; 5 concurrent uncached HTML searches
  got **HTTP 500 after 40–62 s** (PHP overload). The client is sequential (0.5 s pacing) and retries 429/5xx with backoff.
- **Wordfence risk:** it can answer 503/403 "Your access to this site has been limited". The client exits **3** on
  401/403, Wordfence/challenge text (no retry: lockouts escalate) or a persistent 429; `BLOCKED:` gives status, URL,
  title and marker. Normal pages contain "Turnstile"/"reCAPTCHA" form strings, so those are not HTML block markers.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Search finds few or no products of a type | Titles rarely name the type. Use the printed category hint, or `categories --grep` then `list`. |
| Multi-word search finds 0 | One word is not in any title (e.g. "monitor" on curved models). Drop it, or use `list` + `--filter`. |
| `--filter` exit 2 "unknown key/value" | The keys exist per category page. Run `facets <category>` and copy the token. |
| `--filter` "one category at a time" | Widget filters need a single category id or slug, not a union. |
| Filter result smaller than expected | Widget tags are incomplete. Re-check with `list` and title words (`Display` misses 8 of 93 monitors). |
| `list` exit 2 "store returned no products" | The category count is above 0 but the API gave nothing: a soft block or a change. Retry with `-v`. |
| Slow (10–40 s) | Normal: every API call costs 2.5–7 s server-side. Never parallelise against this store. |
| Exit 1 "still HTTP 500/503" | The origin is overloaded (often by parallel requests). Wait a minute, retry once. |
| Exit 3 `BLOCKED` | Wordfence lockout or WAF. Stop and retry later; do not loop. |
| `ean` null | Most listings print none, and untagged variant lists give null. Match by `mpn` or the title's model code; `extra.ean_candidates` (detail) lists what the description printed. |
| `--filter cat=<parent>` | Matches the parent and every descendant (the shop tags many products with the leaf only). |
