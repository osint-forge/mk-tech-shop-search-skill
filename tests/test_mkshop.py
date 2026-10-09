"""Regression tests for mkshop.py against the fake store clients in tests/fake_stores and
tests/fake_alt (offline). Run: python3 -B tests/test_mkshop.py"""
import csv, io, json, os, subprocess, sys, time

B = os.path.dirname(os.path.abspath(__file__))
import os as _pos
# The skill under test: this checkout's copy, or MKSHOP_SKILL_DIR (e.g. an installed copy).
SKILL = _pos.environ.get("MKSHOP_SKILL_DIR") or _pos.path.join(
    _pos.path.dirname(_pos.path.dirname(_pos.path.abspath(__file__))), "skills", "mk-tech-shop-search")
M = SKILL + "/scripts/mkshop.py"
FAKE = os.path.join(B, "fake_stores")
import tempfile as _tempfile  # noqa: E402
_OUT = _tempfile.mkdtemp(prefix="mkshop-test-")   # big outputs and logs, never inside the repo
ALT = os.path.join(B, "fake_alt")
fails = 0


def run(args, env=None, timeout=120):
    e = dict(os.environ, MKSHOP_STORES_DIR=FAKE)
    e.update(env or {})
    t0 = time.time()
    p = subprocess.run([sys.executable, M] + args, capture_output=True, text=True, env=e, timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.time() - t0


def js(args, env=None):
    rc, out, err, dt = run(args + ["--json", "-", "--quiet"], env)
    return rc, json.loads(out), err, dt


def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        fails += 1


# --- search: statuses, parallelism, union, annotations
rc, d, err, dt = js(["search", "mx master", "--q", "logitech mouse", "--timeout", "4"],
                    {"FAKE_BLOCK": "anhoch", "FAKE_SLOW": "setra:30", "FAKE_ERROR": "hivetec",
                     "FAKE_BADJSON": "ddstore", "FAKE_FAIL_QUERY": "neptun:master"})
st = {k: v["status"] for k, v in d["stores"].items()}
check("search exit 0 when some stores ok", rc == 0, rc)
check("blocked store reported", st["anhoch"] == "blocked" and d["stores"]["anhoch"]["message"].startswith("BLOCKED"), st)
check("timeout store reported", st["setra"] == "timeout", st)
check("crashing store -> error", st["hivetec"] == "error", st)
check("bad JSON -> error", st["ddstore"] == "error", st)
check("one failing query -> partial", st["neptun"] == "partial", st)
check("all 11 stores present", len(d["stores"]) == 11)
check("parallel across stores (wall < 8s with a 4s timeout)", dt < 8, dt)
eff = [r["effective_price_mkd"] for r in d["results"]]
check("results sorted by (effective) price", eff == sorted(eff) and
      all(r["effective_price_mkd"] == r["price_mkd"] and r["price_condition"] is None for r in d["results"]))
check("found_by recorded", all(r.get("found_by") for r in d["results"]))
check("match_key + model_key present", all("match_key" in r and "model_key" in r for r in d["results"]))
ids = [(r["store"], r["id"]) for r in d["results"]]
check("union per store by id (no duplicates)", len(ids) == len(set(ids)))

# --- per-store sequencing: no two client runs of one store overlap; stores overlap each other
LOG = os.path.join(_OUT, "seq.log")
if os.path.exists(LOG):
    os.remove(LOG)
rc, d, err, dt = js(["search", "mx master", "--q", "logitech", "--q", "galaxy", "--q", "samsung"], {"FAKE_LOG": LOG, "FAKE_SLOW": "setec:0.5,neptun:0.5"})
rows = [l.rstrip("\n").split("\t") for l in open(LOG)]
by = {}
for st_, pid, t0, t1, cmd, q in rows:
    by.setdefault(st_, []).append((float(t0), float(t1)))
overlap = sum(1 for v in by.values() for a_, b_ in zip(sorted(v), sorted(v)[1:]) if b_[0] < a_[1] - 1e-3)
check("no overlapping client runs within a store", overlap == 0 and len(rows) == 44, (overlap, len(rows)))
cross = any(a_[0] < b_[1] and b_[0] < a_[1] for x in by for y in by if x < y for a_ in by[x] for b_ in by[y])
check("stores run concurrently", cross)

# --- mirrors
rc, d, err, dt = js(["search", "galaxy a56"])
mir = {(r["store"], r.get("seller")): r.get("mirror_of") for r in d["results"] if r.get("mirror_of")}
check("ananas Нексио -> neksio", ("ananas", "Нексио") in mir and mir[("ananas", "Нексио")] == "neksio", mir)
check("ananas PC MARKET -> anhoch", mir.get(("ananas", "PC MARKET")) == "anhoch", mir)
check("ananas ТЕХНОМАРКЕТ -> tehnomarket", mir.get(("ananas", "ТЕХНОМАРКЕТ")) == "tehnomarket", mir)
check("zirafamall SKU in gjirafa50 -> gjirafa50", any(r["store"] == "zirafamall" and r["mirror_of"] == "gjirafa50" for r in d["results"]))
rc, d2, err, dt = js(["search", "galaxy a56", "--hide-mirrors"])
check("--hide-mirrors drops mirror rows", not any(r.get("mirror_of") for r in d2["results"]) and len(d2["results"]) < len(d["results"]))

# --- filters
rc, d, err, dt = js(["search", "galaxy a56", "--in-stock", "--min-price", "20000", "--max-price", "23000"])
check("filters applied", all(r["in_stock"] is not False and 20000 <= r["price_mkd"] <= 23000 for r in d["results"]))
check("dropped counts reported", d["dropped"].get("out of stock", 0) > 0)
rc, d, err, dt = js(["search", "nintendo switch oled", "--stores", "ddstore,anhoch,setec", "--strict"])
check("--strict trims OR-fallback noise", len(d["results"]) == 1 and "oled" in d["results"][0]["title"].lower(), len(d["results"]))
check("OR-fallback warning surfaced", any("fallback" in w.lower() for w in d["stores"]["ddstore"]["warnings"]))

# --- big output and noise: no deadlock
rc, out, err, dt = run(["search", "logitech", "--limit-per-store", "0", "--json", os.path.join(_OUT, "big.json"), "--quiet"],
                       {"FAKE_BIG": "ddstore:50000", "FAKE_NOISE": "ddstore:2000000,neksio:3000000"})
check("50k records + 5MB noise completes", rc == 0 and dt < 60, (rc, dt))

# --- all blocked -> exit 3; all failing -> 1
rc, out, err, dt = run(["search", "x", "--stores", "anhoch,setra", "--quiet"], {"FAKE_BLOCK": "anhoch,setra"})
check("all blocked -> exit 3", rc == 3, rc)
rc, out, err, dt = run(["search", "x", "--stores", "anhoch,setra", "--quiet"], {"FAKE_ERROR": "anhoch,setra"})
check("all error -> exit 1", rc == 1, rc)
rc, out, err, dt = run(["search", "x", "--stores", "nope"])
check("unknown store -> exit 2", rc == 2, rc)
rc, out, err, dt = run(["categories"])
check("categories without --grep/--store -> exit 2", rc == 2, rc)

# --- missing client and --site fallback
rc, d, err, dt = js(["search", "galaxy a56", "--stores-dir", ALT, "--stores", "setec,gjirafa,tehnomarket"])
check("missing client reported as error", d["stores"]["tehnomarket"]["status"] == "error" and "not found" in d["stores"]["tehnomarket"]["message"])
check("--site after-subcommand fallback", d["stores"]["gjirafa50"]["status"] == "ok" and d["stores"]["zirafamall"]["status"] == "ok")

# --- categories / list / facets
rc, d, err, dt = js(["categories", "--grep", "телефон|phone"], {"FAKE_BLOCK": "setra"})
check("categories merged with store column", rc == 0 and all("store" in r for r in d["results"]) and d["stores"]["setra"]["status"] == "blocked")
rc, d, err, dt = js(["list", "--store", "setec", "1001"])
check("list ok", rc == 0 and d["count"] > 0)
rc, out, err, dt = run(["list", "--store", "setec", "nosuchcat", "--quiet"])
check("list unknown category -> exit 2", rc == 2, rc)
rc, d, err, dt = js(["facets", "--store", "ddstore", "1003"])
check("facets ok with tokens", rc == 0 and all(r.get("token") for r in d["results"]))
rc, out, err, dt = run(["facets", "--store", "neptun", "1001", "--quiet"])
check("facets on store without facets -> exit 2", rc == 2, rc)

# --- detail
rc, d, err, dt = js(["detail", "https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003",
                     "www.neptun.mk/categories/samsung-galaxy-a56-5g-8-256gb-pink-1033", "1069",
                     "https://www.amazon.de/x", "https://setec.mk/products/nope-1"])
res = d["results"]
check("detail keeps input order", [r["input"] for r in res] == ["https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003",
                                                                 "www.neptun.mk/categories/samsung-galaxy-a56-5g-8-256gb-pink-1033", "1069",
                                                                 "https://www.amazon.de/x", "https://setec.mk/products/nope-1"], [r["input"] for r in res])
check("detail ok rows", not res[0].get("error") and not res[1].get("error"))
check("detail errors per item", res[2].get("error") and res[3].get("error") and res[4].get("error"))
rc, d, err, dt = js(["detail", "1001", "1002", "--store", "setec"])
check("bare ids with --store", rc == 0 and d["count"] == 2)

# --- match
rc, d, err, dt = js(["match", "https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003"])
same = [c for c in d["results"] if c["match"]["group"] == "same"]
var = [c for c in d["results"] if c["match"]["group"] == "variant"]
check("match URL: exact EAN offers found", sum(c["match"]["confidence"] == "exact" for c in same) >= 6, len(same))
check("match URL: no 128GB / pink in same group", not any("128GB" in c["title"] or "Pink" in c["title"] or "Розева" in c["title"] for c in same))
check("match URL: variants kept with differs", var and all(c["match"]["differs"] for c in var))
check("match URL: reference itself excluded", not any(c["store"] == "setec" and c["id"] == "1003" for c in d["results"]))
check("match URL: EAN verified via detail at gjirafa", any(c["store"] == "gjirafa50" and c.get("verified_by_detail") and c["match"]["confidence"] == "exact" for c in d["results"]))
rc, d, err, dt = js(["match", "5099206103603"])
check("match EAN: title derived", d["reference"]["title"] and "MASTER 3S" in d["reference"]["title"].upper())
check("match EAN: skips stores whose search ignores EAN", set(d["reference"]["ean_search_skipped"]) == {"anhoch", "ddstore", "hivetec", "setra", "tehnomarket"})
check("match EAN: pale grey not in same", not any("grey" in c["title"].lower() or "сив" in c["title"].lower() for c in d["results"] if c["match"]["group"] == "same"))
rc, d, err, dt = js(["match", "Logitech MX Master 3S"])
check("match text: anywhere/master 3 rejected", not any("anywhere" in c["title"].lower() or c["title"].lower().endswith("master 3 graphite") for c in d["results"]))
check("match text: offers split by open colour", len({c["match"].get("variant") for c in d["results"] if c["match"]["group"] == "same"}) == 2)
rc, d, err, dt = js(["match", "Apple iPhone 16 128GB"])
check("match: iPhone 16 Pro in variants, 15 rejected", all(c["match"]["group"] == "variant" for c in d["results"] if "pro" in c["title"].lower())
      and not any(" 15 " in c["title"] for c in d["results"]))
rc, d, err, dt = js(["match", "Samsung QE55Q60D"])
check("match TV: model by code prefix, 65\" excluded", d["results"] and all("55" in c["title"] for c in d["results"]) and all(c["match"]["confidence"] == "model" for c in d["results"]))
rc, d, err, dt = js(["match", "Steam Deck OLED 512GB"])
check("match: not carried -> no results, exit 0", rc == 0 and not d["results"])
rc, d, err, dt = js(["match", "https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003", "--stores", "neptun,anhoch"],
                    {"FAKE_BLOCK": "anhoch"})
check("match: blocked store reported, others still matched", d["stores"]["anhoch"]["status"] == "blocked" and any(c["store"] == "neptun" for c in d["results"]))

# --- member prices (opt-in fake data: two neptun phones get a lower member price)
COND = "Club card, 100 MKD one-off"
MEM = {"FAKE_MEMBER": "neptun:1030=18490;1032=21490", "FAKE_MEMBER_COND": COND}
A56 = ["search", "galaxy a56", "--stores", "neptun,setec,anhoch"]
ids = lambda d: [(r["store"], r["id"]) for r in d["results"]]
rc, d, err, dt = js(A56, MEM)
eff = [r["effective_price_mkd"] for r in d["results"]]
check("member: sorted by effective price, member price first", eff == sorted(eff) and ids(d)[0] == ("neptun", "1030"), ids(d))
n = {r["id"]: r for r in d["results"] if r["store"] == "neptun"}
check("member: JSON price fields", (n["1030"]["price_mkd"], n["1030"]["member_price_mkd"], n["1030"]["effective_price_mkd"],
                                    n["1030"]["price_condition"]) == (20190, 18490, 18490, COND), n["1030"])
check("member: rows without one keep price_mkd, no condition",
      all(r["effective_price_mkd"] == r["price_mkd"] and r["price_condition"] is None
          for r in d["results"] if not r.get("member_price_mkd")))
check("member: envelope member_prices true", d["member_prices"] is True)
rc, d, err, dt = js(A56 + ["--no-member-prices"], MEM)
n = {r["id"]: r for r in d["results"] if r["store"] == "neptun"}
eff = [r["price_mkd"] for r in d["results"]]
check("--no-member-prices: price_mkd order, effective = price_mkd, no condition",
      eff == sorted(eff) and ids(d)[0] != ("neptun", "1030") and n["1030"]["effective_price_mkd"] == 20190
      and n["1030"]["member_price_mkd"] == 18490 and n["1030"]["price_condition"] is None and d["member_prices"] is False)
rc, d, err, dt = js(["--no-member-prices"] + A56, MEM)
check("--no-member-prices also works before the command", d["member_prices"] is False)
rc, d, err, dt = js(A56 + ["--max-price", "19000"], MEM)
check("member: --max-price keeps a member price within it", ids(d) == [("neptun", "1030")]
      and not any("member price" in k for k in d["dropped"]), (ids(d), d["dropped"]))
rc, d, err, dt = js(A56 + ["--max-price", "19000", "--no-member-prices"], MEM)
check("--no-member-prices: --max-price on price_mkd, member price within it counted",
      d["results"] == [] and d["dropped"].get("above --max-price (member price within it)") == 1, d["dropped"])
rc, d, err, dt = js(A56 + ["--min-price", "20000"], MEM)
check("member: --min-price uses the member price", ("neptun", "1030") not in ids(d) and ("neptun", "1031") in ids(d))
rc, d, err, dt = js(A56 + ["--min-price", "20000", "--no-member-prices"], MEM)
check("--no-member-prices: --min-price on price_mkd", ("neptun", "1030") in ids(d))
rc, d, err, dt = js(A56, {"FAKE_MEMBER": "neptun:1030=18490"})
check("member: generic condition when the client names none",
      next(r for r in d["results"] if r["id"] == "1030")["price_condition"] == "loyalty card or membership")
rc, d, err, dt = js(["list", "--store", "neptun", "1001", "--max-price", "19000"], MEM)
eff = [r["effective_price_mkd"] for r in d["results"]]
check("member: list filters and sorts on the effective price", ("neptun", "1030") in ids(d)
      and ("neptun", "1031") not in ids(d) and eff == sorted(eff) and d["member_prices"] is True, ids(d))
rc, out, err, dt = run(A56 + ["--csv", "-", "--quiet"], MEM)
rows = list(csv.DictReader(io.StringIO(out)))
r1030 = next(r for r in rows if r["store"] == "neptun" and r["id"] == "1030")
check("member: CSV fields and values", (r1030["effective_price_mkd"], r1030["price_mkd"], r1030["member_price_mkd"],
                                        r1030["price_condition"]) == ("18490", "20190", "18490", COND)
      and rows[0]["id"] == "1030", r1030)
rc, out, err, dt = run(A56 + ["--quiet"], MEM)
lines = out.splitlines()
check("member: table marks the member price, shows NON-MEMBER", "NON-MEMBER" in lines[0]
      and lines[1].lstrip().startswith("18,490*") and "20,190" in lines[1], lines[:2])
check("member: footnote names the condition", any(l.startswith("* member price") and f"neptun: {COND}" in l for l in lines))
rc, out, err, dt = run(A56 + ["--quiet", "--no-member-prices"], MEM)
lines = out.splitlines()
check("--no-member-prices table: no marker, MEMBER column",
      "*" not in out.split("\n-- ")[0].replace("* member", "") and " MEMBER " in lines[0]
      and any(l.startswith("MEMBER:") for l in lines), lines[:3])
NEP = "https://www.neptun.mk/categories/samsung-galaxy-a56-5g-8-256gb-graphite-1032"
SET = "https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003"
rc, out, err, dt = run(["match", NEP, "--quiet"], MEM)
check("match: reference line uses the member price with its condition",
      f"neptun 21,490 MKD member price ({COND}); without it 23,490 MKD" in out, out[:400])
rc, out, err, dt = run(["match", SET, "--quiet"], MEM)
check("match: summary says the cheapest is a member price",
      "cheapest 21,490 MKD at neptun" in out and f"that is a member price ({COND}); without one: " in out, out[:900])
rc, d, err, dt = js(["match", SET], MEM)
same = [c for c in d["results"] if c["match"]["group"] == "same" and c["match"]["confidence"] == "exact"]
check("match: exact offers ranked by effective price", same and (same[0]["store"], same[0]["effective_price_mkd"]) == ("neptun", 21490)
      and [c["effective_price_mkd"] for c in same] == sorted(c["effective_price_mkd"] for c in same), [(c["store"], c["effective_price_mkd"]) for c in same])
rc, out, err, dt = run(["match", SET, "--quiet", "--no-member-prices"], MEM)
check("--no-member-prices match: cheapest on price_mkd, member price noted",
      "cheapest 21,490" not in out and f"21,490 MKD at neptun with a member price ({COND}; MEMBER column)" in out, out[:900])


def section(out, title):
    """Lines of one match section: its title up to the next blank or footer line."""
    ls = out.splitlines()
    i = next(i for i, l in enumerate(ls) if l.startswith(title))
    j = next((j for j in range(i + 1, len(ls)) if not ls[j].strip() or ls[j].startswith("-- ")), len(ls))
    return ls[i:j]


def header(sec):
    return next(l for l in sec if "PRICE" in l.split() and "STORE" in l.split())


# price columns and member footnote per match table
VMEM = {"FAKE_MEMBER": "setec:1001=17990", "FAKE_MEMBER_COND": COND}
rc, out, err, dt = run(["match", SET, "--quiet"], VMEM)
same_s, var_s = section(out, "SAME PRODUCT"), section(out, "VARIANTS")
check("match: SAME PRODUCT has no member column when only a variant has a member price",
      "MEMBER" not in header(same_s) and not any("member" in l.lower() for l in same_s), same_s[:4])
check("match: VARIANTS shows NON-MEMBER and the footnote under it",
      "NON-MEMBER" in header(var_s) and any("17,990*" in l and "setec" in l for l in var_s)
      and var_s[-1].startswith("* member price (needs") and f"setec: {COND}" in var_s[-1], var_s[:3] + var_s[-1:])
NMEM = {"FAKE_MEMBER": "setec:1014=4990", "FAKE_MEMBER_COND": COND}
rc, out, err, dt = run(["match", "Logitech MX Master 3S", "--near", "--quiet"], NMEM)
near_s = section(out, "NEAR MISSES")
check("match --near: near-miss member price with NON-MEMBER column and its footnote",
      "NON-MEMBER" in header(near_s) and any("4,990*" in l and "5,490" in l for l in near_s)
      and near_s[-1].startswith("* member price") and f"setec: {COND}" in near_s[-1]
      and "MEMBER" not in header(section(out, "SAME PRODUCT")), near_s[:3] + near_s[-1:])
rc, out, err, dt = run(["match", "Logitech MX Master 3S", "--near", "--quiet", "--no-member-prices"], NMEM)
near_s = section(out, "NEAR MISSES")
check("--no-member-prices match --near: MEMBER column and MEMBER footnote",
      header(near_s).split()[:2] == ["PRICE", "MEMBER"] and any("5,490" in l and "4,990" in l for l in near_s)
      and near_s[-1].startswith("MEMBER:"), near_s[:3] + near_s[-1:])

# --min-price: say so when only the member price is below it
rc, d, err, dt = js(A56 + ["--min-price", "20000"], MEM)
check("member: --min-price drop notes the non-member price within it",
      d["dropped"].get("below --min-price (non-member price within it)") == 1 and ("neptun", "1030") not in ids(d), d["dropped"])
rc, d, err, dt = js(A56 + ["--min-price", "20000", "--max-price", "20100"], MEM)
check("member: no such note when the non-member price is outside the range",
      not any("non-member" in k for k in d["dropped"]) and ("neptun", "1030") not in ids(d), d["dropped"])

# --- stores
rc, d, err, dt = js(["stores"])
check("stores lists 11 with capabilities", len(d) == 11 and all(r["capabilities"] for r in d))

# ==== group, match (spaced codes, colours, bare-code names, --also), price windows (2026-10-03) ====
import datetime as _dt, shutil as _sh, tempfile as _tf  # noqa: E401
TMP = _tf.mkdtemp(prefix="mkshop-group-")


def jfile(name, obj):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def by_id(d):
    return {c["id"]: c for c in d["results"]}


# match: a Setec bare-code reference is also searched by its marketed name
ULT = "https://setec.mk/products/sony-whult900nbce7-G101"
rc, d, err, dt = js(["match", ULT])
ref = d["reference"]
c = by_id(d)
check("bare code: marketed name derived from the EAN-confirmed offer", ref.get("marketed_name") == "Sony ULT Wear"
      and {"query": "Sony ULT Wear", "kind": "marketed name"} in ref["queries"]
      and any("bare model code" in n and "Sony ULT Wear" in n for n in ref["notes"]), (ref.get("marketed_name"), ref["notes"]))
qs = lambda k: [q["query"] for q in d["stores"][k]["queries"]]
check("bare code: name searched in shops without EAN search only", "Sony ULT Wear" in qs("anhoch")
      and "Sony ULT Wear" in qs("tehnomarket") and "Sony ULT Wear" not in qs("neptun")
      and "Sony ULT Wear" not in qs("zirafamall"), (qs("anhoch"), qs("neptun")))
check("bare code: Anhoch's listing by name is the same product (black)", c.get("G104", {}).get("match", {}).get("group") == "same"
      and any("via the name 'Sony ULT Wear'" in r for r in c["G104"]["match"]["reasons"]), c.get("G104", {}).get("match"))
check("bare code: other colours by name are variants (colour from Setec's attributes)",
      c["G105"]["match"]["group"] == "variant" and "colour grey vs ref black" in c["G105"]["match"]["differs"]
      and c["G106"]["match"]["group"] == "variant", (c["G105"]["match"], c["G106"]["match"]))
check("bare code: ZirafaMall exact by EAN", c["G103"]["match"]["confidence"] == "exact")
rc, d, err, dt = js(["match", ULT, "--also", "Sony ULT Wear"])
ref = d["reference"]
check("--also: searched as 'also' and not again as the marketed name",
      {"query": "Sony ULT Wear", "kind": "also"} in ref["queries"] and not ref.get("marketed_name")
      and "Sony ULT Wear" in qs("anhoch") and by_id(d)["G104"]["match"]["group"] == "same", ref["queries"])
rc, d, err, dt = js(["match", "Sony headphones X1", "--also", "JBL Live 770NC", "--stores", "anhoch"])
check("--also on free text: extra query run and scored against", "JBL Live 770NC" in [q["query"] for q in d["stores"]["anhoch"]["queries"]]
      and any(c["id"] == "G201" for c in d["results"]), [q["query"] for q in d["stores"]["anhoch"]["queries"]])

# match: spaced codes, composite codes, colour vocabulary
rc, d, err, dt = js(["match", "JBL Live 770NC Black"])
c = by_id(d)
same = {k for k, v in c.items() if v["match"]["group"] == "same"}
check("JBL: same = Anhoch black, Tehnomarket 'LIVE 770 NC BLACK', Ananas LIVE770NCBLK", same == {"G201", "G205", "G207"}, same)
check("JBL: Tehnomarket's spaced code is a model-code match", c["G205"]["match"]["confidence"] == "model"
      and any("written with spaces" in r for r in c["G205"]["match"]["reasons"]), c["G205"]["match"])
check("JBL: Sand is a colour variant", c["G202"]["match"]["group"] == "variant" and "colour sand vs ref black" in c["G202"]["match"]["differs"])
check("JBL: Neptun WHT is white", "colour white vs ref black" in c["G206"]["match"]["differs"])
check("JBL: T770NC (Tune) rejected", "G108" not in c and any(n["id"] == "G108" for n in d["near_misses"]))
rc, d, err, dt = js(["match", "JBL Live 770NC"])
c = by_id(d)
check("JBL free text: Sand gets its own colour sub-group", c["G202"]["match"]["group"] == "same" and c["G202"]["match"]["variant"] == "colour sand"
      and c["G206"]["match"]["variant"] == "colour white", (c["G202"]["match"], c["G206"]["match"]))
rc, out, err, dt = run(["match", "JBL Live 770NC", "--quiet"])
check("JBL free text: table shows the sand sub-group", "  colour sand: 1 offer(s)" in out, out[:1500])

# group: mirrors across two saved list runs
G50, ZM = os.path.join(TMP, "g50.json"), os.path.join(TMP, "zm.json")
run(["list", "--store", "gjirafa50", "1001", "--json", G50, "--quiet"])
run(["list", "--store", "zirafamall", "1001", "--json", ZM, "--quiet"])
zm = json.load(open(ZM))
check("single list run: ZirafaMall mirror not detectable (no Gjirafa50 SKUs in it)", zm["results"]
      and not any(r.get("mirror_of") for r in zm["results"]))
check("envelopes carry generated_at", isinstance(zm.get("generated_at"), str) and zm["generated_at"][:4].isdigit())
g50skus = {r["sku"] for r in json.load(open(G50))["results"]}
rc, d, err, dt = js(["group", G50, ZM])
offers = [o for p in d["results"] for o in p["offers"]]
zmo = [o for o in offers if o["store"] == "zirafamall"]
twins = [o for o in zmo if o["sku"] in g50skus]
check("group: ZirafaMall offers whose SKU Gjirafa50 lists mirror it across files; the others do not",
      len(twins) >= 3 and all(o["mirror_of"] == "gjirafa50" for o in twins)
      and all(o["mirror_of"] is None for o in zmo if o["sku"] not in g50skus), [(o["sku"], o["mirror_of"]) for o in zmo])
tw = [p for p in d["results"] if any(o in twins for o in p["offers"])]
check("group: each mirror sits with its Gjirafa50 twin, joined by SKU, counted as one shop",
      all({o["store"] for o in p["offers"]} == {"gjirafa50", "zirafamall"} and p["n_shops"] == 1
          and {o["group_link"] for o in p["offers"]} == {"gjirafa sku"} for p in tw),
      [(p["shops"], [o["group_link"] for o in p["offers"]]) for p in tw][:3])
check("group: mirrors listed per product", all(p["mirrors"] and p["mirrors"][0]["mirror_of"] == "gjirafa50" for p in tw))
rc, d2, err, dt = js(["group", G50, ZM, "--hide-mirrors"])
check("group --hide-mirrors drops the twin mirror offers only", not any(o in twins for p in d2["results"] for o in p["offers"])
      and d2["count"] == d["count"] and sum(d2["dropped"].values()) == len(twins), d2["dropped"])

# group: EAN groups, part-number aliases, variants apart, prefix vs exact
E = "4711387800123"


def grec(store, rid, title, price, ean=None, **kw):
    r = {"store": store, "id": rid, "sku": kw.pop("sku", None), "title": title, "url": f"https://{store}.example/p/{rid}",
         "brand": kw.pop("brand", "ASUS"), "price_mkd": price, "regular_price_mkd": None, "in_stock": kw.pop("in_stock", True),
         "stock_note": None, "category": "Графички картички", "ean": ean}
    r.update(kw)
    return r


GPU = jfile("gpu.json", [
    grec("setec", "S1", "ASUS DUAL-RTX5070-O12G GeForce RTX 5070 OC 12GB GDDR7", 41990, E),
    grec("ddstore", "D1", "ASUS Dual GeForce RTX 5070 OC Edition 12GB GDDR7 (90YV0M17-M0NA00)", 42490, E),
    grec("neksio", "N1", "VGA ASUS 90YV0M17-M0NA00 RTX 5070 12GB GDDR7", 40990),
    grec("anhoch", "A1", "Graphics Card ASUS DUAL-RTX5070-O12G 12GB", 41490, in_stock=None),
    grec("hivetec", "H1", "ASUS PRIME GeForce RTX 5070 OC 12GB", 43990, sku="PRIME-RTX5070-O12G"),
    grec("setra", "T1", "ASUS DUAL GeForce RTX 5070 12GB", 42990),
    grec("setec", "S2", "ASUS DUAL-RTX5070-O12G-WHITE GeForce RTX 5070 OC 12GB White", 44990, "4711387800999"),
    grec("anhoch", "A2", "Graphics Card ASUS DUAL-RTX5070-O12G 12GB White", 45490, in_stock=False)])
rc, d, err, dt = js(["group", GPU])
prod = {frozenset(o["id"] for o in p["offers"]): p for p in d["results"]}
main = next(p for k, p in prod.items() if "S1" in k)
check("group: EAN pair + vendor part number + marketing code = one product",
      {o["id"] for o in main["offers"]} == {"S1", "D1", "N1", "A1"} and main["n_shops"] == 4, [sorted(k) for k in prod])
links = {o["id"]: o["group_link"] for o in main["offers"]}
check("group: links say how each offer joined", links == {"S1": "ean", "D1": "ean", "N1": "code 90YV0M17-M0NA00",
                                                         "A1": "code DUAL-RTX5070-O12G"}, links)
check("group: best = lowest effective price, codes listed", main["best"]["store"] == "neksio" and main["best"]["effective_price_mkd"] == 40990
      and main["ean"] == E and {"DUAL-RTX5070-O12G", "90YV0M17-M0NA00"} <= set(main["codes"]), (main["best"], main["codes"]))
check("group: offers sorted by effective price", [o["effective_price_mkd"] for o in main["offers"]] == sorted(o["effective_price_mkd"] for o in main["offers"]))
check("group: PRIME (other part number), code-less DUAL and the White edition stay apart",
      all(frozenset([x]) in prod for x in ("H1", "T1")) and frozenset({"S2", "A2"}) in prod, [sorted(k) for k in prod])
check("group: stock summary", main["in_stock"] == {"yes": 3, "no": 0, "unknown": 1} and d["count"] == 4, main["in_stock"])
rc, d, err, dt = js(["group", GPU, "--in-stock", "--max-price", "42000"])
check("group filters: --in-stock and --max-price on offers, empty products dropped",
      d["count"] == 1 and {o["id"] for o in d["results"][0]["offers"]} == {"N1", "A1", "S1"}
      and d["dropped"].get("out of stock") == 1 and d["dropped"].get("above --max-price") == 4, d["dropped"])
rc, d, err, dt = js(["group", GPU, "--min-price", "44000"])
check("group --min-price", {o["id"] for p in d["results"] for o in p["offers"]} == {"S2", "A2"}, d["dropped"])
rc, out, err, dt = run(["group", GPU, "--quiet"])
check("group table: PRODUCTS, OFFERS with LINK, footer", out.startswith("PRODUCTS: 4") and "OFFERS by product" in out
      and "code 90YV0M17-M0NA00" in out and "-- 4 products, 8 offers from 6 shops" in out, out[:600])
rc, out, err, dt = run(["group", GPU, "--quiet", "--show", "1", "--offers", "2"])
check("group --show / --offers", "... 3 more products not shown" in out and "... 2 more offers not shown" in out, out)
rc, out, err, dt = run(["group", GPU, "--csv", "-", "--quiet"])
rows = list(csv.DictReader(io.StringIO(out)))
check("group --csv: one row per offer with product columns", len(rows) == 8 and list(rows[0])[:5] ==
      ["product", "product_title", "product_codes", "product_shops", "group_link"] and rows[0]["product"] == "1", list(rows[0])[:6])

# group: union by store + id keeps the freshest copy, fills gaps from older ones; match / detail envelopes
old = {"command": "search", "generated_at": "2026-10-01T10:00:00+02:00", "stores": {"neksio": {"status": "ok"}},
       "results": [grec("neksio", "N1", "VGA ASUS 90YV0M17-M0NA00 RTX 5070 12GB GDDR7", 39990, E)]}
new = {"command": "search", "generated_at": "2026-10-03T10:00:00+02:00", "stores": {"neksio": {"status": "ok"}, "setra": {"status": "blocked", "message": "BLOCKED: 403"}},
       "results": [grec("neksio", "N1", "VGA ASUS 90YV0M17-M0NA00 RTX 5070 12GB GDDR7", 40990)]}
rc, d, err, dt = js(["group", jfile("new.json", new), jfile("old.json", old)])
o = d["results"][0]["offers"][0]
check("group union: freshest price, EAN filled from the older copy", d["records"] == 1 and o["price_mkd"] == 40990
      and o["ean"] == E and o["sources"] == [0, 1], o)
check("group: a shop not 'ok' in an input is reported", d["stores"]["setra"]["status"] == "blocked"
      and "new.json" in d["stores"]["setra"]["message"], d["stores"].get("setra"))
rc, mj, err, dt = js(["match", "JBL Live 770NC Black"])
rc, d, err, dt = js(["group", jfile("m.json", mj)])
ids = {o["id"] for p in d["results"] for o in p["offers"]}
check("group reads match envelopes (candidates, not near misses)", ids == {c["id"] for c in mj["results"]}, ids)
rc, dj, err, dt = js(["match", ULT])
rc, d, err, dt = js(["group", jfile("u.json", dj)])
check("group adds a match's reference record", any(o["id"] == "G101" for p in d["results"] for o in p["offers"])
      and any({o["id"] for o in p["offers"]} >= {"G101", "G103"} for p in d["results"]))
rc, d, err, dt = js(["group", jfile("grp.json", js(["group", GPU])[1])])
check("group re-reads its own output", d["count"] == 4 and d["offers"] == 8, (d["count"], d["offers"]))
rc, out, err, dt = run(["group", os.path.join(TMP, "nope.json")])
check("group: missing file -> exit 2", rc == 2 and "cannot read" in err, err)
rc, out, err, dt = run(["group", jfile("cats.json", {"command": "categories", "results": [{"id": 1}]})])
check("group: no product records -> exit 2", rc == 2 and "no product records" in err, err)
rc, out, err, dt = run(["group", "--json", GPU, "--quiet"])
check("group --json FILE (swallowed input) reads it and writes JSON to stdout",
      rc == 0 and json.loads(out)["count"] == 4 and "reading" in err, (rc, err))

# price validity windows (tri-state), through the fake clients
now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=2)))
iso = lambda days: (now + _dt.timedelta(days=days)).replace(hour=23, minute=59, second=0, microsecond=0).isoformat()
dm = lambda days: (now + _dt.timedelta(days=days)).strftime("%d.%m")
VAL = {"FAKE_VALID": f"neptun:1030={iso(1)};1031=null;1032=-/{iso(2)},setec:1003={iso(-1)};1004={iso(30)}",
       "FAKE_MEMBER": "neptun:1032=21490", "FAKE_MEMBER_COND": COND}
