"""Automated test suites for FDE Support app."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_HELPERS_DIR = os.path.join(_HERE, "helpers")

for _path in (_REPO_ROOT, _HELPERS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
