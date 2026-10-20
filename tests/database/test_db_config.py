"""SYSTEM_ANALYSIS.md #5: production must fail fast instead of silently using SQLite.

Covers:
* Settings.get_database_url() in production and development modes;
* the backend refusing to start in production when the DB is misconfigured or unreachable,
  without leaking the password into logs.
"""
import logging
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)


from backend.core import config  # noqa: E402
from backend.core.config import DatabaseConfigError, Settings  # noqa: E402

CONN = "proj:us-central1:inst"


def make_settings(**overrides) -> Settings:
    """Settings isolated from the process environment and any .env file."""
    values = dict(
        ENV="development",
        K_SERVICE="",
        DATABASE_URL="",
        DB_PASSWORD="",
        CLOUD_SQL_CONNECTION_NAME=CONN,
        DB_USER="postgres",
        DB_NAME="support_tickets",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


class TestProductionDatabaseUrl(unittest.TestCase):
    def setUp(self):
        # Production must never perform a runtime Secret Manager / gcloud lookup.
        self.lookup = patch.object(config, "fetch_db_password_from_secret_manager",
                                   side_effect=AssertionError("runtime secret lookup in production"))
        self.lookup.start()

    def tearDown(self):
        self.lookup.stop()

    def test_mounted_secret_builds_unix_socket_url(self):
        url = make_url(make_settings(ENV="production", DB_PASSWORD="pw").get_database_url())
        self.assertEqual(url.get_backend_name(), "postgresql")
        self.assertEqual(url.password, "pw")
        self.assertEqual(url.database, "support_tickets")
        self.assertIsNone(url.host)
        self.assertEqual(url.query["host"], f"/cloudsql/{CONN}")

    def test_special_characters_in_password_are_escaped(self):
        tricky = "p@ss:w/rd?#%&"
        url = make_url(make_settings(ENV="production", DB_PASSWORD=tricky).get_database_url())
        self.assertEqual(url.password, tricky)
        self.assertEqual(url.query["host"], f"/cloudsql/{CONN}")

    def test_trailing_newline_from_secret_is_stripped(self):
        url = make_url(make_settings(ENV="production", DB_PASSWORD="pw\n").get_database_url())
        self.assertEqual(url.password, "pw")

    def test_missing_password_fails_fast(self):
        with self.assertRaises(DatabaseConfigError) as ctx:
            make_settings(ENV="production").get_database_url()
        self.assertIn("DB_PASSWORD", str(ctx.exception))
        self.assertIn("--set-secrets", str(ctx.exception))

    def test_missing_connection_name_fails_fast(self):
        with self.assertRaises(DatabaseConfigError) as ctx:
            make_settings(ENV="production", DB_PASSWORD="pw", CLOUD_SQL_CONNECTION_NAME=" ").get_database_url()
        self.assertIn("CLOUD_SQL_CONNECTION_NAME", str(ctx.exception))

    def test_sqlite_url_rejected(self):
        for url in ("sqlite:///./x.db", "sqlite://", "sqlite+pysqlite:///:memory:"):
            with self.assertRaises(DatabaseConfigError, msg=url):
                make_settings(ENV="production", DATABASE_URL=url).get_database_url()

    def test_invalid_url_rejected(self):
        with self.assertRaises(DatabaseConfigError):
            make_settings(ENV="production", DATABASE_URL="not a url").get_database_url()

    def test_explicit_postgres_url_allowed(self):
        url = "postgresql+psycopg2://u:p@10.0.0.5:5432/db"
        self.assertEqual(make_settings(ENV="production", DATABASE_URL=url).get_database_url(), url)

    def test_cloud_run_counts_as_production_even_without_env(self):
        s = make_settings(ENV="development", K_SERVICE="fde-support-backend")
        self.assertTrue(s.is_production)
        with self.assertRaises(DatabaseConfigError):
            s.get_database_url()

    def test_env_value_is_case_insensitive(self):
        self.assertTrue(make_settings(ENV=" Production ").is_production)

    def test_error_never_contains_password(self):
        with self.assertRaises(DatabaseConfigError) as ctx:
            make_settings(ENV="production", DB_PASSWORD="hunter2", CLOUD_SQL_CONNECTION_NAME="").get_database_url()
        self.assertNotIn("hunter2", str(ctx.exception))


class TestDevelopmentDatabaseUrl(unittest.TestCase):
    def test_explicit_url_wins(self):
        self.assertEqual(make_settings(DATABASE_URL="sqlite:///./t.db").get_database_url(), "sqlite:///./t.db")

    def test_password_uses_local_auth_proxy(self):
        with patch.object(config, "fetch_db_password_from_secret_manager", return_value="p@ss") as lookup:
            url = make_url(make_settings().get_database_url())
        lookup.assert_called_once()
        self.assertEqual((url.host, url.port, url.password), ("127.0.0.1", 5432, "p@ss"))

    def test_db_password_env_skips_lookup(self):
        with patch.object(config, "fetch_db_password_from_secret_manager") as lookup:
            url = make_url(make_settings(DB_PASSWORD="pw").get_database_url())
        lookup.assert_not_called()
        self.assertEqual(url.host, "127.0.0.1")

    def test_sqlite_fallback_is_logged_not_silent(self):
        with patch.object(config, "fetch_db_password_from_secret_manager", return_value=""), \
                self.assertLogs("backend.core.config", level=logging.WARNING) as logs:
            url = make_settings().get_database_url()
        self.assertTrue(url.startswith("sqlite:///"))
        self.assertIn("development only", "\n".join(logs.output))


class TestBackendStartupFailsFast(unittest.TestCase):
    """Imports backend.main in a fresh interpreter, as the container does at startup."""

    def _start(self, **env_overrides):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "DB_PASSWORD", "K_SERVICE", "ENV")}
        env.update(PYTHONPATH=ROOT, GCP_PROJECT_ID="no-such-project-for-tests", **env_overrides)
        return subprocess.run([sys.executable, "-c", "import backend.main"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_missing_secret_prevents_startup(self):
        proc = self._start(ENV="production")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DatabaseConfigError", proc.stderr)

    def test_unreachable_database_prevents_startup_without_leaking_password(self):
        proc = self._start(ENV="production",
                           DATABASE_URL="postgresql+psycopg2://postgres:hunter2-secret@127.0.0.1:1/db")
        self.assertNotEqual(proc.returncode, 0)
        output = proc.stdout + proc.stderr
        self.assertIn("refusing to start", output)
        self.assertNotIn("hunter2-secret", output)


if __name__ == "__main__":
    unittest.main()
