"""Offline regression tests for mkshop.py and the store clients, from the fixes made during the
live integration runs (2026-10-03 onwards). Titles are real listings seen in those runs.
Run: python3 -B tests/test_integ_fixes.py"""
import sys
sys.dont_write_bytecode = True
import os as _pos
# The skill under test: this checkout's copy, or MKSHOP_SKILL_DIR (e.g. an installed copy).
SKILL = _pos.environ.get("MKSHOP_SKILL_DIR") or _pos.path.join(
    _pos.path.dirname(_pos.path.dirname(_pos.path.abspath(__file__))), "skills", "mk-tech-shop-search")
sys.path.insert(0, SKILL + "/scripts")
import mkshop as m  # noqa: E402

fails = 0


def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        fails += 1


# Gjirafa50 / ZirafaMall titles carry '55&quot;' and '55\"' literally.
s = m.signature('Телевизор Samsung The Frame 55&quot;, 4K QLED, Smart TV, црн', "Samsung")
check("&quot; parsed as inch, no 'quot' token", s.units.get("inch") == {55} and "quot" not in s.tokens, (s.units, s.tokens))
s = m.signature('Телевизор Samsung Neo QLED QN85F, 55\\", 4K, црн', "Samsung")
check('backslash-quote parsed as inch', s.units.get("inch") == {55}, s.units)

# EAN spellings: UPC products are indexed as 12 digits by Setec/Ananas/Gjirafa.
check("UPC form first", m.ean_query_forms(m.norm_gtin("0740617344790"), "0740617344790") == ["740617344790", "0740617344790"])
check("EAN-13 single form", m.ean_query_forms(m.norm_gtin("5099206129382"), "5099206129382") == ["5099206129382"])
check("EAN-8 form", m.ean_query_forms(m.norm_gtin("96385074"), "96385074")[0] == "96385074")

# Brand-less records key under the brand named in the title, not the first word.
check("model_key brand from title (Albanian lead word)",
      m.model_key({"title": "Maus wireless Logitech MX Master 3S Business, ergonomik, Graphite"})
      == "model:logitech:mx+master+3s:graphite")
check("model_key brand from run vocabulary",
      m.model_key({"title": "Disk SSD Fooxbrand X100 1TB"}, None, {"fooxbrand"}).startswith("model:fooxbrand:"))
check("model_key keeps screen size",
      m.model_key({"title": "Samsung The Frame 55\"", "brand": "Samsung"})
      != m.model_key({"title": "Samsung The Frame 65\"", "brand": "Samsung"}))

# Accessories for a phone are never the phone.
acc_titles = [
    "Hishell Tempered Glass Screen Protector - Apple iPhone 17 Pro",
    "Hishell Protective Case - Apple iPhone 17 Pro GREY (HPC-18 Pro)",
    "Apple iPhone 17 Pro Clear Case with MagSafe",
    "Заштитник за леќи 3MK за Apple iPhone 17 Pro, 1 парче, сина",
    "Маса за телефон 3MK Clear MagCase за Apple iPhone 17 Pro, MagSafe, транспар",
    "Мулесa за телефон 3MK MattCase Pro за Apple iPhone 17 Pro Max, мат, црна",
    "Kllapa Decoded Leather Backcover за Apple iPhone 17, со MagSafe, кожа",
    "Заштитно стакло за Samsung Galaxy S25",
]
for t in acc_titles:
    check(f"accessory: {t[:50]}", m.signature(t, "Hishel").accessory is not None)
not_acc = ["Apple iPhone 17 Pro 256GB Silver", "Samsung Galaxy S25 Ultra 256GB Titanium",
           "Xiaomi Smart Band 9 Black", "Apple Watch Series 10 GPS 46mm", "NZXT H5 Flow RGB Black",
           "Гејмерско кукиште Montech AIR 903", "Monitor Dell P2425H with stand",
           "Таблет Lenovo Tab M11 за деца", "Kindle Paperwhite 16GB"]
for t in not_acc:
    check(f"not accessory: {t}", m.signature(t).accessory is None, m.signature(t).accessory)
ref = m.signature("Apple iPhone 17 Pro 256GB", "Apple")
r = m.score_candidate(ref, m.signature(acc_titles[2], "Apple"), {"store": "setec"})
check("phone ref rejects Apple's own case", r.get("confidence") is None, r)
r = m.score_candidate(ref, m.signature("Apple iPhone 17 Pro 256GB Deep Blue", "Apple"), {"store": "setec"})
check("phone ref accepts the phone", r.get("confidence") == "likely" and r["group"] == "same", r)

# An EAN match is not split by dimensions the reference title does not spell out.
ref = m.signature("SAMSUNG QE55Q7FAAUXXH", "Samsung", "8806097118565")
r = m.score_candidate(ref, m.signature('Телевизор SAMSUNG QE 55 Q7F AAUXXH, 55", 4K QLED', "Samsung", "8806097118565"),
                      {"store": "neptun"})
check("exact match has no open-variant split", r["confidence"] == "exact" and r["variant"] is None, r)

# Warning classification: marker clients vs Setec's keyword style.
check("progress line with 'truncated' is not a warning",
      not m.is_warning_line("gjirafa50", "[gjirafa50] building the category index: menu has 210 categories; "
                                         "fetching 37 truncated group pages (cached for 24 h)"))
check("marker warning kept", m.is_warning_line("anhoch", "WARNING: 'x y': fell back to OR-matching"))
check("setec warning kept", m.is_warning_line("setec", "WARNING: cannot split a window of 1200 products further; only 1000 will come back"))
check("setec truncation kept", m.is_warning_line("setec", "WARNING: 'tv' matched 2588 products but the index returns at most 1000 (relevance-ranked)"))
check("setec tree fallback kept", m.is_warning_line("setec", "WARNING: category tree unavailable (HTTP 502); parents/paths fall back to the index"))
check("setec windowed fetch is progress", not m.is_warning_line("setec", "  2588 products exceed the index's 1000-hit ceiling; fetching in 4 brand/price windows"))
check("setec per-variant count is progress", not m.is_warning_line("setec", "  query 'bosch': 120 match(es), 120 new"))
check("setec retry is progress", not m.is_warning_line("setec", "  HTTP 503 from setec.mk; retrying in 2s"))
check("indented line ignored", not m.is_warning_line("anhoch", "  WARNING: indented"))

# Status note keeps the warning next to a limit message.
note = m._status_note({"message": "hit --limit-per-store 120 for 'x'; more may exist",
                       "warnings": ["WARNING: fell back to OR"]})
check("status note shows message and warning", "more may exist" in note and "fell back" in note, note)

# Free text: brand inferred, typed words searched.
sig = m.signature("Samsung 990 EVO Plus 1TB", "Samsung")
qs = [q for q, _k in m.build_queries({}, sig, "Samsung 990 EVO Plus 1TB", free_text="Samsung 990 EVO Plus 1TB")]
check("free text searched as typed", qs[0] == "Samsung 990 EVO Plus 1TB" and len(qs) >= 2, qs)

# Registry facts
check("tehnomarket has no EAN search", m.REGISTRY["tehnomarket"]["ean_search"] is False)
check("anhoch searches with --all-words", "--all-words" in m.REGISTRY["anhoch"]["search_args"])
check("setec mirror seller on Ananas", any(t == "setec" and m.alnum("Сетек Се од Техника").startswith(p)
                                           for p, t in m.ANANAS_MIRROR_SELLERS))

# --- resumed integration run (after the crash) ---
# The device's own case is not an accessory case; ambiguous 'стакло'/'заштита'
# and 'compatible with iPhone' do not flag a device.
for t in ["Apple Watch Series 10 GPS 46mm Jet Black Aluminium Case with Black Sport Band - M/L",
          "Apple Watch Ultra 3 49mm Black Titanium Case with Black Ocean Band",
          "Apple AirPods Pro 2 with MagSafe Charging Case (USB-C)",
          "Паметен часовник Huawei Watch GT 5 46mm, Stainless Steel case",
          "Електричен котел Tefal KI700830 1.7L стакло",
          "Перална машина Gorenje WNEI84APS 8kg заштита од деца",
          "Smartwatch Xiaomi Watch S4 compatible with iPhone and Android"]:
    check("device, not accessory: " + t[:50], m.signature(t).accessory is None, m.signature(t).accessory)
for t in ["Закалено стакло Hishell iPhone 17 Pro", "Стакло за iPhone 17 Pro",
          "Apple iPhone 17 Pro Silicone Case with MagSafe – Orange",
          "Charger compatible with iPhone 17 20W"]:
    check("still accessory: " + t[:50], m.signature(t).accessory is not None)
# Screen size keys only when no part number already fixes it.
check("model_key: no size when the code carries it",
      m.model_key({"title": "SAMSUNG QE55Q7FAAUXXH", "brand": "Samsung"})
      == m.model_key({"title": "Телевизор Samsung QE55Q7FAAUXXH, 55\", 4K QLED", "brand": "Samsung"}))
check("model_key: no size next to a monitor part number",
      m.model_key({"title": "Monitor Dell P2425H 23.8\"", "brand": "Dell"}) == "model:dell:p2425h")

# Member prices: a lower member_price_mkd (loyalty card / membership) is the
# effective price for ranking, price filters and display, always with its
# condition; --no-member-prices (MEMBER_PRICES = False) reverts to price_mkd.
import io, argparse, csv, os, tempfile  # noqa: E402
COND = "Club card, 100 MKD one-off"


def _cand(store, price, member=None, cond=COND, **kw):
    r = {"store": store, "id": f"{store}-{price}", "price_mkd": price, "member_price_mkd": member, "in_stock": True,
         "title": "Apple iPhone 17 Pro 256GB Silver", "url": "u-" + store, "mirror_of": None,
         "match": {"confidence": "likely", "group": "same", "differs": [], "variant": None}}
    if member and cond:
        r["member_price_condition"] = cond
    r.update(kw)
    return r


def _out(fn, *args):
    buf = io.StringIO()
    fn(*args[:1], buf, *args[1:])
    return buf.getvalue()


m.MEMBER_PRICES = True
check("effective price = lower member price", m.effective_price(_cand("x", 87490, 79990)) == 79990)
check("effective price = price_mkd when the member price is not lower",
      m.effective_price(_cand("x", 79990, 79990)) == 79990 and m.effective_price(_cand("x", 80000, 85000)) == 80000)
check("effective price = price_mkd without a member price", m.effective_price(_cand("x", 5000)) == 5000)
check("unpriced stays unpriced", m.effective_price(_cand("x", None, 4000)) is None)
r = m.clean_record({"title": "t", "price_mkd": "87.490", "member_price_mkd": 79990.0,
                    "member_price_condition": "  Club card,\n 100 MKD one-off "}, "neptun")
check("clean_record adds effective_price_mkd and price_condition",
      r["price_mkd"] == 87490 and r["effective_price_mkd"] == 79990 and r["price_condition"] == COND, r)
r = m.clean_record({"title": "t", "price_mkd": 5000, "member_price_mkd": None}, "setec")
check("no condition without a member price", r["effective_price_mkd"] == 5000 and r["price_condition"] is None, r)
check("error records get no price fields", "effective_price_mkd" not in m.clean_record({"input": "x", "error": "e"}, "neptun"))
check("condition from member_price_name in extra",
      m.member_condition({"extra": {"member_price_name": "Gold"}}) == "Gold loyalty card or membership")
check("generic condition when the client names none", m.member_condition({}) == "loyalty card or membership")
check("clean_record honours --no-member-prices", (setattr(m, "MEMBER_PRICES", False) or True) and
      m.clean_record({"price_mkd": 87490, "member_price_mkd": 79990}, "neptun")
      == {"store": "neptun", "price_mkd": 87490, "member_price_mkd": 79990, "effective_price_mkd": 87490,
          "price_condition": None})
m.MEMBER_PRICES = True

rows = [_cand("anhoch", 82000), _cand("neptun", 87490, 79990), _cand("setec", 80500)]
check("sort by effective price", [r["store"] for r in m.sort_records(rows, "price")] == ["neptun", "setec", "anhoch"])
check("sort by store, then effective price",
      [r["id"] for r in m.sort_records([_cand("neptun", 1000), _cand("neptun", 1200, 900)], "store")]
      == ["neptun-1200", "neptun-1000"])
tie = [_cand("neptun", 87490, 80000, title="A"), _cand("setec", 80000, title="B")]
check("equal effective prices: the offer without a member price first",
      [r["store"] for r in m.sort_records(tie, "price")] == ["setec", "neptun"])
check("equal effective prices: 'cheapest' names the offer without a member price",
      m._group_prices(tie).startswith(", cheapest 80,000 MKD at setec") and "that is a member price" not in m._group_prices(tie),
      m._group_prices(tie))
m.MEMBER_PRICES = False
check("--no-member-prices sorts by price_mkd",
      [r["store"] for r in m.sort_records(rows, "price")] == ["setec", "anhoch", "neptun"])
m.MEMBER_PRICES = True


def _filt(lo, hi, recs):
    return m.filter_records(recs, argparse.Namespace(in_stock=False, strict_stock=False, min_price=lo, max_price=hi), {})


recs = lambda: [_cand("neptun", 87490, 79990), _cand("setec", 90000)]
kept, dropped = _filt(None, 80000, recs())
check("--max-price keeps a member price within it", [r["store"] for r in kept] == ["neptun"]
      and dropped == {"above --max-price": 1}, dropped)
kept, dropped = _filt(80000, None, recs())
check("--min-price uses the member price, notes the non-member price within it", [r["store"] for r in kept] == ["setec"]
      and dropped == {"below --min-price (non-member price within it)": 1}, dropped)
kept, dropped = _filt(80000, 85000, recs())
check("--min-price note only when the non-member price is inside the whole range", kept == []
      and dropped == {"below --min-price": 1, "above --max-price": 1}, dropped)
kept, dropped = _filt(80000, None, [_cand("anhoch", 70000), _cand("neptun", 79000, 75000)])
check("--min-price: plain note without a member price or when both prices are below", kept == []
      and dropped == {"below --min-price": 2}, dropped)
m.MEMBER_PRICES = False
kept, dropped = _filt(None, 80000, recs())
check("--no-member-prices: --max-price on price_mkd, notes the member price within it", kept == []
      and dropped == {"above --max-price (member price within it)": 1, "above --max-price": 1}, dropped)
kept, dropped = _filt(80000, None, recs())
check("--no-member-prices: --min-price on price_mkd", len(kept) == 2 and dropped == {}, dropped)
kept, dropped = _filt(80000, 85000, [_cand("neptun", 87490, 79990)])
check("--no-member-prices: no member note when the member price is below --min-price",
      dropped == {"above --max-price": 1}, dropped)
kept, dropped = _filt(80000, None, [_cand("neptun", 79000, 75000)])
check("--no-member-prices: no non-member note (that note is for the default mode)",
      dropped == {"below --min-price": 1}, dropped)
m.MEMBER_PRICES = True

rows = m.sort_records([_cand("anhoch", 82000), _cand("neptun", 87490, 79990)], "price")
txt = _out(m.print_records, rows, 0, 40)
head = txt.splitlines()[0]
check("table: effective price marked '*', NON-MEMBER column", "79,990*" in txt and "NON-MEMBER" in head
      and head.index("PRICE") < head.index("NON-MEMBER") < head.index("STORE"), txt)
check("table: price without the member price shown", "87,490" in txt.splitlines()[1], txt)
check("table: footnote explains '*' and names the condition",
      txt.splitlines()[-1].startswith("* member price") and "neptun: " + COND in txt.splitlines()[-1], txt)
txt = _out(m.print_records, [_cand("anhoch", 82000), _cand("setec", 80500)], 0, 40)
check("table without member prices: no marker, column or footnote",
      "*" not in txt and "MEMBER" not in txt and len(txt.splitlines()) == 3, txt)
