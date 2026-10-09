# Store client contract

Every store has one self-contained client at `scripts/stores/<key>.py`
(Python 3 standard library plus `requests` and `bs4`; no imports between
store files). `scripts/mkshop.py`
drives them all through this contract, so a client that deviates breaks
cross-store search silently. Read this before fixing a client or adding a
store.

## Store keys

| key | shop | client |
|---|---|---|
| `setec` | setec.mk | `setec.py` |
| `anhoch` | anhoch.com | `anhoch.py` |
| `neksio` | g.store.neksio.mk | `neksio.py` |
| `ddstore` | ddstore.mk | `ddstore.py` |
| `neptun` | neptun.mk | `neptun.py` |
| `hivetec` | hivetec.mk | `hivetec.py` |
| `gjirafa50` | gjirafa50.mk | `gjirafa.py --site gjirafa50` |
| `zirafamall` | zirafamall.mk | `gjirafa.py --site zirafamall` |
| `setra` | setra.mk | `setra.py` |
| `ananas` | ananas.mk (marketplace) | `ananas.py` |
| `tehnomarket` | tehnomarket.com.mk | `tehnomarket.py` |

## Commands

All commands accept `--json PATH`. With it, the client writes a JSON list to
PATH and prints only the record count to stdout. Without it, it prints a
compact human-readable table (price, stock, title, url). Progress, warnings
and notes go to stderr, never stdout.

```
<key>.py info
<key>.py search "<query>" [--limit N] [--in-stock] [--json PATH]
<key>.py categories [--grep REGEX] [--json PATH]
<key>.py list <category> [--in-stock] [--limit N] [--filter TOKEN ...] [--json PATH]
<key>.py detail <url-or-id> [<url-or-id> ...] [--json PATH]
<key>.py facets <category> [--json PATH]          # only if capability "facets"
```

- `info` prints one JSON object to stdout, network-free:
  `{"store": key, "name": ..., "base_url": ..., "capabilities": [...], "sells": "...", "notes": "..."}`.
  Capabilities are drawn from: `search`, `categories`, `list`, `detail`,
  `facets`, `filter`, `ean_in_listing`, `ean_in_detail`, `stock_qty`,
  `per_location_stock`, `warranty`, `seller`, `delivery_estimate`,
  `search_ean` and `search_codes` (the store's own search finds a product by
  its EAN / on-site code; either one makes `mkshop.py match` search that
  store by EAN).
- `search`: the store's own full-text search, all pages up to `--limit`
  (default: everything the store returns, capped where the store caps). If
  the store silently truncates or falls back to OR-matching, say so on stderr.
- Warnings that change how to read a result (truncation, OR fallback, partial
  data) go on an unindented stderr line containing `WARNING`. `mkshop.py`
  copies those into the shop's status note and treats indented lines as
  progress.
- `categories`: the store's category tree as flat records
  `{id, slug, name, path, url, parent, count}`. `path` is the human
  breadcrumb ("Компјутери > Монитори"); `count` is the store-reported product
  count or null. `--grep` matches case-insensitively against name, path and
  slug, and must work for both Cyrillic and Latin input. Where it is cheap,
  also match a Latin transliteration of Cyrillic names, so `--grep monitor`
  finds "Монитори".
- `list`: every product in a category, walking **all** pages, with
  `<category>` given as any `id`, `slug` or `url` that `categories` printed.
  Parent categories include their descendants when the store's own page does.
- `detail`: one record per input, in input order. A failing item must not
  abort the batch: emit `{"input": ..., "error": "..."}` for it and continue.
- `facets` / `--filter`: only where the store exposes structured attributes
  (layered navigation, attribute taxonomies, search-engine facets). `facets`
  returns `{name, value, count, token}`. `token` is the exact string to pass
  to `list --filter`; several `--filter` flags AND together.

## Record schema

Listing records (search / list) — null when genuinely unavailable, never guessed:

| field | type | meaning |
|---|---|---|
| `store` | str | store key |
| `id` | str | store-internal product id |
| `sku` | str/null | the code the store shows on its page (Шифра) |
| `title` | str | full title as listed |
| `url` | str | absolute canonical product URL |
| `brand` | str/null | as the store files it (may be a distributor name) |
| `price_mkd` | int/null | what an ordinary online buyer pays today, VAT included, before delivery fees; null when the store shows no price ("ask for price") |
| `regular_price_mkd` | int/null | struck-through price if the store shows one |
| `in_stock` | bool/null | null = orderable but the store gives no availability, or asks you to enquire |
| `stock_note` | str/null | raw availability text / quantity |
| `category` | str/null | category name or breadcrumb |
| `ean` | str/null | EAN/GTIN if the listing carries it cheaply |

Optional listing fields: `mpn`, `seller` (marketplaces),
`international_supplier` (bool), `delivery_estimate` (str),
`shipping_mkd` (int), `attributes` (object name→value), and member pricing:
`member_price_mkd` (int, only when lower than `price_mkd`; a price that
needs a cheap, easily obtained loyalty card or membership),
`member_price_condition` (short text naming the card or membership and what
it costs) and, as a fallback, `member_price_name`. `price_mkd` stays the
price without any membership. `mkshop.py` derives `effective_price_mkd` and
`price_condition` from these.
`price_valid_until` (str/null): ISO 8601 date-time with the Europe/Skopje
offset (`2026-10-04T23:59:00+02:00`) at which `price_mkd` ends per the shop
(kept if already past but still charged); null = the source gives no end
(standing price), key absent = this record cannot tell (`detail` may).
`member_price_valid_until` (str/null): the same for `member_price_mkd`, only on
records with a member price.

Detail records add `warranty` (str/null), `ean`, `specs` (description/spec
text, whitespace-collapsed), `per_location_stock` (list of
`{location, in_stock, quantity?}` or null), and `extra` (object for
store-specific data such as price floors).

## Behaviour

- **Identity:** one `requests.Session` with a current desktop Chrome
  User-Agent and an `Accept-Language` of `mk,en;q=0.8`. Several stores block
  python-requests / urllib / empty user agents, and some switch language
  by user agent.
- **Politeness:** strictly sequential within a store, with 0.3–1 s between
  paginated requests and backoff on 429/503. Some origins queue parallel
  requests badly (Anhoch, Neksio, Hivetec, Setra). Never parallelise inside a
  client; `mkshop.py` parallelises across stores only.
- **Exit codes:** `0` success, including a genuine zero-hit search. `1`
  unexpected error. `2` bad usage or unknown category/product. `3` blocked: a
  CAPTCHA, Cloudflare challenge, WAF or user-agent block, or a login wall.
  On exit 3, print one stderr line starting `BLOCKED:` with the status code
  and evidence (title, cf-ray, ...).
- **No silent emptiness:** if a store answers 200 with zero items where items
  are expected (unknown category, soft block, layout change), print a warning
  on stderr. For `list`, exit 2.
- **Prices:** integers in MKD. Parse `6.130 ден.`, `6,130`, `5990,00`, minor
  units and so on. Never return a regular price as the current price.
