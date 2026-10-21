import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


import tests.helpers.no_network  # noqa: F401,E402  (blocks outbound network)
import frontend.server as frontend_server
from frontend.server import app, FRONTEND_DIR


def _read_frontend_sources() -> str:
    """Concatenates frontend/index.html and all TypeScript/React files under frontend/src/."""
    chunks: list[str] = []
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            chunks.append(f.read())
    src_dir = os.path.join(FRONTEND_DIR, "src")
    for root, _, files in os.walk(src_dir):
        for name in sorted(files):
            if name.endswith((".ts", ".tsx", ".css")):
                full = os.path.join(root, name)
                with open(full, "r", encoding="utf-8") as f:
                    chunks.append(f.read())
    return "\n".join(chunks)


class TestFrontendServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_frontend_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "FDE Support Web Frontend")
        self.assertNotIn("port", data)

    def test_frontend_index_endpoint(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html_content = response.text
        self.assertIn("FDE Support Portal", html_content)
        self.assertIn("Smart Ticket Router & Grounded Knowledge Assistant", html_content)
        self.assertIn('id="root"', html_content)
        self.assertIn("no-store", response.headers.get("cache-control", ""))

        source_bundle = _read_frontend_sources()
        # Same-origin relative API calls (no hardcoded backend host in the client).
        self.assertIn("BACKEND_URL = '';", source_bundle)
        self.assertNotIn("http://127.0.0.1:8000", source_bundle)
        # Unified current + previous session selector and history restoration hooks
        self.assertIn('id="sessionSelect"', source_bundle)
        self.assertNotIn('id="ticketBadge"', source_bundle)
        self.assertIn("renderSessionSelect(", source_bundle)
        self.assertIn("selectPreviousSession", source_bundle)
        self.assertIn("loadPreviousSessions()", source_bundle)
        self.assertIn("loadTicketSession(", source_bundle)
        self.assertIn("renderHistoricalMessage(", source_bundle)
        self.assertIn("[EXPIRED]", source_bundle)
        self.assertNotIn("dangerouslySetInnerHTML", source_bundle)

    def test_frontend_static_assets_and_404_behavior(self):
        with tempfile.TemporaryDirectory() as tmp_assets:
            sample_js = os.path.join(tmp_assets, "app-bundle.js")
            with open(sample_js, "w", encoding="utf-8") as f:
                f.write("console.log('ready');")

            with patch.object(frontend_server, "ASSETS_DIR", tmp_assets):
                ok_res = self.client.get("/assets/app-bundle.js")
                self.assertEqual(ok_res.status_code, 200)
                self.assertIn("console.log('ready');", ok_res.text)

                missing_res = self.client.get("/assets/does-not-exist.js")
                self.assertEqual(missing_res.status_code, 404)
                self.assertEqual(missing_res.json().get("detail"), "Not Found")

        # Non-API, non-asset route falls back to SPA index shell
        spa_res = self.client.get("/tickets/deep-link")
        self.assertEqual(spa_res.status_code, 200)
        self.assertIn('id="root"', spa_res.text)

        # If Vite dist/ bundle is present, verify every referenced /assets/* file serves 200 OK
        asset_paths = re.findall(r'["\'](/assets/[^"\']+)["\']', spa_res.text)
        for asset_path in asset_paths:
            asset_res = self.client.get(asset_path)
            self.assertEqual(asset_res.status_code, 200, f"Failed to serve {asset_path}")
            self.assertGreater(len(asset_res.text), 100)


if __name__ == "__main__":
    unittest.main()
