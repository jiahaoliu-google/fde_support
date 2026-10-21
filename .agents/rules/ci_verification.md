# CI Verification & Pre-Completion Rule

Whenever you modify any Python source code (`backend/`, `agents/`, `common/`, `frontend/`), tests (`tests/`), dependencies (`requirements.txt`, `pyproject.toml`), or GitHub workflows (`.github/workflows/`), you MUST verify that the GitHub Actions CI suite passes before concluding your task:

1. **Run the full CI-equivalent pytest command with coverage enforcement**:
   ```bash
   DATABASE_URL="sqlite:///./test_local.db" .venv/bin/pytest --cov --cov-report=term-missing
   ```
   - Every test must pass (`0 failed`).
   - Total coverage must meet or exceed the `fail_under = 80` threshold in `pyproject.toml`.

2. **Never rely on gitignored local artifacts in tests**:
   - Files such as `agents/*/deployment_metadata.json` and `.env` are gitignored and do not exist in GitHub Actions runners.
   - Any test that exercises code reading deployment metadata or environment config must explicitly create a temporary fixture or mock the path/env var.

3. **Keep dependency files synchronized**:
   - When updating package versions (such as `google-adk`), update `requirements.txt`, `agents/*/requirements.txt`, and `agents/*/pyproject.toml` together so `tests/test_ci_invariants.py` passes.
