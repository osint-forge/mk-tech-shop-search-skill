"""Fake store client implementing references/client-contract.md over a local
JSON catalogue (data/<key>.json). For testing mkshop.py only.

Env switches (comma lists of store keys, optional :N):
  FAKE_BLOCK=anhoch         exit 3 with a BLOCKED: line
  FAKE_SLOW=setra:30        sleep N seconds per command
  FAKE_ERROR=hivetec        crash with a traceback (exit 1)
  FAKE_BADJSON=ddstore      exit 0 but write broken JSON
  FAKE_NOJSON=ddstore       exit 0 and write nothing
  FAKE_NOISE=ddstore:N      print N bytes to stdout and stderr
  FAKE_BIG=ddstore:N        search returns N synthetic records
  FAKE_WARN=anhoch          print an OR-fallback warning on search
  FAKE_FAIL_QUERY=neptun:foo  exit 1 when the query contains foo
  FAKE_LOG=path             append "store pid start end argv" per run
  FAKE_MEMBER=neptun:1030=18490;1032=21490  add member_price_mkd to those ids
  FAKE_MEMBER_COND=text     ...and member_price_condition = text
  FAKE_VALID=neptun:1030=P/M;1031=P   price windows on those ids: P -> price_valid_until,
                            M -> member_price_valid_until; an ISO string, 'null' (standing)
                            or '-' (key absent)
  FAKE_VALID_DETAIL=...     the same, applied in detail only (Neptun's haPPy window)
"""
import argparse, json, os, re, sys, time, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

CAPS = {
    "setec": ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing",
              "ean_in_detail", "stock_qty", "per_location_stock", "warranty"],
    "anhoch": ["search", "categories", "list", "detail", "stock_qty", "per_location_stock", "warranty"],
    "neksio": ["search", "categories", "list", "detail", "ean_in_detail", "stock_qty", "warranty"],
    "ddstore": ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing", "ean_in_detail", "warranty"],
    "neptun": ["search", "categories", "list", "detail", "ean_in_listing", "ean_in_detail",
               "per_location_stock", "warranty"],
    "hivetec": ["search", "categories", "list", "detail", "facets", "filter", "warranty", "delivery_estimate"],
    "gjirafa50": ["search", "categories", "list", "detail", "ean_in_detail", "warranty", "delivery_estimate"],
    "zirafamall": ["search", "categories", "list", "detail", "ean_in_detail", "seller", "delivery_estimate"],
    "setra": ["search", "categories", "list", "detail", "warranty"],
    "ananas": ["search", "categories", "list", "detail", "facets", "filter", "ean_in_listing", "ean_in_detail",
               "seller", "delivery_estimate"],
    "tehnomarket": ["search", "categories", "list", "detail", "ean_in_listing", "ean_in_detail", "warranty"],
}
CODE_SEARCH = {"setec", "neksio", "neptun", "gjirafa50", "zirafamall", "ananas", "tehnomarket"}
BASE = {"setec": "https://setec.mk", "anhoch": "https://www.anhoch.com", "neksio": "https://g.store.neksio.mk",
        "ddstore": "https://ddstore.mk", "neptun": "https://www.neptun.mk", "hivetec": "https://hivetec.mk",
        "gjirafa50": "https://gjirafa50.mk", "zirafamall": "https://zirafamall.mk", "setra": "https://setra.mk",
        "ananas": "https://ananas.mk", "tehnomarket": "https://tehnomarket.com.mk"}
LISTING = ["store", "id", "sku", "title", "url", "brand", "price_mkd", "regular_price_mkd", "in_stock",
           "stock_note", "category", "ean", "seller", "international_supplier", "delivery_estimate", "shipping_mkd"]

CYR = dict(zip("абвгдѓежзѕијклљмнњопрстќуфхцчџш",
               ["a","b","v","g","d","gj","e","zh","z","dz","i","j","k","l","lj","m","n","nj","o","p","r","s","t","kj","u","f","h","c","ch","dz","sh"]))