m.MEMBER_PRICES = False
txt = _out(m.print_records, m.sort_records([_cand("anhoch", 82000), _cand("neptun", 87490, 79990)], "price"), 0, 40)
check("--no-member-prices table: PRICE = price_mkd, MEMBER column, no marker",
      "*" not in txt.splitlines()[2] and txt.splitlines()[2].lstrip().startswith("87,490")
      and " MEMBER " in txt.splitlines()[0] and "NON-MEMBER" not in txt
      and txt.splitlines()[-1].startswith("MEMBER:") and COND in txt.splitlines()[-1], txt)
m.MEMBER_PRICES = True

path = os.path.join(tempfile.mkdtemp(), "x.csv")
m.write_csv([m.clean_record(_cand("neptun", 87490, 79990), "neptun"), m.clean_record(_cand("setec", 80500), "setec")], path)
with open(path, encoding="utf-8") as f:
    got = list(csv.DictReader(f))
check("CSV has the four price fields", {"price_mkd", "member_price_mkd", "effective_price_mkd", "price_condition"}
      <= set(got[0]), list(got[0]))
check("CSV values", (got[0]["effective_price_mkd"], got[0]["price_mkd"], got[0]["member_price_mkd"],
                     got[0]["price_condition"]) == ("79990", "87490", "79990", COND)
      and (got[1]["effective_price_mkd"], got[1]["member_price_mkd"], got[1]["price_condition"]) == ("80500", "", ""), got)

ref_rec = m.clean_record(_cand("neptun", 87490, 79990), "neptun")
env = {"reference": {"kind": "url", "input": "x", "title": "Apple iPhone 17 Pro 256GB", "notes": [], "queries": [],
                     "record": ref_rec},
       "results": [_cand("anhoch", 82000), _cand("neptun", 87490, 79990, id="n2")], "near_misses": [],
       "stores": {"anhoch": {"status": "ok"}, "neptun": {"status": "ok"}}}
ns = argparse.Namespace(title_width=40, near=False)
txt = _out(m.print_match, env, ns)
check("match reference line: effective price with condition and the price without it",
      f"neptun 79,990 MKD member price ({COND}); without it 87,490 MKD" in txt, txt)
check("match summary: cheapest is the member price and says so",
      "cheapest 79,990 MKD at neptun (range 79,990-82,000, member prices included); "
      f"that is a member price ({COND}); without one: 82,000 MKD at anhoch" in txt, txt)
check("match table: marker, NON-MEMBER column, footnote",
      "79,990*" in txt and "NON-MEMBER" in txt and f"neptun: {COND}" in txt, txt)
txt = _out(m.print_match, dict(env, results=[_cand("anhoch", 82000), _cand("setec", 80500)]), ns)
check("match summary without member offers unchanged",
      "cheapest 80,500 MKD at setec (range 80,500-82,000)\n" in txt and "NON-MEMBER" not in txt, txt)
m.MEMBER_PRICES = False
txt = _out(m.print_match, env, ns)
check("--no-member-prices match: reference on price_mkd, member price noted",
      f"neptun 87,490 MKD; member price 79,990 ({COND})" in txt, txt)
check("--no-member-prices match: summary on price_mkd, cheaper member price noted",
      "cheapest 82,000 MKD at anhoch (range 82,000-87,490); "
      f"79,990 MKD at neptun with a member price ({COND}; MEMBER column)" in txt and "NON-MEMBER" not in txt, txt)
m.MEMBER_PRICES = True


# Match tables: price columns and the member footnote per section, so a
# member column shows exactly where its footnote explains one.
def _section(txt, title):
    """Lines of one match section, from its title to the next blank or footer line."""
    lines = txt.splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith(title))
    j = next((j for j in range(i + 1, len(lines)) if not lines[j].strip() or lines[j].startswith("-- ")), len(lines))
    return lines[i:j]


def _header(sec):
    return next(l for l in sec if l.split()[:2] in (["CONF", "PRICE"], ["PRICE", "NON-MEMBER"], ["PRICE", "MEMBER"],
                                                   ["PRICE", "STORE"]))


_var = lambda *a, **kw: _cand(*a, match={"confidence": "likely", "group": "variant", "differs": ["colour"],
                                         "variant": None}, **kw)
_miss = lambda *a, **kw: _cand(*a, match={"confidence": None, "why_not": "model token(s) Pro missing"}, **kw)
env3 = dict(env, results=[_cand("anhoch", 82000, regular_price_mkd=85000), _var("neptun", 87490, 79990)])
txt = _out(m.print_match, env3, ns)
same_s, var_s = _section(txt, "SAME PRODUCT"), _section(txt, "VARIANTS")
check("match: SAME PRODUCT table has no member column when only variants have member prices",
      "MEMBER" not in _header(same_s) and "WAS" in _header(same_s) and not any("*" in l for l in same_s), same_s)
check("match: VARIANTS table has its own NON-MEMBER column and the footnote under it",
      "NON-MEMBER" in _header(var_s) and any("79,990*" in l and "87,490" in l for l in var_s)
      and var_s[-1].startswith("* member price (needs") and f"neptun: {COND}" in var_s[-1], var_s)
check("match: no footnote under a section without member prices", not any("member" in l.lower() for l in same_s), same_s)
m.MEMBER_PRICES = False
txt = _out(m.print_match, env3, ns)
same_s, var_s = _section(txt, "SAME PRODUCT"), _section(txt, "VARIANTS")
check("--no-member-prices match: MEMBER column only in the table that needs it",
      "MEMBER" not in _header(same_s) and " MEMBER " in _header(var_s) and var_s[-1].startswith("MEMBER:"), txt)
m.MEMBER_PRICES = True

nsn = argparse.Namespace(title_width=40, near=True)
env4 = dict(env, results=[_cand("anhoch", 82000)], near_misses=[_miss("setec", 60000, 55000, regular_price_mkd=65000)])
txt = _out(m.print_match, env4, nsn)
near_s = _section(txt, "NEAR MISSES")
check("match --near: near-miss member price shown with NON-MEMBER, no WAS",
      "NON-MEMBER" in _header(near_s) and "WAS" not in _header(near_s)
      and any("55,000*" in l and "60,000" in l and "65,000" not in l for l in near_s), near_s)
check("match --near: footnote under the near misses names their condition, none under SAME PRODUCT",
      near_s[-1].startswith("* member price (needs") and f"setec: {COND}" in near_s[-1]
      and not any("member" in l.lower() for l in _section(txt, "SAME PRODUCT")), txt)
m.MEMBER_PRICES = False
txt = _out(m.print_match, env4, nsn)
near_s = _section(txt, "NEAR MISSES")
check("--no-member-prices match --near: MEMBER column next to PRICE, footnote says MEMBER",
      _header(near_s).split()[:2] == ["PRICE", "MEMBER"] and any(l.lstrip().startswith("60,000  55,000") for l in near_s)
      and near_s[-1].startswith("MEMBER:"), near_s)
m.MEMBER_PRICES = True
txt = _out(m.print_match, dict(env4, near_misses=[_miss("setec", 60000)]), nsn)
check("match --near without member prices: PRICE only, no footnote",
      _header(_section(txt, "NEAR MISSES")).split()[:2] == ["PRICE", "STORE"]
      and not any(l.startswith("* member") for l in txt.splitlines()), txt)

env5 = dict(env, results=[_cand("neptun", 87490, 79990), _cand("setra", 80000), _var("neptun", 90000, 85000)],
            near_misses=[_miss("setec", 60000, 55000, member_price_condition="Setec card")])
txt = _out(m.print_match, env5, nsn)
notes = [l for l in txt.splitlines() if l.startswith("* member price")]
check("match: one footnote per section with member prices, full text once",
      len(notes) == 3 and notes[0].startswith("* member price (needs")
      and all(n.startswith("* member price; NON-MEMBER is the price without it (see the note above). Conditions: ")
              for n in notes[1:]), notes)
check("match: each footnote lists its own section's conditions",
      notes[0].endswith(f"neptun: {COND}") and notes[1].endswith(f"neptun: {COND}")
      and notes[2].endswith("setec: Setec card"), notes)
check("match: every section with a footnote shows a NON-MEMBER column",
      all("NON-MEMBER" in _header(_section(txt, t)) for t in ("SAME PRODUCT", "VARIANTS", "NEAR MISSES")), txt)

# Neptun's card name: DiscountPriceName is 'HaPPy' in category listings and
# 'haPPy' in search/detail; the condition text must be the same everywhere.
sys.path.insert(0, SKILL + "/scripts/stores")
import neptun as nep  # noqa: E402
check("neptun card name normalised", [nep.card_name({"DiscountPriceName": v}) for v in
                                      ("HaPPy", "haPPy", "  HAPPY ", None, "")] == ["haPPy"] * 5)
check("neptun card name keeps other words and other names",
      nep.card_name({"DiscountPriceName": "HaPPy  цена"}) == "haPPy цена"
      and nep.card_name({"DiscountPriceName": "Gold"}) == "Gold")
N = object.__new__(nep.Neptun)
P = {"Id": 7, "Title": "TV X (UE55X1)", "ShortTitle": "TV X", "Url": "tv-x", "RegularPrice": 9999,
     "DiscountPrice": 7999, "DiscountPriceType": 3}
lst = N.listing_record(dict(P, DiscountPriceName="HaPPy"))
srch = N.search_record(dict(P, DiscountPriceName="haPPy"), {})
det = N.detail_record(dict(P, DiscountPriceName="HaPPy"), [], "7")
want = f"haPPy loyalty card, {nep.MEMBER_CARD_TERMS}"
check("neptun condition identical from listing, search and detail",
      lst["member_price_condition"] == srch["member_price_condition"] == det["member_price_condition"] == want
      and lst["member_price_mkd"] == 7999, (lst["member_price_condition"], srch["member_price_condition"]))
check("neptun detail extra.member_price_name normalised", det["extra"]["member_price_name"] == "haPPy", det["extra"])
check("neptun: same condition through mkshop for every path",
      len({m.clean_record(dict(r), "neptun")["price_condition"] for r in (lst, srch, det)}) == 1)
check("neptun: no condition without a member price",
      N.listing_record(dict(P, DiscountPrice=0, DiscountPriceName="HaPPy"))["member_price_condition"] is None)

# Displayed titles lose Gjirafa's escaping; ordinary ampersands stay.
check("record title &quot; unescaped",
      m.clean_record({"title": "Телевизор Samsung The Frame 55&quot;, 4K QLED"}, "gjirafa50")["title"]
      == 'Телевизор Samsung The Frame 55", 4K QLED')
check("record title backslash-quote unescaped",
      m.clean_record({"title": 'Samsung Neo QLED QN85F, 55\\", 4K'}, "zirafamall")["title"] == 'Samsung Neo QLED QN85F, 55", 4K')
check("record title plain ampersand kept",
      m.clean_record({"title": "Barnes & Noble AT&T"}, "ananas")["title"] == "Barnes & Noble AT&T")

# Neptun's spaced Samsung codes ('UE 55 U8072H UXXH'): the 43"/50"/65" of the
# series are variants (part number differs), the 55" is the same product.
ref = m.signature("SAMSUNG UE 55 U8072H UXXH", "SAMSUNG")
r43 = m.score_candidate(ref, m.signature('SAMSUNG UE-43U8072HUXXH CRYSTAL 4K 43" SMART LED TV', "Samsung"), {})
r55 = m.score_candidate(ref, m.signature('SAMSUNG UE-55U8072HUXXH CRYSTAL 4K 55" SMART LED TV', "Samsung"), {})
check("spaced code: 43in sibling is a variant", r43.get("group") == "variant", r43)
check("spaced code: 55in is the same product", r55.get("confidence") == "model" and r55.get("group") == "same", r55)
check("spaced code keys like the glued one",
      m.model_key({"title": "Телевизор SAMSUNG QE 55 Q7F AAUXXH, 55\", 4K QLED", "brand": "SAMSUNG"})
      == m.model_key({"title": "SAMSUNG QE55Q7FAAUXXH", "brand": "Samsung"}) == "model:samsung:qe55q7faauxxh")
check("no glue from a plain word", "qe55q7fsmart" not in m.signature("Samsung QE 55 Q7F Smart").strong)

check("Samsung TV code states the size", m.signature("SAMSUNG QE55Q7FAAUXXH", "Samsung").units.get("inch") == {55})
check("size from code: 43in sibling differs in size",
      any("size 43" in d for d in r43.get("differs", [])), r43)
check("no size inferred off-brand", "inch" not in m.signature("Kingston UE55U8072 cable").units)

check("series name is not a size (QN90D)", "inch" not in m.signature("Samsung QN90D Neo QLED", "Samsung").units)

# SSD read speed / curvature are specs, not model codes (Neptun's NV3 title
# made match search every shop for '6000R').
sig = m.signature("SSD M.2 Kingston NV3 NVMe 1TB PCIe Gen 4x4 6000R/4000W", "KINGSTON")
check("6000R is not a model code", "6000r" not in sig.strong and
      all(q != "6000R" for q, _k in m.build_queries({}, sig, "x")), sig.strong)
check("1000R curvature is not a model code",
      m.signature('Monitor Samsung Odyssey G5 27" 1000R LS27CG552EUXEN', "Samsung").strong == {"ls27cg552euxen"})

# ==== store clients: price validity windows (price_valid_until / member_price_valid_until) ====
# Separate section (store-client owner, 2026-10-03). ISO 8601 with the Europe/Skopje offset;
# null = the source gives no end (standing price); key absent = the record cannot tell.
# Fake payloads use 2099 dates; real shapes are from live Ananas / Neptun / Setec / Anhoch data.
import os as _os, subprocess as _sp  # noqa: E401,E402
_STORES = SKILL + "/scripts/stores"
if _STORES not in sys.path:
    sys.path.insert(0, _STORES)
import ananas as ana  # noqa: E402
import anhoch as anh  # noqa: E402
import neptun as nep  # noqa: E402
import setec as sec  # noqa: E402

_ISO_CASES = [
    ("2026-10-15T23:00", {}, "2026-10-15T23:00:00+02:00"),            # Ananas priceV2.dateTo (CEST)
    ("2026-10-30T23:59:59", {}, "2026-10-30T23:59:59+01:00"),         # after the 25 Oct DST switch
    ("2026-10-15T23:00:00.000000", {}, "2026-10-15T23:00:00+02:00"),  # Algolia discountEndTime
    ("/Date(1791151140000)/", {}, "2026-10-04T23:59:00+02:00"),       # Neptun promotion ValidTo
    ("/Date(-62135596800000)/", {}, None),                            # .NET 0001-01-01 placeholder
    ("0001-01-01T00:00:00", {}, None),
    ("2026-12-31T22:59:00.000Z", {}, "2026-12-31T23:59:00+01:00"),    # Medusa ends_at (UTC)
    ("2026-10-04T23:59:59+0200", {}, "2026-10-04T23:59:59+02:00"),
    ("2026-10-31", {}, "2026-10-31T23:59:59+01:00"),                  # bare date = end of that day
    ("4.10.2026.", {}, "2026-10-04T23:59:59+02:00"),
    ("04.10.2026 15:30", {}, "2026-10-04T15:30:00+02:00"),
    ("2099-07-01 10:00:00", {"naive_is_utc": True}, "2099-07-01T12:00:00+02:00"),
    (1791151140, {}, "2026-10-04T23:59:00+02:00"),
    ("", {}, None), (None, {}, None), ("garbage", {}, None), ("2026-02-30", {}, None),
]
for _mod in (ana, anh, nep, sec):
    _bad = [(v, kw, _mod.skopje_iso(v, **kw), want) for v, kw, want in _ISO_CASES
            if _mod.skopje_iso(v, **kw) != want]
    check(f"{_mod.__name__}: skopje_iso parses every shop format", not _bad, _bad)
_zi = sys.modules.get("zoneinfo")
sys.modules["zoneinfo"] = None          # no tzdata: the EU-rule fallback must agree
try:
    _bad = [(v, ana.skopje_iso(v, **kw), want) for v, kw, want in _ISO_CASES if ana.skopje_iso(v, **kw) != want]
    check("skopje_iso: EU DST fallback without zoneinfo", not _bad, _bad)
finally:
    if _zi is None:
        del sys.modules["zoneinfo"]
    else:
        sys.modules["zoneinfo"] = _zi
