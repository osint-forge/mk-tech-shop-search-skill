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

- Python 3.9 or newer, with `requests` and `beautifulsoup4`.
- Linux or macOS.
- Network access to the shops (see [Network access](#network-access)).

### Installing the Python packages

```bash
python3 -m pip install requests beautifulsoup4
```

If your system's Python doesn't allow that, use a virtualenv and point the skill at it:

```bash
python3 -m venv ~/.venvs/mkshop
~/.venvs/mkshop/bin/pip install requests beautifulsoup4
export MKSHOP_PYTHON=~/.venvs/mkshop/bin/python
```

`MKSHOP_PYTHON` has to be set in the environment your agent runs commands in.

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

## Files it writes

- Each task's JSON and HTML report go into a folder the agent creates for that task.
- The Gjirafa client caches category trees for 24 hours in `$XDG_CACHE_HOME/mk-tech-shop-search/` (default `~/.cache/mk-tech-shop-search/`). Set `MKSHOP_CACHE_DIR` to use another folder.

## Development

### Tests

The tests run offline against fake shop clients. CI runs them on Python 3.9 and 3.14.

```bash
python3 -B tests/test_docs.py         # docs match the code
python3 -B tests/test_integ_fixes.py  # mkshop.py and the shop clients
python3 -B tests/test_mkshop.py       # mkshop.py end to end against fake clients
```

### Shop clients

Every shop client implements [`references/client-contract.md`](skills/mk-tech-shop-search/references/client-contract.md). Read it before fixing a client or adding a shop.

## License

MIT, see [LICENSE](LICENSE).