rc, d, err, dt = js(A56, VAL)
r = {(x["store"], x["id"]): x for x in d["results"]}
check("valid: string end kept as effective_price_valid_until", r[("neptun", "1030")]["effective_price_valid_until"] == iso(1))
check("valid: null = standing", r[("neptun", "1031")]["effective_price_valid_until"] is None
      and "effective_price_valid_until" in r[("neptun", "1031")])
check("valid: member price effective -> its window", r[("neptun", "1032")]["effective_price_valid_until"] == iso(2)
      and "price_valid_until" not in r[("neptun", "1032")])
check("valid: record without the fields has no effective key", "effective_price_valid_until" not in r[("anhoch", next(
    i for s, i in r if s == "anhoch"))])
rc, d, err, dt = js(A56 + ["--no-member-prices"], VAL)
check("valid: --no-member-prices -> price_mkd's window, 'unknown' when absent",
      {(x["store"], x["id"]): x for x in d["results"]}[("neptun", "1032")]["effective_price_valid_until"] == "unknown")
rc, out, err, dt = run(A56 + ["--quiet", "--show", "0"], VAL)
lines = out.splitlines()
check("valid: VALID column right after PRICE", lines[0].split()[:2] == ["PRICE", "VALID"], lines[0])
row = lambda u: next(l for l in lines if u in l)
check("valid: soon / past ends marked, a far end and standing prices not",
      f"until {dm(1)}" in row("graphite-1030") and f"until {dm(2)}" in row("graphite-1032")
      and f"ended {dm(-1)}" in row("graphite-1003") and "until" not in row("1004") and "until" not in row("1031"), lines[:6])