def fold(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(CYR.get(c, c) for c in s)


def env_map(name):
    out = {}
    for part in (os.environ.get(name) or "").split(","):
        if part.strip():
            k, _, v = part.strip().partition(":")
            out[k] = v
    return out


def load(store):
    with open(os.path.join(HERE, "data", store + ".json"), encoding="utf-8") as f:
        return json.load(f)


def member_prices(store):
    spec = env_map("FAKE_MEMBER").get(store) or ""
    return {k: int(v) for k, _, v in (p.partition("=") for p in spec.split(";")) if v}


def listing(r, store):
    out = {k: r.get(k) for k in LISTING}
    if "ean_in_listing" not in CAPS[store]:
        out["ean"] = None
    if store not in ("ananas", "zirafamall"):
        out.pop("seller", None)
    out = {k: v for k, v in out.items() if v is not None or k in ("sku", "brand", "regular_price_mkd", "in_stock",
                                                                 "stock_note", "category", "ean")}
    mem = member_prices(store).get(str(r.get("id")))
    if mem:
        out["member_price_mkd"] = mem
        if os.environ.get("FAKE_MEMBER_COND"):
            out["member_price_condition"] = os.environ["FAKE_MEMBER_COND"]
    return apply_valid(out, store, "FAKE_VALID")


def apply_valid(out, store, name):
    spec = env_map(name).get(store) or ""
    for part in spec.split(";"):
        rid, _, val = part.partition("=")
        if rid != str(out.get("id")) or not val:
            continue
        for key, v in zip(("price_valid_until", "member_price_valid_until"), val.split("/")):
            if v == "-":
                out.pop(key, None)
            else:
                out[key] = None if v == "null" else v
    return out


def detail_rec(r, store):
    out = listing(r, store)
    out["ean"] = r.get("ean") if "ean_in_detail" in CAPS[store] else None
    for k in ("warranty", "specs", "per_location_stock", "mpn", "extra", "attributes"):
        out[k] = r.get(k)
    return apply_valid(out, store, "FAKE_VALID_DETAIL")


def cats(store, recs):
    nodes = {}
    for r in recs:
        path = r.get("category") or "Друго"
        parts = [p.strip() for p in path.split(">")]
        for i in range(len(parts)):
            p = " > ".join(parts[:i + 1])
            if p not in nodes:
                slug = re.sub(r"[^a-z0-9]+", "-", fold(parts[i])).strip("-")
                cid = str(abs(hash(p)) % 90000 + 10000) if False else str(1000 + len(nodes))
                nodes[p] = {"id": cid, "slug": slug, "name": parts[i], "path": p,
                            "url": f"{BASE[store]}/category/{slug}-{cid}",
                            "parent": nodes[" > ".join(parts[:i])]["id"] if i else None, "count": 0}
            nodes[p]["count"] += 1
    return list(nodes.values())


def emit(rows, a, human):
    if a.json:
        if os.environ.get("FAKE_BADJSON", "") and STORE in env_map("FAKE_BADJSON"):
            open(a.json, "w").write("[{\"broken\": ")
        elif STORE in env_map("FAKE_NOJSON"):
            pass
        else:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        print(len(rows))
    else:
        for r in rows:
            print(human(r))


STORE = None


def main(store=None):
    global STORE
    p = argparse.ArgumentParser()
    p.add_argument("--site", choices=["gjirafa50", "zirafamall"])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sp = sub.add_parser("search"); sp.add_argument("query"); sp.add_argument("--limit", type=int)
    sp.add_argument("--in-stock", action="store_true"); sp.add_argument("--json")
    sp.add_argument("--all-words", action="store_true")  # anhoch only; mkshop passes it there
    sp = sub.add_parser("categories"); sp.add_argument("--grep"); sp.add_argument("--json")
    sp = sub.add_parser("list"); sp.add_argument("category"); sp.add_argument("--in-stock", action="store_true")
    sp.add_argument("--limit", type=int); sp.add_argument("--filter", action="append"); sp.add_argument("--json")
    sp = sub.add_parser("detail"); sp.add_argument("refs", nargs="+"); sp.add_argument("--json")
    if store is None and "--site" in sys.argv:
        i = sys.argv.index("--site")
        store = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    STORE = store
    if store and "facets" in CAPS.get(store, []):
        sp = sub.add_parser("facets"); sp.add_argument("category"); sp.add_argument("--json")
    a = p.parse_args()
    store = store or a.site
    if not store:
        p.error("--site is required")
    STORE = store
    t0 = time.time()
    try:
        return run(store, a)
    finally:
        if os.environ.get("FAKE_LOG"):
            with open(os.environ["FAKE_LOG"], "a") as f:
                f.write(f"{store}\t{os.getpid()}\t{t0:.3f}\t{time.time():.3f}\t{a.cmd}\t{getattr(a, 'query', '')}\n")


def run(store, a):
    if a.cmd == "info":
        print(json.dumps({"store": store, "name": store.capitalize() + " (fake)", "base_url": BASE[store],
                          "capabilities": CAPS[store], "sells": f"fake {store} catalogue",
                          "notes": "fake client for mkshop tests"}, ensure_ascii=False))
        return 0
    slow = env_map("FAKE_SLOW")
    if store in slow:
        time.sleep(float(slow[store] or 5))
    else:
        time.sleep(0.05)
    noise = env_map("FAKE_NOISE")
    if store in noise:
        n = int(noise[store] or 1000000)
        sys.stdout.write("x" * n + "\n"); sys.stderr.write(("noise " * (n // 6)) + "\n")
    if store in env_map("FAKE_BLOCK"):
        print(f"[{store}] GET {BASE[store]}/ -> 403", file=sys.stderr)
        print(f"BLOCKED: HTTP 403 from {BASE[store]} (Cloudflare challenge, title 'Just a moment...', cf-ray 8c1f2e3d4a5b6c7d-SOF)",
              file=sys.stderr)
        return 3
    if store in env_map("FAKE_ERROR"):
        raise RuntimeError(f"fake {store} parser broke: unexpected layout")
    fq = env_map("FAKE_FAIL_QUERY")
    if store in fq and getattr(a, "query", None) and fq[store] in a.query:
        raise ValueError(f"fake {store} failed on query {a.query!r}")
    recs = load(store)
    if a.cmd == "search":
        if store in env_map("FAKE_BIG"):
            n = int(env_map("FAKE_BIG")[store] or 50000)
            rows = [{"store": store, "id": f"big{i}", "sku": None, "title": f"Big item {i} {a.query}",
                     "url": f"{BASE[store]}/p/big{i}", "brand": "Bulk", "price_mkd": 100 + i,
                     "regular_price_mkd": None, "in_stock": True, "stock_note": "ok", "category": None, "ean": None}
                    for i in range(n)]
            rows = rows[: a.limit] if a.limit else rows
            emit(rows, a, lambda r: r["title"])
            return 0
        words = fold(a.query).split()
        def hay(r):
            h = " ".join(str(r.get(k) or "") for k in ("title", "brand", "sku"))
            if store in CODE_SEARCH:
                h += " " + (r.get("ean") or "") + " " + (r.get("mpn") or "")
            return fold(h)
        hits = [r for r in recs if all(w in hay(r) for w in words)]
        if not hits and store in ("ddstore", "anhoch") and len(words) > 1:
            hits = [r for r in recs if any(w in hay(r) for w in words if len(w) > 2)]
            print(f"[{store}] warning: no hit has every query word; OR fallback returned {len(hits)}", file=sys.stderr)
        if store in env_map("FAKE_WARN"):
            print(f"[{store}] warning: results capped at 1000 by the shop", file=sys.stderr)
        if a.in_stock:
            hits = [r for r in hits if r.get("in_stock") is not False]
        if a.limit:
            hits = hits[: a.limit]
        print(f"[{store}] search {a.query!r}: {len(hits)} hits", file=sys.stderr)
        emit([listing(r, store) for r in hits], a, lambda r: f"{r['price_mkd']}  {r['title']}  {r['url']}")
        return 0
    if a.cmd == "categories":
        cs = cats(store, recs)
        if a.grep:
            rx = re.compile(a.grep, re.I)
            cs = [c for c in cs if rx.search(c["name"]) or rx.search(c["path"]) or rx.search(c["slug"])
                  or rx.search(fold(c["path"]))]
        emit(cs, a, lambda c: f"{c['id']}  {c['path']}  ({c['count']})")
        return 0
    if a.cmd in ("list", "facets"):
        cs = cats(store, recs)
        c = next((c for c in cs if a.category in (c["id"], c["slug"], c["url"])), None)
        if not c:
            print(f"[{store}] unknown category {a.category!r}", file=sys.stderr)
            return 2
        rows = [r for r in recs if (r.get("category") or "Друго") == c["path"]
                or (r.get("category") or "").startswith(c["path"] + " > ")]
        if a.cmd == "facets":
            counts = {}
            for r in rows:
                for n, v in [("Бренд", r.get("brand"))] + list((r.get("attributes") or {}).items()):
                    if v:
                        counts[(n, v)] = counts.get((n, v), 0) + 1
            out = [{"name": n, "value": v, "count": k, "token": f"{n}::{v}"} for (n, v), k in sorted(counts.items())]
            emit(out, a, lambda f: f"{f['name']}={f['value']} ({f['count']})")
            return 0
        if a.in_stock:
            rows = [r for r in rows if r.get("in_stock")]
        for tok in a.filter or []:
            n, _, v = tok.partition("::")
            rows = [r for r in rows if (r.get("brand") if n == "Бренд" else (r.get("attributes") or {}).get(n)) == v]
        if a.limit:
            rows = rows[: a.limit]
        if not rows:
            print(f"[{store}] warning: category {a.category} returned no products", file=sys.stderr)
        emit([listing(r, store) for r in rows], a, lambda r: f"{r['price_mkd']}  {r['title']}")
        return 0
    if a.cmd == "detail":
        out = []
        for ref in a.refs:
            r = next((r for r in recs if ref in (r["id"], r.get("sku"), r["url"]) or ref.rstrip("/") == r["url"].rstrip("/")), None)
            out.append(detail_rec(r, store) if r else {"input": ref, "error": "not found"})
        emit(out, a, lambda r: json.dumps(r, ensure_ascii=False)[:200])
        return 0 if any("error" not in r for r in out) else 2
    return 1
