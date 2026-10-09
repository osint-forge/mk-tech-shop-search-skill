# Ananas (ananas.mk marketplace): store reference

Client: `scripts/stores/ananas.py` (key `ananas`). Verified live 2026-10-03.

## What it sells and when it is useful

Ananas is a multi-seller marketplace (the MK branch of ananas.rs): **~285,000 listings from ~650 sellers**
in 24 departments. Listings by department (incl. size/colour variants): home and garden 102,895; fashion
44,953; car/moto 22,547 (tyres 5,871); DIY tools 17,903; toys 13,658 (LEGO 798); beauty 13,316 (perfume
1,071); baby 12,281; IT 9,567 (laptops 79); office/school 8,894; health 6,015; pet 6,014; sport 5,238;
books 4,155; phones/photo 3,847 (smartphones only 45); large appliances 3,511; small appliances 3,102;
food 2,496; TV/audio/video 2,193 (TVs 225); cleaning 1,359; gaming 1,323; personal-care appliances 1,010.

Use it for breadth (home, toys, beauty, tyres, DIY, small appliances) and as a second channel for big
chains. **Many listings mirror shops covered here, usually at the same price**: ТЕХНОМАРКЕТ, "Сетек Се од
Техника" (= Setec; its `extra.seller_sku` is Setec's own `sku`, e.g. 79269, same price), Нептун, Нексио,
Setra, PC MARKET (= Anhoch). Ananas' own shop ("АНАНАС ШОП", merchant 776) has ~96,000 listings, mostly
home and garden. TVs on 2026-10-03: ТЕХНОМАРКЕТ 90, Сетек 38, Бител 21, own shop 18, ELIPSO 13, Maximum 12.

## Access

All public, no cookies or tokens. The client needs ~2–4 requests per `list`/`facets`, 1 per 1,000 hits.

- **Algolia** (search, list, facets): `POST https://Y1BSBVJ7AC-dsn.algolia.net/1/indexes/prod_merchant_inventories_mk_mk/query`
  with headers `x-algolia-application-id: Y1BSBVJ7AC`, `x-algolia-api-key: 78d3f4f3befb3c4a68f4ebbf8c38fd81`
  (the public search-only key in `pages/_app-*.js`; re-read from the bundle on 401/403). Body:
  `{query, filters, page, hitsPerPage<=1000, attributesToRetrieve, facets, distinct:false}`.
  `facets:["*"]` returns every facetable attribute (384 index-wide). **Pagination stops at hit 30,000**;
  the client splits bigger walks into price ranges (Мода: 9 ranges, 44,953 listings, 72 requests, 28 s).
- **Category tree**: `POST https://api.ananas.rs/graphql-gateway/public`, headers `x-ananas-market: MK`,
  operation `Categories` (`getCategories(rootOnly:false)` with nested `children{id name slug}`): 2,247 nodes
  in one 300 KB response. Counts come from Algolia facets `product.categories.lvl0..lvl3` (lvl2 has >1,000
  values, so it is re-queried per group of departments, keeping only paths under that group's own
  departments: a listing filed in two departments otherwise overwrote counts, e.g. Кадифени играчки 1
  instead of 847; fixed 2026-10-03, all 2,247 counts now equal a per-department walk). If GraphQL fails,
  the tree is rebuilt from facets (paths only; no ids or slugs). Ids then exit 3 if the gateway blocked
  us, and 2 otherwise. Slugs and paths keep working.
- **Detail**: the product page `GET /proizvod/<slug>/<id>` (React flight data holds the merchant-inventory
  object), the Algolia hit by `objectID`, and the guest delivery promise via GraphQL
  `GetCalculateGuestDeliveryPromise`. 3 requests, ~2 s per item (2 for an out-of-stock listing). Input:
  product URL (query/fragment ignored), listing id, or Шифра (any case). `/proizvod/x/<id>` 308-redirects
  to the listing's own slug, and `url` is that URL. The page's `canonicalId` / `<link rel=canonical>` is
  the product's SEO-canonical offer, often **another seller's listing** (ТЕХНОМАРКЕТ 3929700 at 5,499 →
  King Soft 3409583 at 4,990). It goes only to `extra.canonical_offer_url`; before 2026-10-03 the client
  wrongly used it as `url`.
- **Category pages** `/kategorii/<slug>` are read only by `list --check-site` or for slugs not in the tree.

## Category model

