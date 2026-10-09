---
name: mk-tech-shop-search
description: Search, compare and price-check products across North Macedonian online shops (Setec, Neptun, Tehnomarket, Anhoch, Neksio, DDStore, Hivetec, Gjirafa50, ZirafaMall, Setra and the Ananas marketplace) through their structured catalogue backends instead of scraping pages. Use this whenever the user wants to find, shortlist, compare, price-check or deal-hunt anything to buy in Macedonia, such as TVs, phones, laptops, PC parts, monitors, peripherals, appliances, kitchen and home electronics, gaming, anything. That includes "where is X cheapest", "who has X in stock", "which store can I pick it up from", "how long is the warranty", "is this a good price", "can I get it this week", a question about just one of these shops, a pasted product link from any of them, or a product category with requirements and a budget in денари/MKD, even when no shop is named.
---

# Macedonian shop search

Eleven Macedonian storefronts, one toolkit. Each shop has its own client that
talks to the shop's structured backend: JSON APIs, search indexes, or the data
the shop's own frontend loads. That data is far better than what scraping
rendered pages gives you. `scripts/mkshop.py` drives them all: cross-store
search, category discovery, listing, facets, product detail and cross-store
product matching.

Paths such as `scripts/mkshop.py` below are relative to this skill's
directory. Call the tools by their absolute path and don't `cd` into the
skill: `mkshop.py` finds its shop clients itself. Keep each task's files
(`--json`, `--csv`, the report) together in one folder outside the skill:
the user's project directory if they named one, otherwise something like
`./mk-shop-<topic>-<date>/`. `<out>` below stands for that folder. Write
absolute paths, since shell variables don't survive between tool calls.

```bash
python3 scripts/mkshop.py --help
python3 scripts/mkshop.py stores          # who sells what, and what data each shop exposes
```

## The stores

| key | shop | what it sells | data worth knowing about |
|---|---|---|---|
| `setec` | Setec | The largest electronics and home chain, with stores nationwide: IT, phones, TV, small and large appliances, home/garden, sport | per-store unit counts, warranty months, EAN, attribute facets |
| `neptun` | Neptun | Electronics and appliance chain with stores nationwide: appliances, TV, phones, computers, gaming | loyalty-card price, per-store availability, EAN |
| `tehnomarket` | Tehnomarket | Electronics and appliance chain with stores nationwide: appliances, TV, phones, computers | per-store availability in every listing; no EAN |
| `anhoch` | Anhoch | IT and consumer electronics chain: gaming, computers, TV/audio, phones, networking, components, AC/smart home | per-store availability, quantity (capped at 10), MPN, brand facets |
| `neksio` | Neksio | An IT distributor's webshop: peripherals, components, cables/networking, gaming gear, small appliances; no TVs or large appliances | exact quantities, EAN, warranty days |
| `ddstore` | DDStore | IT and electronics: components, peripherals, printers/toner, laptops, phones, monitors, TVs, networking, AC | EAN, facets, availability labels incl. "ask for stock" |
| `hivetec` | Hivetec | Gaming and PC specialist | facets; no EAN, no quantities |
| `gjirafa50` | Gjirafa50 | Online-only, very large catalogue with the widest brand range | delivery quote per product; most items are supplier orders |
| `zirafamall` | ZirafaMall | Gjirafa's marketplace: mirrors most Gjirafa50 electronics at identical prices, and adds white goods, home, fashion, sport, toys | international-supplier badge, delivery quotes |
| `setra` | Setra | IT, gaming, peripherals, PC builds | slow backend; no struck-through prices |
| `ananas` | Ananas | Marketplace with hundreds of sellers across most departments, incl. home & garden, toys, beauty, fashion | seller, per-listing delivery fee, EAN |

Two of these are not independent shops for pricing purposes. **Ananas** is a
marketplace where several sellers are shops covered here (Neksio, Neptun,
Setec, Setra, Tehnomarket, and Anhoch as PC MARKET). **ZirafaMall** mirrors
most of Gjirafa50's catalogue at identical prices. `mkshop.py` marks those
listings with `mirror_of` within a run; `group` (step 5) does it across
saved runs. Count a mirrored offer once, under the shop that actually sells
it, unless the mirror is genuinely cheaper. That applies everywhere you
mention availability, including fallbacks and side notes.

