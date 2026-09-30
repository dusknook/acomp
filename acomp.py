#!/usr/bin/env python
"""acomp 命令行入口（`python acomp.py ...`）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acomp.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