- **Ids** are integers (`48397` TVs, `48508` smartphones, `49122` small kitchen appliances, `48602`
  perfume, `48038` LEGO). **Slugs** are Latin slash paths (`tv-audio-video/tv/televizori`). Depth ≤ 4
  (24 / 201 / 1,380 / 642 nodes per level). `list` accepts id, slug path, unique last slug segment
  (`televizori`), URL (`/kategorii/...`, `?page=` ignored), `A > B > C` path or a unique exact name.
  16 names are duplicated (Слушалки, Звучници, ...): exit 2 lists the ids.
- **Filter** = `product.categories.lvl<depth>:"<path>"`. Algolia values are the raw names joined with
  `" > "` and stripped; a few DIY/office names end in a space, which survives inside deeper paths.
- **Parents include all descendants** (hierarchical facets). Departments and some parents (LEGO) render
  as tiles with no product grid, so `--check-site` cannot read a total there.
- **Variants.** The index folds a seller's size/colour/volume variants into one hit (Algolia `distinct`
  on `group` = merchant + product group + colour). `list`/`search` turn that off and return every
  orderable listing; `--group-variants` reproduces the site's cards. Perfume: 1,071 listings vs 1,003
  cards; fashion 44,953 vs 23,977; electronics ≈1:1. `categories` counts are listings incl. variants.
- **Cross-checked 2026-10-03** (page total = Algolia cards = fetched): TVs 225, smartphones 45, small
  kitchen 815, perfume 1,003 cards (1,071 listings), LEGO City 78; tyres 5,871 over 6 pages. Independent
  verifier pass (the page's own "N производи", `--group-variants` count, default listing count): ТВ parent
  982/982/982 (= union of its 8 children), Мобилни телефони 55/55/56 (= smartphones + feature phones),
  Блендери 250, Робот правосмукалки 74, Машини за перење алишта 202, Монитори 266, Кадифени играчки
  844/844/847, Препарати за сончање 387/387/388. A price-split walk with the cap forced to 300 returned
  the same 847-id set. `--limit` 1000/1001/1500 on a 1,789-listing category returned exactly that many.
- `categories --grep` matches name, path, slug, a Latin transliteration and English/colloquial tags:
  `monitor` = `монитор`, `washing` and `перални` find "Машини за перење алишта", `сушара` finds dryers,
  `судомашина` finds dishwashers, and `фритез` also finds the Latin-named "Air fryer ..." node.

## Facets and filters

- `facets <cat>` lists price range, child categories, then the category's own filter widgets
  (`site_filter: true`, from Algolia's per-category `facetOrdering`, e.g. TVs: DisplayDiagonal,
  DisplayResolution, DisplayType, HdmiPortsQuantity, OperatingSystemInstalled; air fryers: Capacity,
  Power, DeviceType, Timer, ...), brand, seller, flags, then every other attribute by coverage.
- Tokens: `price=MIN-MAX`, `category=<id>`, `brand=X`, `seller=X` (`seller=Ананас Шоп` maps to merchant
  776), `in_stock`, `on_sale`, `fulfilled_by_ananas`, `free_shipping`, and `<group>.<Key>=<value>` with
  group `select|bool|measure|color|text` (e.g. `select.DisplayDiagonal=55"`, `measure.Power=1500|W`,
  `color.Color=Црна|#000000|false`). A bare `Key=value` is resolved with one extra request. `||` ORs
  values inside a token; repeated `--filter` ANDs (TVs 55"||65": 104; + Samsung, ≤40,000, in stock: 12).
  Every token's facet count equals what `list --filter` returns (14 tokens of every type, monitors and
  washing machines, once `on_sale` used the numeric form below).
- **The flags `on_sale`, `in_stock`, `fulfilled_by_ananas` and `free_shipping` filter numerically**
  (`onSale=1`). Algolia's facet form `onSale:true` (what InstantSearch widgets send, so probably the
  site's own toggle too; not observed) misses many on-sale listings: kitchenware 676 against 1,139 with
  `onSale` true, monitors 53 against 56, and every out-of-stock one. The numeric form matches the
  attribute and the facet count exactly.
- **Attributes are seller-entered and sparse**: Model on 37 of 225 TVs; DisplayDiagonal on 221 of 225.
  Listing records carry them as `attributes` (select/bool/measure/colour), so you can filter locally.

## Search semantics

- Algolia relevance search over the whole marketplace: typo-tolerant and prefix-matching. **Latin
  spellings of Macedonian words are mapped only for some words** (store-defined synonyms, not general
  transliteration). Pairs on 2026-10-03, Latin vs Cyrillic: `televizor` 3,397 = `телевизор` 3,397;
  `toster` 232 = `тостер`; `laptop` 650 vs `лаптоп` 651; `frizider` 463 vs `фрижидер` 470; `tastatura` 586
  vs 601; `monitor` 366 vs `монитор` 501; `mikser` 302 vs `миксер` 604; `blender` 303 vs `блендер` 680 (a
  strict subset); `igracka` **3** vs `играчка` **18,688**; and the other way round, `kafemat` 1,539 vs
  `кафемат` 259; `lego technic` 53 vs `лего техник` 6. **Search product words in Macedonian Cyrillic and
  brands/models in Latin**, and union both when unsure. The client logs a reminder for all-Latin queries.
