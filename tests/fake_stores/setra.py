import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakecore import main
sys.exit(main("setra"))
