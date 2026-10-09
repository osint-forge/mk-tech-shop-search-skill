# Neptun (neptun.mk): store reference

Client: `scripts/stores/neptun.py` (key `neptun`). Verified live 2026-10-03, then independently
re-verified the same day (HTML page walks, a full 5,599-product catalogue scan, an anonymous cart test).

## What it sells and when it is useful

Neptun is a large Macedonian consumer-electronics and appliance chain with about 25 stores and about
**5,600 products online** in 12 departments: home appliances 1,026 (kitchen, vacuums, irons, purifiers,
cookware); computers 1,008 (laptops 110, peripherals, networking, tablets, monitors, PC parts, printers);
phones/watches 718 (phones 220); large appliances 714 (fridges, washers/dryers 145, dishwashers, cookers,
built-in, boilers); gaming 611; TV/audio/video 607 (TVs 167); personal care 355; adult toys 287;
cooling/heating 142 (ACs 71, heat pumps); sport 67; cameras 56; vouchers and the haPPy card 8.

Use it for TVs, large and small appliances, ACs, phones and mainstream laptops. It is weak on enthusiast PC
parts. `detail` gives **per-store availability for every product** (yes/no, like Setec, Anhoch and
Tehnomarket). **EANs** are in listings for 5,299 of 5,599 products (95%; 100% of TVs, phones, washers,
fridges and monitors). Almost all the gaps are adult items. The private labels FUEGO and HOOBART (438
products) carry store-internal codes with prefixes 20–29, which no other shop shares. Ananas also lists
Neptun as a seller (`mirror_of: neptun`).

## Access

The client calls the shop's own AngularJS JSON endpoints: plain `POST` requests with no cookies or tokens.

- **Listing:** `NeptunCategories/LoadProductsForCategory`. Body:
  `{"model": {CategoryId, Sort:7, Manufacturers, PriceRange, MultiSelectFeatures:[{Id,FilterValue}],
  DropdownFeatures, BoolFeatures, ShowAllProducts, ItemsPerPage, CurrentPage}}`
  It returns `Batch.Config.TotalItems` and `Batch.Items[]` (Id, CodeNumber = Шифра, Barcode = EAN, Title, Url,
  Manufacturer, Category, prices, AvailableWebshop, Preorder, Active, Warranty). `ItemsPerPage` works up to
  200; 250+ is silently clamped to 120. **An out-of-range page returns page 1 again**, so the client pages by
  `TotalItems`.
- **Search:** `Product/SearchProductsAutocomplete` with `{term, page, itemsPerPage}`.
- **Detail:** `Product/GetProduct` `{id}` and `Product/GetShopsForProduct` `{productid}`. Product URLs are read
  through the page's `data-productModel`.