check("valid: footnote", any(l.startswith("VALID: the shop's end date") for l in lines))
rc, out, err, dt = run(A56 + ["--quiet", "--csv", "-"], VAL)
rows = {(x["store"], x["id"]): x for x in csv.DictReader(io.StringIO(out))}
check("valid: CSV columns with the tri-state", (rows[("neptun", "1030")]["effective_price_valid_until"], rows[("neptun", "1031")]["price_valid_until"],
                                               rows[("neptun", "1032")]["price_valid_until"], rows[("neptun", "1032")]["member_price_valid_until"],
                                               rows[("setec", "1001")]["price_valid_until"])
      == (iso(1), "", "unknown", iso(2), "unknown"), rows[("neptun", "1032")])
rc, out, err, dt = run(A56 + ["--quiet", "--csv", "-"])
check("valid: no validity columns when no record has the fields", "price_valid_until" not in out.splitlines()[0])
rc, out, err, dt = run(["detail", "https://www.neptun.mk/categories/samsung-galaxy-a56-5g-8-256gb-graphite-1032", "--quiet"],
                       {"FAKE_MEMBER": "neptun:1032=21490", "FAKE_VALID_DETAIL": f"neptun:1032=null/{iso(1)}"})
check("valid: detail line", f"valid: price 23,490 standing (no end date); member price 21,490 until "
      f"{(now + _dt.timedelta(days=1)).strftime('%d.%m.%Y')} 23:59" in out, out)
