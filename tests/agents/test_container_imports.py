"""Each agent must import with its own directory as the import root, as on Agent Runtime.

Regression: absolute ``agents.faq_assistant...`` imports passed every repo-root test but
crashed the deployed FAQ agent with ``ModuleNotFoundError: No module named 'agents'``.
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGENT_DIRS = ("faq_assistant", "sentiment_router")

# Runs inside the agent directory (the container's WORKDIR) with the repo root removed.
_PROBE = """
import os, sys
root = os.path.realpath(sys.argv[1])
sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.getcwd()) != root]
try:
    import agents  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("repo root is still importable; the probe would not catch the bug")
import agent
print("imported", agent.app.name)
"""


class TestContainerImports(unittest.TestCase):
    def test_agents_import_without_repo_root(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for name in AGENT_DIRS:
            with self.subTest(agent=name):
                proc = subprocess.run(
                    [sys.executable, "-c", _PROBE, ROOT],
                    cwd=os.path.join(ROOT, "agents", name),
                    env=env, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:] or proc.stdout)
                self.assertIn("imported", proc.stdout)


if __name__ == "__main__":
    unittest.main()
