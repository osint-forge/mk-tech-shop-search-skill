# DDStore (ddstore.mk): store reference

Client: `scripts/stores/ddstore.py` (key `ddstore`). Verified live on 2026-10-03. The numbers will drift.

## What it sells and when it is useful

An IT and electronics shop in Skopje (Фрањо Клуз 12) with about 13,800 products. Departments (product counts): Компјутерски компоненти 3,961 (cases, cooling, PSU, RAM, SSD/HDD, GPU, CPU, UPS, cables) · Периферни уреди 2,633 · Принтери, скенери и ПОС 2,520 (2,318 toner/ink) · Лаптопи, Таблети, Смартфони 918 (laptops 255, phones 64, smartwatches 209) · Монитори, ТВ и проектори 899 (monitors 592, TVs 65, projectors 39) · Мрежи и комуникации 888 · Гејминг 527 · Апарати за дом 427 (vacuums 205, kitchen 101, personal care 79) · Хоби, фото и мобилност 257 · Паметен дом и безбедност 208 · НОВИ Компјутери 177 (prebuilt "DD" PCs) · Refurbished компјутери 135 · Клима, греење и воздух 130 (split ACs 60) · Мебел, канцеларија и алат 98 · Бела техника 7.

Use it for PC parts, peripherals, monitors, consumables, networking, refurbished gear and mid-range phones and TVs. Large appliances are a token range: fridges, dishwashers and cookers are empty categories.

## Access

- **Primary: anonymous Magento 2 GraphQL**, `POST https://ddstore.mk/graphql`.
  - Headers: `Store: mk`, a Chrome User-Agent, JSON body `{query, variables}`.
  - `categoryList` returns the whole tree with `product_count` in one call.
  - `products(search:, filter:{category_id, <attribute_code>:{eq|in}, price:{from,to}, sku:{in}}, pageSize, currentPage)` returns `total_count`, items and `aggregations`, which are the site's layered-navigation facets.
  - `route(url:)` resolves `<url_key>.html`, `pid/<sku>` and `catalog/product/view/id/<id>`.
  - `customAttributeMetadataV2` maps option ids to labels.
- **Cost:** `list` = 3 requests + 1 per 300 products (the 3,961-product department: 16 requests, about 20 s); `search` = 2 + 1 per 300 hits; `detail` = 1–2 GraphQL requests + the product page (about 1.3 MB) for the offer list + 1 more when there are several offers (`--no-offers` skips the page).
- **Fallback: server-rendered HTML** (`--backend html`; `auto` switches to it when GraphQL fails): category pages `/mk/<url_path>.html?<code>=<id>&p=N` (24 per page); product pages with JSON-LD and the "Повеќе информации" table; `categories` from the site menu (236 of 257 nodes, ids are url_paths). Listing cards carry no brand and no EAN; otherwise the backends match field for field (TVs 65/65, TV equipment 153/153, internal SSDs 171/171: identical ids, sku, price, regular price, stock and url). "Прашај нѐ за цена" cards have no price box and no cart form; their id comes from the compare/wishlist link.
- **Never request `custom_attributesV2` without `filters:{is_visible_on_front:true}`.** Unfiltered, it exposes internal distributor names, distributor SKUs and purchase prices.
- **Server limits:** at most 10 aliases per query; `pageSize` up to at least 600; sort only by `name`/`position`/`relevance`/`brand` (no price sort); the `url_key` filter is ignored (returns all 13,808 products, so use `route`); `part_number` is not filterable.

## Category model

- **Size and shape:** 257 nodes. There are 15 roots (level 2) and 203 leaves, and nothing below level 4.
- **ids** are numbers (`545`); the **slug** is the English-ish `url_path` (`monitorstvandprojectors/monitorsandequipment/monitors`, some with capitals); **names** are Macedonian. `list` accepts an id, slug, URL (query-string filters are kept) or exact name (`Монитори`).
- **Parents include all descendants**, as on the site. `product_count` equals the site's "од N" toolbar (checked TVs 65, monitors 592, internal SSDs 171, TV equipment 153, laptops 255, tablets 54, split ACs 60, peripherals 2,633; menu counts matched the tree for all 236 menu nodes). The client walks every page and checks against `total_count`.
- 9 nodes are hidden from the menu (`in_menu: false`), mostly empty ones such as Бела техника > Фрижидери. `list` and `facets` on an empty category exit 2. Бела техника holds only washers (729, 4), freezers (733, 2) and a dryer (730, 1).
- `list` with `--filter`/`--in-stock` that matches nothing in a non-empty category is a genuine zero: exit 0, an empty list and a `WARNING: ... (genuine zero)` line on stderr.
- `--grep` matches name, path, slug and a Latin transliteration. `monitor`, `монитор`, `televiz`, `ssd`, `vacuum`, `laptop`, `klima`, `washing` and `телефон` all work. Because it searches paths, `monitor` also hits `HealthandMonitoring` and `tv` hits Софтвер (transliterated "softver"); anchors apply per field (`^Гејминг$`, `^gejming$`).
- Grep for the store's own words: washing machines are "Машини за перење" (`перење` or `washing`; `перални` finds nothing), fridges are `Refrigerators`/`Фрижидери` (`fridge` finds nothing), mice are `mice`/"Компјутерски глувци" (`mouse` finds only mouse pads and sets).

