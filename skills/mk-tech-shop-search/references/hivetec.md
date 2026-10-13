# Hivetec (hivetec.mk): store reference

Client: `scripts/stores/hivetec.py` (key `hivetec`). Verified live 2026-10-03. Counts drift.

## What it sells and when it is useful

A gaming and PC specialist with a catalogue of a few thousand products. Titles are almost all English. Category
terms:
- **Peripherals:** keyboards (plus a gaming term), gaming mice and mice, gaming headsets and headphones, speakers,
  webcams, mouse pads/keyboard accessories, racing wheels, controllers.
- **Monitors:** gaming monitors and monitors (two terms), monitor arms/accessories.
- **PC parts:** cases, coolers, GPUs, motherboards, PSUs, CPUs, RAM, SSD, HDD.
- **Other:** gaming chairs, desks, laptops and gaming laptops, tablets, prebuilt PCs, console games, networking, UPS,
  printers, projectors, smartwatches, e-scooters, action cameras, drones.
- **Not sold:** phones, TVs (only TV boxes and projectors), home appliances.

Use it for gaming peripherals (Keychron, Logitech G, Razer, HyperX, Dark Project), gaming monitors (good facets),
PC parts, chairs and desks. There are no EANs, so match products to other shops by title and model code (`sku` is
often the manufacturer part number).

## Access

WordPress + WooCommerce (Woodmart) behind Cloudflare. All data comes from the anonymous **WooCommerce Store API**,
with no HTML parsing, cookies or login.
- `GET /wp-json/wc/store/v1/products` takes `category=<ids>` (a comma list means OR), `search=`, `sku=` (exact),
  `include=<ids>`, `stock_status=instock`, `per_page` (max 100), `page` and `_fields` (cuts a 100-product page to
  a third of the size, no slower). Counts are in `X-WP-Total` / `X-WP-TotalPages`. Each object has `prices` (minor
  units), `is_in_stock`, `stock_availability`, `categories`, `attributes`, `short_description` (brand and warranty),
  `description` and `variations`.
- `GET .../products/<slug>` returns one product (404 JSON if the slug is unknown). `GET .../products/categories`
  returns the terms, paged like products. `GET /wp-json/wp/v2/pages?slug=kupuvanje-i-isporaka-na-proizvodi` is the
  delivery policy, read once per `detail` run.
- Rejected: `wc/v3` (401, needs API keys), `wp/v2/product` (no price or stock), the Woodmart AJAX search (no id,
  stock or SKU) and `products/brands` (empty).
- **Cost:** `list` takes 1 request per 100 terms plus 1 per 100 products. A multi-word `search` adds 1 count request
  per word (up to 4). `detail` fetches about 20 ids per request; slugs and SKUs take 1 request each, an id that
  `include=` does not return (a variation id, or a numeric SKU) costs 1 more for the by-id lookup, a variable product
  costs 1 per variation, and the delivery policy page costs 1 per run.
- `GET .../products/<id>` returns a product **or a variation** (type `variation`, with `parent` and its own price);
  `include=` returns only top-level products.

## Category model

- **Identifiers.** Ids are numeric (`610`), slugs are the shop's Latin forms (`gaming-monitori`, `slusalki`), and URLs are
  `/product-category/<slug>/`. `list` takes an id, a slug, a hivetec.mk URL (with or without `https://`; a `/page/N/`
  suffix is ignored), an exact name, the breadcrumb `categories` prints (`ПРОЦЕСОРИ > INTEL`), or a comma union
  (`kukista,kuleri,grafichki-karti`).
- **Unknown categories.** The API answers an unknown id or slug with `200 []`, so the client checks the argument against
  the term list first and exits 2 with near matches. A category URL from another shop exits 2 ("not a hivetec.mk URL").
- **The taxonomy is flat and multi-valued.** Every term has parent 0 except INTEL (under ПРОЦЕСОРИ; all of its
  products are also in the parent). A product sits in 1–5 terms: type terms next to brand terms (LOGITECH, KEYCHRON),
  series terms (NVIDIA RTX 50 Series) and promo terms (КРЕИРАЈ СВОЈА КОНФИГУРАЦИЈА, ПРЕПОРАЧУВАМЕ).
- **Departments are split** into a plain and a gaming term. List both: `list gaming-monitori,monitori`; the two terms
  barely overlap.
- **Counts.** A term `count` equals `X-WP-Total` and the site's "Прикажувам 1–12 од N резултати", out-of-stock items
  included. Checked for gaming monitors, monitors, SSD, ХАРД ДИСКОВИ/ ССД, gaming headsets, headphones, gaming laptops,
  laptops and CPUs. The id sets of the listed products, and of all keyboards, equal an independent catalogue walk, with
  identical prices, stock flags, titles and SKUs.
