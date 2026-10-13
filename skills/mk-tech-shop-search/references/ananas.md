# Ananas (ananas.mk marketplace): store reference

Client: `scripts/stores/ananas.py` (key `ananas`). Verified live 2026-10-03.

## What it sells and when it is useful

Ananas is a multi-seller marketplace (the MK branch of ananas.rs): **hundreds of thousands of listings
from hundreds of sellers**. Departments, largest first: home and garden (by far the largest); fashion;
car/moto (incl. tyres); DIY tools; toys (incl. LEGO); beauty (incl. perfume); baby; IT; office/school;
health; pet; sport; books; phones/photo; large appliances; small appliances; food; TV/audio/video;
cleaning; gaming; personal-care appliances. Tech is a small share: only dozens of laptops and
smartphones, and a couple of hundred TVs.

Use it for breadth (home, toys, beauty, tyres, DIY, small appliances) and as a second channel for big
chains. **Many listings mirror shops covered here, usually at the same price**: ТЕХНОМАРКЕТ, "Сетек Се од
Техника" (= Setec; its `extra.seller_sku` is Setec's own `sku`, e.g. 79269, same price), Нептун, Нексио,
Setra, PC MARKET (= Anhoch). Ananas' own shop ("АНАНАС ШОП", merchant 776) has tens of thousands of
listings, mostly home and garden. TVs on 2026-10-03 came most often from ТЕХНОМАРКЕТ, then Сетек, Бител, the
own shop, ELIPSO and Maximum.

## Access

All public, no cookies or tokens. The client needs ~2–4 requests per `list`/`facets`, 1 per 1,000 hits.

- **Algolia** (search, list, facets): `POST https://Y1BSBVJ7AC-dsn.algolia.net/1/indexes/prod_merchant_inventories_mk_mk/query`
  with headers `x-algolia-application-id: Y1BSBVJ7AC`, `x-algolia-api-key: 78d3f4f3befb3c4a68f4ebbf8c38fd81`
  (the public search-only key in `pages/_app-*.js`; re-read from the bundle on 401/403). Body:
  `{query, filters, page, hitsPerPage<=1000, attributesToRetrieve, facets, distinct:false}`.
  `facets:["*"]` returns every facetable attribute (hundreds index-wide). **Pagination stops at hit 30,000**;
  the client splits bigger walks into price ranges (the fashion department, tens of thousands of listings,
  takes under a minute).
- **Category tree**: `POST https://api.ananas.rs/graphql-gateway/public`, headers `x-ananas-market: MK`,
  operation `Categories` (`getCategories(rootOnly:false)` with nested `children{id name slug}`): the whole
  tree in one response. Counts come from Algolia facets `product.categories.lvl0..lvl3` (lvl2 has >1,000
  values, so it is re-queried per group of departments, keeping only paths under that group's own
  departments, since a listing filed in two departments would otherwise overwrite counts; the counts
  equal a per-department walk). If GraphQL fails, the tree is rebuilt from facets (paths only; no ids or
  slugs). Ids then exit 3 if the gateway blocked us, and 2 otherwise. Slugs and paths keep working.
- **Detail**: the product page `GET /proizvod/<slug>/<id>` (React flight data holds the merchant-inventory
  object), the Algolia hit by `objectID`, and the guest delivery promise via GraphQL
  `GetCalculateGuestDeliveryPromise`. 3 requests, ~2 s per item (2 for an out-of-stock listing). Input:
  product URL (query/fragment ignored), listing id, or Шифра (any case). `/proizvod/x/<id>` 308-redirects
  to the listing's own slug, and `url` is that URL. The page's `canonicalId` / `<link rel=canonical>` is
  the product's SEO-canonical offer, often **another seller's listing** (a ТЕХНОМАРКЕТ listing pointed to a
  King Soft one). It goes only to `extra.canonical_offer_url`.
- **Category pages** `/kategorii/<slug>` are read only by `list --check-site` or for slugs not in the tree.

## Category model

- **Ids** are integers (`48397` TVs, `48508` smartphones, `49122` small kitchen appliances, `48602`
  perfume, `48038` LEGO). **Slugs** are Latin slash paths (`tv-audio-video/tv/televizori`). Depth ≤ 4.
  `list` accepts id, slug path, unique last slug segment
  (`televizori`), URL (`/kategorii/...`, `?page=` ignored), `A > B > C` path or a unique exact name.
  Some names are duplicated (Слушалки, Звучници, ...): exit 2 lists the ids.
- **Filter** = `product.categories.lvl<depth>:"<path>"`. Algolia values are the raw names joined with
  `" > "` and stripped; a few DIY/office names end in a space, which survives inside deeper paths.
- **Parents include all descendants** (hierarchical facets). Departments and some parents (LEGO) render
  as tiles with no product grid, so `--check-site` cannot read a total there.
- **Variants.** The index folds a seller's size/colour/volume variants into one hit (Algolia `distinct`
  on `group` = merchant + product group + colour). `list`/`search` turn that off and return every
  orderable listing; `--group-variants` reproduces the site's cards. Fashion has nearly twice as many
  listings as cards, perfume a few percent more; electronics ≈1:1. `categories` counts are listings incl.
  variants.
