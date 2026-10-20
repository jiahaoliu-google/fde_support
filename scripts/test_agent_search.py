#!/usr/bin/env python3
"""Compatibility entry point delegating to scripts.dev.test_agent_search."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).parent / "dev" / "test_agent_search.py"
    sys.path.insert(0, str(Path(__file__).parent.parent))
    runpy.run_path(str(target), run_name="__main__")