check("skopje_iso: identical helper in every client",
      len({_mod.skopje_iso.__code__.co_code for _mod in (ana, anh, nep, sec)}) == 1)

# Ananas listing: Algolia discountEndTime only while onSale; absent when a sale hit has none.
_A = object.__new__(ana.Ananas)
_H = {"objectID": "5418872", "price": 6690.0, "basePrice": 13000.0, "onSale": True, "discountType": "SALE",
      "merchant": {"id": 776, "displayName": "AНАНАС ШОП"}, "product": {"name": "VILLAGER VHW 140", "slug": "v"}}
_r = _A.hit_to_record(dict(_H, discountEndTime="2099-10-04T23:00:00.000000"))
check("ananas listing: sale window from the index", _r.get("price_valid_until") == "2099-10-04T23:00:00+02:00", _r)
check("ananas listing: sale hit without a window -> key absent",
      "price_valid_until" not in _A.hit_to_record(dict(_H)))
check("ananas listing: stale window on a hit no longer on sale (938098) -> null",
      _A.hit_to_record(dict(_H, onSale=False, discountEndTime="2099-10-05T15:00:00.000000"))
      .get("price_valid_until", "ABSENT") is None)
check("ananas listing: undiscounted price -> null",
      _A.hit_to_record(dict(_H, price=13000.0, onSale=False)).get("price_valid_until", "ABSENT") is None)
# Ananas detail: priceV2.dateTo of a running SALE; kept when already past (548835 lagged a day).
_PV = {"discountType": "SALE", "dateFrom": "2026-10-01T00:00", "dateTo": "2099-10-30T23:59:59"}
check("ananas detail: priceV2.dateTo", ana.Ananas.sale_valid_until(_PV, 8490, 13890) == "2099-10-30T23:59:59+01:00")
check("ananas detail: lapsed window kept",
      ana.Ananas.sale_valid_until(dict(_PV, dateTo="2026-10-02T23:59:59"), 2974, 3499) == "2026-10-02T23:59:59+02:00")
check("ananas detail: standing discounted price (no discountType) -> null",
      ana.Ananas.sale_valid_until({"discountType": None, "dateTo": None}, 2690, 4290) is None)
check("ananas detail: no markdown -> null", ana.Ananas.sale_valid_until(_PV, 4290, 4290) is None)

# Neptun: the haPPy price's window is the promotion GetProduct names in PromotionId.
_HW = {"Id": 4343, "PromotionName": "M20260395 | INSTORE #01 HAPPY WEEKS #2 48-0 na se 21.09-04.10.2026",
       "ValidFrom": "/Date(1789941600000)/", "ValidTo": "/Date(4083947940000)/"}       # 2099-05-31
_WAR = {"Id": -3245, "PromotionName": "M20260832 | #02 Samsung TV 2+3YW", "CustomPromotionName": "Samsung TV 2+3YW",
        "ValidFrom": "/Date(1787566320000)/", "ValidTo": "/Date(4102441140000)/"}      # 2099-12-31
_ND = dict(P, DiscountPriceName="haPPy", DiscountPriceType=3, PromotionId=4343, WebPromotionId=0,
           Promotions=[_WAR, _HW])
_d = N.detail_record(dict(_ND), [], "7")
check("neptun detail: member window = PromotionId's ValidTo",
      _d.get("member_price_valid_until") == "2099-05-31T23:59:00+02:00" and _d.get("price_valid_until", "ABSENT") is None, _d)
check("neptun detail: every promotion window in extra",
      [w["id"] for w in _d["extra"]["promotion_windows"]] == [-3245, 4343]
      and _d["extra"]["promotion_windows"][0]["valid_to"] == "2099-12-31T23:59:00+01:00", _d["extra"])
check("neptun detail: PromotionId 0 with promotions -> member window left out (shop does not say which: unknown)",
      N.detail_record(dict(_ND, PromotionId=0), [], "7").get("member_price_valid_until", "ABSENT") == "ABSENT")
check("neptun detail: type-4 haPPy price without promotions -> null",
      N.detail_record(dict(P, DiscountPriceName="haPPy", DiscountPriceType=4, PromotionId=0), [], "7")
      .get("member_price_valid_until", "ABSENT") is None)
_nd = N.detail_record(dict(P, DiscountPrice=0, DiscountPriceType=0, DiscountPriceName="Цена"), [], "7")
check("neptun detail: no member price -> no member key, regular price null",
      "member_price_valid_until" not in _nd and _nd.get("price_valid_until", "ABSENT") is None, _nd)
_wd = N.detail_record(dict(P, DiscountPrice=0, DiscountPriceType=0, WebshopDiscountPrice=8999, WebPromotionId=77,
                           Promotions=[dict(_HW, Id=77)]), [], "7")
check("neptun detail: online price window = WebPromotionId's ValidTo",
      _wd["price_mkd"] == 8999 and _wd.get("price_valid_until") == "2099-05-31T23:59:00+02:00", _wd)
_l = N.listing_record(dict(P, DiscountPriceName="HaPPy"))
check("neptun listing: regular price null, member window absent (listings have no promotion data)",
      _l.get("price_valid_until", "ABSENT") is None and "member_price_valid_until" not in _l, _l)

# Setec: ends_at of the Medusa price list behind price_mkd.
_WEB = {"price_list": {"id": "plist_WEB", "title": "Web Prices", "role": "club", "starts_at": None, "ends_at": None},
        "prices": {"amount": "1409"}}
_PROMO = {"price_list": {"id": "plist_PROMO", "title": "Promo", "role": "promo", "starts_at": "2099-10-01T00:00:00.000Z",
                         "ends_at": "2099-10-31T22:59:59.000Z"}, "prices": {"amount": "999"}}
check("setec: standing Web Prices list -> null", sec.price_list_window([_WEB], 1409, "plist_WEB") is None)
check("setec: promo list by price_list_id -> its ends_at",
      sec.price_list_window([_WEB, _PROMO], 999, "plist_PROMO") == "2099-10-31T23:59:59+01:00")
check("setec: detail API (no ids) matches the list by amount",
      sec.price_list_window([{k: (dict(v, id=None) if k == "price_list" else v) for k, v in x.items()}
                             for x in (_WEB, _PROMO)], 999) == "2099-10-31T23:59:59+01:00")
check("setec: ambiguous amount match -> unknown (key left out)",
      sec.price_list_window([dict(_PROMO), {"price_list": {"ends_at": "2099-11-30T22:59:59Z"}, "prices": {"amount": 999}}],
                            999) is sec.NO_WINDOW)
_sh = {"id": "prod_1", "title": "X", "handle": "x", "price_lists": [_WEB, _PROMO], "total_web_quantity": 5,
       "variants": [{"calculated_price": {"calculated_amount": 999, "original_amount": 1499,
                                          "calculated_price": {"price_list_id": "plist_PROMO"}}}]}
check("setec listing record carries the window",
      sec.record_from_hit(_sh, 3).get("price_valid_until") == "2099-10-31T23:59:59+01:00")

# Anhoch: FleetCart special_price_end (a day; the special runs through its end in Skopje).
_AN = object.__new__(anh.Anhoch)
_M = lambda v: {"amount": f"{v}.0000"}  # noqa: E731
_row = {"id": 1, "name": "JBL X", "slug": "jbl-x", "price": _M(2490), "selling_price": _M(1990),
        "special_price": _M(1990), "special_price_type": None, "special_price_start": None,
        "special_price_end": None, "is_in_stock": True, "qty": 10, "manage_stock": True}
_lr = lambda **kw: _AN.listing_record(dict(_row, **kw))  # noqa: E731
check("anhoch: open-ended special -> null (key present)",
      "price_valid_until" in _lr() and _lr()["price_valid_until"] is None and _lr()["regular_price_mkd"] == 2490)
check("anhoch: special_price_end -> end of that day in Skopje",
      _lr(special_price_end="2099-10-31")["price_valid_until"] == "2099-10-31T23:59:59+01:00")
check("anhoch: UTC-serialised local midnight -> same day",
      _lr(special_price_end="2099-07-15 00:00:00")["price_valid_until"]
      == _lr(special_price_end="2099-07-14T22:00:00.000000Z")["price_valid_until"] == "2099-07-15T23:59:59+02:00")
check("anhoch: standing price ignores an end date",
      _lr(selling_price=_M(2490), special_price=None, special_price_end="2099-10-31")["price_valid_until"] is None)
check("anhoch: charged price is not the special amount -> unknown (key left out)",
      "price_valid_until" not in _lr(special_price=_M(2100), special_price_end="2099-07-15"))

# Shops that expose no window leave the key out rather than claiming a standing price.
import neksio as nks  # noqa: E402
_r2 = object.__new__(nks.Neksio).listing_record({
    "productId": 1, "productName": "X", "productCode": "1", "priceWTax": 3990, "old_PriceWTax": "4.720 ден.",
    "isOnSale": True, "quantity": 3, "category": "C", "manufacturer": "M", "barCode": "4895213700344",
    "futureDocumentDate": None})
check("neksio: sale price has no invented window",
      _r2["price_mkd"] == 3990 and "price_valid_until" not in _r2, _r2)

# Running a client must not leave a __pycache__ in the skill.
_pyc = _os.path.join(_STORES, "__pycache__")
_had = _os.path.exists(_pyc)
_env = dict(_os.environ)
_env.pop("PYTHONDONTWRITEBYTECODE", None)
_sp.run([sys.executable, _os.path.join(_STORES, "setec.py"), "info"], capture_output=True, env=_env, timeout=60)
check("setec.py writes no bytecode into the skill", _had or not _os.path.exists(_pyc), _pyc)

# ==== mkshop: group, spaced / composite codes, colours, product types, bare-code names, price windows ====
# Separate section (mkshop.py owner, 2026-10-03). Offline: synthetic records shaped like live listings.
import datetime as _mdt  # noqa: E402
m.MEMBER_PRICES = True
_S = lambda t, b=None: m.signature(t, b)

# Codes written with spaces ('LIVE 770 NC' ~ 770NC) and no codes from ordinary words.
check("spaced code: LIVE 770 NC -> 770nc", "770nc" in _S("JBL LIVE 770 NC BLUE СЛУШАЛКИ", "JBL").derived)
check("spaced code: WH 1000 XM5 -> wh1000xm5", "wh1000xm5" in _S("SONY WH 1000 XM5 Black", "SONY").derived)
check("spaced code: WNEI 84 APS (capitals) -> wnei84aps", "wnei84aps" in _S("Gorenje WNEI 84 APS", "Gorenje").derived)
for t in ["ASUS PRIME GeForce RTX 5070 OC 12GB", "Samsung Galaxy S25 Ultra 12 256 GB", "Apple iPhone 16 Pro MAX 256GB",
          "Power bank 100 Wh", "Xiaomi Redmi Note 14 PRO 8 256", "Kingston FURY Beast 32GB DDR5 6000", "MSI MAG B650 TOMAHAWK WIFI"]:
    check(f"no spaced code from words: {t}", not _S(t).derived, _S(t).derived)
r = m.score_candidate(_S("Sapphire PULSE Radeon RX 7800 XT 16GB", "Sapphire"), _S("Sapphire NITRO+ RX7800XT 16GB", "Sapphire"), {})
check("spaced chip name next to another line name is a variant (PULSE vs NITRO)", r["group"] == "variant"
      and "name" in r["conflicts"], r)
ref = _S("JBL Live 770NC", "JBL")
r = m.score_candidate(ref, m.record_sig({"title": "JBL LIVE 770 NC BLUE СЛУШАЛКИ", "brand": "JBL"}), {})
check("spaced code matches the glued one (model)", r["confidence"] == "model" and any("spaces" in x for x in r["reasons"]), r)
r = m.score_candidate(_S("SONY WH 1000 XM5 Silver", "SONY"), m.record_sig({"title": "Sony WH-1000XM5 Silver", "brand": "Sony"}), {})
check("spaced ref matches the glued listing", r["confidence"] == "model" and r["group"] == "same", r)
r = m.score_candidate(ref, m.record_sig({"title": "JBL Безжични слушалки LIVE770NCBLK", "brand": "JBL"}), {})
check("composite vendor code LIVE770NCBLK = 770NC, colour black", r["confidence"] == "model" and r["variant"] == "colour black", r)
r = m.score_candidate(ref, m.record_sig({"title": "JBL T770NC Wireless Over-Ear Headphones Black", "brand": "JBL"}), {})
check("T770NC (Tune) is not 770NC", r.get("confidence") is None, r)
r = m.score_candidate(_S("ASUS PRIME RTX5070 OC 12GB", "ASUS"), m.record_sig({"title": "ASUS TUF RTX 5070 OC 12GB", "brand": "ASUS"}), {})
check("chip name is no code: PRIME vs TUF RTX 5070 not the same product", r.get("group") != "same", r)
check("derived code query after the typed text, not from a series word",
      [q for q, _k in m.build_queries({}, _S("Samsung 990 EVO Plus 1TB", "Samsung"), "Samsung 990 EVO Plus 1TB",
                                      free_text="Samsung 990 EVO Plus 1TB")][0] == "Samsung 990 EVO Plus 1TB"
      and m.build_queries({"brand": "JBL"}, _S("JBL LIVE 770 NC BLUE", "JBL"), "x")[0] == ("770NC", "model code"))

# Colours: marketing names, modifiers, shop abbreviations, attributes, possible colours.
for t, want in [("JBL Live 770NC Sand", {"sand"}), ("Samsung Galaxy Buds3 Pro Latte", {"latte"}),
                ("Sony ULT WEAR Forest Gray", {"grey"}), ("Sony WH-1000XM5 Smoky Pink", {"pink"}),
                ("SONY WH1000XM5L.CE7 ( Midnight Blue )", {"blue"}), ("BT слушалки JBL LIVE 770 NC WHT", {"white"}),
                ("JBL Tune 520BT песочна", {"sand"}), ("Apple iPhone 16 Pro Natural Titanium", {"titanium"}),
                ("Apple MacBook Air M3 Midnight", {"midnight"}), ("Arctic Freezer 36 CPU Cooler", set()),
                ("Royal Kludge RK61 Hot Swap", set()), ("Philips Hue Bridge Champagne", {"gold"})]:
    check(f"colour: {t}", _S(t).colours == want, _S(t).colours)
check("colour from attributes when the title has none",
      m.record_sig({"title": "SONY WHULT900NB.CE7", "brand": "SONY", "attributes": {"Боја": "Црна"}}).colours == {"black"})
check("possible colour: unknown last word after the model", _S("Headphones JBL Live 770NC ANC Wireless Sandstone").maybe_colour == {"sandstone"})
for t in ["Samsung Galaxy A56 5G 8/256GB Dual SIM", "Слушалки Sony ULT Wear WHULT900N, безжични, со длабок бас",
          "Logitech G413 SE GX Linear", "Gorenje WNEI 84 APS", "JBL Live 770NC Wireless Over-Ear Headphones"]:
    check(f"no possible colour: {t}", not _S(t).maybe_colour, _S(t).maybe_colour)
r = m.score_candidate(_S("JBL Live 770NC Black", "JBL"), m.record_sig({"title": "Headphones JBL Live 770NC ANC Wireless Sandstone", "brand": "JBL"}), {})
check("possible colour vs a stated one -> variant", r["group"] == "variant" and "colour sandstone? vs ref black" in r["differs"], r)
r = m.score_candidate(_S("JBL Live 770NC", "JBL"), m.record_sig({"title": "Headphones JBL Live 770NC ANC Wireless Sandstone", "brand": "JBL"}), {})
check("possible colour, ref open -> same, own sub-group", r["group"] == "same" and r["variant"] == "colour sandstone?", r)
r = m.score_candidate(_S("JBL Live 770NC Black", "JBL"), m.record_sig({"title": "Headphones JBL Live 770NC ANC Wireless Sand", "brand": "JBL"}), {})
check("Sand vs Black -> variant", r["group"] == "variant" and "colour sand vs ref black" in r["differs"], r)

# Product types: title noun first, else the category's nearest segment.
check("type from title", _S("SONY SRSULT10B.CE7 Wireless Speaker Black").ptype == "speaker"
      and _S("JBL LIVE 770 NC BLUE СЛУШАЛКИ").ptype == "headphones")