## Facets and filters

- **`facets <cat>`** returns the category's own layered navigation: brand, stock labels, promotion, warranty, condition, and per-category spec attributes. Spec attribute codes carry a category suffix: `diagonal_televisions`, `refresh_rate_televisions`, `form_factor_internal_ssd`, `interface_internal_ssd`, `capacity_internal_ssd`, ...
- **Tokens** are `code=<option id>`, exactly the site's URL parameters. Labels also work (`brand=Samsung`, `capacity_internal_ssd=1 TB,1024 GB`).
- **Combining:** comma-separated values OR (`brand=2220,2363` returns 16 = 10 + 6); separate `--filter` flags AND; the same code given twice, or an unknown code, exits 2.
- **`price=FROM-TO` filters the NET price, excluding VAT**, the same as the site's slider and price facet. VAT varies by product: 5% on IT goods, 18% on TVs and appliances. TVs with `price=10000-20000` come back at 11,830–22,990 MKD.
- **`price_mkd=FROM-TO`** is a client token. It narrows the net price on the server, then filters the real price exactly: TVs at 10,000–20,000 MKD give 29 products.
- **Spec attributes are patchy.** 49 of 65 TVs have `diagonal` = N/A, but the newer `diagonal_televisions` is filled. Some attributes overlap (`capacity_internal_ssd` vs `ssd_capacity`), and the same value can appear twice (`1 TB` vs `1024 GB`). Facets settle membership, not completeness, so cross-check titles.

## Search semantics (GraphQL, verified)

- **How words match:** ANDed (`samsung 55` gives 0), fuzzy (`samsng` finds 278 Samsung products), Cyrillic and Latin treated as synonyms for common words (`робот правосмукалка` = `robot vacuum` = 83 hits; `логитех` ≈ `logitech`; `перална` finds the 4 "Peralna Tesla" washers), over title and description. Phonetic Cyrillic spellings of brands are not always covered (`ајфон` gives 0, `iphone` 33). Single-letter words are ignored.
- **Results:** relevance order, with no cap. `usb` returns 3,756 hits and all of them are retrievable.
- **Broad and noisy.** Accessories and description mentions count: `смартфон` gives 1,718 hits and `телевизор` 1,532. For a category of goods, use `list`. Search is for model names.
- **Broader than the site's search box:**

  | query | GraphQL | site (`--backend html`) |
  |---|---|---|
  | `ssd 1tb` | 276 | 241 |
  | `телевизор` | 1,532 | 977 |

- **Codes:** the КОД/sku is indexed (`1046194` and `1046287` return 1 hit each; several SKUs in one query OR together). Model codes that appear **in the title** are found like any word (`MZ-VAP4T0BW` 2 hits, `75MLED950` 1, `P220S1TB25` 2 including one fuzzy false hit). **EAN and the Part Number attribute are not indexed**: an EAN returns 0 (`8806095811703`), and an MPN that is only in Part Number, such as `210-BWLP`, returns unrelated toner hits. Match on the listing `ean`/`mpn` fields instead.
- **`--category`** restricts the search to a category. `--in-stock` filters on the server.

## Prices

