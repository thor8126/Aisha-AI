"""Aisha launcher — run from the project root: `python run.py [--text|--cli|--ask ...]`.

Adds src/ to the import path and starts the app. This is the simplest way to
run from source without installing the package.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from aisha.app import main  # noqa: E402

if __name__ == "__main__":
    main()
