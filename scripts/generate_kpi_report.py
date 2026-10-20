#!/usr/bin/env python3
"""Compatibility entry point delegating to scripts.eval.generate_kpi_report."""
import runpy
import sys
from pathlib import Path

# The repo root must be importable before `scripts.eval` is imported (CI runs this file directly).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval.generate_kpi_report import *  # noqa: F401,F403,E402
from scripts.eval.generate_kpi_report import main, build_report  # noqa: F401,E402

if __name__ == "__main__":
    target = Path(__file__).parent / "eval" / "generate_kpi_report.py"
    runpy.run_path(str(target), run_name="__main__")