rc, out, err, dt = run(["match", "https://setec.mk/products/samsung-galaxy-a56-5g-8gb-256gb-graphite-1003", "--quiet"],
                       {"FAKE_VALID": f"neksio:1089={iso(1)}"})
check("valid: match summary says when the cheapest price ends",
      f"cheapest 22,790 MKD at neksio; that price ends {dm(1)} (tomorrow) (range" in out, out[:900])
GV = jfile("gv.json", [grec("ananas", "V1", "VILLAGER VHW 140 Prime", 6690, "8605032617633", brand="Villager", price_valid_until=iso(1)),
                       grec("setec", "V2", "Villager VHW 140 Prime", 6990, "8605032617633", brand="Villager", price_valid_until=None),
                       grec("ddstore", "V3", "Villager VHW 140 Prime", 7290, "8605032617633", brand="Villager")])
rc, d, err, dt = js(["group", GV])
b = d["results"][0]["best"]
check("group: best offer carries its price window", (b["store"], b.get("effective_price_valid_until")) == ("ananas", iso(1)), b)
rc, out, err, dt = run(["group", GV, "--quiet"])
check("group table: VALID next to BEST, offers marked, footnote", out.splitlines()[1].split()[:3] == ["#", "BEST", "VALID"]
      and f"until {dm(1)}" in out.splitlines()[2] and any(l.startswith("VALID: the shop's end date") for l in out.splitlines()), out)
