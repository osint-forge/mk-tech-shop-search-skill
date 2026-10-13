# DDStore (ddstore.mk): store reference

Client: `scripts/stores/ddstore.py` (key `ddstore`). Verified live on 2026-10-03. The numbers will drift.

## What it sells and when it is useful

An IT and electronics shop in Skopje (Фрањо Клуз 12) with a large catalogue (over ten thousand products). Departments, roughly largest first: Компјутерски компоненти (cases, cooling, PSU, RAM, SSD/HDD, GPU, CPU, UPS, cables) · Периферни уреди · Принтери, скенери и ПОС (mostly toner/ink) · Лаптопи, Таблети, Смартфони (laptops, phones, smartwatches) · Монитори, ТВ и проектори (mostly monitors; a small TV range) · Мрежи и комуникации · Гејминг · Апарати за дом (vacuums, kitchen, personal care) · Хоби, фото и мобилност · Паметен дом и безбедност · НОВИ Компјутери (prebuilt "DD" PCs) · Refurbished компјутери · Клима, греење и воздух (including split ACs) · Мебел, канцеларија и алат · Бела техника (a handful of products).

Use it for PC parts, peripherals, monitors, consumables, networking, refurbished gear and mid-range phones and TVs. Large appliances are a token range: fridges, dishwashers and cookers are empty categories.

## Access

- **Primary: anonymous Magento 2 GraphQL**, `POST https://ddstore.mk/graphql`.
  - Headers: `Store: mk`, a Chrome User-Agent, JSON body `{query, variables}`.
  - `categoryList` returns the whole tree with `product_count` in one call.
  - `products(search:, filter:{category_id, <attribute_code>:{eq|in}, price:{from,to}, sku:{in}}, pageSize, currentPage)` returns `total_count`, items and `aggregations`, which are the site's layered-navigation facets.
  - `route(url:)` resolves `<url_key>.html`, `pid/<sku>` and `catalog/product/view/id/<id>`.
  - `customAttributeMetadataV2` maps option ids to labels.
- **Cost:** `list` = 3 requests + 1 per 300 products (a department of a few thousand products walks in well under a minute); `search` = 2 + 1 per 300 hits; `detail` = 1–2 GraphQL requests + the heavy product page (over a megabyte) for the offer list + 1 more when there are several offers (`--no-offers` skips the page).
- **Fallback: server-rendered HTML** (`--backend html`; `auto` switches to it when GraphQL fails): category pages `/mk/<url_path>.html?<code>=<id>&p=N` (24 per page); product pages with JSON-LD and the "Повеќе информации" table; `categories` from the site menu (most but not all tree nodes, ids are url_paths). Listing cards carry no brand and no EAN; otherwise the backends match field for field (checked on TVs, TV equipment and internal SSDs: identical ids, sku, price, regular price, stock and url). "Прашај нѐ за цена" cards have no price box and no cart form; their id comes from the compare/wishlist link.
- **Never request `custom_attributesV2` without `filters:{is_visible_on_front:true}`.** Unfiltered, it exposes internal distributor names, distributor SKUs and purchase prices.
- **Server limits:** at most 10 aliases per query; `pageSize` up to at least 600; sort only by `name`/`position`/`relevance`/`brand` (no price sort); the `url_key` filter is ignored (returns the whole catalogue, so use `route`); `part_number` is not filterable.

## Category model

- **Shape:** the departments are the roots (level 2), most nodes are leaves, and nothing is below level 4.
- **ids** are numbers (`545`); the **slug** is the English-ish `url_path` (`monitorstvandprojectors/monitorsandequipment/monitors`, some with capitals); **names** are Macedonian. `list` accepts an id, slug, URL (query-string filters are kept) or exact name (`Монитори`).
- **Parents include all descendants**, as on the site. `product_count` equals the site's "од N" toolbar (checked on TVs, monitors, internal SSDs, TV equipment, laptops, tablets, split ACs and peripherals; menu counts matched the tree for every menu node). The client walks every page and checks against `total_count`.
- A few nodes are hidden from the menu (`in_menu: false`), mostly empty ones such as Бела техника > Фрижидери. `list` and `facets` on an empty category exit 2. Бела техника holds only washers (729), freezers (733) and dryers (730).
- `list` with `--filter`/`--in-stock` that matches nothing in a non-empty category is a genuine zero: exit 0, an empty list and a `WARNING: ... (genuine zero)` line on stderr.
- `--grep` matches name, path, slug and a Latin transliteration. `monitor`, `монитор`, `televiz`, `ssd`, `vacuum`, `laptop`, `klima`, `washing` and `телефон` all work. Because it searches paths, `monitor` also hits `HealthandMonitoring` and `tv` hits Софтвер (transliterated "softver"); anchors apply per field (`^Гејминг$`, `^gejming$`).
- Grep for the store's own words: washing machines are "Машини за перење" (`перење` or `washing`; `перални` finds nothing), fridges are `Refrigerators`/`Фрижидери` (`fridge` finds nothing), mice are `mice`/"Компјутерски глувци" (`mouse` finds only mouse pads and sets).

