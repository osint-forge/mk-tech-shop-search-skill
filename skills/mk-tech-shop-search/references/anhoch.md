# Anhoch (anhoch.com): store reference

Client: `scripts/stores/anhoch.py` (key `anhoch`). Verified live 2026-10-03.

## What it sells and when it is useful

Anhoch is a Macedonian IT and electronics chain with stores in Skopje and other towns, and a webshop
whose departments are: gaming & streaming (consoles, games, chairs, gaming peripherals); computers, laptops
& tablets; TV/audio/video (TVs, soundbars, headphones, projectors); phones & watches; networking, tools,
cables & adapters; PC components; home, business & outdoor (smart home, air conditioners, UPS, e-scooters,
power stations); printers & scanners (incl. toners, 3D printers); portable storage; photo/video/drones; vouchers.
It sells **no fridges, washing machines or kitchen appliances** (a title scan of the whole catalogue on
2026-10-03 found none; the nearest is a car vacuum cleaner). It is strong on laptops,
monitors, PC parts, phones (Samsung, Xiaomi, Apple), peripherals and gaming.

## Access

Everything goes through FleetCart's own storefront JSON, so no HTML scraping is needed for listings.

- **Listing and search:** `GET /products` with header `X-Requested-With: XMLHttpRequest` (without it
  you get the HTML shell) returns `{products: <Laravel paginator>, brands: [...], attributes: []}`.
  Parameters: `categories[0]=<slug>` (several are OR-ed; a parent includes its descendants), `query=`,
  `brand[0]=<numeric brand id>`, `inStockOnly=1` (in stock) or `2` (all, the site default),
  `sort=latest|relevance|alphabetic|priceLowToHigh|priceHighToLow|topRated`, `perPage` (the server
  accepts 1000+; the client uses 250) and `page`.
- **Rows** carry `id`, `name`, `slug`, `price`, `selling_price`, `special_price*`, `qty`, `is_in_stock`,
  `in_stock_date`, `installments` and `actions` (campaign badges), but **no brand, MPN or EAN**.
- **Category tree:** `:initial-categories` JSON embedded in any `/categories/<slug>/products`
  page (several hundred KB). It holds every node's id, slug and name, but no counts.
- **Detail:** `GET /products/<slug>` (HTML, several hundred KB) embeds the whole product model in
  `<product-show :product="...">`. It also has a breadcrumb, `li.sku` "Шифра" and JSON-LD.
  An unknown slug returns 404.
- **Detail by id:** no id route exists and search does not index ids, so the client uses the anonymous
  compare list (`GET /compare` for the CSRF token, `POST /compare productId=<id>`, `GET /compare`): 3 requests
  for the first id, 2 for each later one. An unknown id gets a 404 on the POST.

Typical cost: a category `list` with brands filled takes tens of requests and under a minute; a
department-sized `list` (brand skipped) needs little more than one request per 250 rows; `detail <url>` 1 request.
Brand enrichment is most of the cost (one request per brand, even with `--limit`); add `--no-brands`
when the brand is not needed.

## Category model

- The tree has 3 levels. Ids are 6–7 digit numbers (`590814`). Slugs are
  transliterated Macedonian (`monitori`); some carry capitals or spaces (`Televisions`, `bluetooth adapteri`),
  which the client URL-quotes. `list` accepts an id, a slug, a category URL or an exact Cyrillic name.
- A parent includes all its descendants (the `printeri-i-skeneri` leaves sum exactly to its total), and the
  root counts sum to the catalogue total, so each product has one home tree.
- `categories` returns `count: null` because the tree has no counts. `categories --grep X --counts`
  fetches them (one request per category, at most 40).
- `--grep` is a case-insensitive regex over name, path, slug and a Latin transliteration (`monitor`, `Монитор`,
  `ssd`, `telefon`, `принтер`, `televiz`, `slushalki`, `клима` all work). Path matching means `laptop` hits the
  whole "Компјутери, лаптопи и таблети" tree; anchor with `^...$` to match one field.
- Category names are Macedonian, so **English words mostly miss** (`headphone` 0; use `slushalki`/`слушалки`),
  and short Latin terms can hit transliterations (`tv` also matches Софтвер = "softver"). `washing`, `перални`,
  `frizider` match nothing because the store sells no such appliances.
- `list` matched an independent walk id for id on `monitori`, `interni-ssd`, `site-laptopi`,
  `mobilni-telefoni`, `laserski-printeri`, `Televisions`, `eksterni-ssd` and the parents `laptopi` and
  `monitori-i-oprema` (exactly the sum of its three leaves).

## Facets and filters

- The only layered navigation is **brand**: `attributes` is `[]` in every root, so there is no spec facet
  (size, capacity, refresh rate). Parse the spec-dense English titles (`Monitor 27" ... 240Hz QHD`) or detail `specs`.
- `facets <cat>` returns `{name:"brand", value, count, token:"brand=<slug>"}` (one request per brand); the
  counts partition the category (on SSDs they sum exactly to its total). `list <cat> --filter brand=samsung`
  filters server-side; `brand=samsung,kingston` means either. Two `brand=` flags would AND to nothing, so exit 2.