- Synonyms exist (`air fryer` 190 = `фритеза` 190). An **EAN**, the site's **Шифра** (`apId`,
  e.g. `R3XJP34XIC`) or a **model code** (`QE55Q7FAAUXXH`, prefix `QE55Q7F`) finds the listing(s).
- **All words must match; when nothing does, Algolia drops words** (`samsung qwxzvbn` → 680 Samsung
  hits). The client warns on stderr; `--strict` returns 0 instead. Cap 30,000 per query. Pure nonsense
  (`zxqvjkwpt`, `qwzx vbnmk`) returns 0 and exits 0. EAN `6941812768556`, Шифра `20NGLN7JFH` and model
  codes `65C61K` and `MP225V` each find their listing(s) (MP225V: 2 sellers, 4,990 and 5,499).

## Price semantics

- `price_mkd` = Algolia `price` = page `priceV2.sellablePrice`: what every buyer pays, VAT included
  (`extra.vat_percent`, 18 on most goods). No card, club or login price. Whole MKD.
- `regular_price_mkd` = `basePrice` when higher: the top struck-through figure during a seller's SALE
  (~half of all listings). Some show a second struck-through `regularDiscountPrice` (`extra.
  regular_discount_price_mkd`; FOX AC: 21,170 → 17,990 → **12,990**). `detail` `extra.price_note` gives the
  sale window (e.g. `SALE -32% ..., valid 2026-10-01..2026-10-05`). Reference prices are seller-set.
- **Promo codes** (`extra.promo_note`, e.g. abrzo10 = extra 10% on "ā брзо" items) need a logged-in
  checkout and are never applied to `price_mkd`.
- **Delivery is per listing** for guests/non-Ananas+ buyers: `shipping_mkd` 0, 190, 250/500/700 (bulky,
  TVs by size), up to 1,200–6,000 for furniture or 98" TVs; null for a few sellers (SuperMart). The mix
  depends on the category: 0 for 74% and 190 for 24% of the builder's 8,825-listing sample, but 190 for
  54%, 0 for 20% and 250–700 for 25% of 6,051 TV/phone/appliance/kitchenware/toy listings.
  `extra.shipping_mkd_ananas_plus` is the member fee. The page renders the fee client-side; the flight
  `shippingCost.price` matches `shipping_mkd`.
- Verified 2026-10-03 against 11 live product pages (visible `text-2xl` price, `line-through` prices,
  JSON-LD price/availability, `<h1>`, flight `ean`), all matching: an 86" display at 187,849, an OOS TCL TV,
  a discounted Redmi phone (17,890, was 29,000), a 699 blender, a 190 plush toy, a robot vacuum with two
  struck prices (43,250 → 29,990 → 25,550), sunscreen, a washing machine and an OOS monitor.
- `price_note` sale windows can lag: 548835 showed "valid ..2026-10-02T23:59:59" on 2026-10-03 while the
  page still charged the sale price. Trust `price_mkd`.
- `price_valid_until` = the SALE's end: `detail` reads page `priceV2.dateTo` (null when priceV2 shows no
  running sale; key absent when the page has no priceV2 or the sale has no readable end); listings read Algolia
  `discountEndTime`, which only ~3% of sale hits carry (key absent on the others, null when not on sale).
  Both are naive seller wall-clock times ("Понудата важи до ..."), given the Skopje offset.

## Stock semantics

- `in_stock` = `onStock` = `available > 0`. The index holds almost only orderable listings (~1.2% out of
  stock), so `--in-stock` rarely changes much (TVs 225 → 218).
- `stock_note` "има на залиха (available N)" is the seller's unit count (`extra.available_units`;
  `detail` adds `stockLevel`). **200 is a default** on ~77,000 own-shop listings, not a real count.
- No per-location stock (`per_location_stock` null). `detail.delivery_estimate` is the guest delivery
  promise computed today: 3–5 days for Ananas-fulfilled (`fulfilled_by_ananas`, "брзо"), 4–6 days for
  seller-shipped items (2026-10-03 samples). It is null for out-of-stock listings: the gateway quotes
  dates even when 0 units are available. `international_supplier` (`ananasGlobal`) is false everywhere.

## Warranty