# ==== heuristic gaps from the independent live verification (2026-10-04), end to end ====
# match: a Setec dotted code is also searched by its base code; 'w/Microphone' gives no marketed name
XM5L = "https://setec.mk/products/sony-wh1000xm5lce7-midnight-blue-G301"
rc, d, err, dt = js(["match", XM5L])
ref = d["reference"]
c = by_id(d)
check("dotted code: base code 'WH1000XM5L' searched after 'WH1000XM5L.CE7'",
      [q["query"] for q in ref["queries"]][:2] == ["WH1000XM5L.CE7", "WH1000XM5L"]
      and "WH1000XM5L" in qs("anhoch"), ref["queries"])
check("dotted code: Anhoch's WH1000XM5L found and the same product (model code)",
      c.get("G302", {}).get("match", {}).get("group") == "same" and c["G302"]["match"]["confidence"] == "model",
      c.get("G302", {}).get("match"))
check("dotted code: no marketed-name query from 'w/Microphone'", not ref.get("marketed_name")
      and not any("Microphone" in q["query"] for q in ref["queries"]), ref["queries"])
rc, d, err, dt = js(["match", "https://setec.mk/products/sony-wh1000xm5sce7-platinum-silver-G304"])
check("'Platinum Silver': no 'SONY Platinum' title query", not any("Platinum" in q["query"] for q in d["reference"]["queries"])
      and "WH1000XM5S" in [q["query"] for q in d["reference"]["queries"]], d["reference"]["queries"])