## Search semantics (verified)

- The query is matched as an **ordered phrase of word prefixes** over the title and the description
  (not the MPN, see below).
  - Order matters: `1tb ssd` returns several times as many hits as `ssd 1tb`, and `rtx 5070` dozens
    where `5070 rtx` finds almost nothing.
  - Prefixes match (`keychro` returns the same as `keychron`), but mid-word fragments do not (`ychron` returns 0).
  - Words are split on **every non-alphanumeric character**, not only spaces: `backlitkb` matches the laptop
    titles that contain `.../BacklitKB`, and `EP-T4511XBEGEU` is searched as `ep t4511xbegeu`.
- **If the phrase matches nothing, the site silently ORs the words** (`nvme samsung` and `dark project`
  return hundreds of unrelated items). The client splits the query the same way, fetches each 3+ character
  word's own count (one request per word, up to 5) and prints `WARNING: ... fell back to OR-matching` when the
  total exceeds the rarest word's count. That also catches hyphenated codes, where only a short fragment matched.
- **`--all-words`** (client feature): products whose **title** contains every word in any order,
  built from the rarest word's hits plus the phrase hits. In-stock `ssd 1tb` gives a couple of hits by phrase
  and dozens with `--all-words`. Use it for multi-word spec queries.
- **Totals cap at 1000** (`монитор`, `usb`), and the client warns when this happens. Narrow the search with `--category` or use `list`.
- A whole query **under 3 characters returns 0** (`4k`, `27`), and the client rejects it (exit 2).
  Short words inside a longer phrase still filter: every hit for `monitor 27` contains "27".
- Cyrillic works against descriptions (`телевизор` returns a few hundred hits, mostly TVs and mounts). Titles
  are English, so the Latin form usually gives cleaner hits. There is no stemming.
- Code lookup: **the MPN is not indexed.** A code only matches when it is part of the title (`P2425H`
  finds the Dell P2425H/P2425HE because both titles contain it; `DS-610` finds the Sbox stand). Part
  numbers that appear only in detail `mpn` return 0 (`83DM0006US` Lenovo Yoga 7, `VOA150K001` Nikon Z50 II,
  `9H.FC2TC.DE1` BenQ, `JBLGO4PUR`, `SENIOR10PLUSBLACK`, `BZHC6EED65SP`, all verified 2026-10-03).
  Hyphenated ones are worse: `EP-T4511XBEGEU` (a Samsung 45W charger) returns unrelated "EP-" earphones
  and `TS-507NQ` unrelated items (a TS-PC racing wheel, DJ controllers) through the OR fallback. The client
  warns on those and adds a hint on 0-hit code queries. Search by model name instead (`yoga 7`,
  `super fast charger 45w`).
  The internal id/Шифра is **not** indexed either, so use `detail <id>`. **No EAN exists anywhere**, so `ean` is always null.
- Warnings that change how to read the result (`search cap`, `OR-matching`, shared slugs) are printed as
  unindented `WARNING:` lines, which `mkshop.py` copies into its envelope; indented lines are progress only.

## Prices

- `price_mkd` = `selling_price`, the price the buyer pays, VAT included, in whole MKD (the API sends `"8980.0000"`).
  It equals JSON-LD `offers.price` and the visible `.product-price`.
- `regular_price_mkd` = `price`, set only when it is higher than the selling price. That is the struck-through
  `.previous-price` while a special price runs. Discounts are rare (well under 1% of products sitewide on
  2026-10-03). A "was" price here is a real, temporary markdown. No product has a non-integer price, and
  there is no club or web-only price.
- `price_valid_until` (listing and detail) comes from FleetCart's `special_price_end`, which both the `/products`
  rows and the detail model carry: the special price runs while today <= that date, so the value is the end of that
  day in Skopje (`2099-10-31T23:59:59+01:00`). It is null for an unmarked list price and for an open-ended special
  (the charged price is the special price and it has no end date); on 2026-10-03 no special had an end date
  (markdowns can sit in a named campaign, but campaign windows are not linked to the price and are not used).
  The key is left out (unknown) when a markdown is not the special price (no special price, or another amount)
  or the end date is unreadable.
- `extra.installments` (`{months, per_month_mkd}`, the "24 рати x N" badge) and `extra.campaigns` (name and window,
  e.g. "Rati - Laptopi (<start> .. <end>)", "Понуда 24/0 ...") are badge campaigns (FleetCart flash-sale records with
  no price) with their own windows. They do not change the cash price.
- Delivery (from the FAQ and the terms, as of 2026-10-03): **230 MKD per order up to 5,000 MKD, 179 MKD above**,
  and pickup in any Anhoch store is **free**. `shipping_mkd` is that fee for a one-item order. Skopje deliveries
  usually arrive the same day (about 5 h if ordered by 11:00); other towns take about 24 h. Ordering requires an
  account; browsing does not.

## Stock

- `in_stock` = `is_in_stock`, the online flag. It agreed with `qty > 0` for every product on 2026-10-03.
  The `--in-stock` server filter returned the same set as the flag (checked on SSDs, `monitori-i-oprema`
  and TVs).
