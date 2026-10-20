"""Cached Google Application Default Credentials for calling Agent Runtime endpoints."""
import logging
import threading

logger = logging.getLogger("backend")

_gcp_credentials = None
_gcp_credentials_lock = threading.Lock()

def get_gcp_auth_headers() -> dict:
    """Returns an OAuth access-token header for calling Reasoning Engine endpoints.

    Credentials are loaded once and refreshed only when expired or about to expire
    (google-auth's ``valid`` includes a refresh margin), so a normal request does no
    blocking network I/O here.
    """
    global _gcp_credentials
    try:
        import google.auth
        import google.auth.transport.requests
        with _gcp_credentials_lock:
            if _gcp_credentials is None:
                _gcp_credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            if not _gcp_credentials.valid:
                _gcp_credentials.refresh(google.auth.transport.requests.Request())
            return {"Authorization": f"Bearer {_gcp_credentials.token}"}
    except Exception as e:
        logger.warning(f"GCP Auth token acquisition failed: {type(e).__name__}: {e}")
        return {}
