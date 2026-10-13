# Gjirafa50 (gjirafa50.mk) and ZirafaMall (zirafamall.mk): store reference

Client: `scripts/stores/gjirafa.py --site gjirafa50|zirafamall` (`store` = site key). Live 2026-10-03.

## What they sell and when they are useful

- **Gjirafa50**: large online-only electronics shop. Departments, largest first: Додатоци (printers,
  peripherals, audio, networking, CCTV, office, cables, storage, UPS, solar, monitors); Мобилни,
  Таблети и Навигација; Компјутер, Лаптоп и Монитор; ТВ, Аудио и Фотоапарати; Компјутерски делови;
  SMART (smart home, wearables, e-scooters, drones, small appliances); Gaming. No white goods.
- **ZirafaMall**: Gjirafa's marketplace. Технологија mirrors Gjirafa50 (vendor "Basics from
  GjirafaMall", same `sku`, same price and stock; over 80% of Gjirafa50 phones and orderable disks
  have a mirror). It adds Дом with **Бела текника** (washers, fridges, dishwashers, ovens, air
  conditioners, boilers, vacuums), Козметика, Облека, fashion, Спорт, Деца, books/office, tools, auto,
  health, food.
- Good for choice and the long tail; **not** for "this week": almost everything orderable is a
  supplier order arriving in ~3-4 weeks (as of 2026-10-03). A ZirafaMall listing with a Gjirafa50
  `sku` is one offer (`mkshop.py` marks it `mirror_of`).

## Access

Same nopCommerce (ASP.NET Core) behind Cloudflare. The storefront's XHRs return JSON whose `html`
is the rendered card grid (there is no product JSON API):

- `GET /category/products?categoryId=<id>&pagenumber=<n>&orderby=16[&pagesize=48]` + filters
  `is=true` (orderable), `hls=true` (48h local stock), `hd=true` (discounted, ZirafaMall),
  `ms=<manufacturerId,..>`, `specs=<specId>,<opt>,<opt>;<specId>,..;`, `price=<min>-<max>`
  (inclusive) → `{totalpages, html, paginationHtml: "1-48 од вкупно N производи" (Gjirafa50)}`.
- `GET /product/search?q=<q>&pagenumber=<n>[&is=true]` (same plus `totalHits`) and
  `GET /Catalog/GetManufacturerFilter?entityId=<categoryId>&entityType=Category` (manufacturers).
- HTML category pages give id, breadcrumb, sub-category tiles and the attribute filters; product
  pages (`/p/<id>` 301s to the slug) give price, Количина, delivery quote and specs with EAN. Cards
  carry id, `Код` (sku), title, prices, badges and the ZirafaMall vendor; **no brand, no EAN**.
- Page size: Gjirafa50 categories 48, search 24; ZirafaMall always 16. Walks use `orderby=16`
  (newest; relevance/price orders drift); duplicate rows trigger a relevance re-walk and merge.
- **Elasticsearch window: no listing or search returns more than 10,000 results** (deeper pages
  come back empty). `list` detects a larger category and walks it as price slices (`price=a-b`) that
  each fit, merging by id (a whole department such as Компјутер, Лаптоп и Монитор takes several
  minutes).
- Cost: `list` = category page + manufacturer list + 1 request/page at 0.5 s pacing (a category of a
  few thousand listings takes a minute or two on Gjirafa50, and up to ten minutes or more on
  ZirafaMall's 16-item pages; the client prints the page count first, so pass `--limit` or a filter
  for a quick look). `detail` 1 request per product; `categories` a few dozen requests cold, then
  cached 24 h.

## Category model

- Numeric ids (Gjirafa50 2160, ZirafaMall 14528); slugs are Macedonian transliterations on Gjirafa50
  (`monitor-dodatoci`, some with Cyrillic `ј`/`њ`: `kompјuter`) and often **Albanian** on ZirafaMall
  (`monitor-teknologji`, `tastiere-teknologji`). Names are Macedonian. `list` takes id, slug or URL.