Only when stated: a listing badge ("Гаранција 1 година" on own-shop items, "2+3 Години гаранција со
регистрација..." on FOX ACs) or a "Гаранција: N год." phrase in the description (Сетек TV: "2 + 3 години").
Otherwise null; Ananas does not show the selling chain's own terms (Нексио's 360 days are absent).

## Data-quality traps

- **One product, many listings**: compare by `ean` (74% filled in TVs/phones/kitchen/toys/perfume, 21% in
  tyres). `extra.seller_sku` is the seller's code (seen: Setec sku, Neksio Шифра, an MPN, an EAN).
- **Homoglyphs**: own shop is "AНАНАС ШОП" with a Latin A (records show the Cyrillic form); category names
  like "Авто Mото", "Aпарати за нега". The client folds these for grep and name matching.
- **Machine-translated attribute values**: DisplayType `ДЛЕД`/`ТФТ`, OS `Тизен`/`Вида У`, plus duplicates
  (`DLED` vs `ДЛЕД`). A seller tagged a Hisense Hi-QLED as OLED; air-fryer Power "2 W" (kW typed as W).
- **Misfiled items**: a Gorenje fridge sits in small kitchen appliances. Check titles, not only membership.
- `mpn` comes only from the sparse `Model` attribute; descriptions often say "Модел: X".

## Bot protection and politeness (evidence 2026-10-03)

- **CloudFront WAF blocks crawler-library UAs**: `Scrapy/2.11` and `Python-urllib/3.14` → 403, 919 bytes
  "ERROR: The request could not be satisfied ... Request blocked." (`X-Cache: Error from cloudfront`,
  x-amz-cf-id `iJKQLrChER74...`). python-requests default UA, an empty UA and Chrome → 200 (1 MB).
  Earlier probes: Wget, Go, okhttp, aiohttp, HeadlessChrome all 200. No CAPTCHA, JS challenge or login wall.
- **GraphQL gateway** (AWS ALB): any operation named `GetMerchantInventoryV2` → **403 from awselb/2.0**
  (520-byte HTML), an operation-name rule against product scraping. The client does not work around it;
  it reads the same object from the product page. `Categories` and the guest delivery promise → 200.
- **Algolia**: 6 back-to-back unpaced queries → 200 in 0.12 s each, no rate-limit headers; `/browse`
  → 403 (search-only key, expected). Queries count against Ananas' quota: the client uses 1,000-hit pages.
- Verifier re-probe (26 requests, 2026-10-03): Scrapy/2.11 and Python-urllib/3.14 → 403 919 bytes again
  (0.09 s, `Error from cloudfront`); curl/8.5.0, Go-http-client/1.1, Wget/1.21.4 and a bare `Mozilla/5.0`
  → the normal 308 to the canonical slug; 8 unpaced product pages → all 200 (~1 s, 0.5 MB each); 10
  unpaced Algolia queries → all 200 in 0.54 s with no rate-limit or Retry-After headers.
- robots.txt disallows `/kategorije/`, `/mk/`, `/en/`, `/dostava/`; the client uses `/kategorii/`, `/proizvod/`.
  No Crawl-delay; one `Sitemap:` line.
- The client is sequential (0.3 s between Algolia calls, 0.7 s between page/GraphQL calls, backoff on
  429/5xx). Exit 3 prints `BLOCKED: ... HTTP 403, server=CloudFront, ...`; a 429 after 4 tries is also 3.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Search returns unrelated items | Algolia dropped a word (stderr warning). Rephrase, or `--strict`. |
| Search misses items | Latin spellings of Macedonian words map only partly (`igracka` 3 vs `играчка` 18,688). Search the Cyrillic word, Latin brand/model, an EAN, or `list` the category. |
| An "on sale" count elsewhere is smaller than ours | Facet-style `onSale:true` misses listings; the client's numeric `onSale=1` is complete. |
| detail `url` ≠ the page's canonical link | Intended: `url` is this seller's listing; the canonical offer (maybe another seller) is `extra.canonical_offer_url`. |
| `list` exit 2 "ambiguous" | Duplicate category names. Pass the id from `categories --grep`. |
| `list` count > the site's | Variants. The log prints the site's card count; use `--group-variants` to match. |
| `--check-site` warns "no InstantSearch state" | A department or tile landing page. The list is still complete. |
| Duplicate-looking offers | Several sellers or variants; group by `ean` and `seller`. |
| A filter drops items | Attributes are sparse; check titles, `attributes` and `detail` specs. |
| Exit 3 `BLOCKED` | CloudFront 403: keep a browser UA and retry later. Algolia 401/403: key rotated; the client re-reads it once from the bundle. |
| `categories` shows `id` null | The GraphQL tree failed; paths still work in `list`, ids do not. |