check("type from the category's last segment, not 'Телефони' above it",
      m.record_sig({"title": "SONY WHULT900NB.CE7", "category": "Телефони/Фото и Навигација > Дополнителна опрема за "
                                                             "мобилни телефони > Bluetooth слушалки"}).ptype == "headphones")
hp = m.record_sig({"title": "Sony ULT Wear Black", "brand": "Sony", "category": "Аудио > Слушалки"})
r = m.score_candidate(hp, m.record_sig({"title": "Sony ULT Wear Party Speaker Black", "brand": "Sony"}), {})
check("likely lead of another product type rejected", r.get("confidence") is None and "product type differs (speaker vs ref headphones)"
      == r.get("why_not"), r)
r = m.score_candidate(m.record_sig({"title": "Sony ULT Wear WH-ULT900N Black", "brand": "Sony", "category": "Слушалки"}),
                      m.record_sig({"title": "Sony WH-ULT900N Speaker Black", "brand": "Sony"}), {})
check("same code, other type -> variant with the type named", r["group"] == "variant" and "type speaker vs ref headphones" in r["differs"], r)

# Bare-code references and marketed names.
check("bare code detected", m.is_bare_code(_S("SONY WHULT900NB.CE7", "SONY"), "SONY")
      and m.is_bare_code(_S("SONY WHCH520B.CE7 ( Black )", "SONY"), "SONY")
      and not m.is_bare_code(_S("Sony ULT Wear WH-ULT900N", "Sony"), "Sony"))
check("name phrase from a marketed title", m.name_phrase("Слушалки Sony ULT Wear WHULT900N, безжични, со длабок бас") == "ULT Wear"
      and m.name_phrase("Headphones Sony ULT WEAR Bluetooth Wireless Noise Cancelling Black", "Sony") == "ULT WEAR"
      and m.name_phrase("Sony WH-1000XM5 Wireless Noise Cancelling Headphones", "Sony") is None)
REFR = {"brand": "SONY", "title": "SONY WHULT900NB.CE7",
        "specs": "SONY WHULT900NB.CE7, Wireless BLUETOOTH Noise Canceling Headphones, Black"}
check("marketed name from confirmed offers", m.marketed_names("Sony", REFR, [{"title": "Слушалки Sony ULT Wear WHULT900N, безжични"}])
      == ["Sony ULT Wear"])
check("no name from specs that put a code after the brand", m.marketed_names("Sony", REFR, []) == [])
check("name from specs right after the brand", m.marketed_names("Sony", dict(REFR, specs="Sony ULT WEAR wireless headphones, black"), [])
      == ["Sony ULT WEAR"])

# Mirrors: 'Basics from GjirafaMall' alone is not a Gjirafa50 mirror; the SKU is.
zm = {"store": "zirafamall", "seller": "Basics from GjirafaMall", "sku": "14435951mo", "title": "Машина за перење Beko"}
check("ZirafaMall vendor alone is no mirror", m.mirror_target(zm) is None)
check("ZirafaMall SKU seen on Gjirafa50 is a mirror", m.mirror_target(zm, {"14435951mo"}) == "gjirafa50")
recs = m.annotate([dict(zm, mirror_of="stale"), {"store": "gjirafa50", "sku": "14435951MO", "title": "Beko"}])
check("annotate recomputes mirror_of over the record set", recs[0]["mirror_of"] == "gjirafa50" and recs[1]["mirror_of"] is None)


# group_records: EAN, aliases, Gjirafa SKU twins, variants apart, ambiguity, exact over prefix.
def _g(store, rid, title, ean=None, **kw):
    return m.clean_record(dict({"store": store, "id": rid, "title": title, "price_mkd": 100, "brand": kw.pop("brand", "ASUS"),
                                "ean": ean, "url": f"u/{store}/{rid}"}, **kw), store)


def _groups(recs):
    m.annotate(recs)
    gs = m.group_records(recs)[0]
    return sorted(sorted(recs[i]["id"] for i in g) for g in gs)


E = "4711387800123"
gpu = [_g("setec", "S1", "ASUS DUAL-RTX5070-O12G GeForce RTX 5070 OC 12GB GDDR7", E),
       _g("ddstore", "D1", "ASUS Dual GeForce RTX 5070 OC 12GB GDDR7 (90YV0M17-M0NA00)", E),
       _g("neksio", "N1", "VGA ASUS 90YV0M17-M0NA00 RTX 5070 12GB GDDR7"),
       _g("anhoch", "A1", "Graphics Card ASUS DUAL-RTX5070-O12G 12GB"),
       _g("hivetec", "H1", "ASUS PRIME GeForce RTX 5070 OC 12GB", sku="PRIME-RTX5070-O12G"),
       _g("setec", "S2", "ASUS DUAL-RTX5070-O12G-WHITE GeForce RTX 5070 OC 12GB White", "4711387800999"),
       _g("anhoch", "A2", "Graphics Card ASUS DUAL-RTX5070-O12G 12GB White")]
check("group: codes of an EAN group are aliases; White and PRIME apart", _groups(gpu) ==
      [["A1", "D1", "N1", "S1"], ["A2", "S2"], ["H1"]], _groups(gpu))
check("group: part number from specs joins", _groups([_g("setec", "S1", "ASUS Dual RTX 5070 OC", E, specs="... P/N: 90YV0M17-M0NA00 ..."),
                                                      _g("neksio", "N1", "VGA ASUS 90YV0M17-M0NA00 RTX 5070 OC")]) == [["N1", "S1"]])
check("group: spaced code joins the glued one",
      _groups([_g("anhoch", "A", "Headphones JBL Live 770NC ANC Wireless Black", brand="JBL"),
               _g("tehnomarket", "T", "JBL LIVE 770 NC BLACK СЛУШАЛКИ", brand="JBL")]) == [["A", "T"]])
check("group: colours never merge (same code, black vs blue)",
      _groups([_g("anhoch", "A", "Headphones JBL Live 770NC ANC Wireless Black", brand="JBL"),
               _g("tehnomarket", "T", "JBL LIVE 770 NC BLUE СЛУШАЛКИ", brand="JBL")]) == [["A"], ["T"]])
check("group: capacity never merges", _groups([_g("setec", "S", "Crucial P3 Plus 1TB CT1000P3PSSD8", brand="Crucial"),
                                               _g("setra", "T", "SSD Crucial P3 Plus 2TB CT1000P3PSSD8", brand="Crucial")]) == [["S"], ["T"]])
check("group: different EANs never merge, even with one code",
      _groups([_g("setec", "S", "Samsung Galaxy A56 SM-A566B 8/256GB", "8806095810035", brand="Samsung"),
               _g("neptun", "N", "Samsung Galaxy A56 SM-A566B 8/256GB", "8806095810042", brand="Samsung")]) == [["N"], ["S"]])
check("group: a code that fits two EAN products equally joins neither",
      _groups([_g("setec", "S", "Samsung Galaxy A56 SM-A566B 8/256GB", "8806095810035", brand="Samsung"),
               _g("neptun", "N", "Samsung Galaxy A56 SM-A566B 8/256GB", "8806095810042", brand="Samsung"),
               _g("anhoch", "A", "Samsung Galaxy A56 SM-A566B 8/256GB", brand="Samsung")]) == [["A"], ["N"], ["S"]])
check("group: Gjirafa SKU twins", _groups([_g("gjirafa50", "1", "Телефон Samsung Galaxy A56, 8GB, 256GB", sku="123mo", brand=None),
                                          _g("zirafamall", "1", "Samsung Galaxy A56 8/256", sku="123MO", brand=None)]) == [["1", "1"]])
check("group: titles alone never join", _groups([_g("setec", "S", "Logitech MX Master 3S Graphite", "5099206103603", brand="Logitech"),
                                                _g("anhoch", "A", "Mouse Logitech MX Master 3S Graphite", brand="Logitech")]) == [["A"], ["S"]])
check("group: placeholder EAN ignored", m._group_ean({"ean": "0000000000000"}) is None and m._group_ean({"ean": "5099206103603"}))
check("group: one-sided tier is no conflict under the same full part number",
      m.score_candidate(m.record_sig(gpu[0]), m.record_sig(_g("neksio", "X", "VGA ASUS DUAL-RTX5070-O12G 12GB")), {})["group"] == "same")
check("group: ...but it is for a short model name (AK820 vs AK820 Pro)",
      m.score_candidate(_S("Ajazz AK820 keyboard white"), _S("Ajazz AK820 Pro keyboard"), {})["group"] == "variant")

# Price validity windows: tri-state, display, CSV, summaries; records without the fields unchanged.
_now = _mdt.datetime.now(_mdt.timezone(_mdt.timedelta(hours=2)))
_iso = lambda d: (_now + _mdt.timedelta(days=d)).replace(hour=23, minute=59, second=0, microsecond=0).isoformat()
_dm = lambda d: (_now + _mdt.timedelta(days=d)).strftime("%d.%m")
plain = {"title": "t", "price_mkd": 5000}
check("no window fields -> record exactly as before", m.clean_record(dict(plain), "setec")
      == {"store": "setec", "title": "t", "price_mkd": 5000, "effective_price_mkd": 5000, "price_condition": None})
r = m.clean_record(dict(plain, price_valid_until=_iso(1)), "ananas")
check("string window kept", r["effective_price_valid_until"] == _iso(1) and m.until_text(r) == f"until {_dm(1)}")
r = m.clean_record(dict(plain, price_valid_until=None), "setec")
check("null = standing, no marker", "effective_price_valid_until" in r and r["effective_price_valid_until"] is None and m.until_text(r) == "")
r = m.clean_record(dict(plain, price_mkd=139999, member_price_mkd=132999, price_valid_until=None,
                        member_price_valid_until="2026-10-04T23:59:00+02:00"), "neptun")
check("member price effective -> its window (Neptun haPPy)", r["effective_price_valid_until"] == "2026-10-04T23:59:00+02:00")
r = m.clean_record(dict(plain, price_mkd=139999, member_price_mkd=132999, member_price_valid_until=_iso(2)), "neptun")
m.MEMBER_PRICES = False
r2 = m.clean_record(dict(r), "neptun")
m.MEMBER_PRICES = True
check("--no-member-prices: price_mkd's window, 'unknown' when only the member window is known",
      r2["effective_price_valid_until"] == "unknown" and m.until_text(r2) == "")
check("past end kept and marked 'ended'", m.until_text(m.clean_record(dict(plain, price_valid_until=_iso(-2)), "ananas")) == f"ended {_dm(-2)}")
check("far end not marked in tables, but in prose", m.until_text(m.clean_record(dict(plain, price_valid_until=_iso(30)), "setec")) == ""
      and m.until_text(m.clean_record(dict(plain, price_valid_until=_iso(30)), "setec"), always=True).startswith("until "))
check("Z offset parsed", m.parse_until("2026-10-04T21:59:00Z").utcoffset() == _mdt.timedelta(0))
rows = [m.clean_record(_cand("ananas", 6690, price_valid_until=_iso(1), id="a"), "ananas"),
        m.clean_record(_cand("setec", 6990, price_valid_until=None, id="s"), "setec"),
        m.clean_record(_cand("anhoch", 7100, id="h"), "anhoch")]
txt = _out(m.print_records, rows, 0, 40)
check("table: VALID column next to PRICE, marked row only, footnote", txt.splitlines()[0].split()[:2] == ["PRICE", "VALID"]
      and f"until {_dm(1)}" in txt.splitlines()[1] and "until" not in txt.splitlines()[2]
      and any(l.startswith("VALID: the shop's end date") for l in txt.splitlines()), txt)
txt = _out(m.print_records, [m.clean_record(_cand("setec", 6990, price_valid_until=_iso(30)), "setec")], 0, 40)
check("table: no VALID column when nothing ends soon", "VALID" not in txt, txt)
check("summary: cheapest price's end", f"cheapest 6,690 MKD at ananas; that price ends {_dm(1)} (tomorrow)" in m._group_prices(rows),
      m._group_prices(rows))
check("summary: past end", "has passed" in m._group_prices([m.clean_record(_cand("ananas", 6690, price_valid_until=_iso(-1)), "ananas")]))
path = os.path.join(tempfile.mkdtemp(), "v.csv")
m.write_csv(rows, path)
with open(path, encoding="utf-8") as f:
    got = {x["store"]: x for x in csv.DictReader(f)}
check("CSV: validity columns with the tri-state", (got["ananas"]["effective_price_valid_until"], got["setec"]["price_valid_until"],
                                                   got["anhoch"]["price_valid_until"], got["anhoch"]["effective_price_valid_until"])
      == (_iso(1), "", "unknown", "unknown"), got["anhoch"])
check("detail line: Neptun member window, standing price",
      m.validity_line(dict(plain, price_mkd=139999, member_price_mkd=132999, price_valid_until=None,
                           member_price_valid_until="2099-10-04T23:59:00+02:00"))
      == "valid: price 139,999 standing (no end date); member price 132,999 until 04.10.2099 23:59")
check("detail line: none for standing / absent only", m.validity_line(dict(plain, price_valid_until=None)) is None
      and m.validity_line(plain) is None)

# Laptop / PC titles: part codes (CPU, GPU) and shop names are no model codes; configurations join only in full.
check("system title drops CPU/GPU codes", _S("PowerCube G3270 Volcano G14 i7-14700F/ 32GB DDR5/ NVMe 2.0TB / RTX5070 12GB").strong == {"g3270"}
      and _S("Laptop Dell Alienware 16X Aurora AC16251, 16\", Ultra 9 275HX").strong == {"ac16251"})
check("a CPU's own code stays a code", _S("AMD Ryzen 7 7800X3D processor").strong == {"7800x3d"}
      and _S("Gigabyte GeForce RTX5070 WINDFORCE OC SFF 12G").strong == {"rtx5070"})
check("shop name is no code", "gjirafa50" not in _S("RTX 5070, AMD Ryzen 7 9800X3D, 32GB RAM, 1TB SSD - Gaming PC GJIRAFA50").strong)
check("piece counts / bus widths are no codes", not _S("Dark Project keycaps 177PCS Sunrise").strong
      and not _S("nVidia GeForce RTX 5070 12GB 192-BIT").strong - {"rtx5070"})
check("group: laptop configurations sharing a model prefix stay apart",
      _groups([_g("gjirafa50", "1", "Гејминг лаптоп ASUS ROG Strix G16 G614PR, Ryzen 9, 16GB, 1TB"),
               _g("setec", "2", "Лаптоп ASUS ROG Strix G16 G614PR-RV132W, 32GB, 1TB")]) == [["1"], ["2"]])
check("group: the same full laptop code joins", _groups([_g("setec", "1", "Лаптоп ASUS TUF A16 FA608UM-RV015 16GB 512GB"),
                                                        _g("neptun", "2", "ASUS TUF Gaming A16 FA608UM-RV015 16GB/512GB")]) == [["1", "2"]])

# --- Verification run 2026-10-03 (live data) ---
# Spec ranges are no model codes: '560-590 MHz' (glued to '560-590MHz') joined two different
# wireless-mic systems in `group`; Neptun's '20Hz-20,000Hz' became code 20HZ-20.
for t in ("560-590MHz", "20Hz-20", "10Hz-25kHz", "100-240V", "2.4-5GHz", "5-12V", "1.5-3A", "2400-3200MHz"):
    check(f"unit range is no code: {t}", not m._is_strong_code(t) and not m._is_weak_code(t))
for t in ("910-007501", "WH-CH520W", "SM-A576BLBDEUC", "UE-75U8072FUXXH", "DHE-7000", "MHS-U-001", "981-000978"):
    check(f"still a code: {t}", m._is_strong_code(t))
check("Neptun 20Hz-20,000Hz adds no code", _S("Слушалки SONY BT WH-CH520W, 20Hz-20,000Hz").strong == {"whch520w"})
check("group: a shared frequency range joins nothing",
      _groups([_g("zirafamall", "1", "Безжичен систем микрофони UWH 1, слушалка со bodypack, UHF 560-590 MHz, црн", brand=None),
               _g("zirafamall", "2", "Систем безжични микрофони ДНА UWM 1, УХФ 560-590 MHz, растојание 50 m, црн",
                  brand=None)]) == [["1"], ["2"]])
