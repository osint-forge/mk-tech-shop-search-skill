# Variant that only accepts --site AFTER the subcommand (to test mkshop's fallback).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and sys.argv[1] == "--site":
    print("usage: gjirafa.py {info,search,...} ... --site SITE", file=sys.stderr)
    print("gjirafa.py: error: unrecognized arguments: --site " + sys.argv[2], file=sys.stderr)
    sys.exit(2)
if "--site" in sys.argv:
    i = sys.argv.index("--site"); site = sys.argv[i + 1]; del sys.argv[i:i + 2]
    sys.argv[1:1] = ["--site", site]
from _fakecore import main
sys.exit(main(None))