- **Cross-checked 2026-10-03** (page total = Algolia cards = fetched): TVs, smartphones, small kitchen,
  perfume (cards; its listings are a few more), LEGO City, and tyres over several pages. Independent
  verifier pass (the page's own "N производи", `--group-variants` count, default listing count): ТВ parent
  (= union of its children), Мобилни телефони (= smartphones + feature phones), Блендери, Робот
  правосмукалки, Машини за перење алишта, Монитори, Кадифени играчки and Препарати за сончање; the page
  total and the `--group-variants` count always agreed, and the listing count was equal or a few higher.
  A price-split walk with the cap forced to 300 returned the same Кадифени играчки id set. `--limit`
  1000/1001/1500 on a category with more listings than that returned exactly that many.
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
  values inside a token; repeated `--filter` ANDs (TVs 55"||65", then + Samsung, ≤40,000, in stock).
  Every token's facet count equals what `list --filter` returns (checked on tokens of every type in
  monitors and washing machines, once `on_sale` used the numeric form below).
- **The flags `on_sale`, `in_stock`, `fulfilled_by_ananas` and `free_shipping` filter numerically**
  (`onSale=1`). Algolia's facet form `onSale:true` (what InstantSearch widgets send, so probably the
  site's own toggle too; not observed) misses many on-sale listings (about 40% of them in kitchenware, a
  few in monitors) and every out-of-stock one. The numeric form matches the attribute and the facet count
  exactly.
- **Attributes are seller-entered and sparse**: Model on only about 15% of TVs; DisplayDiagonal on almost all.
  Listing records carry them as `attributes` (select/bool/measure/colour), so you can filter locally.

## Search semantics

- Algolia relevance search over the whole marketplace: typo-tolerant and prefix-matching. **Latin
  spellings of Macedonian words are mapped only for some words** (store-defined synonyms, not general
  transliteration). Latin vs Cyrillic on 2026-10-03: `televizor` and `toster` matched their Cyrillic
  forms exactly, and `laptop`, `frizider` and `tastatura` nearly; `monitor`, `mikser` and `blender` found
  only half to three quarters as many (`blender` a strict subset); `igracka` found **almost nothing**
  against **thousands** for `играчка`; and the other way round, `kafemat` and `lego technic` found
  several times as many as `кафемат` and `лего техник`. **Search product words in Macedonian Cyrillic and
  brands/models in Latin**, and union both when unsure. The client logs a reminder for all-Latin queries.
- Synonyms exist (`air fryer` = `фритеза`). An **EAN**, the site's **Шифра** (`apId`,
  e.g. `R3XJP34XIC`) or a **model code** (`QE55Q7FAAUXXH`, prefix `QE55Q7F`) finds the listing(s).
- **All words must match; when nothing does, Algolia drops words** (`samsung qwxzvbn` → hundreds of Samsung
  hits). The client warns on stderr; `--strict` returns 0 instead. Cap 30,000 per query. Pure nonsense
  (`zxqvjkwpt`, `qwzx vbnmk`) returns 0 and exits 0. EAN `6941812768556`, Шифра `20NGLN7JFH` and model
  codes `65C61K` and `MP225V` each find their listing(s) (MP225V: 2 sellers).

## Price semantics

- `price_mkd` = Algolia `price` = page `priceV2.sellablePrice`: what every buyer pays, VAT included
  (`extra.vat_percent`, 18 on most goods). No card, club or login price. Whole MKD.
- `regular_price_mkd` = `basePrice` when higher: the top struck-through figure during a seller's SALE
  (~half of all listings). Some show a second struck-through `regularDiscountPrice` (`extra.
  regular_discount_price_mkd`; seen on FOX ACs). `detail` `extra.price_note` gives the
  sale window (e.g. `SALE -32% ..., valid <from>..<to>`). Reference prices are seller-set.
- **Promo codes** (`extra.promo_note`, e.g. a code for an extra discount on "ā брзо" items) need a logged-in
  checkout and are never applied to `price_mkd`.
- **Delivery is per listing** for guests/non-Ananas+ buyers (fees as of 2026-10-03): `shipping_mkd` 0, 190,
  250/500/700 (bulky, TVs by size), up to 1,200–6,000 for furniture or 98" TVs; null for a few sellers
  (SuperMart). The mix depends on the category: most listings overall ship free and nearly all the rest
  cost 190, but in TVs, phones, appliances, kitchenware and toys 190 is the most common fee and many cost
  250–700. `extra.shipping_mkd_ananas_plus` is the member fee. The page renders the fee client-side; the
  flight `shippingCost.price` matches `shipping_mkd`.
- Verified 2026-10-03 against 11 live product pages (visible `text-2xl` price, `line-through` prices,
  JSON-LD price/availability, `<h1>`, flight `ean`), all matching, from a large display to a plush toy,
  including discounted and out-of-stock listings and a robot vacuum with two struck prices.