# Light Blue and Dark Blue are two editions (Galaxy A57 SM-A576BLB.. vs BDB..), not one colour.
ref = m.signature("SAMSUNG Galaxy A57 5G 8+256GB (SM-A576BLBDEUC) Light Blue", "SAMSUNG", "8806099025908")
for t, grp in (("Samsung Galaxy A57 5G 8GB/256GB Dark Blue", "variant"),
               ("Телефон Samsung Galaxy A57 5G A576, 256GB, 8GB RAM, темно сина боја", "variant"),
               ("Samsung Galaxy A57 5G 8GB/256GB Light Blue", "same"),
               ("Телефон Samsung Galaxy A57 A576, 256GB, 5G, светло сина боја", "same"),
               ("Samsung Galaxy A57 5G 8GB/256GB Blue", "same")):
    r = m.score_candidate(ref, m.signature(t, "Samsung"), {"store": "anhoch"})
    check(f"shade: {t[-28:]} -> {grp}", r.get("group") == grp, r)
check("shade: Pale Gray still fits Gray", m.score_candidate(m.signature("Logitech MX Master 3S Pale Gray", "Logitech"),
                                                             m.signature("Logitech MX Master 3S Gray", "Logitech"),
                                                             {"store": "x"}).get("group") == "same")
check("group: Light / Dark Blue with one part number stay apart",
      _groups([_g("ddstore", "1", "Samsung Galaxy A57 5G SM-A576B 8GB/256GB Light Blue", brand="Samsung"),
               _g("anhoch", "2", "Samsung Galaxy A57 5G SM-A576B 8GB/256GB Dark Blue", brand="Samsung")]) == [["1"], ["2"]])

# Neptun: a haPPy price whose promotion GetProduct does not name (PromotionId 0) while the product has
# promotions cannot tell its end: the key is left out ('unknown'), never null ('standing').
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("neptun_client", SKILL + "/scripts/stores/neptun.py")
_np = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_np)
_TCL = {"Id": 299140, "RegularPrice": 25999.0, "DiscountPrice": 19999.0, "DiscountPriceType": 3,
        "WebshopDiscountPrice": 0, "ActualPrice": 19999.0, "PromotionId": 0, "WebPromotionId": 0,
        "Promotions": [{"Id": -3296, "PromotionName": "M20260898 | #02 TCL September", "ValidTo": "/Date(1791669540000)/"},
                       {"Id": 4353, "PromotionName": "M20250127 | #01 STD Interna Happy", "ValidTo": "/Date(1791669540000)/"}]}
w = _np.Neptun.price_windows(_TCL)
check("Neptun: unnamed promotion -> member window left out", "member_price_valid_until" not in w and w.get("price_valid_until", "x") is None, w)
check("Neptun: named promotion -> its end", _np.Neptun.price_windows(dict(_TCL, PromotionId=4353))["member_price_valid_until"]
      == "2026-10-10T23:59:00+02:00")
check("Neptun: no promotion at all -> standing (null)", _np.Neptun.price_windows(dict(_TCL, Promotions=[]))
      == {"price_valid_until": None, "member_price_valid_until": None})
check("Neptun: unknown member window reads 'unknown' downstream",
      m.clean_record({"price_mkd": 25999, "member_price_mkd": 19999, **w}, "neptun")["effective_price_valid_until"] == "unknown")

# ==== heuristic gaps from the independent live verification (2026-10-04) ====
# Real titles from the verifier's saved runs (scratchpad/verify: s_all, l_g50, l_zm, m_xm5s, s_lap).
m.MEMBER_PRICES = True


def _gp(recs):
    """group_records over records -> sorted lists of ids (annotate first, as cmd_group does)."""
    m.annotate(recs)
    gs = m.group_records(recs)[0]
    return sorted(sorted(recs[i]["id"] for i in g) for g in gs)


def _hints(recs):
    m.annotate(recs)
    _gs, _s, _e, hints, g = m.group_records(recs)
    return [(sorted({recs[i]["id"] for i in g.members[g.find(a)]}), sorted({recs[i]["id"] for i in g.members[g.find(b)]}),
             why) for a, b, _c, why in hints]


# 1. Setec dotted / slashed suffixes: the base code is a code of the title, searched and compared.
s = _S("SONY WH1000XM5L.CE7 ( Midnight Blue )", "SONY")
check("dotted suffix: base code added", {"wh1000xm5lce7", "wh1000xm5l"} <= s.strong and s.pkg_base == {"wh1000xm5l": "wh1000xm5lce7"}, s.strong)
check("slashed suffix: SM-S931B/DS -> SM-S931B", "sms931b" in _S("Samsung Galaxy S25 SM-S931B/DS 12/128GB Navy", "Samsung").strong)
check("dotted suffix: no base from a tier / unit tail", not _S("ASUS RTX4070.TI 12GB").pkg_base and not _S("Kabel 1.5M").pkg_base)
qs = [q for q, _k in m.build_queries({"brand": "SONY"}, s, "SONY WH1000XM5L.CE7 ( Midnight Blue )")]
check("dotted suffix: base code searched after the full code", qs[:2] == ["WH1000XM5L.CE7", "WH1000XM5L"], qs)
anh = m.record_sig({"title": "Headphones Sony WH-1000XM5L Noise Cancelling Bluetooth w/Microphone Blue", "brand": "Sony"})
r = m.score_candidate(s, anh, {"store": "anhoch"})
check("dotted suffix: Anhoch WH-1000XM5L is the same product (equal base code)",
      r["confidence"] == "model" and r["group"] == "same" and "WH1000XM5L" in r["reasons"][0], r)
check("model_key keys a dotted code by its base", m.model_key({"title": "SONY WHULT900NB.CE7", "brand": "SONY"}) == "model:sony:whult900nb")
s5 = _S("SONY WH1000XM5S.CE7 ( Platinum Silver )", "SONY")
check("'Platinum Silver' is silver, and no title query 'SONY Platinum'",
      s5.colours == {"silver"} and "platinum" not in s5.tokens
      and not any("Platinum" in q for q, _k in m.build_queries({"brand": "SONY"}, s5, "SONY WH1000XM5S.CE7 ( Platinum Silver )")))
for t, want in (("Laptop Lenovo IdeaPad Slim 3 15ABR8, Ryzen 5, 16GB RAM, 1TB SSD, арктичко сива", {"grey"}),
                ("Laptop гејминг ASUS TUF A15 FA507NV LP025, 15.6\", Ryzen 5 7535HS, RTX 4060, 16GB RAM, Mecha Gray", {"grey"}),
                ("Лаптоп ASUS Vivobook 15 X1504VA-BQ2981 (Quiet Blue )", {"blue"}), ("Kufje Gembird MHS-U-001, të zeza", {"black"}),
                ("Kufje Panasonic PR-HJE125E, të kaltërta", {"blue"})):
    check(f"marketing / Albanian colour: {t[-28:]}", _S(t).colours == want, _S(t).colours)
check("no marketed name from 'w/Microphone'",
      m.name_phrase("Headphones Sony WH-1000XM5L Noise Cancelling Bluetooth w/Microphone Blue", "Sony") is None
      and m.marketed_names("Sony", {"brand": "SONY", "title": "SONY WH1000XM5L.CE7 ( Midnight Blue )",
                                    "specs": "SONY WH1000XM5L.CE7 ( Midnight Blue ), Overhead Wireless Noise Cancelling Headphones"},
                           [{"title": "Headphones Sony WH-1000XM5L Noise Cancelling Bluetooth w/Microphone Blue"}]) == [])
check("product type ignores what follows 'w/' ('W/MICROPHONE' on headphones)",
      _S("SONY WH-1000XM5S NOISE CANCELLING BLUETOOTH W/MICROPHONE SILVER").ptype is None
      and _S("Kufje me mikrofon Gembird (MHS-123), me kabllo, të zeza").ptype == "headphones")

# 2. Configurations: CPU model / tier, RAM, storage, GPU; RAM + storage pairs.
for t, cpu, tier, ram, st, gpu in (
        ('Laptop за гејминг ASUS TUF A15 FA507NU, 15.6", Ryzen 5, 16GB, 512GB, RTX 4050, црн', set(), {"ryzen5"}, {16}, {512}, {"rtx4050"}),
        ("ASUS Лаптоп Vivobook Go 15 E1504FA-BQ1867 R5-7520U/16GB DDR5/512GB", {"7520u"}, {"ryzen5"}, {16}, {512}, set()),
        ('Asus VivoBook 15 X515EA, 15.6", Intel Core i5-1135G7, 8GB RAM, Intel Iris Xe, Slate Grey', {"1135g7"}, {"corei5"}, {8}, set(), set()),
        ("Lenovo IdeaPad Slim 3 15IRH10 i7-13620H/ 16GB/ SSD 512GB/ 15.3″ WUXGA IPS", {"13620h"}, {"corei7"}, {16}, {512}, set()),
        ('ASUS Vivobook V16 V3607VJ-TK183 16" 144Hz/Ultra 5 210H/16GB DDR5/512GB/RTX 3050 6GB', {"210h"}, {"ultra5"}, {16}, {512}, {"rtx3050"}),
        ('Laptop ASUS Vivobook 15, 15,6" IPS, C7-150U, 16GB RAM, 512GB SSD, сребрен', {"150u"}, {"core7"}, {16}, {512}, set()),
        ('Laptop ASUS VivoBook 15 X1504VA, 15.6", Core 7 150U, 16GB RAM, 512GB SSD, сина боја', {"150u"}, {"core7"}, {16}, {512}, set()),
        ("Лаптоп за гејминг Асус ТУФ Гејминг А15 Рајзен 7, 16GB RAM, 1TB SSD, црн", set(), {"ryzen7"}, {16}, {1000}, set()),
        ("Samsung Galaxy A57 5G 8+256GB (SM-A576BLBDEUC) Light Blue", set(), set(), {8}, {256}, set()),
        ("Телефон Samsung Galaxy A57 A576, 256GB, 8GB RAM, темно сина боја", set(), set(), {8}, {256}, set()),
        ("Xiaomi Redmi Note 14 Pro 12/256 Black", set(), set(), {12}, {256}, set())):
    sg = _S(t)
    got = (sg.cpu, sg.cpu_tier, sg.ram, sg.storage, sg.gpu)
    check(f"config: {t[:48]}", got == (cpu, tier, ram, st, gpu), got)
check("config: a GPU's memory is no RAM; a RAM kit keeps its capacity",
      _S('ASUS Vivobook V16 V3607VJ-TK183 16" 144Hz/Ultra 5 210H/16GB DDR5/512GB/RTX 3050 6GB').vram == {6}
      and m._cap(_S("Kingston FURY Beast 32GB DDR5 6000")) == {32} and not _S("Kingston FURY Beast 32GB DDR5 6000").ram)
check("'Intel Core i7' / 'Core Ultra 7' are no model tiers",
      _S("Lenovo Лаптоп, IdeaPad Slim 3 15IRH10, Intel Core i7 13620H, 16GB, 512GB SSD").tiers == {"slim"}
      and not _S("Laptop Dell XPS 13 Core Ultra 7 155H 16GB 512GB").tiers)
r = m.score_candidate(_S("Samsung Galaxy A57 5G 8+256GB (SM-A576BLBDEUC) Light Blue", "Samsung"),
                      _S("Samsung Galaxy A57 5G 8GB/256GB Light Blue", "Samsung"), {})
check("memory pair: 8+256GB vs 8GB/256GB, no spurious capacity difference", r["group"] == "same" and not r["differs"], r)
r = m.score_candidate(_S("Samsung Galaxy A57 5G 8+256GB Light Blue", "Samsung"), _S("Samsung Galaxy A57 5G 256GB Light Blue", "Samsung"), {})
check("memory pair: RAM the other leaves out is no capacity '+8GB'",
      r["group"] == "same" and not any(x.startswith("capacity") for x in r["differs"]), r)
r = m.score_candidate(_S("Xiaomi Redmi Note 14 Pro 8/256GB Black", "Xiaomi"), _S("Xiaomi Redmi Note 14 Pro 12/256GB Black", "Xiaomi"), {})
check("memory pair: 8/256 vs 12/256 is a RAM variant", r["group"] == "variant" and "RAM" in r["conflicts"], r)
LAPS = {
    "u5": 'Laptop за гејминг ASUS TUF A15 FA507NU, 15.6", Ryzen 5, 16GB, 512GB, RTX 4050, црн',
    "u7": 'Laptop за гејминг ASUS TUF Gaming A15 FA507NU, 15.6", Ryzen 7, RTX 4050, црн',
    "c5": 'Laptop Asus VivoBook 15 X1504VA, 15.6", Core 5, 24GB RAM, 512GB SSD, сина боја',
    "c7": 'Laptop Asus VivoBook 15 X1504VA, 15.6", Core 7, 24GB RAM, 512GB SSD, сина боја'}
r = m.score_candidate(_S(LAPS["u7"], "ASUS"), _S(LAPS["u5"], "ASUS"), {})
check("FA507NU Ryzen 7 vs Ryzen 5: CPU variant", r["group"] == "variant" and "CPU" in r["conflicts"]
      and "CPU Ryzen 5 vs ref Ryzen 7" in r["differs"], r)
check("group: a platform code never joins different CPUs (FA507NU, X1504VA)",
      _gp([_g("gjirafa50", "u5", LAPS["u5"], sku="14133777mo"), _g("gjirafa50", "u7", LAPS["u7"], sku="MOBASUNOTBAJEa"),
           _g("gjirafa50", "c5", LAPS["c5"], sku="15867650mo"), _g("gjirafa50", "c7", LAPS["c7"], sku="15912024mo")])
      == [["c5"], ["c7"], ["u5"], ["u7"]])
check("group: Lenovo 15ABR8 Ryzen 5 vs Ryzen 7 stay apart",
      _gp([_g("gjirafa50", "a", "Лаптоп Lenovo IdeaPad Slim 3 15ABR8, Ryzen 5, 16GB RAM, 1TB SSD, арктичко сива", brand="Lenovo"),
           _g("gjirafa50", "b", 'Laptop Lenovo IdeaPad Slim 3 15ABR8, 15.6", Ryzen 7, 16GB RAM, сив', brand="Lenovo")]) == [["a"], ["b"]])
check("group: a CPU code is never the join key (X515EA via I5-1135G7)",
      _gp([_g("gjirafa50", "a", 'Лаптоп Asus VivoBook 15 X515EA (X515EA-BQ1445), 15.6", Full HD, Intel Core i5-1135G7, 8GB RAM, 512GB SSD, сив', brand=None),
           _g("gjirafa50", "b", 'Asus VivoBook 15 X515EA, 15.6", Intel Core i5-1135G7, 8GB RAM, Intel Iris Xe, Slate Grey', brand=None)])
      == [["a"], ["b"]])
check("group: a platform code with no configuration on one side joins nothing (15ARP10, EAN listing)",
      _gp([_g("ddstore", "d", "LENOVO IdeaPad Slim 3 15ARP10", brand="Lenovo"),
           _g("neptun", "n", "LENOVO IdeaPad Slim 3 15ARP10 Ryzen 7 170/16GB DDR5/512GB", "0199274963484", brand="Lenovo")])
      == [["d"], ["n"]])
check("group: the same platform with the same configuration stated on both sides joins (15AMN8)",
      _gp([_g("hivetec", "h", "Lenovo IdeaPad Slim 3 15AMN8 Ryzen 3 30 16GB 512GB SSD 15.6″ FHD Laptop", brand="Lenovo"),
           _g("neptun", "n", "LENOVO IdeaPad Slim 3 15AMN8 Ryzen 3 30/16GB LPDDR5/512GB", "0199274963439", brand="Lenovo")])
      == [["h", "n"]])
