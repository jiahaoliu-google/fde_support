# Eval report previews

When generating or previewing the KPI / evaluation report (`scripts/eval/generate_kpi_report.py`):

- Always run a **fresh full eval over all 100 golden records** first. Don't render the
  preview from an older run directory and don't take shortcuts:
  - Use `--suite full` (the default). Never pass `--limit` or `--segments`.
  - Golden set: `tests/eval/golden/golden_set_v1.json` (100 tickets).
  - Command: `.venv/bin/python tests/eval/run_inference.py --concurrency 3`
- Then build the report from that run only:
  `.venv/bin/python scripts/generate_kpi_report.py --run-dir artifacts/eval/<new-timestamp>`
- Before reporting, check that `n_cases` in the run's `system_metrics.json` is 100. If
  fewer cases ran (errors, timeouts), say so in the summary.
- The run calls real Vertex AI Gemini and Discovery Engine, so it is billed. Tell the user
  before starting, but the user has approved running all 100 records for previews.
