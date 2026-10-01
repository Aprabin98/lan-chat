#!/usr/bin/env python3
"""Backward-compatible entry point — the app now lives in lanchat/."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lanchat.main import main

if __name__ == "__main__":
    main()