check("group: a full configuration code still joins without the configuration (M1502YA-BQ161)",
      _gp([_g("hivetec", "h", "ASUS Vivobook 15 M1502YA-BQ161 Ryzen 7 7730U 16GB 512GB SSD 15.6″ FHD IPS Laptop – Silver"),
           _g("setec", "s", "Лаптоп ASUS Vivobook 15 M1502YA-BQ161 (Silver)", "4711387639603")]) == [["h", "s"]])
check("group: B850M-PLUS II vs B850M-PLUS WIFI (a version on one side) stay apart",
      _gp([_g("ddstore", "ii", "Motherboard ASUS TUF GAMING B850M-PLUS II — AM5, B850, Micro ATX, DDR5", "4711636181853"),
           _g("ddstore", "wifi", "ASUS TUF GAMING B850M-PLUS WIFI AM5 B850 Micro ATX Motherboard DDR5 PCIe 5.0")]) == [["ii"], ["wifi"]])
check("'Gen-2' is a version, not a code", not _S("Razer Huntsman V3 Pro Mini Razer Analog Optical Gen-2 RGB").strong)

# 3. Code prefixes: letters after the model number name another model unless colour / region / packaging.
ZX = [_g("gjirafa50", "zx", "Слушалки Sony MDR-ZX110 On-Ear, бели", brand="Sony", sku="RD013451rst"),
      _g("zirafamall", "zx", "Слушалки Sony MDR-ZX110 On-Ear, бели", brand="Sony", sku="RD013451rst"),
      _g("zirafamall", "zxw", "Слушалки Sony MDR-ZX110W, 20Hz - 20kHz, бели", brand="Sony"),
      _g("zirafamall", "zxap", "Sony MDR-ZX110AP Слушалки за на Уво со Микрофон, Бели", brand="Sony")]
check("prefix: MDR-ZX110 joins its white MDR-ZX110W, not MDR-ZX110AP", _gp(ZX) == [["zx", "zx", "zxw"], ["zxap"]], _gp(ZX))
for a_, b_ in (("Слушалки Rode NTH-100, црни", "Слушалки Rode NTH-100M, професионални со микрофон, over-ear, црни"),
               ("Esperanza EH193K Слушалки за во уво со Микрофон, Црни", "Esperanza EH193KR Слушалки за во уво со Микрофон, Црно-Црвени"),
               ("Динамички микрофон Maono PD200X, USB C и XLR, RGB, бел", "Динамички микрофон Маоно PD200XS, USB XLR, РГБ, бел"),
               ("Слушалки Conceptronic POLONA04B, Bluetooth, со кабел, црни", "Слушалки Conceptronic POLONA04BA, со кабел и блутут, USB Type C, црни"),
               ("Слушалки Samsung AKG EO-IC100, бели", "Слушалки Samsung EO-IC100BWE, USB-C, стерео во уво, бели"),
               ("Tastierë CHERRY G80-3000, me kabllo, USB, US English, e zezë", "Tastierë Cherry G80-3000N RGB TKL, mekanike, USB, e zezë")):
    check(f"prefix: {a_.split(',')[0][-22:]} vs longer code stay apart",
          _gp([_g("zirafamall", "a", a_, brand=None), _g("zirafamall", "b", b_, brand=None)]) == [["a"], ["b"]])
check("prefix: a colour letter a title confirms joins (WF-1000XM5 black ~ WF1000XM5B.CE7)",
      _gp([_g("setec", "s", "SONY WF1000XM5B.CE7 (Black)", "4548736143487", brand="SONY", attributes={"Боја": "Црна"}),
           _g("zirafamall", "z", "Слушалки Sony Noise Cancelling WF-1000XM5, црни", brand=None)]) == [["s", "z"]])
check("prefix: a region / packaging tail still joins (QE55Q60D ~ QE55Q60DAUXXH)",
      _gp([_g("setec", "s", "Samsung QE55Q60DAUXXH 55\" QLED", brand="Samsung"), _g("anhoch", "a", "TV Samsung QE55Q60D 55\" QLED 4K", brand="Samsung")])
      == [["a", "s"]])
check("suffix kinds", (m._suffix_kind("auxxh", "qe55q60dauxxh", _S("QE55Q60DAUXXH"), _S("QE55Q60D")),
                       m._suffix_kind("ap", "mdrzx110ap", _S("MDR-ZX110AP Бели"), _S("MDR-ZX110 бели")),
                       m._suffix_kind("w", "mdrzx110w", _S("MDR-ZX110W бели"), _S("MDR-ZX110 бели")),
                       m._suffix_kind("s", "pd200xs", _S("Maono PD200XS бел"), _S("Maono PD200X бел")))
      == ("packaging", None, "colour", None))
check("a model suffix written apart ('CVM-V01SP UC') is another model",
      _gp([_g("zirafamall", "uc", "Mikrofon lavalier Comica CVM-V01SP UC, USB Type C, 2.5m, црн", brand=None),
           _g("zirafamall", "p", "Микрофон Comica CVM-V01SP, 2.5 m, црн", brand=None)]) == [["p"], ["uc"]]
      and not _S("ASUS TUF-RTX5080-O16G-GAMING, AI Performance").code_suffix
      and not _S("VGA ASUS 90YV0M17-M0NA00 RTX 5070 12GB").code_suffix)
check("cable lengths are a variant (6m vs 2.5 m)", _S("Микрофон Comica CVM-V01SP, 2.5 m, црн").units.get("m") == {2.5}
      and m.score_candidate(_S("Лавалиер микрофон Comica CVM-V01SP, 6m, за смартфон, црн"),
                            _S("Микрофон Comica CVM-V01SP, 2.5 m, црн"), {})["group"] == "variant")

# 4. Gjirafa50 / ZirafaMall SKU twins need titles that agree.
TW = [("Kufje Gembird MHS-U-001, të zeza", "Gembird", "Главен цилиндар за сопирачки за Forte", None, False),
      ("Слушалки Fury Phantom, сина боја", None, "Fury Phantom Жичен Гејминг Слушалки, Црн", None, False),
      ("Слушалки MediaRange, 20 Hz", "MediaRange", "MediaRange MROS231 Леворук USB Оптички Глушец, 2400 DPI", "MediaRange", False),
      ("Kufje HP Blackwire C3210, të zeza", "HP", "Poly Blackwire 3210 Моноурална USB-C Слушалка", "Poly", True),
      ("Kufje HP Voyager 4320-M, të zeza", "HP", "Poly Voyager 4320 UC Безжични Слушалки со Станица за Полнење, USB-A", "Poly", True),
      ("Тастатура Dell Alienware 510K Dark Side of the Moon со Cherry MX Red", "Dell", "Alienware AW510K Low-Profile RGB Механичка Гејминг Тастатура", None, True),
      ("Слушалки Esperanza Liberto, црни", "Esperanza", "Esperanza EH163K Bluetooth Стерео Слушалки, Црна", "Esperanza", True),
      ("Kufje Panasonic PR-HJE125E, të bardha", "Panasonic", "Panasonic RPHJE125 Ergofit Стерео Слушалки за во Уво, Бели", "Panasonic", True),
      ("Kufje Gembird BTHS-01, të hirta", "Gembird", "Gembird BTHS-01-SV Безжични Bluetooth Слушалки, Сребрени", "Gembird", True),
      ("Kufje Gembird, të bardha", "Gembird", "Gembird MHS-03-WTRD Стерео Слушалки, Бело/Црвено", "Gembird", True),
      ("Mеханичка тастатура Keychron V5 Max, безжична, RGB, Gateron Jupiter Red, црна", "Keychron",
       "Mеханичка тастатура Keychron V5 Max, безжична, RGB, Gateron Jupiter Red, црна", "Keychron", True)]
for ta, ba, tb, bb, ok in TW:
    got = m._twin_ok(m.record_sig({"title": ta, "brand": ba}), m.record_sig({"title": tb, "brand": bb}))
    check(f"twin {'kept' if ok else 'refused'}: {ta[:30]} / {tb[:30]}", got[0] is ok, got)
recs = [_g("gjirafa50", "1", TW[0][0], sku="606820mo", brand="Gembird"), _g("zirafamall", "2", TW[0][2], sku="606820mo", brand=None),
        _g("gjirafa50", "3", TW[1][0], sku="7290477mo", brand=None), _g("zirafamall", "4", TW[1][2], sku="7290477mo", brand=None),
        _g("gjirafa50", "5", TW[3][0], sku="13230182mo", brand="HP"), _g("zirafamall", "6", TW[3][2], sku="13230182mo", brand="Poly")]
check("group: refused twins apart, a rebrand twin kept", _gp(recs) == [["1"], ["2"], ["3"], ["4"], ["5", "6"]], _gp(recs))
check("refused twin is no mirror; the kept one is", (recs[1]["mirror_of"], recs[3]["mirror_of"], recs[5]["mirror_of"])
      == (None, None, "gjirafa50"), [r["mirror_of"] for r in recs])
check("refused twin noted as a lead", any(sorted([a_, b_]) == [["1"], ["2"]] and why.startswith("same Gjirafa SKU 606820mo, but the titles disagree")
                                          for a_, b_, why in _hints(recs)), _hints(recs))
c = m.score_candidate(m.record_sig({"title": TW[1][0]}), m.record_sig({"title": TW[1][2]}),
                      {"store": "zirafamall", "sku": "7290477mo"}, {"store": "gjirafa50", "sku": "7290477mo"})
check("match: a twin whose titles disagree is no 'same Gjirafa SKU' match", "same Gjirafa SKU" not in (c.get("reasons") or []), c)

# 5. Accessories and spare parts never join the product; titles that list included parts are products.
for t in ("Резервни перничиња за слушалки ISK HD9999, мек материјал, за долготрајна употреба",
          "Јастучиња за уши HP Poly Savi 8200 од вештачка кожа, за W8210 W8220, сет 2 парчиња, црни",
          "Перници за замена за слушалки Делл WL3024, безжични, црни", "Резервна батерија HP Poly CS540, за слушалки Plantronics CS540",
          "Кутија за полнење Poly Voyager Free 60+ UC, BT700 USB C, екран на допир, црна", "HP Poly CS540 EU Safety EMEA батерија.",
          "Батерија за лаптоп Green Cell GC AS165, 7.6V, 4150mAh, за Asus VivoBook 15", "USB кабел Auerswald H-200, за слушалки COMfortel, црн",
          "Poly BT600 Bluetooth USB Адаптер, Црн", "Sony WH-1000XM5 Ear Pads", "Headphone Stand Trust GXT 260",
          "Аксесоари за слушалки HP Poly CS540, earloops и earbuds, црни"):
    check(f"accessory: {t[:50]}", _S(t).accessory is not None)
for t in ("Безжични слушалки Плантроникс CS540 со кревач HL10, DECT, за фиксен телефон",
          "Слушалки Poly Voyager Free 60+ UC M, со BT700 USB C, црни", "Слушалки Sony WH-1000XM6 со велурни перничиња, црни",
          "Безжичен лавалиер микрофон BOYA BY-V3, со поништување шум, со полначка батерија, црн",
          "JBL Tune 520BT Headphones with charging cable", "Слушалки Dell WH1022, стерео, со микрофон, со USB A кабел, црни",
          "Систем микрофони без жици Saramonic Blink500 Pro XB3 2023, за iPhone Lightning", "Corsair 4000D Airflow Case Black",
          "Xiaomi Smart Band 9 Black"):
    check(f"product, not accessory: {t[:50]}", _S(t).accessory is None, _S(t).accessory)
recs = [_g("zirafamall", "pads", "Резервни перничиња за слушалки ISK HD9999, мек материјал, за долготрајна употреба", brand=None, price_mkd=1490),
        _g("gjirafa50", "hp", "Слушалки ISK HD-9999, црни", brand=None, sku="977733mo", price_mkd=6890),
        _g("zirafamall", "hp", "Слушалки ISK HD-9999, црни", brand=None, sku="977733mo", price_mkd=6890),
        _g("zirafamall", "bat", "Резервна батерија HP Poly CS540, за слушалки Plantronics CS540", brand=None, price_mkd=4090),
        _g("zirafamall", "cs", "Безжични слушалки Плантроникс CS540 со кревач HL10, DECT, за фиксен телефон", brand=None, price_mkd=14090)]
check("group: pads and battery stay off the headsets", _gp(recs) == [["bat"], ["cs"], ["hp", "hp"], ["pads"]], _gp(recs))
h = _hints(recs)
check("group: the accessory shown as a related lead", any(why.startswith(("one is an accessory", "prices ")) for _a, _b, why in h), h)
recs = [_g("zirafamall", "a", "Mикрофон Comica CVM-V01CP, 6m, oмнидирекционален, црн", brand=None, price_mkd=1000),
        _g("zirafamall", "b", "Лавалиер микрофон Comica CVM-V01CP, омнидирекционален, кабел 6 m, црн", brand=None, price_mkd=3500)]
check("group: offers over 3x apart in price do not join on a code", _gp(recs) == [["a"], ["b"]]
      and any(w.startswith("prices 1,000 vs 3,500 MKD") for _a, _b, w in _hints(recs)), _hints(recs))

# 6. Sony WH-CH520 W/B/L: the full code joins across a colour-less Neptun title and Ananas' 'dark grey' attribute.
CH = [_g("setec", "s", "SONY WHCH520B.CE7 ( Black )", "4548736142374", brand="SONY", attributes={"Боја": "Црна"}),
      _g("neptun", "n", "BT слушалки SONY BT WH-CH520B", "4548736142374", brand="SONY"),
      _g("ananas", "an", "Sony Слушалки bt wh-ch520b", "4548736142374", brand="Sony", attributes={"Color": "Темно сива"}),
      _g("anhoch", "ah", "Headphones Sony WH-CH520B Bluetooth Black", brand="Sony"),
      _g("tehnomarket", "t", "Sony WH-CH520B Bluetooth Headphones Black", brand="Sony"),
      _g("gjirafa50", "g", "Слушалки Sony WH-CH520, црни", brand=None, sku="12809614mo"),
      _g("zirafamall", "g", "Слушалки Sony WH-CH520, црни", brand=None, sku="12809614mo"),
      _g("setec", "sw", "SONY WHCH520W.CE7 ( White )", "4548736142817", brand="SONY", attributes={"Боја": "Бела"}),
      _g("neptun", "nw", "Слушалки SONY BT WH-CH520W, 20Hz-20,000Hz", "4548736142817", brand="SONY"),
      _g("anhoch", "ahw", "Headphones Sony WH-CH520W Bluetooth White", brand="Sony"),
      _g("zirafamall", "zw", "Слушалки Sony WH-CH520, бели", brand=None, sku="RD007937rst"),
      _g("anhoch", "ahl", "Headphones Sony WH-CH520L Bluetooth Blue", brand="Sony"),
      _g("neptun", "nl", "BT слушалки SONY BT WH-CH520L", "4548736142862", brand="SONY")]
check("WH-CH520: one row per colour (black, white, blue)", _gp(CH) == [["ah", "an", "g", "g", "n", "s", "t"], ["ahl", "nl"],
                                                                      ["ahw", "nw", "sw", "zw"]], _gp(CH))
r = m.score_candidate(m.record_sig({"title": "Headphones Sony WH-CH520W Bluetooth White", "brand": "Sony"}),
                      m.record_sig({"title": "Слушалки SONY BT WH-CH520W, 20Hz-20,000Hz", "brand": "SONY"}), {})
check("colour letter W fills a colour-less title (no 'colour stated on one side')", r["group"] == "same" and not r["differs"], r)
r = m.score_candidate(m.record_sig({"title": "Headphones Sony WH-CH520B Bluetooth Black", "brand": "Sony"}),
                      m.record_sig({"title": "Sony Слушалки bt wh-ch520b", "brand": "Sony", "attributes": {"Color": "Темно сива"}}), {})
check("an exact full code outranks a seller's colour attribute", r["group"] == "same", r)
check("a colour letter no title confirms changes nothing (SM-A566B silent vs White)",
      _gp([_g("anhoch", "a", "Samsung Galaxy A56 SM-A566B 8/256GB", brand="Samsung"),
           _g("ddstore", "d", "Samsung Galaxy A56 SM-A566B 8/256GB White", brand="Samsung")]) == [["a"], ["d"]])