- `qty` is **capped at 10** (`stock_note` "qty 10+"). It can be negative (usually by a few units, sometimes by
  thousands), which means out of stock. `in_stock_date`, when set, appears as "expected <date>" (no product
  had one on 2026-10-03).
- `detail.per_location_stock`: `[{location, in_stock, region: Скопје|Македонија, walk_in}]`, yes/no
  only with no per-store quantities. It covers only the stores the product is attached to.
  "Главен магацин" is the central warehouse (`walk_in: false`): stock there can be ordered online but
  not picked up. The in-stock store sets matched the page's "Достапност по салони" modal on every
  in-stock product checked.
  Out-of-stock pages hide that modal, but the model can still mark a store in stock: an out-of-stock
  Samsung 45W charger listed "Анхоч Дирекција" as in stock. Trust `in_stock` for
  ordering online and phone the store before relying on such a row.
- Stock is mostly stable, but on 2026-10-01 a few keyboards flipped out and back within minutes (an ERP
  sync), and the online and per-store flags can disagree. Re-check a shortlist with `detail` just before
  recommending.

## Warranty

`detail.warranty` is the page text ("36 месеци"), and `extra.warranty_months` is the integer.
Seen on 2026-10-03: laptops 12 (Gigabyte AORUS 24), phones 24 (MeanIT 12), monitors, printers and Verbatim
SSDs 36, TVs and cameras 24, a Samsung charger 6. Some accessories have none (Sbox monitor stand), so
`warranty` is null.

## Data-quality traps

- **Duplicate slug `oprema-i-dodatoci`.** Two categories share it: 590914 (drawing-tablet accessories)
  and 590966 (photo/drone accessories). The endpoint filters by slug, so the slug and the
  site's own page return the union. `list 590914` / `list 590966` separate them by intersecting
  with the nearest ancestor that has a unique slug (verified disjoint, together equal to the union).
- **Brand is store-assigned and sometimes a distributor or placeholder.** `Other` (generic SSDs, desktop PCs);
  White Shark/Baracuda filed as `SBOX`; Fury as `Natec`. Prefer the title for model matching.
- **No brand in listings.** The client fills it with one query per brand. It skips this above 60 brands
  for `list` (any top-level category) or 25 for `search`, and the field stays null. Use a leaf category, `--filter brand=`, or `detail`.
- `sku` = Шифра = internal id (not the MPN); the MPN (`mpn`) is only in `detail`. The model's own
  `categories[]` is incomplete, so detail `category` comes from the breadcrumb.
- A nonexistent slug in `/products` returns 200 with 0 rows (the client validates against the tree and the
  category page's 404, so an unknown category exits 2). A trailing slash on a category page gives HTTP 500; the client strips it.

## Bot protection and politeness (checked 2026-10-03)

- The LiteSpeed origin and the Cloudflare edge each refuse some library user agents with a 403: an HTML page, or
  JSON error 1010 on the XHR listing calls. The client sends a desktop Chrome UA, recognises both forms, and exits
  3 with `BLOCKED: HTTP 403 ...` naming the layer.
- No CAPTCHA, Turnstile, JS challenge, rate-limit headers or login wall on any route used. The word "captcha"
  appears in every page (a route name), so it is not a block marker.
- **The origin queues concurrency**: parallel requests each took several times as long as one alone. Keep
  requests sequential; the client paces them 0.5 s apart and exits 3 if a 429 persists through 6 attempts.
- `robots.txt` allows everything. Paced requests got no 429 or 5xx.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| Exit 3, `BLOCKED: HTTP 403 LiteSpeed` | The UA header was lost or replaced. Check the session headers. |
| Exit 3, `BLOCKED: HTTP 403 Cloudflare error 1010` | Same cause, caught at the Cloudflare edge (Python-urllib-like UA, or a new WAF rule). |
| A part number finds nothing, or unrelated items | The MPN is not indexed (see the search notes). Search the model name from the title. |
| Exit 3, `expected JSON ... got text/html` | The `X-Requested-With` header is missing, or a real soft block. Retry one request by hand. |
| Search returns hundreds of junk items | The OR fallback (see the warning). Use `--all-words`, reorder the words to the title's order, or add `--category`. |
| Search misses obvious products | Phrase order. Use `--all-words` or a single distinctive word plus `--category`. |
| Exactly 1000 hits | The search cap. Narrow the query or `list` the category. |
| `brand` null in listings | Over the brand-query cap, or `--no-brands`. Use a leaf category or `detail`. |
| `list` exit 2 "returned 0 products" | Unknown or renamed slug, or an empty category. Re-run `categories --grep`. |
| Category id gives an unexpected mix | Shared slug (see the traps above). Pass the id, not the slug. |
| `detail <id>` not found | The id is not live (the POST to `/compare` returned 404). Ids are 4, 5 or 9 digits, like Шифра `743067139` or `32128`. |
| Stock looks wrong | Online flag vs. store flags, or the ERP sync. Re-run `detail` and read `per_location_stock`. |
