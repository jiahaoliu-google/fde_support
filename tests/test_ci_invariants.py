"""CI invariant tests (Layers 2 & 3) to catch local-vs-CI drift before git push.

Layer 2: Untracked & gitignored file guards
  - Fails if any untracked `.py` file exists under `backend/`, `agents/`, `common/`,
    `frontend/`, or `tests/` so a test never passes locally due to a forgotten `git add`.
  - Verifies that gitignored `agents/*/deployment_metadata.json` files are isolated from
    tests by default (`_hide_gitignored_deployment_metadata` in `tests/conftest.py`).

Layer 3: Dependency & version drift guards
  - Verifies installed packages in the test environment satisfy `requirements.txt` and
    every `agents/*/requirements.txt`.
  - Verifies `agents/*/pyproject.toml` dependencies stay in sync with `agents/*/requirements.txt`.
  - Verifies `google-adk` version specifiers match across all requirements files.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import pathlib
import subprocess
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Optional GCP client libraries that are stubbed out in local/unit test environments
# (see tests/backend/test_model_armor_*.py and tests/helpers/) if not installed locally.
_OPTIONAL_LOCAL_PACKAGES = {
    "google-cloud-secret-manager",
    "google-cloud-modelarmor",
    "google-cloud-aiplatform",
}


def _iter_requirements(req_path: pathlib.Path) -> list[Requirement]:
    reqs: list[Requirement] = []
    for raw_line in req_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        reqs.append(Requirement(line))
    return reqs


# ---------------------------------------------------------------------------
# Layer 2: Untracked & gitignored file guards
# ---------------------------------------------------------------------------


def test_no_untracked_python_files_in_code_or_tests():
    """Fail if any untracked .py file exists in source or test directories.

    A classic cause of "passes locally, fails in CI" is creating a new helper or
    module locally and forgetting `git add`.
    """
    tracked_dirs = ["backend", "agents", "common", "frontend", "tests"]
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *tracked_dirs],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("git status unavailable in this environment")

    untracked_py = [
        line[3:].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("?? ") and line.strip().endswith(".py")
    ]
    assert not untracked_py, (
        "Untracked Python file(s) found in source/test directories (would be missing in CI): "
        f"{untracked_py}. Stage them with `git add` or remove them."
    )


def test_gitignored_deployment_metadata_cannot_leak_into_tests():
    """Verify the autouse fixture hides local `deployment_metadata.json` by default."""
    for agent_name in ("faq_assistant", "sentiment_router"):
        meta_path = REPO_ROOT / "agents" / agent_name / "deployment_metadata.json"
        with pytest.raises(FileNotFoundError, match="Gitignored in CI checkout"):
            open(meta_path, encoding="utf-8")  # noqa: SIM115



# ---------------------------------------------------------------------------
# Layer 3: Dependency & version drift guards
# ---------------------------------------------------------------------------


def test_installed_packages_satisfy_all_requirements_files():
    """Ensure local `.venv` package versions satisfy root and agent requirements.txt."""
    req_files = [
        REPO_ROOT / "requirements.txt",
        *sorted(REPO_ROOT.glob("agents/*/requirements.txt")),
    ]
    mismatches: list[str] = []

    for req_file in req_files:
        rel_path = req_file.relative_to(REPO_ROOT)
        for req in _iter_requirements(req_file):
            if req.marker and not req.marker.evaluate():
                continue
            pkg_name = canonicalize_name(req.name)
            try:
                installed_version = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                if pkg_name in _OPTIONAL_LOCAL_PACKAGES:
                    continue
                mismatches.append(f"{rel_path}: '{req}' is not installed")
                continue

            if req.specifier and not req.specifier.contains(installed_version, prereleases=True):
                mismatches.append(
                    f"{rel_path}: installed {pkg_name}=={installed_version} "
                    f"does not satisfy '{req}'"
                )

    assert not mismatches, (
        "Local environment drifted from requirements files:\n  - " + "\n  - ".join(mismatches)
    )


def test_agent_pyproject_matches_agent_requirements_txt():
    """Ensure each agent's pyproject.toml and requirements.txt declare identical specs."""
    drifts: list[str] = []

    for pyproject_path in sorted(REPO_ROOT.glob("agents/*/pyproject.toml")):
        agent_dir = pyproject_path.parent
        req_path = agent_dir / "requirements.txt"
        if not req_path.exists():
            continue

        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        pyproject_deps = {
            canonicalize_name(Requirement(dep).name): str(Requirement(dep).specifier)
            for dep in data.get("project", {}).get("dependencies", [])
        }
        req_deps = {
            canonicalize_name(req.name): str(req.specifier)
            for req in _iter_requirements(req_path)
        }

        for pkg, spec in req_deps.items():
            if pkg in pyproject_deps and pyproject_deps[pkg] != spec:
                drifts.append(
                    f"{agent_dir.name}: '{pkg}' specifier is '{spec}' in requirements.txt "
                    f"but '{pyproject_deps[pkg]}' in pyproject.toml"
                )

    assert not drifts, "Agent dependency specifiers drifted:\n  - " + "\n  - ".join(drifts)


def test_google_adk_version_specifier_consistent_across_repo():
    """Ensure root requirements.txt and all agent requirements.txt agree on google-adk."""
    req_files = [
        REPO_ROOT / "requirements.txt",
        *sorted(REPO_ROOT.glob("agents/*/requirements.txt")),
    ]
    adk_specs: dict[str, str] = {}

    for req_file in req_files:
        rel_path = str(req_file.relative_to(REPO_ROOT))
        for req in _iter_requirements(req_file):
            if canonicalize_name(req.name) == "google-adk":
                adk_specs[rel_path] = str(req.specifier)

    unique_specs = set(adk_specs.values())
    assert len(unique_specs) == 1, (
        f"Inconsistent google-adk version specifiers across requirements files: {adk_specs}"
    )


def test_frontend_package_lock_uses_public_npmjs_registry():
    """Ensure frontend/package-lock.json has no internal proxy URLs or --prefix relative keys."""
    lock_path = REPO_ROOT / "frontend" / "package-lock.json"
    assert lock_path.exists(), "frontend/package-lock.json must exist for deterministic CI builds"

    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock_data.get("packages", {})
    assert packages, "frontend/package-lock.json packages map must not be empty"

    bad_entries: list[str] = []
    for pkg_key, pkg_meta in packages.items():
        if not pkg_key:
            continue
        if not pkg_key.startswith("node_modules/"):
            bad_entries.append(f"{pkg_key}: package key must start with 'node_modules/'")
        resolved = pkg_meta.get("resolved", "")
        if resolved and not resolved.startswith("https://registry.npmjs.org/"):
            bad_entries.append(f"{pkg_key}: non-public resolved URL '{resolved}'")
        if "/-/@" in resolved:
            bad_entries.append(f"{pkg_key}: malformed scoped tarball path '{resolved}'")

    assert not bad_entries, (
        "frontend/package-lock.json contains internal proxy URLs or corrupted keys:\n  - "
        + "\n  - ".join(bad_entries[:10])
    )