check("a three-way tie of listings naming one colour joins (Dell WH1022)",
      _gp([_g("zirafamall", "1", "Слушалки Dell WH1022, стерео, со микрофон, со USB A кабел, црни", brand=None),
           _g("zirafamall", "2", "Kабли Dell WH1022, стерео, USB, со микрофон со поништување на бучава, црни", brand=None),
           _g("zirafamall", "3", "Слушалки Dell стерео WH1022, со микрофон, за канцеларија, црни", brand=None)]) == [["1", "2", "3"]])
check("...but not colourways the vocabulary does not know (VGN V98Pro)",
      _gp([_g("zirafamall", "1", "Гејминг тастатура VGN V98Pro V2, механичка 98%, РГБ, Кристал Вино Касис", brand=None),
           _g("zirafamall", "2", "Гејминг тастатура VGN V98Pro V2, механичка 98%, RGB, безжична, Кристал Вино Морска Сол", brand=None),
           _g("zirafamall", "3", "Гејминг тастатура VGN V98Pro V2, механичка 98%, три режими на поврзување, Блеккерант", brand=None)])
      == [["1"], ["2"], ["3"]])
check("keyboard layouts are regions (QWERTZ vs AZERTY)",
      _gp([_g("zirafamall", "de", "Tастатура CHERRY G80-3000N RGB TKL, механичка, германски QWERTZ, црна", brand=None),
           _g("zirafamall", "fr", "Tastierë CHERRY G80-3000N RGB, mekanike, USB AZERTY frëngjisht, e zezë", brand=None)]) == [["de"], ["fr"]])

# 7. Price windows: nothing in mkshop turns 'unknown' (key absent) into 'standing' (null).
_old = {"store": "neptun", "id": "W", "title": "t", "price_mkd": 100, "price_valid_until": None}
_new = {"store": "neptun", "id": "W", "title": "t", "price_mkd": 100}
u = m.union_records([(2.0, 0, m.clean_record(dict(_new), "neptun")), (1.0, 1, m.clean_record(dict(_old), "neptun"))])[0]
check("union: an older null does not fill a fresh 'unknown'", "price_valid_until" not in u and "effective_price_valid_until" not in u, u)
u = m.union_records([(2.0, 0, m.clean_record(dict(_new), "neptun")),
                     (1.0, 1, m.clean_record(dict(_old, price_valid_until="2099-10-10T23:59:00+02:00"), "neptun"))])[0]
check("union: an older end date for the same price still fills it", u.get("effective_price_valid_until") == "2099-10-10T23:59:00+02:00", u)
check("clean_record: member price effective, its window unknown -> 'unknown', not null",
      m.clean_record({"price_mkd": 200, "member_price_mkd": 150, "price_valid_until": None}, "neptun")["effective_price_valid_until"] == "unknown")
# trivial client fixes (null -> absent where the source cannot tell)
check("Neptun: a promotion id the product does not list -> member window left out",
      "member_price_valid_until" not in nep.Neptun.price_windows(dict(_TCL, PromotionId=777, Promotions=[])), nep.Neptun.price_windows(dict(_TCL, PromotionId=777, Promotions=[])))
check("Neptun: a named promotion with a placeholder ValidTo -> left out",
      "member_price_valid_until" not in nep.Neptun.price_windows(dict(_TCL, PromotionId=4353, Promotions=[
          {"Id": 4353, "PromotionName": "x", "ValidTo": "/Date(-62135596800000)/"}])))
check("Ananas listing: on sale without a usable base / end -> key absent",
      "price_valid_until" not in _A.hit_to_record(dict(_H, basePrice=None)))
check("Ananas detail: a running sale without dateTo -> no window (left out)",
      ana.Ananas.sale_valid_until(dict(_PV, dateTo=None), 8490, 13890) is ana.NO_WINDOW)


# ==== price windows: null only on positive evidence of a standing price (final pass, 2026-10-04) ====
# Cases where the source cannot tell now leave the key out ('unknown') instead of null ('standing').
sec, anh, nep, ana = (sys.modules[_k] for _k in ("setec", "anhoch", "neptun", "ananas"))  # names reused above
# Setec: the index names the list (price_list_id); null id = Medusa charged the base price (standing).
_iMac_lists = [{"price_list": {"id": "plist_WEB", "title": "Web Prices", "role": "club", "starts_at": None, "ends_at": None},
                "prices": {"amount": "114659"}}]
check("setec: price_list_id null (base price charged, stale club list above it) -> null",
      sec.price_list_window(_iMac_lists, 99999, None, id_known=True) is None)
check("setec: price_list_id not among the price lists -> unknown",
      sec.price_list_window([_WEB], 1409, "plist_GONE", id_known=True) is sec.NO_WINDOW)
check("setec: ids that disagree on ends_at -> unknown",
      sec.price_list_window([_PROMO, dict(_PROMO, price_list=dict(_PROMO["price_list"], ends_at=None))], 999,
                            "plist_PROMO", id_known=True) is sec.NO_WINDOW)
check("setec: unreadable ends_at -> unknown",
      sec.price_list_window([dict(_PROMO, price_list=dict(_PROMO["price_list"], ends_at="soon"))], 999,
                            "plist_PROMO", id_known=True) is sec.NO_WINDOW)
check("setec: detail API, no list with the charged amount -> unknown",
      sec.price_list_window(_iMac_lists, 99999) is sec.NO_WINDOW)
check("setec: detail API, no price list at all (base price) -> null", sec.price_list_window([], 99999) is None)
check("setec: detail API, price lists missing -> unknown", sec.price_list_window(None, 99999) is sec.NO_WINDOW)
check("setec: no price -> unknown", sec.price_list_window([_WEB], None, "plist_WEB", id_known=True) is sec.NO_WINDOW)
_ih = {"id": "prod_imac", "title": "Apple iMac 24", "handle": "imac", "price_lists": _iMac_lists, "total_web_quantity": 5,
       "variants": [{"calculated_price": {"calculated_amount": 99999, "original_amount": 99999,
                                          "calculated_price": {"price_list_id": None}}}]}
_ir = sec.record_from_hit(_ih, 3)
check("setec listing: base price charged -> null (key present)", "price_valid_until" in _ir and _ir["price_valid_until"] is None, _ir)
_ir = sec.record_from_hit(dict(_ih, variants=[{"calculated_price": {"calculated_amount": 99999,
                                                                     "calculated_price": {"price_list_id": "plist_GONE"}}}]), 3)
check("setec listing: unmatched price_list_id -> key left out", "price_valid_until" not in _ir, _ir)
_ir = sec.record_from_hit(dict(_ih, variants=[{"calculated_price": {"calculated_amount": 99999}}]), 3)
check("setec listing: no price_list_id key -> amount match (none) -> key left out", "price_valid_until" not in _ir, _ir)


def _setec_detail(hit, product):
    """Run setec.cmd_detail offline on one handle: the index returns `hit`, the detail API `product`."""
    import argparse as _ap
    saved = (sec.search_body, sec._request, sec.order_threshold, sec.emit)
    got = []
    try:
        sec.search_body = lambda body: {"hits": [hit] if hit else []}
        sec._request = lambda *a, **k: {"product": product}
        sec.order_threshold = lambda: 3
        sec.emit = lambda recs, path, kind="products": got.extend(recs)
        sec.cmd_detail(_ap.Namespace(inputs=["imac"], json="x.json"))
    finally:
        sec.search_body, sec._request, sec.order_threshold, sec.emit = saved
    return got[0]


_ip = {"id": "prod_imac", "title": "Apple iMac 24", "price_lists": _iMac_lists,
       "variants": [{"calculated_price": {"calculated_amount": 99999, "original_amount": 99999}, "total_web_quantity": 5}],
       "product_extra_details": {"output_warranty": 24}, "description": "x"}
_dr = _setec_detail(_ih, _ip)
check("setec detail: the index's null (base price) survives the amount-only detail API", _dr.get("price_valid_until", "ABSENT") is None, _dr)
_dr = _setec_detail(_ih, dict(_ip, variants=[{"calculated_price": {"calculated_amount": 98999}, "total_web_quantity": 5}]))
check("setec detail: price moved and no list has it -> key left out", "price_valid_until" not in _dr and _dr["price_mkd"] == 98999, _dr)
_dr = _setec_detail(dict(_ih, variants=[{"calculated_price": {"calculated_amount": 1409, "calculated_price": {"price_list_id": "plist_GONE"}}}],
                         price_lists=[_WEB]),
                    dict(_ip, price_lists=[_WEB], variants=[{"calculated_price": {"calculated_amount": 1409}, "total_web_quantity": 5}]))
check("setec detail: index cannot tell, detail amount-matches the standing list -> null", _dr.get("price_valid_until", "ABSENT") is None, _dr)

# Anhoch: null = no markdown (selling == list price) or the markdown IS an open-ended special price.
check("anhoch: markdown with no special price behind it -> key left out",
      "price_valid_until" not in _lr(special_price=None))
check("anhoch: unreadable special_price_end -> key left out",
      "price_valid_until" not in _lr(special_price_end="whenever"))
check("anhoch: open-ended special still null", _lr().get("price_valid_until", "ABSENT") is None)
check("anhoch: percent special that produces the charged price -> its end",
      _lr(special_price={"amount": "20.0000"}, special_price_type="percent", price=_M(2500), selling_price=_M(2000),
          special_price_end="2099-10-31")["price_valid_until"] == "2099-10-31T23:59:59+01:00")
check("anhoch: percent special that does not produce the charged price -> key left out",
      "price_valid_until" not in _lr(special_price={"amount": "10.0000"}, special_price_type="percent", price=_M(2500),
                                     selling_price=_M(2000), special_price_end="2099-10-31"))
check("anhoch: no price at all -> key left out",
      "price_valid_until" not in _lr(selling_price=None, price=None, special_price=None))
check("anhoch: special_until is the helper both records use",
      anh.special_until(dict(_row, special_price=None), 1990, 2490) is anh.NO_WINDOW
      and anh.special_until(dict(_row, selling_price=_M(2490), special_price=None), 2490, None) is None)

# Neptun: price_mkd null only when it is RegularPrice; haPPy null only with no promotion attached at all.
_N4 = {"Id": 300844, "RegularPrice": 99999.0, "DiscountPrice": 94999.0, "DiscountPriceType": 4, "DiscountPriceName": "haPPy",
       "WebshopDiscountPrice": 0, "ActualPrice": 94999.0, "PromotionId": 0, "WebPromotionId": 0, "Promotions": []}
check("neptun: type-4 haPPy price with no promotion attached (LG 65 B6, 2026-10-04) -> both null",
      nep.Neptun.price_windows(_N4) == {"price_valid_until": None, "member_price_valid_until": None})
check("neptun: haPPy price, PromotionId 0 but a brand promotion listed (Honor 600 Pro) -> member window left out",
      nep.Neptun.price_windows(dict(_N4, Promotions=[{"Id": -3190, "PromotionName": "Honor 600 & 600 Pro+",
                                                      "ValidTo": "/Date(1793487540000)/"}])) == {"price_valid_until": None})
_PUB = {"Id": 9, "RegularPrice": 9999.0, "DiscountPrice": 7999.0, "DiscountPriceType": 2, "DiscountPriceName": "Акција",
        "WebshopDiscountPrice": 0, "PromotionId": 0, "WebPromotionId": 0, "Promotions": []}
check("neptun: public discount, PromotionId 0 and no promotions -> key left out (no evidence it is standing)",
      nep.Neptun.price_windows(_PUB) == {})
check("neptun: public discount with its promotion named -> its end",
      nep.Neptun.price_windows(dict(_PUB, PromotionId=5, Promotions=[{"Id": 5, "ValidTo": "/Date(1791151140000)/"}]))
      == {"price_valid_until": "2026-10-04T23:59:00+02:00"})
check("neptun: online price, WebPromotionId 0 and no promotions -> key left out",
      nep.Neptun.price_windows(dict(_PUB, DiscountPrice=0, DiscountPriceType=0, WebshopDiscountPrice=8999)) == {})
check("neptun: price from another source (ActualPrice only) -> key left out",
      nep.Neptun.price_windows({"RegularPrice": 0, "WebshopDiscountPrice": 0, "DiscountPrice": 0, "ActualPrice": 4999,
                                "PromotionId": 0, "Promotions": []}) == {})
check("neptun: plain regular price -> null",
      nep.Neptun.price_windows(dict(_PUB, DiscountPrice=0, DiscountPriceType=0)) == {"price_valid_until": None})
check("neptun detail record: public discount with an unnamed promotion leaves the key out",
      "price_valid_until" not in N.detail_record(dict(P, DiscountPrice=7999, DiscountPriceType=2, DiscountPriceName="Акција",
                                                      PromotionId=0, Promotions=[]), [], "7"))

# Ananas: a detail page without priceV2 cannot tell.
check("ananas detail: no priceV2 -> no window (left out)", ana.Ananas.sale_valid_until({}, 8490, None) is ana.NO_WINDOW)
check("ananas detail: priceV2 without a running sale still null",
      ana.Ananas.sale_valid_until({"discountType": None, "dateTo": None, "sellablePrice": 4290}, 4290, 4290) is None)


# ==== group re-check, fresh live runs (TVs, phones, printers + consumables; 2026-10-04) ====
# Spec tokens are never model codes: model years, sizes, volumes, port / zone counts.
for _t, _bad in (("Заштитна фолија за хауба GrizzProtector BodyShield, Audi A6 C8 Sedan 2018-2023, транспарентна", "20182023"),
                 ("Epson Value Glossy Photo Paper, 10x15 cm, 100 Листови", "10x15cm"),
                 ("Printer multifunksional Epson EcoTank L3270, A4, 5760x1440 dpi, i zi", "5760x1440dpi"),
                 ("Мастило за печатач PRISM Canon GI-41BK, 135ml, црно", "135ml"),
                 ("TP-Link Switch 24port 10/100/1000 TL-SG3428 Rack mountable w/4 SFP ports", "24port"),
                 ("Keyboard SteelSeries Apex 3 TKL Lavander Gaming IP32 Resistant, Whisper-quiet switches, 8-Zone RGB", "8zone"),
                 ("Asus Матична Плоча Am4 A520M-Plus Ii Tuf Gaming 4Xddr4 4866Mhz(O.C), 1xPCIEx16, 2xPCIEx1", "4xddr4"),
                 ("USB Hub Baseus MagPro Series 7-in-1 Type-C w/15W Qi2/2xUSB-A/USB-C/USB-C PD100W/HDMI", "2xusba")):
    check(f"spec is no code: {_bad}", _bad not in m.signature(_t).strong, m.signature(_t).strong)
check("real codes that look like specs stay codes (Lenovo 4X..., Logitech 920-..., GI-41BK)",
      {"4xd1m45627"} <= m.signature("Слушалки Lenovo 4XD1M45627, црни").strong
      and {"920014207"} <= m.signature("Logitech 920-014207").strong
      and {"gi41bk"} <= m.signature("Мастило за печатач PRISM Canon GI-41BK, 135ml, црно").strong)
check("volume is a unit, written apart or in Cyrillic",
      [m.signature(t).units.get("ml") for t in ("Epson Шише со жолто мастило, 103, 65 ml", "PRISM GI-41BK, 135ml", "Шампон 250мл")]
      == [{65}, {135}, {250}])
check("a size kit keeps 2x16GB", m.signature("Kingston FURY 2x16GB DDR4").units.get("kit") == {"2x16gb"})
check("group: car foils for different parts sharing only model years stay apart",
      _groups([_g("zirafamall", "1", "Заштитна фолија за хауба GrizzProtector BodyShield, Audi A6 C8 Sedan 2018-2023, транспарентна", brand=None),
               _g("zirafamall", "2", "Заштитна фолија за багажник Grizz GrizzProtector, Audi A6 C8 Седан 2018-2023, транспарентна", brand=None)])
      == [["1"], ["2"]])
check("group: Epson and HP photo paper sharing only '10x15 cm' stay apart",
      _groups([_g("zirafamall", "e", "Epson Value Glossy Photo Paper, 10x15 cm, 100 Листови", brand=None),
               _g("zirafamall", "h", "HP Everyday Glossy Photo Paper, 10x15 cm, 200 gsm, 100 Sheets", brand=None)]) == [["e"], ["h"]])
