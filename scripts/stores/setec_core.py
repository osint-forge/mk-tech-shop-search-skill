#!/usr/bin/env python3
"""
setec.py - query setec.mk's product catalogue.

Two public backends power the site; this wraps both.

  1. Meilisearch  https://search.sp.solslab.dev/indexes/products/search
     Structured catalogue: titles, brands, categories, per-product attributes,
     prices, total stock. Fast, filterable, facetable.

  2. Product detail  https://setec.mk/api/medusa/products-with-details-web?handle=<slug>
     Per-store inventory, warranty in months, EAN, and internal price floors.

No dependencies beyond the standard library.

Commands
  categories  list category handles (facet counts) - find the right one first
  attrs       list attribute Name::Value pairs, optionally within a category
  brands      list brands, optionally within a category
  search      query/filter products
  detail      full detail for one product (price floors, warranty, EAN)
  stores      per-store stock for one or more products

Run any command with -h for its flags.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SEARCH_URL = "https://search.sp.solslab.dev/indexes/products/search"
# Public client-side search key, shipped in the site's own JS bundle to every visitor.
SEARCH_KEY = "c0424dab588b8cbbbe0a4809fc10b5f1c0c7d183b5b28ebe799f3fbf583ab358"
DETAIL_URL = "https://setec.mk/api/medusa/products-with-details-web?handle={}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Both backends normally answer in well under a second; the generous timeout
# only matters when a host is cold or struggling, and beats hanging forever.
TIMEOUT = 45
# Detail fetches are I/O-bound; 6 in flight keeps a shortlist fast without
# hammering the storefront API.
DETAIL_WORKERS = 6
# The central warehouse ships online orders but cannot be visited; keep it out
# of store counts so "N stores" always means places a buyer can walk into.
WAREHOUSE = "Главен Магацин"

# Meilisearch refuses to return more than this many hits for one query, no matter
# the limit you pass. Facet counts are NOT subject to it, so use facets for totals
# and chunk by category/brand when you need to enumerate more than this.
MAX_TOTAL_HITS = 1000


def _post(body, retries=3):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SEARCH_URL, data=data,
        headers={"Authorization": "Bearer " + SEARCH_KEY,
                 "Content-Type": "application/json", "User-Agent": UA})
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                # Deterministic rejection; the body says what's wrong
                # (e.g. which field isn't filterable), so surface it.
                detail = e.read().decode(errors="replace")
                try:
                    detail = json.loads(detail).get("message", detail)
                except ValueError:
                    pass
                raise SystemExit(f"search request rejected ({e.code}): {detail}")
            last = e
        except Exception as e:  # noqa: BLE001 - transient network/5xx
            last = e
    raise SystemExit(f"search request failed: {last}")


def _get_detail(handle, retries=3):
    """Returns (product, None) or (None, reason-it-failed)."""
    url = DETAIL_URL.format(urllib.parse.quote(handle))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    err = "unrecognised response shape"
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
            p = d.get("product") if isinstance(d, dict) and "product" in d else d
            if isinstance(p, list):
                p = p[0] if p else None
            if p and "variants" in p:
                return p, None
            err = "response carries no product - handle probably wrong"
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}" + (" - no such handle" if e.code == 404 else "")
            if e.code < 500:
                break
        except json.JSONDecodeError:
            err = "non-JSON response (blocked, or bad User-Agent?)"
        except Exception as e:  # noqa: BLE001 - transient network
            err = str(e)
    return None, err


# --- transliteration -------------------------------------------------------
# Category handles encode non-ASCII and punctuation as -<hex> escapes, so
# "Напојувања" becomes "napo-d1-98uvanja-29" (-d1-98 is ј, trailing -29 is the
# category id). Decoding is lossy but good enough to eyeball the right handle.
_SUBS = [("-d1-98", "j"), ("-d1-9f", "dz"), ("-d1-9c", "kj"), ("-d1-9b", "gj"),
         ("-20", " "), ("-2f", "/"), ("-26", "&"), ("-2c", ","), ("-2b", "+")]


def decode_handle(h):
    s = h
    for a, b in _SUBS:
        s = s.replace(a, b)
    return s


# --- price / stock helpers -------------------------------------------------
def price_of(hit):
    """Club price (what the customer actually pays) and the regular price.

    calculated_amount is the club/web price the site headlines; original_amount
    is the struck-through 'Редовна цена'. Treat calculated_amount as the real one.
    """
    try:
        cp = hit["variants"][0]["calculated_price"]
        return cp.get("calculated_amount"), cp.get("original_amount")
    except Exception:  # noqa: BLE001
        return None, None


def attrs_of(hit):
    return {a["name"]: a["value"]
            for a in (hit.get("attributes") or []) if a.get("value")}


def stores_of(product):
    """{store name: available units} from a detail-API product."""
    per = {}
    for v in product.get("variants") or []:
        for inv in v.get("inventory") or []:
            for lvl in inv.get("location_levels") or []:
                loc = (lvl.get("stock_locations") or [{}])[0]
                chans = loc.get("sales_channels") or [{}]
                name = chans[0].get("name")
                q = lvl.get("available_quantity") or 0
                if name and q:
                    per[name] = per.get(name, 0) + q
    return per


# --- commands --------------------------------------------------------------
def cmd_categories(a):
    body = {"q": "", "limit": 0, "facets": ["product_categories.handle"]}
    if a.filter:
        body["filter"] = a.filter
    d = _post(body)
    fd = d["facetDistribution"]["product_categories.handle"]
    rows = sorted(fd.items(), key=lambda x: -x[1])
    pat = re.compile(a.grep, re.I) if a.grep else None
    shown = 0
    for h, n in rows:
        dec = decode_handle(h)
        if pat and not (pat.search(h) or pat.search(dec)):
            continue
        print(f"{n:6}  {h:52}  {dec}")
        shown += 1
        if a.limit and shown >= a.limit:
            break
    sys.stdout.flush()
    print(f"\n-- {shown} shown of {len(rows)} categories", file=sys.stderr)


def cmd_attrs(a):
    body = {"q": "", "limit": 0, "facets": ["attribute_pairs"]}
    f = list(a.filter)
    if a.category:
        f.append(f'product_categories.handle = "{a.category}"')
    if f:
        body["filter"] = f
    d = _post(body)
    fd = d["facetDistribution"]["attribute_pairs"]
    pat = re.compile(a.grep, re.I) if a.grep else None
    rows = sorted(fd.items())
    shown = 0
    for k, n in rows:
        if pat and not pat.search(k):
            continue
        if a.names_only:
            continue
        print(f"{n:6}  {k}")
        shown += 1
    if a.names_only:
        names = {}
        for k, n in rows:
            nm = k.split("::")[0]
            names[nm] = names.get(nm, 0) + n
        for nm, n in sorted(names.items(), key=lambda x: -x[1]):
            if pat and not pat.search(nm):
                continue
            print(f"{n:6}  {nm}")
            shown += 1
    sys.stdout.flush()
    print(f"\n-- {shown} shown; total products matched: "
          f"{d.get('estimatedTotalHits')}", file=sys.stderr)


def cmd_brands(a):
    body = {"q": "", "limit": 0, "facets": ["brand_name"]}
    f = list(a.filter)
    if a.category:
        f.append(f'product_categories.handle = "{a.category}"')
    if f:
        body["filter"] = f
    d = _post(body)
    for k, n in sorted(d["facetDistribution"]["brand_name"].items(),
                       key=lambda x: -x[1]):
        print(f"{n:6}  {k}")
    sys.stdout.flush()
    print(f"\n-- products matched: {d.get('estimatedTotalHits')}",
          file=sys.stderr)


def _collect(q, filters, limit):
    body = {"q": q, "limit": min(limit, MAX_TOTAL_HITS)}
    if filters:
        body["filter"] = filters
    d = _post(body)
    return d.get("hits", []), d.get("estimatedTotalHits") or 0


def cmd_search(a):
    filters = list(a.filter)
    if a.category:
        filters.append(f'product_categories.handle = "{a.category}"')
    if a.in_stock:
        filters.append("total_web_quantity > 0")

    hits = {}
    lim = min(a.limit, MAX_TOTAL_HITS)
    truncated = []
    queries = a.q or [""]
    for q in queries:
        batch, est = _collect(q, filters, a.limit)
        if len(batch) >= lim:
            truncated.append((q, est))
        for h in batch:
            hits[h["id"]] = h

    rows = []
    for h in hits.values():
        club, orig = price_of(h)
        row = {
            "title": h.get("title", ""),
            "handle": h.get("handle", ""),
            "brand": h.get("brand_name"),
            "sku": h.get("external_id"),
            "club": club, "regular": orig,
            "qty": h.get("total_web_quantity"),
            "available": h.get("is_web_available"),
            "attrs": attrs_of(h),
            "url": "https://setec.mk/products/" + (h.get("handle") or ""),
        }
        if a.desc:
            row["description"] = h.get("description")
        rows.append(row)
    rows.sort(key=lambda r: (r["club"] is None, r["club"] or 0))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"wrote {len(rows)} products -> {a.json}", file=sys.stderr)

    for r in rows:
        disc = ""
        if r["regular"] and r["club"] and r["regular"] != r["club"]:
            disc = f" (was {r['regular']:,})"
        print(f"{(r['club'] or 0):9,} ден{disc:14} qty={str(r['qty']):>4}  "
              f"{r['title'][:66]}")
    sys.stdout.flush()
    print(f"\n-- {len(rows)} products", file=sys.stderr)
    for q, est in truncated:
        n = f"{MAX_TOTAL_HITS}+ (index ceiling)" if est >= MAX_TOTAL_HITS else f"~{est}"
        label = f"--q '{q}'" if q else "the filter-only query"
        print(f"!! {label} matched {n} products but returned only {lim}; "
              "raise --limit, narrow with --category/--filter, or chunk by brand.",
              file=sys.stderr)


def _detail_row(handle):
    p, err = _get_detail(handle)
    if not p:
        return {"handle": handle, "error": err}
    ed = p.get("product_extra_details") or {}
    v = (p.get("variants") or [{}])[0]
    cp = v.get("calculated_price") or {}
    reserved = sum((l.get("reserved_quantity") or 0)
                   for inv in (v.get("inventory") or [])
                   for l in (inv.get("location_levels") or []))
    return {
        "handle": handle,
        "title": p.get("title"),
        "description": p.get("description"),
        "sku": p.get("external_id"),
        "club": cp.get("calculated_amount"),
        "regular": cp.get("original_amount"),
        "rrp": ed.get("recommended_retail_price"),
        "min_web": ed.get("min_web_price_with_vat"),
        "min_retail": ed.get("min_retail_price_with_vat"),
        "min_wholesale": ed.get("min_wholesale_price_with_vat"),
        "warranty_months": ed.get("output_warranty"),
        "ean": ed.get("catalogue_number"),
        "tax_pct": ed.get("tax_percentage"),
        "reserved": reserved,
        "stores": stores_of(p),
        "attrs": {a["name"]: a["value"]
                  for a in (p.get("attributes") or []) if a.get("value")},
        "url": "https://setec.mk/products/" + handle,
    }


def _handles(args):
    out = []
    for h in args:
        h = h.strip().rstrip("/")
        if "/products/" in h:
            h = h.rsplit("/products/", 1)[1]
        out.append(h)
    return out


def cmd_gaps(a):
    """Products in a category that carry NO value for an attribute.

    A value filter like `attribute_pairs = "Refresh Rate::144 Hz"` can only match
    products that have the attribute populated. Anything with a blank or absent
    value vanishes from the result with no error, so this reports that blind spot
    explicitly: you recover those from title/description rather than losing them.
    """
    cat = f'product_categories.handle = "{a.category}"'
    # estimatedTotalHits saturates at MAX_TOTAL_HITS; facet counts don't,
    # so the facet gives the true category size.
    fd = _post({"q": "", "limit": 0, "filter": [cat],
                "facets": ["product_categories.handle"]})["facetDistribution"]
    n_total = fd.get("product_categories.handle", {}).get(a.category, 0)

    # Note: filtering on `attributes.name` is NOT a reliable coverage test -
    # products carry the attribute name with an empty string as a schema
    # placeholder, so that filter counts them as present. Only an actual scan of
    # the values tells you the truth, which is what happens below.
    hits = _post({"q": "", "limit": min(a.limit, MAX_TOTAL_HITS),
                  "filter": [cat]}).get("hits", [])
    missing = []
    for h in hits:
        v = attrs_of(h).get(a.attr)
        if not v:
            missing.append(h)
    n_blank = len(missing)
    if a.in_stock:
        missing = [h for h in missing if (h.get("total_web_quantity") or 0) > 0]

    scanned = len(hits)
    if scanned < n_total:
        print(f"!! scanned only {scanned} of {n_total} products (the index "
              f"returns at most {min(a.limit, MAX_TOTAL_HITS)} per query); "
              "coverage is partial - chunk by brand or price band for the rest",
              file=sys.stderr)
    print(f"category holds {n_total} products; scanned {scanned}; "
          f'{scanned - n_blank} carry a usable "{a.attr}" value')
    print(f"{len(missing)} product(s) below have it blank or absent - a filter "
          f'on "{a.attr}" drops these silently:\n')
    for h in sorted(missing, key=lambda x: (price_of(x)[0] or 0)):
        club, _ = price_of(h)
        print(f"{(club or 0):9,} ден  qty={str(h.get('total_web_quantity')):>4}  "
              f"{h.get('title', '')[:64]}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump([{"title": h.get("title"), "handle": h.get("handle"),
                        "club": price_of(h)[0],
                        "qty": h.get("total_web_quantity"),
                        "description": h.get("description"),
                        "attrs": attrs_of(h)} for h in missing],
                      fh, ensure_ascii=False, indent=1)
        sys.stdout.flush()
        print(f"wrote -> {a.json}", file=sys.stderr)


def cmd_detail(a):
    hs = _handles(a.handles)
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        rows = list(ex.map(_detail_row, hs))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"wrote {len(rows)} -> {a.json}", file=sys.stderr)
    for r in rows:
        if r.get("error"):
            print(f"!! {r['handle']}: {r['error']}")
            continue
        print(f"\n{r['title']}")
        d = r.get("description")
        if d:
            print(f"  {d[:140]}{'...' if len(d) > 140 else ''}")
        print(f"  club {r['club'] or 0:,}  regular {r['regular'] or 0:,}  "
              f"RRP {r['rrp'] or 0:,}")
        print(f"  floors: web {r['min_web'] or 0:,} | retail "
              f"{r['min_retail'] or 0:,} | wholesale {r['min_wholesale'] or 0:,}")
        if r["min_web"] and r["club"] and r["club"] < r["min_web"]:
            print(f"  ** priced {r['min_web'] - r['club']:,} ден BELOW its own "
                  "min web price - unusually aggressive")
        print(f"  warranty {r['warranty_months']} mo | EAN {r['ean']} | "
              f"SKU {r['sku']} | reserved {r['reserved']}")
        per = r["stores"]
        tot = sum(per.values())
        n_stores = len(per) - (1 if WAREHOUSE in per else 0)
        unit = "store" if n_stores == 1 else "stores"
        if WAREHOUSE in per:
            where = "warehouse only" if not n_stores else f"{n_stores} {unit} + warehouse"
        else:
            where = f"{n_stores} {unit}"
        print(f"  stock {tot} across {where}")
        for s, q in sorted(per.items(), key=lambda x: -x[1]):
            print(f"      {q:4} x {s}")


def cmd_stores(a):
    hs = _handles(a.handles)
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        rows = list(ex.map(_detail_row, hs))
    agg = {}
    for r in rows:
        for s, q in (r.get("stores") or {}).items():
            agg.setdefault(s, []).append((r["title"], q))
    for s, items in sorted(agg.items(), key=lambda x: -len(x[1])):
        tot = sum(q for _, q in items)
        print(f"{s:26} {len(items):2} models, {tot:3} units")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote -> {a.json}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("categories", help="list category handles")
    p.add_argument("--grep")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--filter", action="append", default=[])
    p.set_defaults(fn=cmd_categories)

    p = sub.add_parser("attrs", help="list attribute Name::Value pairs")
    p.add_argument("--category")
    p.add_argument("--grep")
    p.add_argument("--names-only", action="store_true")
    p.add_argument("--filter", action="append", default=[])
    p.set_defaults(fn=cmd_attrs)

    p = sub.add_parser("brands", help="list brands")
    p.add_argument("--category")
    p.add_argument("--filter", action="append", default=[])
    p.set_defaults(fn=cmd_brands)

    p = sub.add_parser("search", help="query/filter products")
    p.add_argument("--q", action="append",
                   help="free-text query; repeat to union several sweeps")
    p.add_argument("--category")
    p.add_argument("--filter", action="append", default=[],
                   help='Meilisearch filter, e.g. \'attribute_pairs = "Тип на меморија::DDR4"\'')
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--desc", action="store_true",
                   help="include descriptions in --json rows (for parsing "
                        "specs from text); off by default to keep JSON lean")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--json")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("gaps", help="products missing a value for an attribute")
    p.add_argument("--category", required=True)
    p.add_argument("--attr", required=True,
                   help='attribute name, e.g. "Refresh Rate"')
    p.add_argument("--in-stock", action="store_true")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--json")
    p.set_defaults(fn=cmd_gaps)

    p = sub.add_parser("detail", help="full detail incl. price floors + stores")
    p.add_argument("handles", nargs="+", help="product handle(s) or URL(s)")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_detail)

    p = sub.add_parser("stores", help="per-store rollup across products")
    p.add_argument("handles", nargs="+")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_stores)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