- Tree = home-page mega-menu + each truncated group's page, cached 24 h in `~/.cache/mk-tech-shop-search/`
  (`MKSHOP_CACHE_DIR` overrides; `--refresh`): three complete levels, which hold only a minority of
  each site's categories. `categories --grep X --deep` fetches matching pages to
  name deeper children (≤40 per run, cached); `list` prints subcategories on stderr. Only department
  ids (and truncated groups') come from the menu; other ids are null until a page fetch learns them
  (`list <slug>`, `--deep` and `--counts` write them into the cache, after which `list <id>` /
  `facets <id>` work). `parent` = the parent's id, else its slug.
- A parent listing includes its descendants **plus products filed on the parent only**: Gjirafa50
  Диск and Таблет each hold hundreds or thousands more than all their children together (Таблет's iPad
  OS child is empty and Google Android nearly so: real tablets sit on the parent). List the parent when
  in doubt. `count` is null unless `--counts` (Gjirafa50: exact "вкупно N", one request each;
  ZirafaMall shows no total anywhere).
- `--grep` matches name, path, slug, a Latin transliteration (`monitor`, `монитор`, `tastatur`) and
  English / colloquial tags (`washing|перални` → Машини за алишта, `fridge`, `vacuum`, `phone`,
  `laptop`, `gpu`, `headphones`, `tv`). Path matches mean a department word pulls in its whole
  subtree (`laptop` lists all of Компјутер, Лаптоп и Монитор).
- Main categories (from about a hundred listings to several thousand each). Gjirafa50: Додатоци >
  Монитор; Мобилни > Touchscreen; ТВ; Компјутерски делови > Диск (> За лаптоп); Екстерни дискови >
  SSD; Таблет. ZirafaMall: Аксесоари > Монитор; Touchscreen phones; Делови за компјутер > Дискови;
  Таблети; ТВ & Проектори; Фрижидери; Правосмукалки; washers.

## Facets and filters

- `facets <slug|url>` → availability flags (`in-stock`, `local-stock`, ZirafaMall `discount`), the
  price range (`price=MIN-MAX`), manufacturers (`brand=<id>`; names accepted) and attribute options
  (`spec=<specId>:<optionId>`). **No per-value counts** on the site; `facets --counts` (Gjirafa50)
  costs one request per value.
- Several `--filter` flags AND; values of one attribute OR inside a token (`spec=64:186590,186601`).
- **Attributes are sparse and messy.** Gjirafa50 monitors have only a handful of refresh-rate values,
  no 144 Hz; 165 Hz (`spec=1390:356624`) returns only a handful of the
  thousands of monitors. Disks have no SSD/HDD or
  capacity attribute. Values repeat (`27` / `27"` / `23,8`). ZirafaMall has two brand systems (the
  manufacturer filter `brand=Beko` and a spec "Брендови" Beko, on washers). Filter on titles; use specs
  only to confirm.
- The manufacturer list is not limited to brands present: Gjirafa50 external SSDs (2206) list dozens
  of brands including Samsung, and `brand=Samsung` there matches nothing. A filter combination
  that matches nothing makes `list` exit 2 ("the filters match nothing"), which is an answer, not an
  error.

## Search semantics

- Elasticsearch: **AND over words, typo-tolerant** (`samsng` = `samsung`).
  A word that matches nothing is **dropped silently** (`samsung zzqxv` = `samsung`); the client
  warns when a word of 3+ letters has no hits alone (one request per word, max 5).
- High recall, low precision: `monitor` returns around ten thousand hits on Gjirafa50 (nearly three
  times as many as `монитор`) and about twice that on ZirafaMall. A product the shop does not sell
  still returns hundreds of fuzzy hits (`samsung 990 pro` → Flip displays, watch straps). Relevance
  order; without `--limit` the client stops at 1,000.
- Minimum 3 non-space characters (exit 2); `search --in-stock` = `is=true` (see Stock for ZirafaMall).
  **EAN, Код (`13124905mo`, `15471180mo`) and model codes (`AW2524HF`, `MSA372M`, `KGN497ICT`) find
  the exact product** on both sites.

## Prices

- `price_mkd` = the displayed price, VAT included ("со ДДВ"), whole denars (`data-discountedprice`
  "36990,00" is only a cross-check). No club or instalment prices.
- `regular_price_mkd` = the struck-through price, on **most cards** (about 60-90%, by category),
  typically about 10% above the price and sometimes 20-25%. Not evidence of a deal.
- No price validity window is exposed (`price_valid_until` is never emitted): cards, product pages,
  "Специјална понуда" (`/Catalog/GetSpecialOfferProducts`) and campaign pages carry no countdown or
  end date, and the product page's schema.org `priceValidUntil` is boilerplate (always the fetch
  date + 1 year on both sites, checked 2026-10-03).
