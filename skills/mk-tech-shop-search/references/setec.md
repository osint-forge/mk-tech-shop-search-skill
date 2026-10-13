# Setec (setec.mk): store reference

Client: `scripts/stores/setec.py`.
Read only when you need Setec specifics. Facts were checked live on 2026-10-03 (some on
2026-10-09) and can drift.

## What it sells, and when to use it

North Macedonia's largest electronics/home chain: a catalogue of over ten thousand products and
walk-in stores in over twenty towns plus a central warehouse. Departments, roughly largest
first: Компјутери и IT опрема (PC parts, monitors, peripherals, networking) · Мали Апарати ·
Дом, Градина и Алати · Бела Техника · Телефони/Фото и Навигација · Спорт и рекреација ·
ТВ/Аудио/Видео · Ладење и Греење · Преносни Компјутери и Таблети · Осветлување · Конзоли ·
Оптички уреди. Use it for almost any electronics or appliance question, and for per-store
pickup.

## Access and endpoints

All public JSON; no cookies or login.
- **Catalogue (Meilisearch).** `POST https://search.sp.solslab.dev/indexes/products/search`
  with `Authorization: Bearer <SEARCH_KEY>` (`setec.py`), the public key from the site's JS.
  Body: `q`, `filter`, `facets`, `sort`, `limit`/`offset`, `attributesToRetrieve`.
  `POST /multi-search` batches queries.
- **Detail.** `GET https://setec.mk/api/medusa/products-with-details-web?handle=<slug>`
  gives warranty, per-location `available_quantity`, description and price list.
  An unknown handle returns `404 {"product":null}`.
- **Category tree.** `GET https://setec.mk/api/strapi/category?locale=mk-MK&withSubcategories=true`
  is the site's menu: nodes with `medusaID` (`pcat_…` id), `slug` (handle) and name.
- **Web config.** `GET https://setec.mk/api/medusa/web-config` returns `order_threshold`, which
  the client reads live (see Stock semantics).
- **Store locator.** `GET https://setec.mk/api/strapi/page?locale=mk-MK&slug=prodazni-saloni`:
  `sales_channels[]` with `name`, `title`, `address`, `working_hours`, `contact_info` (the
  shops plus head office, service centre and central warehouse). Its names differ from the
  detail API's (`Сетек ГОДИ` vs `СЕТЕК Годи`); match them by name, not just by town, since some
  towns have two locations. The client does not call it.
- **URLs.** `https://setec.mk/products/<handle>` (canonical), `https://setec.mk/category/<handle>`.
- **Base filter** (the site's own, added by the client): `status = 'published' AND is_web_active = 'true'`.

## Category model

- **Identifiers.** `id` is `pcat_01JFZ…`; `slug` is an escaped handle with a numeric
  suffix (`komp-d1-98uteri-20i-20it-20oprema-19`). `list` takes the id, slug, URL or exact
  name.
- **`--grep`** matches name, path, slug, a loose Latin transliteration (`monitor`, `frizider`,
  `klima` all work) and a built-in list of English and colloquial words per department
  (`laptop`, `washing`, `перални`, `smartphone`/`смартфон`, `fridge`, `vacuum`,
  `air condition`, `playstation`). It is a substring regex, so `tv` also hits Тврди дискови;
  use `\btv\b` for whole words.
- **Tree.** One menu root per department, 3 levels. Products carry their leaf and usually its
  ancestors, but not always. Several parents hold products tagged only with a child (for
  example Компјутерска галантерија, whose direct count falls short of its total with
  children). As on the site's category page, `list` and `count` cover the parent plus all menu
  descendants.
- **Off-menu and empty.** Some categories that hold products are missing from the menu (for
  example Оптички Уреди > Двоглед). They print with `"in_site_menu": false`, and their
  parent is inferred from co-membership. A few menu nodes are empty; `list` on one exits 2.
- **1000-hit ceiling.** No query returns more than 1,000 hits, and the site's own category
  page stops at "1000" / 50 pages. `list` counts exactly through facets (uncapped), splits
  the category into windows by brand and then price, and verifies the total. A root of
  several thousand products comes back complete in seconds.

## Facets and filters

`facets <cat>` returns the category's `attribute_pairs` facet. Each token is the exact
`Name::Value`, e.g. `Refresh Rate::144 Hz`, `Капацитет::8кг`, `Големина на екран::27"`.

- `--filter` flags are ANDed; matching is case-insensitive (`ram::12 gb` works).
- `Бренд::X` is ORed with `brand_name = X`, as the site does.
- If the list comes back empty, each token is checked against the category's facets (and
  `Бренд::X` against its brands); one that never occurs exits 2. Valid tokens that simply
  AND to zero exit 0 with a stderr note.