## Facets and filters

- **`facets <cat>`** returns the category's own layered navigation: brand, stock labels, promotion, warranty, condition, and per-category spec attributes. Spec attribute codes carry a category suffix: `diagonal_televisions`, `refresh_rate_televisions`, `form_factor_internal_ssd`, `interface_internal_ssd`, `capacity_internal_ssd`, ...
- **Tokens** are `code=<option id>`, exactly the site's URL parameters. Labels also work (`brand=Samsung`, `capacity_internal_ssd=1 TB,1024 GB`).
- **Combining:** comma-separated values OR (`brand=2220,2363` returns both brands' products); separate `--filter` flags AND; the same code given twice, or an unknown code, exits 2.
- **`price=FROM-TO` filters the NET price, excluding VAT**, the same as the site's slider and price facet. VAT varies by product: 5% on IT goods, 18% on TVs and appliances. TVs with `price=10000-20000` include some priced above 20,000 MKD.
- **`price_mkd=FROM-TO`** is a client token. It narrows the net price on the server, then filters the real price exactly.
- **Spec attributes are patchy.** Most TVs have `diagonal` = N/A, but the newer `diagonal_televisions` is filled. Some attributes overlap (`capacity_internal_ssd` vs `ssd_capacity`), and the same value can appear twice (`1 TB` vs `1024 GB`). Facets settle membership, not completeness, so cross-check titles.

## Search semantics (GraphQL, verified)

- **How words match:** ANDed (`samsung 55` gives 0), fuzzy (`samsng` finds Samsung products), Cyrillic and Latin treated as synonyms for common words (`робот правосмукалка` = `robot vacuum`, same hits; `логитех` ≈ `logitech`; `перална` finds the "Peralna Tesla" washers), over title and description. Phonetic Cyrillic spellings of brands are not always covered (`ајфон` gives 0 where `iphone` finds the iPhones). Single-letter words are ignored.
- **Results:** relevance order, with no cap. `usb` returns thousands of hits and all of them are retrievable.
- **Broad and noisy.** Accessories and description mentions count: `смартфон` and `телевизор` each give well over a thousand hits. For a category of goods, use `list`. Search is for model names.
- **Broader than the site's search box:** the same query finds more through GraphQL than through the site (`--backend html`): a little more for `ssd 1tb`, about half as many again for `телевизор`.
- **Codes:** the КОД/sku is indexed (a КОД returns its product; several SKUs in one query OR together). Model codes that appear **in the title** are found like any word (`MZ-VAP4T0BW`, `75MLED950`; fuzzy matching can add a false hit, as with `P220S1TB25`). **EAN and the Part Number attribute are not indexed**: an EAN returns 0, and an MPN that is only in Part Number, such as `210-BWLP`, returns unrelated toner hits. Match on the listing `ean`/`mpn` fields instead.
- **`--category`** restricts the search to a category. `--in-stock` filters on the server.

## Prices

- **`price_mkd`** = GraphQL `final_price` (VAT included) rounded half-up, which is what the site shows (6129.9 → "6.130 ден."). The FAQ confirms all prices include VAT. There is no club, card or web price. The Iute instalment widget is financing only.
- **`regular_price_mkd`** = the struck-through price, set only while a promotion runs (`attributes.promotion`, e.g. "Флеш Понуда").
- **`price_valid_until`: not exposed, so the client never emits it.** Flash offers ("Флеш Понуда") carry a `special_price` but no end date. In a walk of the whole catalogue on 2026-10-03, only one product had GraphQL `special_to_date`: a past date with no special price behind it. Product pages, the "Флеш понуди" listings and the Magnetix offer list show no countdown or end date either; the flash tooltip says only "Оваа цена важи само за онлајн нарачки".
- **Price 0 means "Прашај нѐ за цена"**, so `price_mkd` is null and `stock_note` says "price on request". These are almost all "По нарачка".
- **Multi-offer products ("N понуди", Magnetix):**
  - One product page can bundle 2–4 supplier offers, each a hidden product with its own price, stock and warranty. This is common: about half of a sample of SSD cards had several.
  - Listings and `price_mkd` show the default "winning" offer (usually the cheapest available). That offer can be a different hidden product from the listed one, so the listed item's own offer can cost more than the price the page shows.
  - `detail` lists every offer in `extra.offers` (`id`, `sku`, `price_mkd`, `in_stock`, `stock_note`, `warranty`, `ean`, `winner`). It appends to `stock_note` when a pricier offer is in stock while the winner is not (the cheap winner "ask", a dearer offer "1-3 дена").
  - The offer list's own stock words are `Call` (= Прашај за залиха), `48 Hours` (= 1-3 дена), `Yes` (seen only on "Во ДДСтор магацин" offers) and `по нарачка`. GraphQL enrichment replaces them with the real labels; `--backend html` maps them.
- **Delivery** (FAQ, as of 2026-10-03): free for orders over 3,000 MKD (parcels up to 5 kg, zone 1), otherwise 150 MKD. Heavier parcels and remote areas pay the courier tariff, quoted before payment. Cash on delivery adds 177 MKD + 1% and voids the free delivery. Pickup at the Skopje warehouse is free (Mon–Fri 09–16). `shipping_mkd` is 0 or 150 by that rule, so it is wrong for heavy goods: product weight is a placeholder 1 for every product.

## Stock

Every product carries exactly one label from `if_in_stock` / `if_out_stock`. Meanings are from the FAQ as of 2026-10-03:

| label | `in_stock` | meaning (FAQ) |
|---|---|---|
| 1-3 дена | true | at the distributor; 1–3 working days + courier (the most common label) |
| Во ДДСтор магацин | true | in their own warehouse; ready the same day if paid by 15:00 |
| Последно парче / Мала залиха / 3-5 дена / Нови количини | true | rare |
| **Прашај за залиха** | **null** | no confirmed distributor stock; they call back with quantity and lead time (about 30% of the catalogue) |
| По нарачка | false | not orderable online; supplier order, 1–6 weeks, ask; usually no price |
| Нема залиха | false | |
| Продадено | mostly null, some false | conflicting: Magento still says salable |

- There are no quantities and no per-store stock (`per_location_stock` is null). Stock syncs several times a day.
- **Every order is confirmed for stock and lead time before you pay.** An account is required to order but not to browse. Treat "1-3 дена" as likely, not guaranteed.
- `delivery_estimate` maps these labels to plain text.

## Warranty

`warranty` is in days, as the site shows it ("720 денови"); `extra.warranty_days` holds the integer. Typical values (2026-10-03): 360 / 720 / 1080 / 1800. `9999` shows on the site as "ПО КОМПОНЕНТИ" (per component, on DD-built PCs). `1` on consumables is a placeholder, not one day. About a fifth of products have no warranty value (null). Alternative offers can carry a different warranty.

## Data-quality traps

- **`ean`** = Part Number when it is a valid GTIN (the first valid one of a list like `EAN1,EAN2`). Otherwise the field holds an MPN (`mpn`: `210-BWLP`, `SM-S931BZSDEUC`), an internal number or `N/A`. EAN coverage varies by category: about 70% on monitors and vacuums, under half on TVs and about a fifth on phones. `detail` borrows the EAN from sibling offers when all of them agree (`extra.ean_from_offer`).
- **Brand** is the store's own option. It can be a distributor or house brand (`DDCom`).
- **Titles** mix English, Macedonian and distributor shorthand. Duplicate listings of one model can exist with different prices and stock: hidden offers, colour variants, refurbished "(OUTLET) Used ...". Check `attributes.condition`, which is Refurbished on refurbished stock.
- **URLs:** some products have no URL key, so their canonical is `/mk/pid/<sku>` and the `<url_key>.html` form returns 404. Always use the record `url`.

## Bot protection and politeness (checked 2026-10-03)

- Cloudflare challenges or blocks common tool and crawler user agents (403 "Just a moment...",
  `cf-mitigated: challenge`), by user agent only. The client sends a desktop Chrome UA and exits 3 with
  `BLOCKED: HTTP 403 ... cf-ray=...` on a challenge.
- **Do not treat `captcha` or `challenge-platform` in page HTML as a block.** Every page has Magento's captcha
  config and Cloudflare's passive beacon.
- **Load:** no 429 or rate-limit headers over a few hundred sequential requests. The origin queues concurrent
  requests (a parallel burst slowed each to seconds), so keep everything sequential; the client paces 0.5 s and
  backs off on 429/5xx.
- **robots.txt** disallows `/graphql`, `/rest/`, `/catalogsearch/`, `?q=` and most filter parameters (`brand=`,
  `price=`, `if_in_stock=`, ...). Its comments say this is for SEO and crawl budget. It allows `?p=` pagination,
  `/catalog/category/view/` and `/catalog/product/view/`, and sets no crawl-delay.
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
| `in_stock` null on many items | "Прашај за залиха" (about 30% of the catalogue). Run `detail`: another offer may be in stock, or ask the shop. |
| `price_mkd` null | "Прашај нѐ за цена" (mostly "По нарачка"). No online price exists. |
| Listing price differs from the product's own offer | Multi-offer: the page shows the winning offer. See `detail` → `extra.offers`. |
| GraphQL "Max Aliases ... 10" | Batch at most 10 aliased `route`s per query. |