- **`--grep`** matches the name, path, slug, two transliterations (`slushalki` and the shop's `slusalki`) and English
  words (`headphone`, `mouse`, `gpu`, `psu`, `chair`...). `монитор` and `monitor` both find all the monitor terms.

## Facets and filters

- `facets <cat>` walks the category and counts values exactly, with in-stock counts. It covers brand (`pa_brand`, or
  "Производител:" from the short description), every WooCommerce attribute present, "also in category" (brand,
  series and promo terms), the price range and stock.
- **Attributes depend on the department.** Monitors have a size bucket `golemina`, `refreshrate`, `panel`
  (IPS/VA/OLED/QD-OLED/TN...), `rezolucija` and `responsetime`. Headsets and mice have `tip` (wireless or wired) and
  `boja` (colour); others have GPU series, cooler size, `kapacitet` or `brzina`. Most PC parts and laptops have only a
  brand, so read their specs from the title.
- **Tokens:** `brand=samsung`, `panel=oled,qd-oled`, `refreshrate=240hz360hz`, `tip=bezicni`, `cat=asus`,
  `price=-10000` / `5000-` / `MIN-MAX`, `stock=in|out`.
  Commas OR within a token, and repeated `--filter` flags AND. Keys also match labels (`ПАНЕЛ=IPS`); values match the
  term id, slug, name or a transliterated name. An unknown key or value exits 2 and lists the valid ones. Filtering is
  client-side after the walk, so it is exact.

## Search semantics (verified)

- **The store matches the whole query as one case-insensitive substring of the title or SKU.** It does no word AND and
  does not search descriptions or categories. `nitor` finds the same as `monitor`; `990 pro` finds the 990 PROs but
  `pro 990` gives 0; `Гаранција`, which is in every short description, gives 0; a SKU that is not in the title
  (`CT1000P310SSD8`) still finds its product.
- **The client emulates word-AND.** It counts each word, searches the rarest, and keeps hits whose title or SKU contains
  every word. Words of 1–2 characters (letters or digits) must be whole tokens, so `oled 27` keeps the 27″ panels
  but not the 26.5″ AOC Q27G41ZDF. `samsung ssd` finds the Samsung SSDs (the raw store search gives 0), and
  `logitech g pro` works too. `--phrase` sends the query unchanged.
- **Cyrillic finds almost nothing** (only a handful of titles contain Cyrillic). A Cyrillic word with 0 hits is retried
  as Latin: `монитор` → `monitor`, `самсунг ссд` → `samsung ssd`, `лаптоп asus` → `laptop asus`. A Cyrillic word beyond
  the 4 counted ones is accepted as typed or transliterated (`samsung 990 pro 2tb ссд` finds the same as with `ssd`). If
  a word still finds nothing (`слушалки`, `тастатура`), the client prints the matching categories to `list`.
- **Recall is good for product types and models.** Titles are normalised English and name the product type: all
  headphones/headsets and nearly all monitors have that word in the title, and every Samsung SSD has "SSD". Specs that
  only appear in descriptions (Thunderbolt, HDMI 2.1, ANC...) are not searchable: `list` the category with `--filter`,
  then read `specs` with `detail`.
- **The storefront's search box is a different engine.** `/?s=<q>&post_type=product` is relevance-ranked and also
  searches descriptions, so it returns far more, looser hits (for `samsung ssd` or `thunderbolt`, an order of magnitude
  more than the client). Don't compare the client's counts with what the site's search page shows.
- **Traps:** `monitor` also hits monitor arms, and `rtx 5070` also hits 5070 Ti cards and laptops. There is no cap and no
  relevance ranking; results are newest first.
- **Code lookup.** `search <MPN/SKU>` works, and `detail sku:<code>` uses the exact `sku=` parameter. A number that is
  neither a product id nor a variation id is retried as a SKU, because some SKUs are numeric.

## Prices

- `price_mkd` = `prices.price / 100`, **VAT included** (policy page). It equals the page's "Current price" and the
  JSON-LD `offers.price`.
- `regular_price_mkd` = `prices.regular_price` when it is higher. **Nearly all products** have one, with a gap of mostly
  8–25%. It is a permanent list price, **not a promotion signal**, so compare current prices across shops.
- **Variable products** are rare (Gigabyte A16 laptops, with and without Windows). In listings `price_mkd` is the lowest
  variant ("без Windows") and `attributes["variant price range (MKD)"]` shows the range. `detail` adds
  `extra.price_range_mkd` and `extra.variations`, each with its own `price_mkd`, `regular_price_mkd`, `in_stock` and
  `url`. A link that names a variant (`?attribute_pa_operativen=...`, as the shop's variant links do) or a variation
  id (`39416`) reports **that variant's** price, stock and URL. The record `id` stays the parent product's, and
  `extra.selected_variation` is set.
- There is no card, cash or club price. There is a cart and checkout, and orders by phone or e-mail are also accepted.
- **Sale window: not exposed**, so `price_valid_until` is never set. The Store API has no `date_on_sale_*` keys
  (`extensions` empty), `wc/v3` is 401, `wp/v2/product` `meta` is empty, and product and promo-category pages
  render no Woodmart countdown (checked 2026-10-03; site clock is a fixed UTC+1).
- **Delivery** is free over 4,500 ден (header banner as of 2026-10-03, not in the API). The fee below that is not
  published, so `shipping_mkd` is unset.

## Stock and delivery

- `in_stock` = `is_in_stock`. Out-of-stock items stay listed. `stock_note` is "no quantity shown" or the raw
  "Нема на залиха". `--in-stock` uses the server's `stock_status=instock`.
- **There are no quantities and no per-store split.** `low_stock_remaining` is always null, `is_on_backorder` is always
  false, and `per_location_stock` is null.
- **When checking a product page by hand:** the page shows no stock text for an in-stock item (the theme hides it).
  Out-of-stock items say "Нема на залиха", and listing cards say "Производот е достапен" or "Производот не е достапен".
  The reliable signals are the `instock`/`outofstock` class on `div#product-<id>` and the JSON-LD `availability`. The
  related-products carousel on the same page carries other products' stock labels.
- `delivery_estimate` (in-stock items only) is the **store-wide** policy (as of 2026-10-03): 72 h after order
  confirmation, in working days.
  Orders after 16:00 are confirmed the next working day. Delivery is by the shop's own courier, Тотал Пост or
  Еко Логистик, with a phone call first. There are no per-product or supplier lead times.

## Warranty

`detail.warranty` is the "Гаранција:" line of the short description, and `extra.warranty_days` holds the integer.
- **Common values** (2026-10-03), most frequent first: 360 дена, 720, 1050, 1080, 1800.
- **"0 дена"** (desks, chairs, accessories) means not entered. `warranty` is then null, and the raw text is
  kept in `extra.warranty_raw`.
- **Bundles and prebuilt PCs** say "Секој производ посебна гаранција" (each part has its own warranty).

## Data-quality traps

- **No EAN or GTIN anywhere** (API, JSON-LD, descriptions), so `ean` is always null.
- **Brands are as the shop files them** (`LOGITECH G` vs `Logitech`; mixed casing). A few products have no `pa_brand`.
- **Colour variants are separate products and can differ widely in price** (the Logitech G733 costs much more
  in lilac or blue than in white or black). The same model is sometimes listed twice under different ids and prices.
- **Attribute values are coarse buckets** ("24'' - 26.9''", "120Hz - 240Hz", "1080P до 2K" even on QHD panels).
- **Slugs and names don't always agree.** ПРОЕКТОРИ has the slug `prezenteri`, ПРЕЗЕНТЕРИ has `prezenteri-2`, and
  product URL slugs come from old titles.
- **SKUs.** A few products have no SKU, and numeric SKUs can look like ids.

## Bot protection and politeness (checked 2026-10-03)

- **Cloudflare challenges long query strings.** At about 200 characters, counted percent-encoded, the edge
  answers 403 `cf-mitigated: challenge` "Just a moment..."; the path length doesn't count, and the next request
  is fine. The client caps queries at
  170 characters (commas stay literal, `include=` chunks of about 120) and refuses a longer search with exit 2
  before sending it. A Cyrillic letter costs 6 characters, so one search word can have about 24 Cyrillic letters.
- Cloudflare refuses some library user agents (error 1010). The client sends a desktop Chrome UA.
- **Keep it sequential.** The origin queues concurrent requests (a parallel burst slowed each to several seconds).
  A few hundred paced requests saw no 429, `Retry-After` or `X-RateLimit`.
- One unexplained transient block was seen in a burst of back-to-back runs; the identical run succeeded seconds
  later. The client therefore retries a block response once after 5 s before it exits 3.
- Every HTML page embeds Cloudflare's `/cdn-cgi/challenge-platform/` beacon. That is not a challenge; the client
  checks only the first 20 KB of an HTML error body for challenge markers.
- `robots.txt` (2026-10-09) disallows only cart, admin and upload paths.
- The client paces at 0.6 s, retries 429/5xx with backoff, retries a block response once, and exits 3 on a
  401/403, `cf-mitigated`, challenge markup or a persistent 429.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Multi-word search finds too little | Only titles and SKUs are searched. Drop spec words, or `list` the category with `--filter`. |
| Cyrillic search returns 0 | Titles are English. Use English words or model codes, or the category the client suggests. |
| Search returns accessories or other models | Substring matching. Add words, use `--category`, or `list` + `--filter`. |
| Exit 2 "query string would be N chars" | Cloudflare challenges queries over ~200 characters. Shorten the search text. |
| Exit 2 "unknown category" | Use an id or slug from `categories --grep`. Give a name containing a comma whole, or use its id. |
| `list` exit 2 "store returned no products" | The term count is above 0 but the API returned nothing: a soft block or a catalogue change. Retry with `-v`. |
| `list --in-stock` 0 rows, exit 0 | Nothing in that category is in stock. |
| A department seems small | It is split into a plain and a gaming term. Use a comma union. |
| `detail` "not found" for a number | It is not a product id, a variation id or a SKU. Use the product URL. |
| `detail` price differs from the page for a laptop | It is a variable product. Pass the variant link (with `?attribute_...`), or read `extra.variations`. |
| The site's search page shows hundreds of hits | That is a different, relevance-ranked engine that also searches descriptions. The client matches titles and SKUs only. |
| Exit 3 `BLOCKED` | Error 1010 means a UA problem. "Just a moment" means the query was too long or the rules changed. Retry one request later; never parallelise. |