- Other text is passed through as raw Meilisearch
  (`variants.calculated_price.calculated_amount < 20000`).

Attributes settle membership, not completeness. About half the monitors lack `Тип на Екран`,
so run `gaps` before trusting a value filter and recover the blanks from the `specs`
(description) text it returns: most monitors with a blank Refresh Rate state their Hz only
there, not in the title. Values
are banded (`Батерија (mAh)::4000-4999`) and sometimes split across names
(`Дубина`/`Длабочина`). Monitors also carry the TV attribute `Технологија на телевизор`.

## Search semantics

- **Matching** follows the site: `matchingStrategy: "all"` (AND). It matches prefixes and
  tolerates typos (1 for 4+ letters, 2 for 8+).
- **Fields.** Descriptions and category names are searched, so recall is wide: `frizider`
  ranks fridge organisers above fridges; use `list` for clean product types. The site's
  hybrid mode is inactive (no embedder exists).
- **Script.** Cyrillic beats Latin: `televizor` and `slusalki` find a small fraction of what
  `телевизор` and `слушалки` find. The client also runs the query in the other script and
  appends new hits; stderr shows counts per variant. Digit-bearing words (models, EANs) and
  whole brand names (`bosch`, `cooler master`) stay as typed, because `Bosch` spelt `босч`
  matched Gorenje `BOS…` ovens. So `slushalki sony` searches `слушалки sony` (as typed it
  found nothing). Words that only occur inside a brand (`fitness`, `master`) are still
  transliterated (`фитнес` adds many genuine hits).
- **Typos.** Typo tolerance is off for `external_id`, `handle`, `id` and
  `variants.catalogue_number`, but on for numbers elsewhere, so a Шифра can also hit a
  description.
- **The last word is a prefix.** Glue words (`for`, `and`, `за`, `со`) are ignored anywhere but
  at the end, where the last word is prefix-matched like any other: on 2026-10-09 `laptop` and
  `for laptop` found 246, `laptop for gaming` = `laptop gaming` 18, but `laptop for` 14, `laptop
  an` 21 and `laptop and` 0; `фрижидер со` 90 against `фрижидер` 273. Never end a query on a glue
  word or a cut-off word.
- **Codes.** An EAN or the on-site Шифра (`external_id`) finds the product; `detail`
  accepts both.
- **Ceiling.** Each variant caps at 1,000 hits, and stderr gives the true total.

## Price semantics