# match: a CPU tier is a variant dimension of a laptop configuration
rc, d, err, dt = js(["match", "ASUS TUF A15 FA507NU Ryzen 7", "--stores", "gjirafa50"])
c = by_id(d)
check("laptop: FA507NU Ryzen 7 same, Ryzen 5 a CPU variant",
      c.get("G402", {}).get("match", {}).get("group") == "same" and c.get("G401", {}).get("match", {}).get("group") == "variant"
      and "CPU Ryzen 5 vs ref Ryzen 7" in c["G401"]["match"]["differs"], [(k, v["match"]) for k, v in c.items()])
rc, out, err, dt = run(["match", "ASUS TUF A15 FA507NU Ryzen 7", "--stores", "gjirafa50", "--quiet"])
check("laptop: the table names the CPU difference", "CPU Ryzen 5 vs ref Ryzen 7" in out, out[:1500])


# group: real listings from the verifier's saved runs (s_all, l_g50, l_zm, s_lap)
def vrec(store, rid, title, price, ean=None, **kw):
    r = {"store": store, "id": rid, "sku": kw.pop("sku", None), "title": title, "url": f"https://{store}.example/p/{rid}",
         "brand": kw.pop("brand", None), "price_mkd": price, "regular_price_mkd": None, "in_stock": True, "stock_note": None,
         "category": kw.pop("category", "Слушалки"), "ean": ean}
    r.update(kw)
    return r


