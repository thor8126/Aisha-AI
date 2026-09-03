"""Pytest bootstrap: put src/ on sys.path so `from aisha... import ...` works
without needing to set PYTHONPATH or install the package."""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