- `price_note` sale windows can lag: on 2026-10-03 a listing whose note said the sale had ended the day
  before still charged the sale price. Trust `price_mkd`.
- `price_valid_until` = the SALE's end: `detail` reads page `priceV2.dateTo` (null when priceV2 shows no
  running sale; key absent when the page has no priceV2 or the sale has no readable end); listings read Algolia
  `discountEndTime`, which only ~3% of sale hits carry (key absent on the others, null when not on sale).
  Both are naive seller wall-clock times ("Понудата важи до ..."), given the Skopje offset.

## Stock semantics

- `in_stock` = `onStock` = `available > 0`. The index holds almost only orderable listings (about 1% out of
  stock), so `--in-stock` rarely changes much.
- `stock_note` "има на залиха (available N)" is the seller's unit count (`extra.available_units`;
  `detail` adds `stockLevel`). **200 is a default** on most own-shop listings, not a real count.
- No per-location stock (`per_location_stock` null). `detail.delivery_estimate` is the guest delivery
  promise computed today: 3–5 days for Ananas-fulfilled (`fulfilled_by_ananas`, "брзо"), 4–6 days for
  seller-shipped items (2026-10-03 samples). It is null for out-of-stock listings: the gateway quotes
  dates even when 0 units are available. `international_supplier` (`ananasGlobal`) is false everywhere.

## Warranty

Only when stated: a listing badge ("Гаранција 1 година" on own-shop items, "2+3 Години гаранција со
регистрација..." on FOX ACs) or a "Гаранција: N год." phrase in the description (Сетек TV: "2 + 3 години").
Otherwise null; Ananas does not show the selling chain's own terms (Нексио's are absent).

## Data-quality traps

- **One product, many listings**: compare by `ean` (about three quarters filled in
  TVs/phones/kitchen/toys/perfume, about a fifth in tyres). `extra.seller_sku` is the seller's code
  (seen: Setec sku, Neksio Шифра, an MPN, an EAN).
- **Homoglyphs**: own shop is "AНАНАС ШОП" with a Latin A (records show the Cyrillic form); category names
  like "Авто Mото", "Aпарати за нега". The client folds these for grep and name matching.
- **Machine-translated attribute values**: DisplayType `ДЛЕД`/`ТФТ`, OS `Тизен`/`Вида У`, plus duplicates
  (`DLED` vs `ДЛЕД`). A seller tagged a Hisense Hi-QLED as OLED; air-fryer Power "2 W" (kW typed as W).
- **Misfiled items**: a Gorenje fridge sits in small kitchen appliances. Check titles, not only membership.
- `mpn` comes only from the sparse `Model` attribute; descriptions often say "Модел: X".

## Bot protection and politeness (checked 2026-10-03)

- A CloudFront WAF refuses some crawler-library user agents with a 403 "Request blocked" page. The client sends
  a desktop Chrome UA. There is no CAPTCHA, JS challenge or login wall.
- The GraphQL gateway refuses product-inventory operations by name (403 from the load balancer). The client does
  not work around it: it reads the same data from the product page.
- Algolia `/browse` is not allowed with the public search-only key, and queries count against Ananas' quota, so
  the client asks for 1,000-hit pages.
- robots.txt disallows `/kategorije/`, `/mk/`, `/en/`, `/dostava/`; the client uses `/kategorii/`, `/proizvod/`.
  No Crawl-delay.
- The client is sequential (0.3 s between Algolia calls, 0.7 s between page and GraphQL calls) and backs off on
  429/5xx. A block exits 3 with `BLOCKED: ... HTTP 403, server=CloudFront, ...`, as does a 429 that persists
  through 4 tries.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Search returns unrelated items | Algolia dropped a word (stderr warning). Rephrase, or `--strict`. |
| Search misses items | Latin spellings of Macedonian words map only partly (`igracka` finds almost nothing, `играчка` thousands). Search the Cyrillic word, Latin brand/model, an EAN, or `list` the category. |
| An "on sale" count elsewhere is smaller than ours | Facet-style `onSale:true` misses listings; the client's numeric `onSale=1` is complete. |
| detail `url` ≠ the page's canonical link | Intended: `url` is this seller's listing; the canonical offer (maybe another seller) is `extra.canonical_offer_url`. |
| `list` exit 2 "ambiguous" | Duplicate category names. Pass the id from `categories --grep`. |
| `list` count > the site's | Variants. The log prints the site's card count; use `--group-variants` to match. |
| `--check-site` warns "no InstantSearch state" | A department or tile landing page. The list is still complete. |
| Duplicate-looking offers | Several sellers or variants; group by `ean` and `seller`. |
| A filter drops items | Attributes are sparse; check titles, `attributes` and `detail` specs. |
| Exit 3 `BLOCKED` | CloudFront 403: keep a browser UA and retry later. Algolia 401/403: key rotated; the client re-reads it once from the bundle. |
| `categories` shows `id` null | The GraphQL tree failed; paths still work in `list`, ids do not. |
