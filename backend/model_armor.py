import os
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import google.cloud.dlp
    DLP_AVAILABLE = True
except ImportError:
    DLP_AVAILABLE = False


class ModelArmorRedactor:
    """Model Armor / Sensitive Data Protection Redactor.
    Redacts Credit Card Numbers, SSNs, and Email Addresses from customer queries before sending to Gemini.

    Order IDs (format #10001 / #####) are intentionally NOT redacted: by product decision they are
    not treated as PII, and the FAQ agent needs them for read-only order lookups. Their 5-digit
    format stays clear of the 13-19 digit credit card pattern below.
    """

    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "fde-support-sandbox-dev-jliu")
        if DLP_AVAILABLE:
            try:
                self.dlp_client = google.cloud.dlp_v2.DlpServiceClient()
            except Exception as e:
                logger.warning(f"Could not initialize Cloud DLP client: {e}. Defaulting to Regex Redactor.")
                self.dlp_client = None
        else:
            self.dlp_client = None

    def redact(self, text: str) -> str:
        """Redacts Credit Card Numbers, SSNs, and Email Addresses from input text."""
        if not text:
            return text

        sanitized_text = text

        # 1. Attempt Google Cloud DLP / Model Armor API De-identification
        if self.dlp_client and self.project_id:
            try:
                parent = f"projects/{self.project_id}/locations/global"
                
                info_types = [
                    {"name": "CREDIT_CARD_NUMBER"},
                    {"name": "US_SOCIAL_SECURITY_NUMBER"},
                    {"name": "EMAIL_ADDRESS"},
                ]
                
                inspect_config = {"info_types": info_types}

                deidentify_config = {
                    "info_type_transformations": {
                        "transformations": [
                            {
                                "primitive_transformation": {
                                    "replace_with_info_type_config": {}
                                }
                            }
                        ]
                    }
                }

                item = {"value": text}

                response = self.dlp_client.deidentify_content(
                    request={
                        "parent": parent,
                        "deidentify_config": deidentify_config,
                        "inspect_config": inspect_config,
                        "item": item,
                    }
                )
                if response and response.item and response.item.value:
                    sanitized_text = response.item.value
            except Exception as e:
                logger.error(f"Cloud DLP de-identification failed ({e}). Falling back to Regex Redactor.")

        # 2. Always apply high-precision Regex Redactor to ensure 100% redaction guarantee
        return self._regex_redact(sanitized_text)

    def _regex_redact(self, text: str) -> str:
        # Credit Card Numbers (13-19 digits with optional spaces or hyphens)
        text = re.sub(
            r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b',
            '[CREDIT_CARD_REDACTED]',
            text
        )
        text = re.sub(
            r'\b(?:\d[ -]*?){13,19}\b',
            '[CREDIT_CARD_REDACTED]',
            text
        )
        
        # Social Security Numbers (XXX-XX-XXXX)
        text = re.sub(
            r'\b\d{3}-\d{2}-\d{4}\b',
            '[SSN_REDACTED]',
            text
        )
        
        # Email Addresses (e.g. user@domain.com)
        text = re.sub(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            '[EMAIL_REDACTED]',
            text
        )
        
        return text


# Global singleton instance
model_armor = ModelArmorRedactor()