V = jfile("verify.json", {"command": "search", "generated_at": "2026-10-03T23:13:00+02:00",
                          "stores": {k: {"status": "ok"} for k in ("setec", "neptun", "ananas", "anhoch", "tehnomarket", "gjirafa50", "zirafamall")},
                          "results": [
    vrec("setec", "33705", "SONY WHCH520B.CE7 ( Black )", 2090, "4548736142374", brand="SONY", attributes={"Боја": "Црна"}),
    vrec("neptun", "84100326", "BT слушалки SONY BT WH-CH520B", 2899, "4548736142374", brand="SONY"),
    vrec("ananas", "E673PK7ISZ", "Sony Слушалки bt wh-ch520b", 2990, "4548736142374", brand="Sony", attributes={"Color": "Темно сива"}),
    vrec("anhoch", "599879057", "Headphones Sony WH-CH520B Bluetooth Black", 2080, brand="Sony"),
    vrec("tehnomarket", "29396426", "Sony WH-CH520B Bluetooth Headphones Black", 2399, brand="Sony"),
    vrec("setec", "33782", "SONY WHCH520W.CE7 ( White )", 2090, "4548736142817", brand="SONY", attributes={"Боја": "Бела"}),
    vrec("neptun", "84103074", "Слушалки SONY BT WH-CH520W, 20Hz-20,000Hz", 2899, "4548736142817", brand="SONY"),
    vrec("anhoch", "599879058", "Headphones Sony WH-CH520W Bluetooth White", 2080, brand="Sony"),
    vrec("gjirafa50", "g1", "Kufje Gembird MHS-U-001, të zeza", 1590, sku="606820mo", brand="Gembird"),
    vrec("zirafamall", "z1", "Главен цилиндар за сопирачки за Forte", 1590, sku="606820mo", seller="Basics from GjirafaMall"),
    vrec("zirafamall", "z2", "Резервни перничиња за слушалки ISK HD9999, мек материјал, за долготрајна употреба", 1490),
    vrec("gjirafa50", "g3", "Слушалки ISK HD-9999, црни", 6890, sku="977733mo"),
    vrec("zirafamall", "z3", "Слушалки ISK HD-9999, црни", 6890, sku="977733mo"),
    vrec("zirafamall", "z4", "Слушалки Rode NTH-100, црни", 11890),
    vrec("zirafamall", "z5", "Слушалки Rode NTH-100M, професионални со микрофон, over-ear, црни", 14390),
    vrec("gjirafa50", "l5", 'Laptop за гејминг ASUS TUF A15 FA507NU, 15.6", Ryzen 5, 16GB, 512GB, RTX 4050, црн', 56090,
         sku="14133777mo", brand="ASUS", category="Лаптопи"),
    vrec("gjirafa50", "l7", 'Laptop за гејминг ASUS TUF Gaming A15 FA507NU, 15.6", Ryzen 7, RTX 4050, црн', 69890,
         sku="MOBASUNOTBAJEa", brand="ASUS", category="Лаптопи")]})
rc, d, err, dt = js(["group", V])
prods = {frozenset(o["id"] for o in p["offers"]): p for p in d["results"]}
check("group e2e: WH-CH520B across EAN, colour-less Neptun, 'dark grey' Ananas and code-only shops is one row",
      frozenset({"33705", "84100326", "E673PK7ISZ", "599879057", "29396426"}) in prods, [sorted(k) for k in prods])
check("group e2e: WH-CH520W (Neptun names no colour) is one row", frozenset({"33782", "84103074", "599879058"}) in prods,
      [sorted(k) for k in prods])
check("group e2e: the Forte twin, the ear pads, NTH-100M and the Ryzen 5 / 7 configurations stay apart",
      all(frozenset([x]) in prods for x in ("g1", "z1", "z2", "z4", "z5", "l5", "l7")) and frozenset({"g3", "z3"}) in prods,
      [sorted(k) for k in prods])
z1 = next(o for p in d["results"] for o in p["offers"] if o["id"] == "z1")
check("group e2e: the refused twin is no mirror", z1["mirror_of"] is None and d["count"] == 10, (z1["mirror_of"], d["count"]))
rel = [x["why_apart"] for x in d["related"]]
check("group e2e: refused twin and accessory listed as RELATED leads",
      any(w.startswith("same Gjirafa SKU 606820mo, but the titles disagree") for w in rel)
      and any(w.startswith(("one is an accessory", "prices ")) for w in rel), rel)
rc, out, err, dt = run(["group", V, "--quiet"])
check("group e2e table: RELATED lines name the twin and the accessory", "same Gjirafa SKU 606820mo" in out
      and ("one is an accessory" in out or "over 3x apart" in out), out[-1500:])


# ==== tables next to saved JSON, relevance counts, compact categories, search-only offers (2026-10-09) ====
P = os.path.join(_OUT, "sweep.json")
rc, out, err, dt = run(["search", "galaxy a56", "--stores", "ananas,setec", "--json", P])
check("search --json PATH: table, 'wrote' line and footer on stdout",
      rc == 0 and "PRICE" in out and "wrote 13 results to" in out and out.rstrip().splitlines()[-1].startswith("-- "), out[:600])
check("status table shows the all-words and accessories counts", "all-words" in err and "accessories" in err, err[-600:])
d = json.load(open(P, encoding="utf-8"))
check("search JSON: per-shop relevance counts",
      d["stores"]["ananas"]["relevance"] == {"hits": 9, "all_words": 9, "accessories": 0}, d["stores"]["ananas"].get("relevance"))
rc, out, err, dt = run(["search", "galaxy a56", "--stores", "ananas,setec", "--json", P, "--no-table"])
check("search --no-table: only the 'wrote' line and the footer",
      rc == 0 and "PRICE" not in out and out.splitlines()[0].startswith("wrote "), out[:300])
rc, out, err, dt = run(["detail", "https://setec.mk/products/sony-whult900nbce7-G101", "--json", os.path.join(_OUT, "d.json")])
check("detail --json PATH prints the record too", rc == 0 and "WHULT900NB" in out and "wrote 1 records to" in out, out[:400])
rc, out, err, dt = run(["categories", "--grep", "телефон|phone"])
check("categories table: no URL or slug column by default",
      rc == 0 and out.splitlines()[0].split() == ["STORE", "COUNT", "ID", "/", "SLUG", "PATH"] and "https://" not in out,
      out[:300])
rc, out, err, dt = run(["categories", "--grep", ".", "--show", "-1"])
check("categories --show -1 prints every category", rc == 0 and "more categories not shown" not in out, out[-300:])
rc, out, err, dt = run(["categories", "--grep", "телефон|phone", "--urls"])
check("categories --urls adds the URL column", rc == 0 and "URL" in out.splitlines()[0] and "https://" in out, out[:300])
rc, out, err, dt = run(["categories", "--grep", ".", "--show", "1"])
first = [ln.split()[0] for ln in out.splitlines()[1:] if ln and not ln.startswith(("...", "--"))]
check("categories --show caps rows per shop and names what it left out",
      rc == 0 and len(first) == len(set(first)) and "more categories not shown" in out, out[-500:])
WALK = jfile("walk.json", {"command": "list", "generated_at": "2026-10-09T10:00:00+02:00", "store": "setec",
                           "category": "monitori", "filters": [], "stores": {"setec": {"status": "ok"}},
                           "results": [grec("setec", "W1", 'AOC Q27G4XF 27" QHD 180Hz', 12990, category="Монитори"),
                                       grec("setec", "W2", 'LG 27GS75Q-B 27" QHD 180Hz', 15990, category="Монитори")]})