check("group: two TP-Link switches sharing only '24port' stay apart",
      _groups([_g("anhoch", "1", "TP-Link Switch 24port 10/100/1000 TL-SG3428 Rack mountable w/4 SFP ports", brand="TP-Link", price_mkd=11380),
               _g("anhoch", "2", "TP-Link Switch 24port Gigabit + 4SFP slots FS328G", brand="TP-Link", price_mkd=16980)]) == [["1"], ["2"]])
check("group: Apex 3 TKL Lavander / Amethyst sharing only '8-Zone' stay apart",
      _groups([_g("anhoch", "1", "Keyboard SteelSeries Apex 3 TKL Lavander Gaming IP32 Resistant, Whisper-quiet switches, 8-Zone RGB", brand="SteelSeries"),
               _g("anhoch", "2", "Keyboard SteelSeries Apex 3 TKL Amethyst Gaming IP32 Resistant, Whisper-quiet switches, 8-Zone RGB", brand="SteelSeries")])
      == [["1"], ["2"]])

# A toner or ink 'for' a printer is not the printer (consumable vs printer type).
check("product type: toner / cartridge / refill are consumables",
      [m.signature(t).ptype for t in ("Toner HP 150A Black for M111a/M111w/M141a/M141w", "Компатибилен кертриџ HP W1500A, 975 страници, црн",
                                      "TJ ink refill for Canon Pixma G1420/G2420 black 135ml", "Тонер за печатач Asarto за Epson 103YN C13T00S44A, 70 ml, жолт")]
      == ["consumable"] * 4)
check("product type: an MFP / печатач is a printer even when it names its toner",
      [m.signature(t).ptype for t in ("HP LJ Pro MFP 4103dw, 2Z627A, toner W1510A/X", "Мултифункционален печатач HP LaserJet M234SDN, бело/црн")]
      == ["printer", "printer"])
check("product type: a noun right after 'за' / 'for' is what the item is for",
      m.signature("Боја за печатач Epson C13T00S14A, EcoTank 103, black").ptype is None
      and m.signature("Маска за телефон Samsung Galaxy A36").ptype != "phone"
      and m.signature("Слушалки за игри Razer BlackShark V2 X").ptype == "headphones")
check("group: Toner HP 150A 'for M111a/M111w' never joins the M111W printer (3,490 vs 5,990: inside the 3x guard)",
      _groups([_g("anhoch", "t", "Toner HP 150A Black for M111a/M111w/M141a/M141w", brand="HP", price_mkd=3490),
               _g("setra", "p", "HP LaserJet M111W Wi-Fi Monochrome Printer", brand="HP", price_mkd=5990)]) == [["p"], ["t"]])
check("group: a toner never joins the printer whose title names that toner",
      _groups([_g("ddstore", "t", "Toner HP 151A Black (W1510A) — 3 050 pages, for HP Pro 4003 / 4103", brand="Hp", price_mkd=8566),
               _g("ddstore", "p", "HP LJ Pro MFP 4103dw, 2Z627A, toner W1510A/X", brand="Hp", price_mkd=21939),
               _g("ddstore", "q", "HP LJ Pro MFP 4103dw Printer", brand="Hp", price_mkd=20495)]) == [["p", "q"], ["t"]])

# Compatible consumables: another volume, or a maker named only after 'for', is another product.
check("group: compatible 70 ml Epson 103 ink stays apart from the 65 ml original (shared C13T00S44A)",
      _groups([_g("ddstore", "o", "Ink Bottle Epson 103 Yellow 65 ml (C13T00S44A), for Epson EcoTank L3351 / L3366 / L3356",
                  "8715946655871", brand="Epson", price_mkd=679),
               _g("gjirafa50", "c", "Тонер за печатач Asarto за Epson 103YN C13T00S44A, 70 ml, жолт", brand=None, price_mkd=590)])
      == [["c"], ["o"]])
_tj = m.signature("TJ ink refill for Canon Pixma G1420/G2420/G2460/G3420/G3460/G3470 black 135ml (6000p.) 4528C001", "TopJet")
check("for_only: the brand a refill is 'for' is not its maker", "canon" in _tj.for_only
      and not m._brand_ok(_tj, m.signature("Ink Bottle Canon GI-41 Black 135 ml (4528C001)", "Canon"))[0])
check("for_only: a brand also named outside 'for' still confirms",
      m._brand_ok(m.signature("Cart.Epson 103 Magenta Ink bottle 65ml for L1110/L3111/L3151", "Epson"),
                  m.signature("Ink Bottle Epson 103 Magenta 65 ml (C13T00S34A), for Epson EcoTank L3351", "Epson")) == (True, True)
      and m._brand_on("hp", m.signature("Toner HP 150A Black for M111a/M111w/M141a/M141w")))
check("group: a TopJet refill printing Canon's part number stays apart from the Canon bottle",
      _groups([_g("ddstore", "tj", "TJ ink refill for Canon Pixma G1420/G2420/G2460/G3420/G3460/G3470 black 135ml (6000p.) 4528C001",
                  brand="TopJet", mpn="4528C001", price_mkd=250),
               _g("ddstore", "cn", "Ink Bottle Canon GI-41 Black 135 ml (4528C001) — 7 600 pages, for Canon PIXMA G1420 / G2420 / G2460",
                  brand="Canon", mpn="4528C001", price_mkd=912)]) == [["cn"], ["tj"]])
check("group: an Utax / Anpoll toner 'for HP' stays apart from HP's own (same W-number)",
      _groups([_g("ddstore", "u", "HQ drum for HP CLJ 150/A/NW, 178NW,179NW (16k.) No.120A W1120A", brand="Anpoll", mpn="W1120A", price_mkd=3000),
               _g("ddstore", "h", "HP Drum for CLJ 150/A/NW, 178NW,179NW (16k.) No.120A W1120A", brand="Hp", mpn="W1120A", price_mkd=8602)])
      == [["h"], ["u"]])
check("group: the same original sold by two shops still joins on its code",
      _groups([_g("anhoch", "a", "Cart.Epson 103 Black ink bottle 65ml for L1110/L3111/L3151/L1250/L1210/L3211/L3251/L3550", brand="Epson", price_mkd=680),
               _g("tehnomarket", "t", "Cart.Epson 103 Black ink bottle 65ml for L1110/L3111/L3151/L1250/L1210/L3211/L3251/L3550", brand="EPSON", price_mkd=749)])
      == [["a", "t"]])


# A region / packaging tail written apart ('UE 55 U8072H UXXH') or a technology word ('4K QLED') at the
# end of a title is no possible colour: the 55" listing that names its colour is the same product.
check("maybe-colour: no 'uxxh?' / 'qled?'",
      m.signature("SAMSUNG UE 55 U8072H UXXH", "SAMSUNG").maybe_colour == set()
      and m.signature('Телевизор SAMSUNG QE 55 Q7F AAUXXH, 55", 4K QLED', "SAMSUNG").maybe_colour == set()
      and m.signature("Headphones JBL Live 770NC ANC Wireless Sandstone", "JBL").maybe_colour == {"sandstone"})
_r = m.score_candidate(m.signature("SAMSUNG UE 55 U8072H UXXH", "SAMSUNG", "8806097977698"),
                       m.signature("Samsung Паметен телевизор, Crystal UHD 4K, U8072H, 55 инчи, Црн", "Samsung"), {"store": "ananas"})
check("match: UE 55 U8072H UXXH ~ 'U8072H, 55 инчи, Црн' is the same product", _r.get("group") == "same", _r)



# Setec client, self-contained since setec_core was folded in (2026-10-09).
import contextlib as _ctx, io as _io  # noqa: E401,E402
_sspec = importlib.util.spec_from_file_location("setec_client", SKILL + "/scripts/stores/setec.py")
_sc = importlib.util.module_from_spec(_sspec)
_sspec.loader.exec_module(_sc)
check("setec decode_handle", (_sc.decode_handle("napo-d1-98uvanja-29"),
                              _sc.decode_handle("telefoni-2ffoto-20i-20navigatsi-d1-98a-66"))
      == ("napojuvanja-29", "telefoni/foto i navigatsija-66"))
check("setec key regex", _sc._KEY_RE.search('let o=(0,a.m)("https://search.sp.solslab.dev/","' + "a" * 64
                                            + '"),c="products";').groups()
      == ("https://search.sp.solslab.dev", "a" * 64))
_saved = (_sc.resolve_category, _sc.search_body)
_sc.resolve_category = lambda a: ("Empty", "product_categories.id = 'x'", None)
_sc.search_body = lambda body: {"facetDistribution": {"status": {}, "attribute_pairs": {}}}
_err = _io.StringIO()
with _ctx.redirect_stderr(_err), _ctx.redirect_stdout(_io.StringIO()):
    _rc = _sc.main(["facets", "empty-cat"])
_sc.resolve_category, _sc.search_body = _saved
check("setec facets on an empty category: exit 2 with an unindented ERROR line",
      _rc == 2 and any(l.startswith("ERROR: ") for l in _err.getvalue().splitlines()), (_rc, _err.getvalue()))


class _FakeResp:
    def __init__(self, status, headers=None, body="{}"):
        self.status_code, self.headers, self.text, self.content = status, headers or {}, body, body.encode()
        self.url = "https://search.sp.solslab.dev/indexes/products/search"
        self.request = type("R", (), {"method": "POST"})()

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def request(self, *a, **k):
        self.calls += 1
        return self.resp


import json  # noqa: E402
_saved = (_sc.session, _sc.time.sleep, _sc._rediscover_key, _sc.QUIET)
_slept = []
_sc.time.sleep = _slept.append
_sc.QUIET = True
_fs = _FakeSession(_FakeResp(429, {"retry-after": "3600", "content-type": "application/json"}))
_sc.session = lambda: _fs
try:
    _sc._request("POST", "https://search.sp.solslab.dev/x", json={})
    _got = None
except _sc.RateLimited as e:
    _got = str(e)
check("setec: persistent 429 -> RateLimited after MAX_TRIES, Retry-After capped",
      _got is not None and "429" in _got and _fs.calls == _sc.MAX_TRIES
      and [s for s in _slept if s >= 1] and max(_slept) <= _sc.MAX_RETRY_AFTER_S, (_got, _fs.calls, _slept))
_sc._threshold[0] = None
check("setec: persistent 429 on web-config falls back to the default threshold",
      _sc.order_threshold() == _sc.DEFAULT_ORDER_THRESHOLD, _sc._threshold)
_sc._threshold[0] = None
_err = _io.StringIO()
with _ctx.redirect_stderr(_err), _ctx.redirect_stdout(_io.StringIO()):
    _rc = _sc.main(["brands"])
check("setec: persistent 429 on a required call exits 3 with BLOCKED",
      _rc == 3 and any(l.startswith("BLOCKED: ") for l in _err.getvalue().splitlines()), (_rc, _err.getvalue()[-300:]))
_fs = _FakeSession(_FakeResp(401, {"content-type": "application/json"}, '{"message": "invalid_api_key"}'))
_sc.session = lambda: _fs
_sc._rediscover_key = lambda: False
try:
    _sc.search_body({"q": "x"})
    _got = None
except _sc.StoreError as e:
    _got = str(e)
check("setec: rejected key that cannot be re-discovered -> StoreError, not a traceback",
      _got is not None and "could not be re-discovered" in _got, _got)
_sc.session, _sc.time.sleep, _sc._rediscover_key, _sc.QUIET = _saved
check("setec: scheme-less setec.mk URLs get https://",
      (_sc._with_scheme("setec.mk/products/x"), _sc._with_scheme("www.setec.mk/category/y"), _sc._with_scheme("prod_1"))
      == ("https://setec.mk/products/x", "https://www.setec.mk/category/y", "prod_1"))
check("setec: a capitalised host is still a setec.mk URL",
      [r[2] for r in _sc.resolve_products(["Setec.mk/products/abc", "https://SETEC.MK/products/x"])] == [None, None]
      and [r[1] for r in _sc.resolve_products(["Setec.mk/products/abc"])] == ["abc"])


# ==== relevance counts and --strict across spacing (2026-10-09) ====
check("--strict matches across spacing: 'rtx5070' ~ 'RTX 5070'",
      m._passes_strict({"title": "MSI GeForce RTX 5070 VENTUS 2X OC 12GB"}, ["rtx5070"]))
check("--strict still needs every word", not m._passes_strict({"title": "MSI GeForce RTX 5060 VENTUS"}, ["rtx 5070"]))
_glass = [dict(store="ananas", id=f"g{i}", title="Заштитно стакло за Samsung Galaxy A56 - 9H", found_by=["galaxy a56"])
          for i in range(9)]
_phones = [dict(store="ananas", id=f"p{i}", title=f"Samsung Galaxy A56 5G 8/256GB {c}", found_by=["galaxy a56"])
           for i, c in enumerate(("Graphite", "Olive", "Pink"))]
_case = [dict(store="setec", id=f"c{i}", title="Футрола за Samsung Galaxy A56", found_by=["galaxy a56 case"])
         for i in range(12)]
_other = [dict(store="neptun", id="n1", title="Philips 55PUS8000", found_by=["galaxy a56"])]
_recs = m.annotate(_glass + _phones + _case + _other)
_st = {"ananas": {"status": "ok"}, "setec": {"status": "ok"}, "neptun": {"status": "ok"}, "ddstore": {"status": "blocked"},
       "setra": {"status": "timeout"}}
m.relevance_counts(_recs, _st)
check("relevance: counts per shop, accessories noted when they are most of 10+ hits",
      _st["ananas"]["relevance"] == {"hits": 12, "all_words": 12, "accessories": 9}
      and _st["ananas"].get("notes") == ["mostly accessories (9 of 12)"], _st["ananas"])
check("relevance: hits a query for an accessory found are not counted as accessories",
      _st["setec"]["relevance"]["accessories"] == 0 and not _st["setec"].get("notes"), _st["setec"])
check("relevance: off-target hits show as a low all-words count, without a note",
      _st["neptun"]["relevance"] == {"hits": 1, "all_words": 0, "accessories": 0} and not _st["neptun"].get("notes"),
      _st["neptun"])
check("relevance: a shop that did not answer gets no counts",
      "relevance" not in _st["ddstore"] and "relevance" not in _st["setra"], (_st["ddstore"], _st["setra"]))
import argparse as _ap, io as _io2  # noqa: E401,E402
_buf = _io2.StringIO()
m.print_categories([{"store": "gjirafa50", "id": None, "slug": "monitore-teknologji", "path": "Компјутери > Монитори",
                     "url": "https://gjirafa50.mk/monitore-teknologji", "count": 120},
                    {"store": "ddstore", "id": "kompjuterska-oprema/monitori/gejmerski-monitori", "path": "Монитори",
                     "count": 40}], _ap.Namespace(show=None, grep="monitor", urls=False), _buf)
check("categories table: a row without an id shows its slug, long ids are not cut",
      "monitore-teknologji" in _buf.getvalue() and "kompjuterska-oprema/monitori/gejmerski-monitori" in _buf.getvalue(),
      _buf.getvalue())
_foot = m.footer_line(dict(_st, ananas=dict(_st["ananas"], queries=[{"query": "galaxy a56", "hit_limit": True}],
                                            warnings=["WARNING: x"])), 25)
check("footer names shops that hit the limit, are mostly accessories or warned",
      "more may exist (hit --limit-per-store): ananas" in _foot and "mostly accessories: ananas" in _foot
      and "warnings (see the status table): ananas" in _foot, _foot)
check("status note carries the accessories note", "mostly accessories (9 of 12)" in m._status_note(_st["ananas"]),
      m._status_note(_st["ananas"]))
_long = "Компјутери и IT опрема > Монитори и Додатоци > Монитори > Гејмерски монитори 27"
check("category paths are cut from the left so the leaf stays",
      m._path_tail(_long, 50) == "… > Монитори > Гејмерски монитори 27" and m._path_tail("Short > Path") == "Short > Path",
      m._path_tail(_long, 50))

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