- **`price_mkd`** = GraphQL `final_price` (VAT included) rounded half-up, which is what the site shows (6129.9 → "6.130 ден."). The FAQ confirms all prices include VAT. There is no club, card or web price. The Iute instalment widget is financing only.
- **`regular_price_mkd`** = the struck-through price, set only while a promotion runs (`attributes.promotion`, e.g. "Флеш Понуда"). Seen on 47 of 592 monitors and 1 of 65 TVs.
- **`price_valid_until`: not exposed, so the client never emits it.** Flash offers ("Флеш Понуда", 472 products on 2026-10-03) carry a `special_price` but no end date. In a walk of all 13,807 products, only one had GraphQL `special_to_date`: a past date with no special price behind it. Product pages, the "Флеш понуди" listings and the Magnetix offer list show no countdown or end date either; the flash tooltip says only "Оваа цена важи само за онлајн нарачки".
- **Price 0 means "Прашај нѐ за цена"**, so `price_mkd` is null and `stock_note` says "price on request". There are 225 of these among 3,961 components, almost all "По нарачка".
- **Multi-offer products ("N понуди", Magnetix):**
  - One product page can bundle 2–4 supplier offers, each a hidden product with its own price, stock and warranty. 12 of the first 24 SSD cards had several.
  - Listings and `price_mkd` show the default "winning" offer (usually the cheapest available). That offer can be a different hidden product from the listed one: on the 990 PRO 1TB page the listed item's own offer costs 12,980, but the page shows 12,739.
  - `detail` lists every offer in `extra.offers` (`id`, `sku`, `price_mkd`, `in_stock`, `stock_note`, `warranty`, `ean`, `winner`). It appends to `stock_note` when a pricier offer is in stock while the winner is not: Xerox toner at 6,000 "ask" vs 6,697 "1-3 дена"; 990 PRO 1TB at 12,739 "ask" vs 14,650 "1-3 дена".
  - The offer list's own stock words are `Call` (= Прашај за залиха), `48 Hours` (= 1-3 дена), `Yes` (seen only on "Во ДДСтор магацин" offers) and `по нарачка`. GraphQL enrichment replaces them with the real labels; `--backend html` maps them.
- **Delivery** (FAQ): free for orders over 3,000 MKD (parcels up to 5 kg, zone 1), otherwise 150 MKD. Heavier parcels and remote areas pay the courier tariff, quoted before payment. Cash on delivery adds 177 MKD + 1% and voids the free delivery. Pickup at the Skopje warehouse is free (Mon–Fri 09–16). `shipping_mkd` is 0 or 150 by that rule, so it is wrong for heavy goods: product weight is a placeholder 1 for every product.

## Stock

Every product carries exactly one label from `if_in_stock` / `if_out_stock`. Catalogue-wide counts:

| label | count | `in_stock` | meaning (FAQ) |
|---|---|---|---|
| 1-3 дена | 8,156 | true | at the distributor; 1–3 working days + courier |
| Во ДДСтор магацин | 662 | true | in their own warehouse; ready the same day if paid by 15:00 |
| Последно парче / Мала залиха / 3-5 дена / Нови количини | 44 / 16 / 11 / 0 | true | |
| **Прашај за залиха** | **4,158** | **null** | no confirmed distributor stock; they call back with quantity and lead time |
| По нарачка | 453 | false | not orderable online; supplier order, 1–6 weeks, ask; usually no price |
| Нема залиха | 261 | false | |
| Продадено | 47 | null (45) / false (2) | conflicting: Magento still says salable |

- There are no quantities and no per-store stock (`per_location_stock` is null). Stock syncs several times a day.
- **Every order is confirmed for stock and lead time before you pay.** An account is required to order but not to browse. Treat "1-3 дена" as likely, not guaranteed.
- `delivery_estimate` maps these labels to plain text.

## Warranty

`warranty` is in days, as the site shows it ("720 денови"); `extra.warranty_days` holds the integer. Typical values: 360 / 720 / 1080 / 1800. `9999` shows on the site as "ПО КОМПОНЕНТИ" (per component, on DD-built PCs). `1` on 1,698 consumables is a placeholder, not one day. 2,646 products have no warranty value (null). Alternative offers can carry a different warranty (990 PRO: 1800 vs 720).

## Data-quality traps

- **`ean`** = Part Number when it is a valid GTIN (the first valid one of a list like `EAN1,EAN2`). Otherwise the field holds an MPN (`mpn`: `210-BWLP`, `SM-S931BZSDEUC`), an internal number or `N/A`. EAN coverage: monitors 413/592, vacuums 146/205, TVs 27/65, phones 13/64. `detail` borrows the EAN from sibling offers when all of them agree (`extra.ean_from_offer`).
- **Brand** is the store's own option (598 options). It can be a distributor or house brand (`DDCom`).
- **Titles** mix English, Macedonian and distributor shorthand. Duplicate listings of one model can exist with different prices and stock: hidden offers, colour variants, refurbished "(OUTLET) Used ...". Check `attributes.condition`, which is Refurbished for 135 products.
- **URLs:** some products have no URL key, so their canonical is `/mk/pid/<sku>` and the `<url_key>.html` form returns 404. Always use the record `url`.

