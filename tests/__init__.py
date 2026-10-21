"""Automated test suites for FDE Support app."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
