# mk-tech-shop-search

An agent skill for finding, comparing and price-checking products across North Macedonian online shops.

## Shops

Setec, Neptun, Tehnomarket, Anhoch, Neksio, DDStore, Hivetec, Gjirafa50, ZirafaMall, Setra and the Ananas marketplace.

## What it does

- Searches all the shops at once and merges the results.
- Walks a shop's categories, with the shop's own attribute filters where it has them.
- Reads product detail: warranty, EAN and per-store stock where the shop publishes them.
- Finds the same product in other shops by EAN and model code.
- Explains each shop's prices, loyalty-card prices, stock and delivery terms to the agent, so every price in an answer comes with its shop, a link and its availability.
- Writes an HTML report for comparisons.

## Install

### With the skills CLI

```bash
npx skills add osint-forge/mk-tech-shop-search-skill
```

### Manually

Copy or symlink `skills/mk-tech-shop-search/` into your agent's skills folder, for example `~/.claude/skills/mk-tech-shop-search` for Claude Code.

## Requirements

- [uv](https://docs.astral.sh/uv/). The scripts declare their dependencies, so `uv run` installs them, and a suitable Python if needed, the first time it runs.
- Network access to the shops (see [Network access](#network-access)).

### Without uv

Use Python 3.9 or newer and install the two packages yourself:

```bash
python3 -m pip install requests beautifulsoup4
```

Then run the commands with `python3` instead of `uv run` (on Windows, `python` or `py -3`).

## Network access

If your agent runs with a network allowlist, allow these hosts:

| Host | Used for |
|---|---|
| `setec.mk` | Setec categories and product detail |
| `search.sp.solslab.dev` | Setec search and category listings |
| `www.neptun.mk` | Neptun |
| `www.tehnomarket.com.mk` | Tehnomarket |
| `www.anhoch.com` | Anhoch |
| `g.store.neksio.mk` | Neksio |
| `ddstore.mk` | DDStore |
| `hivetec.mk` | Hivetec |
| `gjirafa50.mk` | Gjirafa50 |
| `zirafamall.mk` | ZirafaMall |
| `setra.mk` | Setra |
| `ananas.mk` | Ananas product and category pages |
| `api.ananas.rs` | Ananas categories and delivery estimates |
| `Y1BSBVJ7AC-dsn.algolia.net` | Ananas search |

Setec and Ananas search through public, search-only keys that their own sites send to every visitor. If a shop changes its key or search host, the clients pick up the new one from the site.

The first `uv run` also downloads the packages from PyPI, and Python itself if no suitable version is installed.

## Files it writes

- Each task's JSON and HTML report go into a folder the agent creates for that task.
- The Gjirafa client caches category trees for 24 hours in `$XDG_CACHE_HOME/mk-tech-shop-search/` (default `~/.cache/mk-tech-shop-search/`). Set `MKSHOP_CACHE_DIR` to use another folder.

## Development

### Tests

The tests run offline against fake shop clients. CI runs them on Python 3.9 and 3.14.

```bash
uv run tests/test_docs.py         # docs match the code
uv run tests/test_integ_fixes.py  # mkshop.py and the shop clients
uv run tests/test_mkshop.py       # mkshop.py end to end against fake clients
```

### Shop clients

Every shop client implements [`references/client-contract.md`](skills/mk-tech-shop-search/references/client-contract.md). Read it before fixing a client or adding a shop.

## License

MIT, see [LICENSE](LICENSE).
