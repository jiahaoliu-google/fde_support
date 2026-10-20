"""The pre-deployment eval must never export spans to Cloud Trace.

Regression test for PERMISSION_DENIED 'cloudtrace.traces.patch' in the agent-eval CI job:
the backend probe imports backend.services.chat -> backend.core.telemetry, which used to
install a global Cloud Trace exporter (when google-adk[gcp] is installed, as in CI). That
exporter also exported every in-process ADK agent span with the CI service account.

Each check runs in a fresh interpreter, so modules imported earlier in the test session
(and the OTEL_TRACES_EXPORTER default set by tests/conftest.py) don't hide the problem.
"""
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_PROBE = r"""
import json, os, sys
sys.path.insert(0, {repo!r}); sys.path.insert(0, os.path.join({repo!r}, "tests", "eval"))
{setup}
from backend.services import chat  # noqa: F401  (what BackendProbe imports)
from backend.core import telemetry
from opentelemetry import trace
print(json.dumps({{
    "env": os.environ.get("OTEL_TRACES_EXPORTER"),
    "exporter_installed": telemetry._instrumentation_enabled,
    "global_provider": type(trace.get_tracer_provider()).__name__,
}}))
"""


def _run(setup: str, extra_env: dict = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "OTEL_TRACES_EXPORTER"}
    env.setdefault("DATABASE_URL", "sqlite:///:memory:")
    env.update(extra_env or {})
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=REPO_ROOT, setup=setup)],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=120,
    )
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestEvalDoesNotExportTraces(unittest.TestCase):
    def test_run_inference_disables_cloud_trace_export(self):
        r = _run("import run_inference  # sets OTEL_TRACES_EXPORTER before backend imports")
        self.assertEqual(r["env"], "none")
        self.assertFalse(r["exporter_installed"])
        self.assertNotEqual(r["global_provider"], "TracerProvider")  # no SDK provider -> no export

    def test_explicit_exporter_setting_is_respected(self):
        # setdefault: someone who sets OTEL_TRACES_EXPORTER on purpose keeps their value.
        r = _run("import run_inference", {"OTEL_TRACES_EXPORTER": "otlp"})
        self.assertEqual(r["env"], "otlp")

    def test_backend_honors_otel_traces_exporter_none(self):
        r = _run("", {"OTEL_TRACES_EXPORTER": "none"})
        self.assertFalse(r["exporter_installed"])
        self.assertNotEqual(r["global_provider"], "TracerProvider")


if __name__ == "__main__":
    unittest.main()
