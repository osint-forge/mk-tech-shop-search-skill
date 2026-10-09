# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.28", "beautifulsoup4>=4.11"]
# ///
"""Offline checks that the skill's docs match its code: SKILL.md frontmatter, every shop has a
reference and a table row, and every command and --flag the docs name exists in mkshop.py or
the store client the doc is about. Run: python3 -B tests/test_docs.py"""
import argparse
import glob
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
# The skill under test: this checkout's copy, or MKSHOP_SKILL_DIR (e.g. an installed copy).
SKILL = os.environ.get("MKSHOP_SKILL_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "mk-tech-shop-search")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
import mkshop as m  # noqa: E402

fails = 0


def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        fails += 1


def read(rel):
    with open(os.path.join(SKILL, rel), encoding="utf-8") as f:
        return f.read()


# ---- frontmatter: the portable Agent Skills fields only (agentskills.io/specification)
skill_md = read("SKILL.md")
fm = re.match(r"---\n(.*?)\n---\n", skill_md, re.S)
check("SKILL.md starts with a frontmatter block", fm is not None)
fields = dict(re.findall(r"^([a-z][a-z-]*): ?(.*)$", fm.group(1), re.M)) if fm else {}
check("frontmatter keys are portable",
      set(fields) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}, set(fields))
name, desc = fields.get("name", ""), fields.get("description", "")
check("name matches the skill directory", name == os.path.basename(os.path.normpath(SKILL)), name)
check("name is 1-64 lowercase letters, digits and single hyphens",
      re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) is not None and len(name) <= 64, name)
check("description is 1-1024 characters", 0 < len(desc) <= 1024, len(desc))
check("description has no angle brackets and no unquoted ': '",
      not re.search(r"[<>]", desc) and (": " not in desc or desc[:1] in "\"'"), desc[:80])
check("SKILL.md body stays under 500 lines", skill_md.count("\n") < 500, skill_md.count("\n"))
check("compatibility, when present, is 1-500 characters",
      "compatibility" not in fields or 0 < len(fields["compatibility"]) <= 500, len(fields.get("compatibility", "")))
_lic = os.path.join(os.path.dirname(os.path.dirname(os.path.normpath(SKILL))), "LICENSE")
if "license" in fields and os.path.exists(_lic):
    with open(_lic, encoding="utf-8") as f:
        check("frontmatter license matches the repo's LICENSE file", f.readline().split()[0] == fields["license"],
              fields["license"])

# ---- every shop: a reference file and a row in SKILL.md's store table
refs = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(SKILL, "references", "*.md"))}
for key in m.REGISTRY:
    ref = "gjirafa" if key in ("gjirafa50", "zirafamall") else key
    check(f"{key}: references/{ref}.md exists", ref in refs)
    check(f"{key}: row in SKILL.md's store table", re.search(r"^\| `" + key + r"` \|", skill_md, re.M) is not None)
    check(f"{key}: client script exists", os.path.exists(os.path.join(SKILL, "scripts", "stores", m.REGISTRY[key]["script"])))

# ---- the real CLI surface: mkshop.py from its parser, clients from their -h output
def parser_surface(parser):
    subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    top = {o for a in parser._actions for o in a.option_strings}
    return {cmd: top | {o for a in p._actions for o in a.option_strings} for cmd, p in subs.choices.items()}