- **`price_mkd`** = `calculated_amount`, the "Клуб цена" from the active "Web Prices"
  list. The site says the online price is for web orders only ("ОНЛАЈН ЦЕНАТА ВАЖИ САМО ЗА
  НАРАЧКИ ОД ВЕБ СТРАНАТА"), and any web buyer pays it.
- **`regular_price_mkd`** = the struck "Редовна цена" (`original_amount`), set only when
  higher. Almost everything has one, so a discount alone is not a deal.
- **Units.** MKD, VAT included. A handful of items priced from EUR (Huawei solar inverters)
  carry fractional amounts; the client rounds.
- **Stale club price lists (rare).** Where the "Web Prices" list holds a figure above the
  base price, Medusa charges the lower base price (`calculated_amount`, which is
  `price_mkd`), but the site still prints the stale figure as "Клуб цена", which can be
  several times the Редовна цена. The record keeps `price_mkd` = the base price and adds
  `extra.site_shown_club_price_mkd` = the stale figure so the mismatch is visible.
- **Promotions.** The site would show a separate badge price for an active price list with
  role `promo`; none existed in a whole-catalogue check on 2026-10-03. Every product has one
  `Web Prices` (role `club`) list or none, and `extra.price_list` shows its
  `starts_at`/`ends_at` (all null at that check).
- **`price_valid_until`** = `ends_at` of the price list behind `price_mkd` (index:
  `calculated_price.price_list_id`; detail API: the list whose amount equals the price).
  Null (standing) when that list has no `ends_at` (the one `Web Prices` list, on every product
  on 2026-10-03) or when Medusa charges the base price (`price_list_id` null, as on the stale
  club lists above, or no price list at all). The key is left out (unknown) when the list behind
  the price cannot be identified (no id or amount match, or matches that disagree) or its
  `ends_at` is unreadable.
- **Delivery.** Fees appear only in checkout; a free-delivery threshold exists but needs a
  cart to read.
- **Price floors are gone.** The detail API no longer returns the internal floors
  (`recommended_retail_price`, `min_web/min_retail/min_wholesale_price_with_vat`) or
  `catalogue_number`. `extra.price_floors` stays null unless they return.

## Stock semantics

`total_web_quantity` is the unit count summed over the stores, the warehouse
(`Главен Магацин`) and `СЕТЕК Web`. With `order_threshold` 3 (its value on 2026-10-03; the
client reads it live from web-config):

- **0:** "Извести ме" (notify me) → `in_stock: false`.
- **1–3:** "Нарачај", an order request confirmed by phone → `true`, low stock flagged.
- **4+:** add to cart → `true`.

`stock_note` gives the quantity; `--in-stock` means total > 0 (the site's "Достапно").

`detail` → `per_location_stock` across all locations,
`{location, in_stock, quantity, walk_in, in_web_total}`.

- **Walk-in:** false for the warehouse and СЕТЕК Web; warehouse-only = order online, no pickup today.
- **Names:** out-of-Skopje stores by town (СЕТЕК Битола 2); a name without a town is a Skopje
  store (Годи, Стадион, Драчево, Чаир, Ѓорче Петров, ГТЦ, Ми Сторе: all Skopje addresses on the
  store locator, 2026-10-09). Trust the locator's address over its title, which can be wrong
  (Битола is titled 'Скопје'). Some detail-API locations were not on the locator then
  (`СЕТЕК OUTLET`, `СЕТЕК Струмица 2`, `СЕТЕК Штип 2`): give their town from the name, but no
  address, and say so if it decides the answer.
- **Quantities:** shop stock is often 1.
- **Outlet:** `СЕТЕК OUTLET` units are walk-in only and are not counted in
  `total_web_quantity`; the per-location sum exceeds the total by exactly the outlet's
  quantity. Those rows carry `in_web_total: false`. A product can be out of stock online
  with a unit at the outlet.
- **Delivery times** are not exposed.

## Warranty

`detail` gives `"24 months"` and `extra.warranty_months` (common on 2026-10-03: 12/24/35/60;
0 = none listed).

## Data-quality traps

- **Recycled slugs.** `durbin-302` is Преносни клима уреди and
  `klimi-20rezervni-20delovi-307` is Касетни клима уреди. Match by name/path, never by slug.
- **Mixed scripts.** `Kлиматизери` starts with a Latin K. `--grep` matches a loose
  transliteration, so `klima`, `клима` and `Клима` all work.
- **EANs.** `catalogue_number` may hold several EANs (about 15% of phones) or internal codes
  that fail the checksum (a few percent of white goods). `ean` is the first valid GTIN or
  null; the raw codes are in `extra.catalogue_numbers`.
- **Brands.** Some products have no `brand_name`, and some are house labels (ST, BAUTECH).
  A few brands are stored as `?????` (lost Cyrillic); the client reports those as `brand: null`.
  The raw `attributes` array has empty placeholders and internal codes (`Бренд::00019`),
  so `attributes` is built from the clean `attribute_pairs`.

## Bot protection and politeness

None observed (2026-10-03): no Cloudflare/WAF and no rate-limit headers; the search host answers 401 without the
key. Unknown product pages answer a soft 200, which does not matter because the client reads only the JSON APIs.
`robots.txt` (2026-10-09) allows everything. The client still runs sequentially, 0.35 s per host, backs off on
429/5xx (honouring `Retry-After` up to 60 s), and exits 3 (`BLOCKED:`) on an HTML challenge, a 403, HTML where JSON
was expected, or a 429 that persists after 4 tries on a call it cannot do without (a persistent 429 on web-config,
the category tree or one detail item falls back as for any other error).

## Setec-only commands

These run on the client's own HTTP layer, with the same category resolution, base filters
and block detection as `list`.

- `gaps <cat> --attr NAME [--in-stock] [--limit N] [--json PATH]`: products with the
  attribute blank or absent, as contract records with the description as `specs`
  (one extra request per 100 products shown). Covers the whole category, children
  included and past 1,000. An attribute name that never occurs exits 2 and lists the names
  that do.
- `brands [<cat>] [--json PATH]`: brand counts, category expanded as in `list`.
- `stores <url|id|Шифра|EAN> ... [--json PATH]`: per-location stock rolled up across a
  shortlist ("2/3 models"). Unresolvable inputs are warned about and kept as error rows.

## Troubleshooting

- **Unknown category (exit 2):** `categories --grep` with an English word or a Macedonian
  stem (`laptop|prenosni`, `washing|перење`); only ids, slugs, URLs or exact names work.
- **Filter returns 0:** copy the token verbatim from `facets` (`8кг`, `24"`), then run `gaps`.
- **401 / `invalid_api_key`:** key rotated; the client re-reads it from the site JS once and says so on
  stderr. Update `SEARCH_KEY` (and `SEARCH_BASE` if the host moved) in `setec.py`.
- **"expected N, collected M":** the index changed mid-run; rerun.
- **Detail fails, search finds it:** the record keeps index data plus `extra.detail_error`.
- **Unrelated hits:** descriptions are searched; narrow with `list <cat> --filter`.