- Delivery is free "for a limited time" (Gjirafa50 FAQ, as of 2026-10-03); cash on delivery up to
  30,800 MKD.

## Stock and delivery

- `in_stock` = **orderable**; false only for the sold-out card ("Продадено"). Gjirafa50's `is=true`
  set matches the badge exactly (checked on PC disks, laptop disks and a search for "intenso"), so
  `list --in-stock` uses it. **ZirafaMall's `is=true` index is stale both ways**: it returns a few
  percent sold-out cards and omits some orderable ones (their pages said "Само уште 3" / "повеќе од
  10"). So on ZirafaMall `list --in-stock`
  (or `--filter in-stock`) walks the whole listing and keeps unbadged cards (~1.5x the pages), while
  `search --in-stock` still uses `is=true` and only drops the sold-out leak: to answer "is model X
  orderable on ZirafaMall", search without `--in-stock` or use `detail`. Sold-out items stay listed:
  nearly half of Gjirafa50 monitors and ZirafaMall fridges, and two thirds or more of Gjirafa50
  phones and TVs.
- Local stock = the card's **"48h"** badge (`delivery_estimate: "48h"`, `--filter local-stock`):
  rare (under 1% of Gjirafa50 monitors and TVs, none of ZirafaMall's washers). All else ships from
  suppliers.
- `detail`: "Количина" (`повеќе од 10` / `Само уште N` / `Нема на залиха` → `extra.quantity`;
  supplier stock, not local) and the arrival quote ("СКОПЈЕ 30 октомври 2026 - 31 октомври 2026")
  → `delivery_estimate` with days from today. Tiers seen on 2026-10-03: local 3-4 days ("Земи
  веднаш"), other supplier 19-22 days, international supplier 24-31 days. The FAQ's "6-10 дена" is
  not this.
- `international_supplier`: ZirafaMall = its turtle badge ("Овој производ доаѓа од меѓународен
  добавувач"; on almost all orderable washers and most orderable monitors), null when sold out.
  Gjirafa50 has no badge: on cards false for 48h else null; on `detail` inferred from the quote
  (≥22 days → true, `extra.international_inferred`; matched ZirafaMall's badge on every SKU checked).
- `per_location_stock` is always null (one web figure). `seller` = ZirafaMall vendor.

## Warranty

`detail.warranty` = the spec "Гаранција за производителот" when present (machine-translated: "2
Брзо" means 2 years) plus, on Gjirafa50, the store-wide "1 година" (its stated policy as of
2026-10-03). ZirafaMall pages usually state none (null). GjirafaFLEX (paid replacement) is not a
warranty.

## Data-quality traps

- Some titles carry raw HTML (`Logitech G435, сина<br />`, `<p>`); `mkshop.py` strips the tags,
  direct client runs do not.
- **Two monitor trees on Gjirafa50**: Додатоци > Монитор (2160; about a third of it accessories) is
  the real one; Компјутер, Лаптоп и Монитор > Монитор (5022) is a small subset.
- Gjirafa50 SSDs are spread over Диск > За лаптоп / За Компјутер / Екстерен: list `disk`, filter titles.
  Portable SSDs also sit in a second tree, Додатоци > Екстерни дискови > SSD (2206; it holds internal
  M.2 and mSATA drives too).
- Phone groups are mostly accessories (Gjirafa50 Мобилни and ZirafaMall Мобилни телефони each hold
  dozens of times as many listings as their Touchscreen child): list the Touchscreen child.
- **The grid renders fewer products than the site's own count**: a few fewer in monitors, several
  percent fewer in Компјутер, Лаптоп и Монитор (the hidden ones are mostly the pricier ones);
  ZirafaMall pages show 13-16 of 16 slots. Other orderings find nothing more; shoppers do not see
  them either.
- Titles mix Macedonian, untranslated Albanian ("Telefoni Ulefone ... i zi"), Cyrillic brand names
  ("Самсунг Галакси", "Електролукс") and literal `\"`. Specs are machine-translated.
- Listing `brand` is inferred (earliest name from that category's manufacturer filter found in the
  title; about 15% null on monitors; "Dell Alienware" → Dell); search records have `brand: null`; detail
  `brand` is the page's manufacturer link, itself sometimes wrong.
- Mislabelled listings exist: a "Philips Momentum 27M1F5500P Монитор" card at a battery's price was
  AAA batteries (see its slug `bateri-philips-ultra-aaa-…`). Price typos too: a "Laptop Lenovo
  IdeaPad Slim 3i … Core i3" showed a price in the hundreds of thousands of MKD on its card and page.
  Treat outliers as errors until the EAN and a second shop confirm.
- **Gjirafa50 Таблет (1339) is mostly not tablets**: only a few percent of its listings are tablets,
  about a quarter laptops and the rest accessories. Filter titles (`^Таблет|^Tablet`); ZirafaMall's
  Таблети is similar (a few percent tablets, about a tenth laptops).
- Categories hold strays (ZirafaMall Фрижидери lists a built-in oven). Check titles before counting.
- Outlet (id 1) and "Што има ново?" (id 2) are special listings: use their children. Prices and
  stock move during the day: re-check a shortlist with `detail`.

## Bot protection and politeness (checked 2026-10-03)

- Cloudflare is in front, but no challenge, CAPTCHA, rate limit or login wall was seen: thousands of
  paced requests got no 403, 429 or 5xx.
- The user agent changes the **language**: ZirafaMall serves Albanian (`lang="sq-MK"`) to some non-browser user
  agents. The client sends a desktop Chrome UA and refuses Albanian pages; a stray `lang="en"` on some Macedonian
  pages is accepted.
- `robots.txt` disallows `/search?` and `/shoppingcart/*` (not used). The client paces 0.5 s, backs off on
  429/5xx and exits 3 (`BLOCKED:`) on `cf-mitigated`, 401/403, a challenge body (any status) or a 429 that
  persists through 4 attempts.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `categories` takes ~1 min | Cold index build; later calls use the 24 h cache (`--refresh` rebuilds). |
| `--grep` misses a category | It is level 4+: grep its parent with `--deep`, or `list` the parent. |
| `list` exit 2 "page-not-found (unknown id)" | Wrong id: pass the slug/URL from `categories`. |
| `facets <id>` exit 2 | Id not learned yet (deeper than the menu): pass the slug or URL once. |
| `list` exit 2 "the filters match nothing" | Genuine zero (e.g. a brand the manufacturer list offers but the category lacks). |
| Search returns junk / WARNING word dropped | Fuzzy tail; use model code or EAN, or `list` the category. |
| Exit 3 `BLOCKED:` | A Cloudflare challenge appeared (none seen as of 2026-10-03): retry later, slower. |
