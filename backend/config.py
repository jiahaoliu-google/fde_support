import os
import subprocess
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

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

    # Database URL override (if explicitly provided)
    DATABASE_URL: str = ""

    # Vertex AI Reasoning Engine Endpoints
    FAQ_AGENT_URL: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/2655856592997580800:streamQuery"
    SENTIMENT_AGENT_URL: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/3704069406268063744:streamQuery"
    
    # Circuit Breaker Configuration
    CIRCUIT_FAILURE_THRESHOLD: int = 3
    CIRCUIT_RECOVERY_TIMEOUT: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_database_url(self) -> str:
        """
        Dynamically constructs the database URL.
        1. Uses explicit DATABASE_URL if defined in environment or .env.
        2. Retrieves password from Secret Manager secret 'fde-support-db-credentials'.
        3. Constructs Cloud SQL PostgreSQL URL (Proxy port 5432 for dev, Unix socket for prod).
        4. Falls back to local SQLite if Secret Manager is unreachable.
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL
        
        db_pass = fetch_db_password_from_secret_manager(self.SECRET_ID, self.GCP_PROJECT_ID)
        
        if db_pass:
            if self.ENV == "production":
                # Cloud Run / GCP Container Unix Socket Connection
                return f"postgresql+psycopg2://{self.DB_USER}:{db_pass}@/{self.DB_NAME}?host=/cloudsql/{self.CLOUD_SQL_CONNECTION_NAME}"
            else:
                # Local Development via Cloud SQL Proxy on 127.0.0.1:5432
                return f"postgresql+psycopg2://{self.DB_USER}:{db_pass}@127.0.0.1:5432/{self.DB_NAME}"

        local_sqlite_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'database', 'fde_support.db'))
        return f"sqlite:///{local_sqlite_path}"

settings = Settings()
