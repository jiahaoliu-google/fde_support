import os
import sys
import unittest
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import no_network  # noqa: F401,E402  (blocks outbound network)
from frontend.server import app

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
        # Same-origin relative API calls (no hardcoded backend host in the page).
        self.assertIn("const BACKEND_URL = '';", html_content)
        self.assertNotIn("http://127.0.0.1:8000", html_content)

if __name__ == "__main__":
    unittest.main()