## Bot protection and politeness (evidence 2026-10-03, about 12:50 UTC, SOF POP)

- **Cloudflare managed challenge for non-browser user agents (flag).** On `/graphql` (category pages behave the same):

  | User-Agent | Result |
  |---|---|
  | python-requests default, empty, `curl/8.5.0`, `HeadlessChrome/140` | 403, `cf-mitigated: challenge`, title "Just a moment..." (cf-ray `a44ba49f6810d0d2-SOF`) |
  | GPTBot | 403 "Attention Required! \| Cloudflare" (block, `a44ba4b2ee47bdb7-SOF`) |
  | Chrome desktop | 200 |

  The rule is by User-Agent only; there is no TLS fingerprinting and no CAPTCHA or Turnstile with a browser UA. The client exits 3 with `BLOCKED: HTTP 403 ... cf-ray=...`.
  Re-probe at about 13:15 UTC the same day: it is a **blocklist of tool UAs**, not a browser allowlist. `Wget/1.24.5`, `Python-urllib/3.14` and python-requests got 403 "Just a moment..." (cf-ray `a44c397409abd0e4-SOF`, `a44c397d9f9bf439-SOF`, `a44c399cac6aa2bf-SOF`). Firefox, iPhone Safari, the bare string `chrome`, a Chrome UA without Accept-Language and even an unverified Googlebot UA got 200. `robots.txt` is exempt (200 for python-requests). Twelve back-to-back unpaced GraphQL requests all returned 200 in about 0.5 s each with no rate-limit headers.
- **Do not treat `captcha` or `challenge-platform` in page HTML as a block.** Every page has Magento's captcha config and Cloudflare's passive JSD beacon.
- **Load:** no 429, rate-limit headers or slowdowns over about 250 sequential requests today. The client paces requests 0.5 s apart and backs off on 429/5xx. The origin queues concurrent requests (earlier probe: 25 parallel requests took 1–5 s each), so keep everything sequential.
- **robots.txt** disallows `/graphql`, `/rest/`, `/catalogsearch/`, `?q=` and most filter parameters (`brand=`, `price=`, `if_in_stock=`, ...). The header comments say this is for SEO and crawl budget. It allows `?p=` pagination, `/catalog/category/view/` and `/catalog/product/view/`, and sets no crawl-delay.
  - The GraphQL backend goes against the `/graphql` rule. The storefront itself never calls GraphQL.
  - `--backend html` respects robots for plain listings and product pages, but not for search or filters.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Exit 3 `BLOCKED: HTTP 403 ... Just a moment` | UA lost or Cloudflare rule tightened. Check the session UA. If a Chrome UA is also challenged, nothing scriptable remains. |
| "GraphQL unavailable ... falling back to HTML" | Endpoint removed or schema changed. Results still come (no brand/EAN in listings). Re-probe `categoryList`. |
| `list` exit 2 "unknown category" | Use `categories --grep` (ids and slugs are stable; names can change). |
| `list` exit 2 "is empty" | A hidden or placeholder category (e.g. Фрижидери). Try the parent or `search`. |
| `list` exit 0, 0 rows, "WARNING ... genuine zero" | The category has products but none pass the filters / `--in-stock` / `price_mkd`. Loosen a filter. |
| Price filter returns "too expensive" items | `price=` is net of VAT. Use `price_mkd=LO-HI`. |
| `search` returns hundreds of accessories | Description matches. Use `list <category>` with `--filter`, or add a model word. |
| `search` 0 hits for an EAN or MPN | Not indexed. Use the КОД, a model name, or `ean` from a listing. |
| `in_stock` null on many items | "Прашај за залиха" (30% of the catalogue). Run `detail`: another offer may be in stock, or ask the shop. |
| `price_mkd` null | "Прашај нѐ за цена" (mostly "По нарачка"). No online price exists. |
| Listing price differs from the product's own offer | Multi-offer: the page shows the winning offer. See `detail` → `extra.offers`. |
| GraphQL "Max Aliases ... 10" | Batch at most 10 aliased `route`s per query. |