## Workflow

The order matters: each step narrows the next. A keyword search in one or two
shops is not a market overview.

### 1. Decide which shops matter

Not every shop sells every department. Use the table above, or
`mkshop.py stores`, to pick the relevant set. Keep track of which shops you
covered and which couldn't be checked; the answer will need to say so in a
line.

If the user limits the question to one shop, answer for that shop, using its
client's extras where they help (e.g. `python3 scripts/stores/setec.py stores
<url>` for per-store stock, `setec.py gaps` for blank attributes; see "Reference"
below). For "is this a good price", still `match` the product across the other
shops: a struck-through price is not a deal signal.

### 2. Sweep with cross-store search

```bash
python3 scripts/mkshop.py search "<brand> <model>" --q "<model code>" --q "<Macedonian product noun>" \
  --max-price <budget> --json <out>/sweep.json
```

This runs every relevant shop's own search in parallel (one worker per shop,
sequential inside each shop) and merges the results. Each `--q` adds a
round, so an all-shop search takes tens of seconds per phrasing.

Search engines differ between shops: title vs description matching, silent
OR-fallback, Cyrillic vs Latin spellings. Each shop's reference describes
its own. So sweep with several phrasings: English and Macedonian, brand +
model code, the generic product noun. Don't end a query on a glue word
(`за`, `со`, `for`, `and`): some shops match the last word as a prefix, so
`laptop and` can find nothing.

Useful flags:

- `--strict` keeps only hits containing every query word. It is
  transliteration-aware.
- `--hide-mirrors` drops marketplace duplicates of shops you also searched.
- `--in-stock` drops items marked out of stock but keeps unknown /
  ask-for-stock items; add `--strict-stock` to drop those too.
- `--stores a,b` / `--exclude c` limit the sweep to the shops from step 1.

A shop that hits `--limit-per-store` is flagged "more may exist".

The table prints even when you save `--json` (`--no-table` turns it off),
so read it there; to look at a saved run again, run `group` on the file
rather than printing the file.

Read the per-shop status table and footer before trusting a zero: only `ok`
with zero results is a real zero (see "When a shop fails"). Read them before
trusting a hit count too. `all-words` counts the hits that contain every
word of a query that found them (what `--strict` keeps). A low count can
mean loose matching, but also another language or titles that are only a
model code, so read a few titles before saying a shop carries the product.
`mostly accessories` means most of a shop's hits are cases, glass,
batteries or mounts.

Search is for recall. Membership comes from step 3.

### 3. Walk the categories

```bash
python3 scripts/mkshop.py categories --grep '<macedonian stem>|<english stem>'   # every shop's matching categories
python3 scripts/mkshop.py list --store <key> <category-id-or-url> --json <out>/<key>_list.json
python3 scripts/mkshop.py facets --store <key> <category>                          # where the shop has structured attributes
python3 scripts/mkshop.py list --store <key> <category> --filter '<token from facets>' --json <out>/filtered.json
```

Category handles differ per shop (numeric ids, slugs, URLs, transliterated
Cyrillic). Never guess them: grep for them, and try both a Macedonian stem
and an English one. The table shows each shop's 20 biggest matches (`--show
0` for all, `--urls` adds links). `list` walks every page, so a category
listing is the complete set; a search never is. Where a shop's facets
(structured attributes) are well filled, they decide membership better than
title words. Where they are sparse (the shop's reference says so), use them
to confirm and recover the rest from titles and `detail` specs. Read the
facet values first, because filter tokens must match exactly.

### 4. Check what the structure would miss

A structured filter only matches products whose value is filled in. Shops
leave attributes blank, mislabel them, or file products in sibling
categories, and listing titles often omit the very spec you're filtering on.
Before trusting a filter:

- look at what the category contains that the filter dropped, and recover
  products from their title or `detail` specs text;
- check sibling and parent categories;
- run a widening search for the product noun, then
  `group <out>/sweep.json <out>/<key>_list.json ...`: its FOUND ONLY BY
  SEARCH section lists what the search found in a shop you walked that none
  of your walks holds.

Report what a filter dropped for lack of data and what you recovered.
Silence reads as "nothing was missed", which is the one claim you can't make.

### 5. Recognise the same product across shops

```bash
python3 scripts/mkshop.py match "<product URL from any covered shop>"
python3 scripts/mkshop.py match <EAN>
python3 scripts/mkshop.py match "<brand> <model>"
python3 scripts/mkshop.py group <out>/sweep.json <out>/<key>_list.json ...   # one row per product across saved runs
```

`match` resolves the source product, then searches every shop by EAN, by
model code / MPN, by model phrase, and by cleaned title (30–60 s across all
shops). It labels each same-product candidate:

- `exact`: same EAN;
- `model`: same model code / MPN;
- `likely`: strong title overlap, same brand. Treat it as a lead to confirm,
  not a match.

Rows whose colour, capacity, size, tier, version, part number or EAN
differs from the reference go to a separate VARIANTS group with the
differing tokens shown. Editions named only in words ("for Mac",
"Business", bundles) can still land in the same-product group, so read the
titles. Accessories are usually recognised and kept out; treat a row
priced far below the rest as suspect. Configurations (CPU, RAM, storage)
are not always detected as variants either. A reference that leaves
a variant open prints its same-product offers in sub-groups per variant
value, so prices compare like for like. `--near` also prints rejected
near-misses with the reason, which is useful when a shop seems not to have
the product but may list it under another code or name. When the
reference is known by several names (a marketing name and a model code),
`--also "<other name>"` adds queries.

To turn several saved `search` / `list` runs into a shortlist, use `group`
rather than merging rows by hand. It works offline: no shop is contacted.
It unions the files and re-detects mirrors across them. It groups by EAN,
then bridges shops without EANs through model codes, part numbers and their
aliases. Each product gets one row, with its offers sorted by effective
price. Listings it cannot join safely stay separate and are shown as
RELATED; decide those by reading the titles or running `match`. Joins made
without a shared EAN (by codes or marketplace SKU) are heuristics, so read
those titles too, and question a product whose best price sits far below
its other offers. Compatible toner and ink print the original's part
number: `group` keeps them apart when the shop names another maker or
volume, but a title that names neither ("Компатибилен кертриџ HP W1500A")
can still land with the original, so check who makes each consumable.
Shops often name one product with different code systems (a marketing
model code in one, a vendor part number in another). Before calling two
rows different models, check their EANs or run `match`, and read the
titles of anything grouped by codes alone: regional editions, layouts and
bundles often show only there.

### 6. Get detail on the shortlist

```bash
python3 scripts/mkshop.py detail <url> <url> ... --json <out>/detail.json
```

Only for the handful you'll actually recommend. URLs route to the right shop
automatically. Detail adds warranty, EAN, specs text and per-store stock
where the shop exposes them, plus shop-specific extras (delivery quotes,
seller info, alternative supplier offers, variant prices). Use it to verify
every spec you state about a recommended product.

## Filtering discipline

Most bad results here come from over-eager exclusion, not missing data:

- Titles routinely leave out the product noun or the key spec.
- Number formats vary: bare integers, missing units, years in titles.
- Machine-translated titles and specs can garble words, values and units.
- Shop attributes are good, but they are not infallible.

So:

**Let category or attribute membership settle whether something belongs.**
Apply exclusion patterns only to items you could not confirm structurally.

**Parse numbers permissively.** Accept bare integers. Exclude plausible
years. Sanity-bound the range.

**Sanity-check your own filter.** Compare what you kept against the category
count. When a large set shrinks sharply, look at a sample of what was
dropped before reporting.

## Reading prices

`price_mkd` is what an ordinary online buyer pays today, VAT included,
before delivery fees and without any membership. `regular_price_mkd` is the
struck-through figure, when a shop shows one. Shops differ widely in what
that figure means:

| shop | `price_mkd` is | a struck-through price means | also watch |
|---|---|---|---|
| Setec | the club / web price every online buyer pays | "Редовна цена", shown on almost everything; not a deal signal by itself | delivery fee shown only at checkout |
| Neptun | the price without the loyalty card | nothing; Neptun strikes nothing through | `member_price_mkd` = haPPy card price, often much lower; the card is a one-off 150 MKD online (299 in store) |
| Tehnomarket | the "SMART" price, charged to anonymous online buyers | an anchor price on most products | delivery 199 MKD |
| Anhoch | the selling price | rare, genuine markdowns | delivery 230 MKD up to 5,000, 179 MKD above; pickup free |
| Neksio | "Цена со ДДВ" | rare, genuine temporary markdowns | |
| DDStore | the final price | a named, genuine promotion | `price_mkd` null = "ask for price"; one page can bundle several supplier offers (`detail` lists them); delivery 150 MKD, free over 3,000 |
| Hivetec | the current price | a permanent list price on nearly all products | free delivery over 4,500 MKD |
| Gjirafa50 / ZirafaMall | the displayed price | shown on most products; not a deal signal | prices can move during the day |
| Setra | the current price | (unused) | delivery from 150 MKD |
| Ananas | the price the listing's seller charges | a seller-set reference | `shipping_mkd` per listing for non-member buyers |

**Member prices.** When a shop offers a lower price for a cheap, easily
obtained loyalty card or membership, treat that as the shop's effective
price:

- lead with it, and in the same breath give the condition, its one-off cost
  and the price without it;
- then compare it against the other shops.

`mkshop.py` does this by default. It ranks and filters on
`effective_price_mkd`, marks member prices with `*`, carries
`price_condition`, and keeps the non-member price visible. Member prices can
move a row across a price range's edges; `--no-member-prices` shows the
card-less view.

**Judging a deal.** The strongest evidence that something is underpriced is
the same product (same EAN or model) costing clearly more elsewhere. That
means the other covered shops, and, if you have web tools, a reference price
outside Macedonia, clearly labelled as such. A struck-through figure is not
that evidence. Give the user this price context whenever they ask about
value. Explain a shop's pricing mechanics (what its struck-through figure or
membership means) only when it changes the conclusion or the user asks.

**Time-limited prices.** A price can be a short campaign. When a record
carries an end date (`price_valid_until`, `member_price_valid_until`), state
it next to the price, and judge value on the price the buyer can still get.
`mkshop.py` tables mark only ends within a week; `detail` shows every known
window.

- A `null` value means the shop shows the price as standing.
- A missing field means the shop doesn't say. Several shops never expose end
  dates, and some only do in `detail`, so check `detail` on a shortlisted
  sale price before calling it a lasting deal.

Rules of thumb:

- Compare like with like. Add delivery fees when they differ, and normalise
  bundles and multi-packs.
- A price far below every other shop's for the same EAN is either a real
  deal or a listing error (wrong variant, refurbished, regional edition).
  Check `detail` before presenting it.
- Quote prices in ден / MKD; add a rough EUR figure for expensive items (~61.5 ден per EUR).

## Reading stock and delivery

"In stock" means different things in different shops, and buyers often care
more about *when* than *where*:

| shop | `in_stock` true means | how much / where | when |
|---|---|---|---|
| Setec | units > 0 somewhere (1–3 units = an order request Setec confirms by phone) | exact units; per-store units in `detail`, often just 1 | warehouse-only stock = order online, no pickup |
| Neptun | listings only contain orderable items; in `detail`, orderable online or held by a store | per-store yes/no in `detail` | up to 7 working days (300 MKD) or store pickup |
| Tehnomarket | orderable online | per-store yes/no for every location, in every listing | 2–7 working days, or store pickup |
| Anhoch | the online flag, which can flip within minutes | quantity capped at 10; per-store yes/no in `detail`; "Главен магацин" = warehouse, not a store | pickup in any store free |
| Neksio | quantity > 0 | exact quantity, one web figure | up to 72 h (working days) after confirmation; no online checkout, orders go by inquiry form or phone |
| DDStore | "1-3 дена" (at the distributor), "Во ДДСтор магацин" (own warehouse, same day) and a few low-stock labels. `null` = "Прашај за залиха" (unconfirmed). `false` includes "По нарачка" (multi-week supplier order) | no quantities | "1-3 дена" ≈ 2–8 working days door to door (distributor, then courier); every order is confirmed before payment |
| Hivetec | the in-stock flag; out-of-stock items stay listed | no quantities | 72 h after confirmation |
| Gjirafa50 / ZirafaMall | **orderable, not local stock** | local stock only with the "48h" badge (`--filter local-stock`) | supplier orders typically take 3–4+ weeks; `detail` gives the quote |
| Setra | the in-stock flag; `null` = backorder | no quantities | up to 4 working days |
| Ananas | seller-reported units > 0 (200 is a placeholder default on the own shop) | no per-store data | typically 3–6 days |

Match availability to the user's timeline. If they need it soon, the main
picks should be offers they can actually get in time, with the delivery or
pickup expectation stated per offer. Slower but cheaper options belong in a
clearly separate group. A single unit in one store is fragile: one
reservation empties it, so say so when recommending a trip.

## When a shop fails

`mkshop.py` never drops a shop silently. Each shop gets a status:

- `ok`: answered, possibly with zero results.
- `partial`: some of its queries failed, or a `list` timed out midway; what
  did come back is in the results.
- `not_found`: the category, product or URL does not exist at that shop.
- `blocked`: a CAPTCHA, Cloudflare challenge, WAF block or login wall;
  usually transient.
- `error` / `timeout`: failed or too slow.

Retry a failed shop once on its own (`--stores <key>` for search/match,
`--store <key>` for list/facets). For a timeout, raise `--timeout` or narrow
the listing (`--filter`, `--limit`); very large categories take minutes. If
every shop errors at once, check that `requests` and `bs4` are installed.
If a shop still fails, say in the answer that it could not be checked. The
per-shop reference files have troubleshooting notes.

## Judgement and caveats

The catalogues tell you price, stock and specs. They can't tell you that a
brand has patchy quality control, that a line was superseded, or that a
bargain is cheap for a reason. That judgement is often the most valuable
part of the answer, so offer it, as your own read, clearly separated from
what the data states.

**Warn at the level the warning applies.** That can be a brand or product
line (track record, service network), a characteristic that makes an option
inferior (a missing feature, an older generation, a spec that looks better
than it performs), or one specific listing (a variant trap, a supplier order
presented as stock, a shorter warranty). Group options that share a reason
rather than repeating it per item. Where a warning implies an action, give
it: test on arrival, keep the invoice, ask the shop to confirm. If nothing
warrants a warning, say nothing. Manufactured caveats train readers to
ignore real ones.

**Compare alternatives by what differs.** When weighing models, generations
or siblings, lead with the differences that change the decision, including
easy-to-miss ones (software/update support, warranty, connectivity, energy
use). For each difference, say which side it favours, and explain any
notable price gap between near-identical options. State specs from the
shops' structured data, `detail` text, or a quick authoritative lookup. Mark
anything from memory as such.

**Ground claims cheaply.** If you have web search tools, one quick lookup to
ground a reliability or spec claim is worth it. Never present a brand-quality
claim as if the catalogue supported it.

## Output

For comparisons and shortlists, produce **two** things: your reply in the
conversation, and a standalone HTML report saved alongside any data files.
For a single-product or availability question, a reply with a small table is
the right size; a report there would add a file where a sentence was wanted.

Both must carry the substance. Someone reading only the conversation should
get the recommendation, prices, shops, links, availability and the caveats
that matter. The HTML exists because many products across several shops are
easier to scan rendered than as markdown.

### Principles for both

- **Lead with the answer.** The first line should be accurate and neutral. If
  the best price depends on a condition (a membership, a delivery wait, a
  bundle), state that price with its condition up front rather than leading
  with a verdict that ignores it.
- **Calibrate to the user.** Read their expertise and priorities from how
  they ask. Explain what they likely don't know, skip what they clearly do,
  and include what matters on first read. They can always ask a follow-up.
- **Order by relevance.** Picks first. Then close alternatives worth
  considering. Then the notes and caveats that bear on the decision. The
  rest of the qualifying options go last: complete in the report, and in the
  reply as compact as the question allows.
- **Make the ranking legible.** Say why each pick wins. For the
  alternatives, add only what helps the reader place them for this query (a
  trade-off, when one would be the better choice, a caveat), as a notes
  column or a short grouped line. Any heading that sets items apart says
  what sets them apart.
- **One product per pick or row.** Never merge different models into one
  row, and never give one price or price range for several variants
  (colour, capacity, edition). Variants get their own rows. Variants with
  identical price and stock may share a row only if the row names each one.
- **Link every offer.** Every product you name with a price gets a link to
  its product page, in tables, caveats and side notes alike, in the reply
  and the report. Use the shop name as the link text.
- **Make it scannable.** Use one point per bullet, tables or lists instead
  of long run-on sentences, and model codes in `code` formatting or their
  own column rather than mixed into prose.
- **Point out cheaper routes** briefly when one would materially change the
  outcome: an older or newer model, waiting for a supplier order, buying
  elsewhere. Phrase it as a pointer the user can
  follow up on.
- **Report coverage in one line.** Say which shops you checked and any that
  couldn't be checked, and name the scope you actually walked (e.g. "these
  categories in all N shops; skipped: …"). Mention absences only where they
  inform the decision: a department a relevant shop doesn't sell, or a
  product nobody carries. Don't list every shop that came up empty.
- **Be exact.**
  - Name the shop with every price, including prices in side notes.
  - Say precisely how much of a category you covered when it was less than
    all of it.
  - Double-check any figure you compute.
  - Separate what the data states from your judgement.
  - A missing field means the shop doesn't publish it, not that the thing
    is absent. Say "not listed" and suggest asking.
- **Don't silently drop a better offer for being imperfect.** If an
  out-of-stock, ask-for-stock or supplier-order offer would beat your picks
  on price or spec, show it, flagged, in a separate group, and say what role
  it plays: a better deal if it comes through, or a fallback if a pick sells
  out. Otherwise leave it out.

### The HTML report

Start from `assets/report_template.html`: one accent colour, a system font
stack, light and dark via `prefers-color-scheme`.

- **Structure:** follow the order above. `.pick` cards for the
  recommendations, each opening with the product, offer and price; the
  "best if…" condition follows in the card's subdued `.when` line. Then a
  comparison table with a notes column, the caveats, and finally the
  remaining qualifying options. Drop or adapt any part that doesn't serve
  the query.
- **Columns:** adapt them to the product type, showing the specs that drive
  the decision.
- **Patterns:** keep the visual language. Tables compare products down
  columns; the `kv` list holds facts about one thing. If you're writing CSS
  to undo the template, you picked the wrong pattern. No gradients, shadows,
  animation or icons; right-align numbers with tabular figures.

Write it into the task folder (`<out>/report.html`) and tell the user the path.

### The reply

A shape that usually works:

```markdown
**Pick: <model> at <price> from [Shop](url).** One or two sentences on why it wins.

| Model | key spec | Best offer | Price | Availability |
|---|---|---|---|---|
| ... | ... | [Shop](url) | X ден (condition, if any) | in stock, 3 stores / supplier order ~3 wks |

## Also worth considering   (Model | Best offer | Price | Notes)
## Worth knowing            (one point per bullet)
## Other qualifying options (compact)
```

Drop sections that have nothing in them, and adapt the shape to the
question. A single-product answer may be one table and a verdict.

## Reference

- `references/<store>.md`: per-shop details (endpoints, category model,
  price/stock semantics, data traps, bot protection, troubleshooting). Read
  the relevant ones before relying on a shop's numbers for a recommendation,
  or when a shop errors. `gjirafa.md` covers both Gjirafa50 and ZirafaMall.
- Flags and commands in a shop reference that `mkshop.py` lacks
  (`--category`, `--counts`, `--deep`, `--phrase`, Setec's `gaps` for blank
  attributes, ...) belong to that shop's client: run
  `python3 scripts/stores/<key>.py <command> ...` (Gjirafa sites:
  `gjirafa.py --site gjirafa50|zirafamall <command> ...`).
- `references/client-contract.md`: the interface every store client
  implements. Read it before fixing a client or adding a shop.
