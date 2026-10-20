import os
import subprocess
import logging
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

logger = logging.getLogger(__name__)


class DatabaseConfigError(RuntimeError):
    """The database can't be configured safely. Raised at startup so the revision fails to start."""

def fetch_db_password_from_secret_manager(secret_id: str = "fde-support-db-credentials", project_id: str = "fde-support-sandbox-dev-jliu") -> str:
    """
    Dynamically retrieves DB password from GCP Secret Manager using Python SDK if available,
    falling back to gcloud CLI.
    """
    # Method 1: Try Python Google Cloud Secret Manager SDK if installed
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_val = response.payload.data.decode("UTF-8").strip()
        if secret_val:
            return secret_val
    except Exception as e:
        logger.debug(f"Secret Manager SDK lookup skipped or failed: {e}")

    # Method 2: Try gcloud CLI fallback
    try:
        cmd = [
            "gcloud", "secrets", "versions", "access", "latest",
            f"--secret={secret_id}", f"--project={project_id}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        secret_val = res.stdout.strip()
        if secret_val:
            return secret_val
    except Exception as e:
        logger.debug(f"gcloud secrets lookup failed: {e}")

    return ""

class Settings(BaseSettings):
    """
    Central application settings supporting both Development and Production.
    Environment variables automatically override default values.
    """
    ENV: str = "development" # "development" or "production"
    DEBUG: bool = True
    
    # GCP Cloud SQL Configuration
    GCP_PROJECT_ID: str = "fde-support-sandbox-dev-jliu"
    CLOUD_SQL_CONNECTION_NAME: str = "fde-support-sandbox-dev-jliu:us-central1:fde-support-db-instance"
    DB_USER: str = "postgres"
    DB_NAME: str = "support_tickets"
    SECRET_ID: str = "fde-support-db-credentials"
    # Production: injected by Cloud Run from Secret Manager
    # (`--set-secrets=DB_PASSWORD=fde-support-db-credentials:latest`). Never logged.
    DB_PASSWORD: str = ""

    # Database URL override (if explicitly provided)
    DATABASE_URL: str = ""

    # Set by Cloud Run on every instance. Used so a missing ENV=production can't
    # silently put a Cloud Run revision on a throwaway SQLite file.
    K_SERVICE: str = ""

    # Vertex AI Reasoning Engine Endpoints
    FAQ_AGENT_URL: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/6767283237606653952:streamQuery"
    SENTIMENT_AGENT_URL: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/7184992103045267456:streamQuery"
    
    # Circuit Breaker Configuration
    CIRCUIT_FAILURE_THRESHOLD: int = 3
    CIRCUIT_RECOVERY_TIMEOUT: float = 30.0

    # Caller identity (see backend/security/caller_identity.py)
    IAP_AUDIENCE: str = ""                # audience of the IAP-signed JWT forwarded by the frontend
    FRONTEND_SERVICE_ACCOUNT: str = ""    # only invoker allowed to forward IAP assertions
    ALLOW_DIRECT_CALLERS: bool = False    # testing: other invokers act as their own identity
    DEV_FAKE_USER_EMAIL: str = ""         # local development only; ignored on Cloud Run

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("DB_PASSWORD")
    @classmethod
    def _strip_secret(cls, value: str) -> str:
        # Secret payloads often end with a newline (e.g. `echo pw | gcloud secrets create`).
        return value.strip()

    @property
    def is_production(self) -> bool:
        """True for ENV=production and for anything running on Cloud Run."""
        return self.ENV.strip().lower() == "production" or bool(self.K_SERVICE)

    def get_database_url(self) -> str:
        """Builds the SQLAlchemy database URL.

        Production (ENV=production or on Cloud Run) fails fast, never falls back:
          1. An explicit DATABASE_URL is used if set, but it can't be SQLite.
          2. Otherwise DB_PASSWORD (mounted from Secret Manager) and
             CLOUD_SQL_CONNECTION_NAME are required. Connects over the Cloud SQL Unix socket.
          Anything missing raises DatabaseConfigError, so the revision fails to start.

        Development:
          1. Explicit DATABASE_URL.
          2. DB_PASSWORD, or a Secret Manager / gcloud lookup, to reach Cloud SQL through
             the Auth Proxy on 127.0.0.1:5432.
          3. Local SQLite, with a warning.
        """
        if self.is_production:
            return self._production_database_url()

        if self.DATABASE_URL:
            return self.DATABASE_URL

        db_pass = self.DB_PASSWORD or fetch_db_password_from_secret_manager(self.SECRET_ID, self.GCP_PROJECT_ID)
        if db_pass:
            # Local Development via Cloud SQL Proxy on 127.0.0.1:5432
            return URL.create(
                "postgresql+psycopg2",
                username=self.DB_USER,
                password=db_pass,
                host="127.0.0.1",
                port=5432,
                database=self.DB_NAME,
            ).render_as_string(hide_password=False)

        local_sqlite_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'database', 'fde_support.db'))
        logger.warning(
            "No database password available; using local SQLite at %s (development only).",
            local_sqlite_path,
        )
        return f"sqlite:///{local_sqlite_path}"

    def _production_database_url(self) -> str:
        where = f"ENV={self.ENV!r}, K_SERVICE={self.K_SERVICE!r}"

        if self.DATABASE_URL:
            try:
                backend = make_url(self.DATABASE_URL).get_backend_name()
            except Exception as exc:
                raise DatabaseConfigError(f"DATABASE_URL is not a valid database URL ({where}).") from exc
            if backend == "sqlite":
                raise DatabaseConfigError(
                    f"SQLite is not allowed in production ({where}): data would be lost when the instance stops."
                )
            return self.DATABASE_URL

        missing = [
            name for name, value in (
                ("DB_PASSWORD", self.DB_PASSWORD),
                ("CLOUD_SQL_CONNECTION_NAME", self.CLOUD_SQL_CONNECTION_NAME),
                ("DB_USER", self.DB_USER),
                ("DB_NAME", self.DB_NAME),
            ) if not value.strip()
        ]
        if missing:
            raise DatabaseConfigError(
                f"Production database is not configured ({where}); missing: {', '.join(missing)}. "
                f"Mount the password with `--set-secrets=DB_PASSWORD={self.SECRET_ID}:latest` and attach "
                "the instance with `--set-cloudsql-instances` (see backend/deploy.sh)."
            )

        # Cloud Run Cloud SQL connector: Unix socket at /cloudsql/<connection name>.
        return URL.create(
            "postgresql+psycopg2",
            username=self.DB_USER,
            password=self.DB_PASSWORD,
            database=self.DB_NAME,
            query={"host": f"/cloudsql/{self.CLOUD_SQL_CONNECTION_NAME}"},
        ).render_as_string(hide_password=False)

settings = Settings()