- **Tree:** the home-page mega-menu (1 request, ~800 KB). A category page `/<Slug>.nspx` embeds
  `data-categorydetails` (id, NumberOfProducts, brands, features, children) and `data-initialSearchModel`
  (the page's own listing request).

**Cost:** `categories` takes 1 request. `list` takes 2–3 requests plus 1 per 200 products; the 1,008-product
computers department took 8 requests (~7 s). `detail` takes 2–4 per item. `facets` takes the walk plus up to
40 count requests.

## Category model

- **Ids.** 268 menu nodes in 12 departments, 2–3 levels deep. Ids are small integers: `173` TVs, `151`
  phones, `24` laptops, `204` washers/dryers, `56` ACs. Each department's brand links list its level-2 and
  level-3 ids in menu order. Departments, the 10 level-4 nodes and the adult department therefore get
  `id: null` until `--counts` fills them in.
- **Accepted references.** `list` accepts any slug, URL, name or id, including ids that are not in the menu
  (`340` SSD).
- **Slugs** are page names without `.nspx` (`televizori`, `MASINI_ZA_PERENE`). **The server treats them as
  case-sensitive**: a wrong case or a trailing `/` returns an empty 200. The client normalises them and
  retries with the menu's spelling.
- **Parent categories.** `list` mirrors the page's per-category `ShowAllProducts` flag: TVs (173) shows its
  own 167, not 177 with its accessory children; ДОМАШНО АУДИО (58) and departments include children. Parents
  showing only sub-category tiles (`NumberOfProducts` 0: 204, 56, fridges 43) get their children walked,
  with a note (204 → 145 = 6+91+31+17).
- **URL filters** are honoured, e.g. `televizori.nspx?brands=61_41&priceRange=20000_60000&multi=390:55`.
  Menu aliases such as `/55-tv.nspx` redirect to such URLs.
- **`--grep`** matches name, path, slug, a Latin transliteration, the menu keyword lists, English words
  (`tv`, `fridge`, `washing`, `laptop`, `phone`, `air cond`, `vacuum`) and a few colloquial Macedonian
  names the menu does not use (`перални`/`peralni`, `сушара`, `садомијалка`, `паметни телефони`). Path
  matches pull in whole subtrees, and short words over-match (`tv` is inside "domakjins**tv**o"). Anchor
  with `\b` or `^`.
- **Exit 2** for an unknown id, slug or URL, and for an ambiguous name (ГАЛАНТЕРИЈА exists 4 times; pass the id).

## Facets and filters

- `facets <cat>` returns brand counts (exact, from the walk), child categories (`sub=<id>`), feature values
  (one count request each) and the price range. The child list comes from the page and can be incomplete
  (the AC page names 2 of its 4 children); `categories --grep` shows the menu's view.
- Tokens are `brand=<id|name>[,...]`, `sub=<id|name>`, `f<featureId>=<value>[,...]` and `price=MIN-MAX`.
  Commas OR within a token, and repeated flags AND. Checked: `f531=A` gives 13, `f1703=1400` gives 9, and
  both together give 8.
- **Feature data is sparse.** A diagonal (`f390`) is on only 45 of 167 TVs, a capacity on ~15 of 91 washers,
  and any feature at all on 2 of 220 phones. Feature filters silently drop untagged items, so prefer titles and
  `detail` specs.
- **The server's PriceRange matches by overlap**: RegularPrice ≥ min and the haPPy price ≤ max. The client
  therefore re-applies `price=` exactly to `price_mkd` and logs how many hits it dropped.

## Search semantics

- **AND of substrings.** Every word must occur in the listing title, the ERP title (often with the MPN, e.g.
  `SM-S931BZKDEUC`), the tags, the Шифра or the EAN. Descriptions and ModelNumber are not searched.
- **Ranked, no cap**: `samsung` returns 335, `55` returns 750.
- **No transliteration:** `телевизор` 205 vs `televizor` 192 (via tags), `машина за перење` 100 vs
  `masina za perenje` 0, `клима` 67 vs `klima` 0, `фрижидер` 135 vs `frizider` 2. Use Cyrillic for product
  types, Latin for brands/models. Colloquial words that titles don't use find nothing (`перална` 0).
- An exact EAN, Шифра or MPN returns exactly that product (`4002516863090`, `84122313`, `AW2524HF`).
  `detail` also accepts a 12-digit UPC: leading zeros are ignored when it matches the Barcode.
- `mpn` is filled when the ERP title ends in `(CODE)`: 11 of 15 `galaxy s25` hits.

## Price semantics

- **What the page shows.** "Редовна цена" (RegularPrice) and, when lower, a highlighted **"haPPy цена"**
  (DiscountPrice, `DiscountPriceType` 3/4).
- **The haPPy card.** The haPPy price needs a loyalty card, bought as a cart item: **150 MKD online**,
  299 MKD in store, one-off.
- **`price_mkd`** is the price without the card: RegularPrice, or the "Онлајн цена" (WebshopDiscountPrice)
  when set. WebshopDiscountPrice was 0 on all 5,599 products (full scan). **Verified in an anonymous
  cart:** a TV listed at 8,999 / haPPy 7,499 went in at `Price` 8,999, with `HappyPrice` 7,499 shown
  beside it. At checkout a "forceLoyalty" pop-up offers two options: activate an existing card by SMS
  (the phone or e-mail must match the card application), or add the digital card to the cart. The card
  went into the cart at 150 MKD (regular 299).
- **Discount types:** the full scan found only haPPy prices (`DiscountPriceType` 3 on 3,822 products and
  4 on 195; 3,180 of them below RegularPrice). There were no prices on sale for everyone. If another type
  appears, the client treats it as a public sale: `price_mkd` = the discount and `regular_price_mkd` =
  RegularPrice.
- **`member_price_mkd`** is the haPPy price when it is lower. `extra.actual_price_mkd` (the API's
  `ActualPrice`) equals it.
- **`member_price_valid_until`** (detail only) = `ValidTo` of the promotion GetProduct names in
  `PromotionId` (e.g. "HAPPY WEEKS #2 ... 21.09-04.10.2026" -> 2026-10-04T23:59:00+02:00). When
  `PromotionId` is 0 the shop does not say which promotion sets the price: null (standing) only when
  the product has no promotion at all, which is how type-4 haPPy prices look (17 of 19 sampled on
  2026-10-04: new LG TVs, Galaxy A27, Honor 600 Lite, laptops; no promotion, no eyecatcher, and the
  page shows the haPPy price with no campaign or end); left out (unknown) when it has some (3 of 30
  sampled TVs on 2026-10-03, e.g. a haPPy price whose two promotions both end 10.10.2026; Honor 600
  Pro on 2026-10-04). Every promotion's dates are in `extra.promotion_windows`. Listings carry no
  promotion data, so they leave it out.
- **`price_valid_until`** is null when `price_mkd` is RegularPrice (standing, listings and detail).
  An online or public discount price takes the ValidTo of the promotion named in `WebPromotionId` /
  `PromotionId`; when that promotion is not named or listed, or the price comes from anywhere else,
  the key is left out (unknown).
- **haPPy prices are common and deep.** 160 of 167 TVs, 184 of 220 phones and 45 of 110 laptops have one; a
  Samsung Frame 55" is 80,999 regular and 45,995 haPPy. **Quote both.** For bigger buys, the realistic price
  is `member_price_mkd` + 150.
- **`regular_price_mkd`** is null, because nothing is struck through. Prices are whole MKD with VAT included.
- **Delivery.** "Стандард Плус" is 300 MKD within 7 working days for every online order; the checkout
  string reads "Стандардна испорака во рок од седум работни дена (300 ден.)". Adult-department items cost
  160 MKD. The 99 MKD "Стандард" tier is only for purchases made in a store. Comfort (699) and Premium
  (1,599) exist only in Skopje, Struga, Ohrid and Tetovo, and store pickup is offered. Checkout also has a
  "free delivery" label and `appconfig.freeShippingLimit` is 3,000, but the conditions are unverified;
  confirm in checkout. Instalments ("48 x N", computed on the haPPy price) need the haPPy card.

## Stock semantics

- **Listings and search** contain only active, web-orderable products. `in_stock` is always true there, and
  `--in-stock` is a no-op. In the full scan, all 5,599 products had `AvailableWebshop` true, `Active` true
  and `Preorder` false.
- **`detail`:** `in_stock` = orderable online or held by at least one store. `per_location_stock` lists only
  the stores holding it (yes/no, no quantities); `stock_note` reads "online order: yes; in stock in 24
  store(s): …". No stores with online "yes" most likely means central-warehouse or supplier stock (common for washers, ACs). No lead times;
  `delivery_estimate` is the store policy.
- **Sold-out and delisted products** become `Active:false`. Examples are the recent 297354 "Logitech G Pro
  X Superlight 2 SE" and the old 286652. `GetProduct` still serves them, misleadingly with
  `AvailableWebshop:true`, and the client reports `in_stock:false` with stock_note "inactive: sold out or
  delisted …". Their page is a soft 404 and search drops them, so only `detail <internal id>` works. Their
  URL or Шифра gives an error row ("product not found … inactive"). Treat that as "not orderable at Neptun
  now", not as a bad link.

## Warranty

`Warranty` is in months (12/24/36 seen) and is reported as `"24 months"`. `extra.promotions` lists campaigns,
including extended-warranty offers such as "Samsung TV 2+3YW" (5 years).

## Data-quality traps

- **Inconsistent titles.** "Телевизор LG 65 QNED72 B3B, 65", 4K QNED MiniLED" sits next to
  "LG 86 QNED87 B3A".
- **Brands as filed.** `OTHER` on some phones, `NEPTUN` on vouchers, `FUEGO` and `HOOBART` as private
  labels. Their 20–29-prefix "EANs" are store-internal, so do not expect EAN matches for them elsewhere.
- **Category names differ between menu and product.** A search record's category comes from the menu
  ("ФУТРОЛИ И ЗАШТИТА"), but a listing record's comes from the product ("Футроли, Заштита"). They name the same category.
- **The menu is not the tree.** SMART ТАБЛИ (396) sits under TVs in the menu but is not a child on the TV
  page; "ДОДАТОЦИ ЗА КЛИМА УРЕДИ" appears twice (385, 386); slug `gaming1` is coffee grinders; a product's
  `Category.Url` is not a page slug.
- **Product URLs** are `/categories/<Url>`. `Url` is usually `<Шифра>-<slug>`, but some have no code and some
  are Cyrillic. Use the record's URL: a bare `/categories/<code>` is a soft 404.
- **Non-catalogue items.** The services department sells vouchers and the haPPy card. The adult department has no
  menu ids.
- **`sitemap.xml`** is stale, third-party and category-only. Ignore it.

## Bot protection and politeness (checked 2026-10-03)

- **Cloudflare blocks by user agent only**: empty, some library, and fake search-engine or AI-crawler user agents
  get a 403. There is no CAPTCHA, JS challenge, Turnstile, login wall or TLS fingerprinting. The client sends a
  desktop Chrome UA.
- **Rate limiting:** none seen. 15 unpaced sequential POSTs returned 200 in 53–83 ms with no `Retry-After` or
  `X-RateLimit-*` headers; concurrent load caused occasional 8–9 s stalls (2026-10-01) but no 429s.
- **robots.txt** disallows nothing that the client uses.
- **The client** is strictly sequential at a 0.5 s pace and backs off on 429/5xx. A block (a 403 page,
  `cf-mitigated`, a 200/503 interstitial, a persistent 429) exits 3 with
  `BLOCKED: ... HTTP <status> ... cf-ray=... title=...`.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Search returns 0 | No transliteration and no synonyms. Switch script (`клима`, not `klima`), use the title's word (`машина за перење`, not `перална`), search by brand/model, or `list` the category. |
| `list` exit 2 "not a product category page" | Wrong slug case, a CMS page or a redirect. Use the id from `categories --grep`. |
| `list` exit 2 "ambiguous" | Duplicate names (ГАЛАНТЕРИЈА). Pass the id. |
| A parent's count looks off | The page's ShowAllProducts flag (TVs: 167, not 177). List the children separately. |
| A feature filter finds few items | Sparse attributes. Use titles or `detail` specs. |
| `price=` dropped N hits | The server range overlaps on the haPPy price. The client keeps exact `price_mkd`. |
| `detail` misses a URL or code | The product is inactive (sold out or delisted), so not orderable now. Its internal id still gives the record. |
| Neptun looks expensive | You are probably looking at `price_mkd` (without the card), e.g. under `--no-member-prices`. By default `mkshop` ranks and shows the haPPy price (`effective_price_mkd`, marked `*`) with the non-member price alongside; the card is a one-off 150 MKD online. |
| Exit 3 `BLOCKED` | Cloudflare 403. Check the UA is not empty, then retry once later. |
| One request takes ~8 s | A known stall; the 45 s timeout absorbs it. Never parallelise. |