SRCH = jfile("srch.json", {"command": "search", "generated_at": "2026-10-09T10:05:00+02:00", "queries": ["monitor 27"],
                           "stores": {"setec": {"status": "ok"}, "ananas": {"status": "ok"}},
                           "results": [grec("setec", "W1", 'AOC Q27G4XF 27" QHD 180Hz', 12990, category="Монитори"),
                                       grec("setec", "S8", 'Држач за монитор 27"', 990, category="Додатоци"),
                                       grec("setec", "S9", 'Samsung Odyssey G5 27" QHD 165Hz LS27CG552', 14990,
                                            category="Гејмерски монитори"),
                                       grec("ananas", "A1", 'MSI MAG 275QF 27" QHD 180Hz', 13990, category="Монитори")]})
rc, d, err, dt = js(["group", WALK, SRCH])
so = {o["id"] for p in d["results"] for o in p["offers"] if o.get("search_only")}
check("group: search hits in a walked shop that no walk holds are search_only (unwalked shops are not)",
      rc == 0 and so == {"S8", "S9"} and d["search_only"] == {"setec": 2} and d["walked"] == ["setec"],
      (so, d.get("search_only"), d.get("walked")))
rc, out, err, dt = run(["group", WALK, SRCH, "--quiet"])
sec = out[out.find("FOUND ONLY BY SEARCH"):] if "FOUND ONLY BY SEARCH" in out else ""
check("group table: FOUND ONLY BY SEARCH lists real products before accessories",
      "setec 2 (1 accessories)" in sec and 0 <= sec.find("Odyssey") < sec.find("Држач"), out[-900:])
check("group footer counts the search-only offers", "found only by search (not in a category walk): 2 offers in setec" in out,
      out[-300:])
rc, d, err, dt = js(["group", WALK, SRCH, "--only-search"])
check("group --only-search keeps only those offers",
      rc == 0 and {o["id"] for p in d["results"] for o in p["offers"]} == {"S8", "S9"}, [p["title"] for p in d["results"]])
rc, out, err, dt = run(["group", SRCH, "--only-search"])
check("group --only-search without a saved list exits 2 and says why", rc == 2 and "needs at least one saved `list`" in err,
      (rc, err[-200:]))
# a saved group keeps its walks: its walk rows stay walk rows, its search-only rows stay search-only
GSAVED = os.path.join(_OUT, "g_saved.json")
rc, out, err, dt = run(["group", WALK, SRCH, "--json", GSAVED, "--no-table", "--quiet"])
WALK2 = jfile("walk2.json", {"command": "list", "generated_at": "2026-10-09T11:00:00+02:00", "store": "setec",
                             "category": "televizori", "filters": [], "stores": {"setec": {"status": "ok"}},
                             "results": [grec("setec", "T1", 'Hisense 55A6Q 55" 4K', 23990, category="Телевизори")]})
rc, d, err, dt = js(["group", GSAVED, WALK2])
so = {o["id"] for p in d["results"] for o in p["offers"] if o.get("search_only")}
check("group of a saved group: its walk rows are not gaps, its gaps stay gaps", rc == 0 and so == {"S8", "S9"}, so)
rc, d, err, dt = js(["group", GSAVED, "--only-search"])
check("group --only-search on a saved group alone works", rc == 0 and d["count"] >= 1, (rc, err[-200:]))
# a failed walk walked nothing; an older list envelope without "store" still counts
BAD = jfile("walk_bad.json", {"command": "list", "store": "setec", "category": "monitori",
                              "stores": {"setec": {"status": "blocked"}}, "results": []})
rc, d, err, dt = js(["group", BAD, SRCH])
check("group: a blocked walk does not turn search hits into gaps", rc == 0 and d["walked"] == [] and d["search_only"] == {},
      (d.get("walked"), d.get("search_only")))
OLD = jfile("walk_old.json", {"command": "list", "category": "monitori", "stores": {"setec": {"status": "ok"}},
                              "results": [grec("setec", "W1", 'AOC Q27G4XF 27" QHD 180Hz', 12990, category="Монитори")]})
rc, d, err, dt = js(["group", OLD, SRCH])
check("group: a list envelope without 'store' still counts as a walk", rc == 0 and d["walked"] == ["setec"], d.get("walked"))
rc, out, err, dt = run(["group", WALK, SRCH, "--max-price", "500", "--quiet"])
check("group: search-only offers removed by filters are reported as such",
      "FOUND ONLY BY SEARCH: 2 offers, all removed by the filters" in out, out[-400:])
rc, out, err, dt = run(["group", WALK, "--quiet"])
check("group of walks only: no FOUND ONLY BY SEARCH line", rc == 0 and "FOUND ONLY BY SEARCH" not in out, out[-300:])
SRCHM = jfile("srch_member.json", {"command": "search", "queries": ["monitor 27"], "stores": {"setec": {"status": "ok"}},
                                   "results": [grec("setec", "S9", 'Samsung Odyssey G5 27" QHD 165Hz LS27CG552', 14990,
                                                    category="Гејмерски монитори", member_price_mkd=13990,
                                                    member_price_condition="Setec club card, free")]})
rc, out, err, dt = run(["group", WALK, SRCHM, "--quiet"])
sec = out[out.find("FOUND ONLY BY SEARCH"):]
check("group: member prices in the search-only table are explained under it",
      "13,990*" in sec and "Setec club card, free" in sec, sec[:900])
rc, out, err, dt = run(["group", WALK, SRCH, "--json", os.path.join(_OUT, "g.json")])
check("group --json PATH prints the tables too", rc == 0 and "PRODUCTS:" in out and "wrote " in out, out[:300])

# stores: every client failing to import exits 1 and names the missing module once (2026-10-09)
NOMOD = _tf.mkdtemp(prefix="mkshop-nomod-")
for _s in ("setec", "anhoch", "neksio", "ddstore", "neptun", "hivetec", "gjirafa", "setra", "ananas", "tehnomarket"):
    with open(os.path.join(NOMOD, _s + ".py"), "w") as _f:
        _f.write("import requests_not_installed_xyz\n")
rc, out, err, dt = run(["stores"], {"MKSHOP_STORES_DIR": NOMOD})
check("stores: no client can start -> exit 1 and one install hint naming the module",
      rc == 1 and err.count("missing Python module(s) requests_not_installed_xyz") == 1 and "MKSHOP_PYTHON" in err,
      (rc, err[-400:]))
rc, out, err, dt = run(["stores"])
check("stores: healthy clients -> exit 0", rc == 0, (rc, err[-300:]))
_sh.rmtree(NOMOD, ignore_errors=True)
SOME = os.path.join(_tf.mkdtemp(prefix="mkshop-somemod-"), "stores")
_sh.copytree(FAKE, SOME, ignore=_sh.ignore_patterns("__pycache__"))
for _s in ("anhoch", "neptun", "ddstore"):   # e.g. beautifulsoup4 missing: only the clients that use it fail
    with open(os.path.join(SOME, _s + ".py"), "w") as _f:
        _f.write("import bs4_not_installed_xyz\n")
rc, out, err, dt = run(["stores"], {"MKSHOP_STORES_DIR": SOME})
check("stores: some clients missing a module -> exit 0, but the install hint names how many",
      rc == 0 and "3 of 11 store clients cannot start: missing Python module(s) bs4_not_installed_xyz" in err, (rc, err[-400:]))
_sh.rmtree(os.path.dirname(SOME), ignore_errors=True)

_sh.rmtree(TMP, ignore_errors=True)
_sh.rmtree(_OUT, ignore_errors=True)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