def client_surface(script):
    pre = [script] + (["--site", "gjirafa50"] if script.endswith("gjirafa.py") else [])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def helptext(args):
        p = subprocess.run([sys.executable, "-B"] + args + ["-h"], capture_output=True, text=True, env=env, timeout=60)
        return p.stdout + p.stderr

    top = helptext(pre)
    cmds = re.search(r"\{([a-z_,-]+)\}", top).group(1).split(",")
    top_flags = set(re.findall(r"(?<![\w-])(--?[a-z][a-z0-9-]*)", top.split("positional arguments")[0]))
    return {c: top_flags | set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", helptext(pre + [c]))) for c in cmds}


MKSHOP = parser_surface(m.build_parser())
CLIENTS = {os.path.basename(p): client_surface(p)
           for p in sorted(glob.glob(os.path.join(SKILL, "scripts", "stores", "*.py")))}
ALL_FLAGS = set().union(*MKSHOP.values(), *(f for c in CLIENTS.values() for f in c.values()))
check("mkshop.py exposes its commands", {"search", "list", "detail", "match", "group"} <= set(MKSHOP), set(MKSHOP))


def code_spans(text):
    """Text inside fenced code blocks and inline `code`: where commands and flags are named."""
    blocks = re.findall(r"```.*?```", text, re.S)
    rest = re.sub(r"```.*?```", "", text, flags=re.S)
    return blocks + re.findall(r"`([^`\n]+)`", rest)


def doc_problems(text, allowed_flags):
    bad = []
    for span in code_spans(text):
        for script, cmd in re.findall(r"(?<![\w<>-])([a-z]+)\.py\s+([a-z][a-z-]*)\b", span):
            known = MKSHOP if script == "mkshop" else CLIENTS.get(script + ".py")
            if known is not None and cmd not in known:
                bad.append(f"{script}.py {cmd}")
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", span):
            if flag not in allowed_flags:
                bad.append(flag)
    return sorted(set(bad))


check("SKILL.md names only real commands and flags", not doc_problems(skill_md, ALL_FLAGS),
      doc_problems(skill_md, ALL_FLAGS))
check("client-contract.md names only real commands and flags",
      not doc_problems(read("references/client-contract.md"), ALL_FLAGS),
      doc_problems(read("references/client-contract.md"), ALL_FLAGS))
for ref in sorted(refs - {"client-contract"}):
    client = "gjirafa.py" if ref == "gjirafa" else ref + ".py"
    allowed = set().union(*MKSHOP.values(), *CLIENTS.get(client, {}).values())
    problems = doc_problems(read(f"references/{ref}.md"), allowed)
    check(f"references/{ref}.md names only real commands and flags of mkshop.py and {client}", not problems, problems)


# ---- every script declares its dependencies the same way (PEP 723), so `uv run` works on each and
# they all share one cached environment
_hdr_re = re.compile(r"^# /// script\n((?:#.*\n)*?)# ///$", re.M)
_headers = {}
for _p in [os.path.join(SKILL, "scripts", "mkshop.py")] + sorted(glob.glob(os.path.join(SKILL, "scripts", "stores", "*.py"))):
    with open(_p, encoding="utf-8") as f:
        _m = _hdr_re.search(f.read())
    _headers[os.path.basename(_p)] = _m.group(1) if _m else None
check("every script has a PEP 723 header", all(_headers.values()), [k for k, v in _headers.items() if not v])
check("the headers are identical and name requests and beautifulsoup4",
      len(set(_headers.values())) == 1 and all(d in next(iter(_headers.values())) or "" for d in ("requests", "beautifulsoup4")),
      set(_headers.values()))

# ---- no hidden text: invisible format/bidi characters, variation selectors or Unicode tags in the
# skill's instructions and code, or in the tests (write them as \u escapes)
import unicodedata  # noqa: E402
_repo = os.path.dirname(os.path.dirname(os.path.normpath(SKILL)))
_srcs = [p for p in glob.glob(os.path.join(SKILL, "**", "*"), recursive=True) if p.endswith((".md", ".py", ".html"))]
_srcs += glob.glob(os.path.join(_repo, "tests", "*.py")) + [os.path.join(_repo, "README.md")]
_hidden = []
for _p in _srcs:
    if not os.path.isfile(_p):
        continue
    with open(_p, encoding="utf-8") as f:
        for _n, _line in enumerate(f, 1):
            if any(unicodedata.category(c) == "Cf" or 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF
                   for c in _line):
                _hidden.append(f"{os.path.relpath(_p, _repo)}:{_n}")
check("no invisible or bidi characters in the skill, its tests or the README", not _hidden, _hidden[:10])

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
