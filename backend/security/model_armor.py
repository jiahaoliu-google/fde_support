"""Model Armor screening and PII redaction of customer messages.

Every customer message goes through `model_armor.screen()` before it reaches an agent
(Gemini) or the database. Three layers, tried in order for the PII text:

1. Model Armor `sanitizeUserPrompt` with the template MODEL_ARMOR_TEMPLATE_ID
   (deployment/terraform/agent_gateway.tf). One call does:
     * Sensitive Data Protection de-identification (advanced SDP config: the inspect and
       de-identify templates replace cards, SSNs, emails, phone numbers and person names
       with the same tokens the regex layer uses),
     * prompt-injection / jailbreak detection,
     * malicious-URI detection,
     * Responsible AI filters (hate, harassment, dangerous, sexually explicit).
2. Cloud DLP `deidentify_content`: used when Model Armor is disabled, unavailable, failed,
   or didn't return de-identified text (e.g. the template still has a basic SDP config).
3. Regex: ALWAYS runs on the result. It is the guarantee: if both services are unavailable,
   slow or fail, redaction still happens (PII fails safe).

Threat screening (layer 1 only):
  * prompt injection / jailbreak, malicious URI or CSAM match -> `blocked=True`. The chat
    pipeline sends the ticket to the human queue without calling any LLM.
  * RAI match -> `urgent=True`. The ticket goes to a human like an urgent/frustrated one.
  * If Model Armor can't be reached, threat screening FAILS OPEN (`threat_screened=False`,
    logged at ERROR, span attribute): failing closed would escalate every ticket during a
    Model Armor outage, which breaks the availability objective. PII redaction still
    fails safe through layers 2 and 3.
    TODO(security): alert on `model_armor.threat_screened=false` spans in Cloud Monitoring.

Requirements:
  layer 1: `google-cloud-modelarmor`, modelarmor.googleapis.com enabled, roles/modelarmor.user
           for the caller, and roles/dlp.user + roles/dlp.reader for the Model Armor service
           agent (it runs the SDP templates).
  layer 2: `google-cloud-dlp`, dlp.googleapis.com enabled, roles/dlp.user for the caller.

Settings (environment):
  MODEL_ARMOR_ENABLED       "true" (default) / "false". Offline tests set "false".
  MODEL_ARMOR_TEMPLATE_ID   template ID (default support-router-pii-masking).
  MODEL_ARMOR_LOCATION      template region (default us-central1); regional endpoint.
  MODEL_ARMOR_TIMEOUT_S     per-call deadline in seconds (default 3).
  MODEL_ARMOR_DLP_ENABLED   "true" (default) / "false". Offline tests set "false".
  MODEL_ARMOR_DLP_TIMEOUT_S per-call deadline in seconds (default 5).
  GOOGLE_CLOUD_PROJECT      project billed for both (falls back to GCP_PROJECT_ID).

Customer text is never logged: only filter names, states and error types.

Order IDs (format #10001 / #####) are intentionally NOT redacted: by product decision they
are not treated as PII, and the FAQ agent needs them for read-only order lookups. Their
5-digit format stays clear of the card (13-19 digits, Luhn) and phone (10+ digits) patterns,
and neither DLP configuration asks for an order-number infoType.

Not screened here: FAQ agent answers (`sanitizeModelResponse`). They are built from
grounded tool output and pass agents/faq_assistant/answer_sanitizer.py.
TODO(security): screen FAQ answers with sanitizeModelResponse behind a flag.
"""
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from google.cloud import dlp_v2
    DLP_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment
    dlp_v2 = None
    DLP_AVAILABLE = False

try:
    from google.cloud import modelarmor_v1
    MODEL_ARMOR_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment
    modelarmor_v1 = None
    MODEL_ARMOR_AVAILABLE = False

DEFAULT_PROJECT_ID = "fde-support-sandbox-dev-jliu"
DEFAULT_TEMPLATE_ID = "support-router-pii-masking"
DEFAULT_LOCATION = "us-central1"

PATH_MODEL_ARMOR = "model_armor"  # Model Armor de-identified the text (regex then also ran)
PATH_DLP = "dlp"                  # Cloud DLP de-identified the text (regex then also ran)
PATH_REGEX = "regex"              # regex only: both services disabled, unavailable or failed

# DLP infoType -> replacement token. The regex layer and the Terraform de-identify template
# emit the same tokens, so stored tickets look the same whichever layer ran.
INFO_TYPE_TOKENS = {
    "CREDIT_CARD_NUMBER": "[CREDIT_CARD_REDACTED]",
    "US_SOCIAL_SECURITY_NUMBER": "[SSN_REDACTED]",
    "EMAIL_ADDRESS": "[EMAIL_REDACTED]",
    "PHONE_NUMBER": "[PHONE_REDACTED]",
    "PERSON_NAME": "[NAME_REDACTED]",
}
# Name detection has false positives on brand / store / product names, which hurt FAQ
# retrieval. Only redact names DLP is confident about.
PERSON_NAME_MIN_LIKELIHOOD = "LIKELY"

# Model Armor filter_results keys that block the message from reaching any LLM.
BLOCKING_FILTERS = ("pi_and_jailbreak", "malicious_uris", "csam")
# Model Armor filter_results keys that make the ticket urgent (human queue).
URGENT_FILTERS = ("rai",)
# filter_results key -> FilterResult field holding that filter's result.
_FILTER_RESULT_FIELDS = {
    "pi_and_jailbreak": "pi_and_jailbreak_filter_result",
    "malicious_uris": "malicious_uri_filter_result",
    "csam": "csam_filter_filter_result",
    "rai": "rai_filter_result",
}

# After a service error, skip that service for this long instead of paying a failing round
# trip (and possibly the full timeout) on every message. The next layer covers the gap.
DLP_ERROR_COOLDOWN_S = 60.0
MODEL_ARMOR_ERROR_COOLDOWN_S = 60.0


@dataclass(frozen=True)
class ScreeningResult:
    """Outcome of screening one customer message. Never contains the original text."""
    text: str                       # redacted text, safe to send to agents and to store
    path: str                       # PATH_MODEL_ARMOR | PATH_DLP | PATH_REGEX
    blocked: bool = False           # prompt injection / jailbreak / malicious URI / CSAM
    urgent: bool = False            # Responsible AI filter match
    threat_screened: bool = False   # Model Armor threat filters actually ran
    findings: Tuple[str, ...] = field(default_factory=tuple)  # matched filter names only


@dataclass(frozen=True)
class _ArmorOutcome:
    text: Optional[str]             # de-identified text, or None: use the next PII layer
    blocked: bool
    urgent: bool
    threat_screened: bool
    findings: Tuple[str, ...]


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def _enum_name(value) -> str:
    """Name of a proto enum value (IntEnum member), or the value itself if it's a string."""
    if isinstance(value, str):
        return value
    return getattr(value, "name", str(value))


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Known card prefixes (Visa, Mastercard, Amex, Diners, Discover, JCB), contiguous digits.
_CARD_PREFIX_RE = re.compile(
    r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}'
    r'|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b'
)
# Any 13-19 digit run with optional spaces / hyphens; redacted only if it passes Luhn, so
# tracking and reference numbers are not over-redacted.
_CARD_GENERIC_RE = re.compile(r'\b(?:\d[ -]*?){13,19}\b')
_SSN_RE = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
# Phone numbers: optional +country code, then 3-3-4 digits with optional (), spaces, dots
# or hyphens. 10+ digits, so 5-digit order IDs never match.
_PHONE_RE = re.compile(
    r'(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)'
)


def _redact_luhn_card(match: re.Match) -> str:
    digits = re.sub(r'\D', '', match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return INFO_TYPE_TOKENS["CREDIT_CARD_NUMBER"]
    return match.group(0)


class ModelArmorRedactor:
    """Model Armor screening + PII redaction (Model Armor -> Cloud DLP -> regex).

    Both API clients are created on first use, not at import time, so importing the backend
    (tests, the eval, tools) never needs Google credentials.
    """

    def __init__(self, project_id: Optional[str] = None, dlp_enabled: Optional[bool] = None,
                 timeout_s: Optional[float] = None, client=None, *,
                 armor_enabled: Optional[bool] = None, armor_client=None,
                 template_id: Optional[str] = None, location: Optional[str] = None,
                 armor_timeout_s: Optional[float] = None):
        self.project_id = (project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
                           or os.getenv("GCP_PROJECT_ID") or DEFAULT_PROJECT_ID)

        # Cloud DLP (layer 2)
        self.dlp_enabled = (_env_flag("MODEL_ARMOR_DLP_ENABLED", True)
                            if dlp_enabled is None else dlp_enabled)
        self.timeout_s = timeout_s if timeout_s is not None else _env_float("MODEL_ARMOR_DLP_TIMEOUT_S", 5.0)
        self._client = client          # injected (tests) or created lazily
        self._client_failed = False    # client creation failed: don't retry every message
        self._skip_dlp_until = 0.0     # monotonic time; set after a DLP call error
        self._warned = False

        # Model Armor (layer 1)
        self.armor_enabled = (_env_flag("MODEL_ARMOR_ENABLED", True)
                              if armor_enabled is None else armor_enabled)
        self.template_id = (template_id or os.getenv("MODEL_ARMOR_TEMPLATE_ID") or DEFAULT_TEMPLATE_ID).strip()
        self.location = (location or os.getenv("MODEL_ARMOR_LOCATION") or DEFAULT_LOCATION).strip()
        self.armor_timeout_s = (armor_timeout_s if armor_timeout_s is not None
                                else _env_float("MODEL_ARMOR_TIMEOUT_S", 3.0))
        self._armor_client = armor_client
        self._armor_client_failed = False
        self._skip_armor_until = 0.0
        self._armor_warned = False

        self._lock = threading.Lock()

    # ------------------------------------------------------------------ status
    @property
    def dlp_client(self):
        """The DLP client, created on first access; None if DLP can't be used."""
        return self._get_client()

    @property
    def template_name(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}/templates/{self.template_id}"

    def dlp_status(self) -> str:
        """Why DLP will or won't be used. Doesn't create a client or call the API."""
        if not self.dlp_enabled:
            return "disabled (MODEL_ARMOR_DLP_ENABLED=false)"
        if self._client is None and not DLP_AVAILABLE:
            return "unavailable (google-cloud-dlp not installed)"
        if self._client_failed:
            return "unavailable (DLP client init failed)"
        return "enabled"

    def armor_status(self) -> str:
        """Why Model Armor will or won't be used. Doesn't create a client or call the API."""
        if not self.armor_enabled:
            return "disabled (MODEL_ARMOR_ENABLED=false)"
        if self._armor_client is None and not MODEL_ARMOR_AVAILABLE:
            return "unavailable (google-cloud-modelarmor not installed)"
        if self._armor_client_failed:
            return "unavailable (Model Armor client init failed)"
        return f"enabled ({self.template_name})"

    def status(self) -> str:
        return f"Model Armor: {self.armor_status()}; Cloud DLP fallback: {self.dlp_status()}"

    # ---------------------------------------------------------- client set-up
    def _warn_once(self, reason: str) -> None:
        if not self._warned:
            self._warned = True
            logger.warning("Cloud DLP redaction not used: %s. If Model Armor is also unavailable, "
                           "customer messages are redacted by the regex layer only.", reason)

    def _warn_armor_once(self, reason: str) -> None:
        if not self._armor_warned:
            self._armor_warned = True
            logger.warning("Model Armor not used: %s. Prompt-injection screening is OFF; PII "
                           "redaction falls back to Cloud DLP and regex.", reason)

    def _get_client(self):
        if not self.dlp_enabled or self._client_failed:
            return None
        if self._client is not None:
            return self._client
        if not DLP_AVAILABLE:
            self._warn_once("google-cloud-dlp is not installed")
            return None
        with self._lock:
            if self._client is None and not self._client_failed:
                try:
                    self._client = dlp_v2.DlpServiceClient()
                except Exception as e:  # no credentials, bad environment, ...
                    self._client_failed = True
                    self._warn_once(f"could not initialize the DLP client ({e})")
        return self._client

    def _get_armor_client(self):
        if not self.armor_enabled or self._armor_client_failed:
            return None
        if self._armor_client is not None:
            return self._armor_client
        if not MODEL_ARMOR_AVAILABLE:
            self._warn_armor_once("google-cloud-modelarmor is not installed")
            return None
        with self._lock:
            if self._armor_client is None and not self._armor_client_failed:
                try:
                    # Model Armor is regional: templates are only served by their region's endpoint.
                    self._armor_client = modelarmor_v1.ModelArmorClient(
                        client_options={"api_endpoint": f"modelarmor.{self.location}.rep.googleapis.com"},
                    )
                except Exception as e:  # no credentials, bad environment, ...
                    self._armor_client_failed = True
                    self._warn_armor_once(f"could not initialize the Model Armor client ({type(e).__name__})")
        return self._armor_client

    def warm_up(self) -> None:
        """Creates the API clients in a background thread (no API call, no cost), so the
        first customer message on a new instance doesn't wait for client creation."""
        def run():
            self._get_armor_client()
            self._get_client()

        armor_pending = (self.armor_enabled and self._armor_client is None
                         and not self._armor_client_failed and MODEL_ARMOR_AVAILABLE)
        dlp_pending = (self.dlp_enabled and self._client is None
                       and not self._client_failed and DLP_AVAILABLE)
        if armor_pending or dlp_pending:
            threading.Thread(target=run, name="model-armor-client-warm-up", daemon=True).start()

    # --------------------------------------------------------------- screening
    def screen(self, text: str) -> ScreeningResult:
        """Screens and redacts one customer message. Never raises for service errors."""
        if not text:
            return ScreeningResult(text=text, path=PATH_REGEX)

        armor = self._model_armor_sanitize(text)
        sanitized, path = text, PATH_REGEX
        if armor is not None and armor.text is not None:
            sanitized, path = armor.text, PATH_MODEL_ARMOR
        else:
            dlp_text = self._dlp_deidentify(text)
            if dlp_text is not None:
                sanitized, path = dlp_text, PATH_DLP

        # Always apply the high-precision regex layer to guarantee redaction.
        sanitized = self._regex_redact(sanitized)
        if armor is None:
            return ScreeningResult(text=sanitized, path=path)
        return ScreeningResult(text=sanitized, path=path, blocked=armor.blocked, urgent=armor.urgent,
                               threat_screened=armor.threat_screened, findings=armor.findings)

    def redact(self, text: str) -> str:
        """Redacted text only (see screen())."""
        return self.screen(text).text

    def redact_with_path(self, text: str) -> Tuple[str, str]:
        """(redacted text, PATH_MODEL_ARMOR | PATH_DLP | PATH_REGEX). The regex layer runs in
        every case."""
        result = self.screen(text)
        return result.text, result.path

    # ------------------------------------------------------------ Model Armor
    def _model_armor_sanitize(self, text: str) -> Optional[_ArmorOutcome]:
        """Model Armor's verdict, or None if it wasn't used or the call failed."""
        if time.monotonic() < self._skip_armor_until:
            return None
        client = self._get_armor_client()
        if client is None or not self.project_id:
            return None
        request = {"name": self.template_name, "user_prompt_data": {"text": text}}
        try:
            response = client.sanitize_user_prompt(request=request, timeout=self.armor_timeout_s)
            return self._parse_armor_response(response, text)
        except Exception as e:
            self._skip_armor_until = time.monotonic() + MODEL_ARMOR_ERROR_COOLDOWN_S
            # Log the error type only: API errors can echo request fields (customer text).
            logger.error("Model Armor sanitizeUserPrompt failed (%s). Prompt-injection screening "
                         "skipped and PII redaction falls back to Cloud DLP / regex for %.0f s.",
                         type(e).__name__, MODEL_ARMOR_ERROR_COOLDOWN_S)
            return None

    @staticmethod
    def _parse_armor_response(response, original_text: str) -> _ArmorOutcome:
        result = response.sanitization_result
        invocation = _enum_name(getattr(result, "invocation_result", ""))
        filter_results = getattr(result, "filter_results", None) or {}

        def get(key):
            try:
                return filter_results.get(key)
            except Exception:  # pragma: no cover - defensive, map types vary
                return None

        def matched(sub) -> Optional[bool]:
            """True / False if the filter ran, None if it didn't run or errored."""
            if sub is None or _enum_name(getattr(sub, "execution_state", "")) != "EXECUTION_SUCCESS":
                return None
            return _enum_name(getattr(sub, "match_state", "")) == "MATCH_FOUND"

        findings, blocked, urgent = [], False, False
        threat_ran = []
        for key in BLOCKING_FILTERS + URGENT_FILTERS:
            fr = get(key)
            hit = matched(getattr(fr, _FILTER_RESULT_FIELDS[key], None)) if fr is not None else None
            if key == "pi_and_jailbreak":
                threat_ran.append(hit is not None)
            if hit:
                findings.append(key)
                if key in BLOCKING_FILTERS:
                    blocked = True
                else:
                    urgent = True

        # PII: only the de-identify result carries rewritten text. A basic SDP config
        # (inspect only), a skipped or failed SDP filter -> None, so Cloud DLP runs.
        deid_text = None
        sdp = get("sdp")
        deid = getattr(getattr(sdp, "sdp_filter_result", None), "deidentify_result", None) if sdp is not None else None
        deid_hit = matched(deid)
        if deid_hit is False:
            deid_text = original_text  # SDP ran and found no PII: the original is clean
        elif deid_hit:
            value = getattr(getattr(deid, "data", None), "text", "") or ""
            deid_text = value or None  # an empty rewrite of a non-empty input is not valid

        threat_screened = all(threat_ran) and bool(threat_ran)
        if not threat_screened:
            logger.error("Model Armor prompt-injection filter did not run (invocation=%s). "
                         "Message continues without injection screening.", invocation)
        if blocked or urgent:
            logger.warning("Model Armor flagged a customer message: %s", ",".join(findings))
        return _ArmorOutcome(text=deid_text, blocked=blocked, urgent=urgent,
                             threat_screened=threat_screened, findings=tuple(findings))

    # ---------------------------------------------------------------- Cloud DLP
    def _deidentify_request(self, text: str) -> dict:
        return {
            "parent": f"projects/{self.project_id}/locations/global",
            "inspect_config": {
                "info_types": [{"name": name} for name in INFO_TYPE_TOKENS],
                "min_likelihood_per_info_type": [
                    {"info_type": {"name": "PERSON_NAME"}, "min_likelihood": PERSON_NAME_MIN_LIKELIHOOD},
                ],
            },
            "deidentify_config": {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "info_types": [{"name": name}],
                            "primitive_transformation": {
                                "replace_config": {"new_value": {"string_value": token}},
                            },
                        }
                        for name, token in INFO_TYPE_TOKENS.items()
                    ]
                }
            },
            "item": {"value": text},
        }

    def _dlp_deidentify(self, text: str) -> Optional[str]:
        """DLP's de-identified text, or None if DLP wasn't used or failed."""
        if time.monotonic() < self._skip_dlp_until:
            return None
        client = self._get_client()
        if client is None or not self.project_id:
            return None
        try:
            response = client.deidentify_content(request=self._deidentify_request(text),
                                                 timeout=self.timeout_s)
        except Exception as e:
            self._skip_dlp_until = time.monotonic() + DLP_ERROR_COOLDOWN_S
            # Log the error type and message only, never the customer text.
            logger.error("Cloud DLP de-identification failed (%s: %s). Falling back to the regex "
                         "redactor for %.0f s.", type(e).__name__, e, DLP_ERROR_COOLDOWN_S)
            return None
        value = getattr(getattr(response, "item", None), "value", None)
        # An empty value for a non-empty input is not a valid de-identification.
        return value if value else None

    # -------------------------------------------------------------------- regex
    def _regex_redact(self, text: str) -> str:
        # Credit card numbers: known prefixes, then any Luhn-valid 13-19 digit run.
        text = _CARD_PREFIX_RE.sub(INFO_TYPE_TOKENS["CREDIT_CARD_NUMBER"], text)
        text = _CARD_GENERIC_RE.sub(_redact_luhn_card, text)
        # Social Security Numbers (XXX-XX-XXXX)
        text = _SSN_RE.sub(INFO_TYPE_TOKENS["US_SOCIAL_SECURITY_NUMBER"], text)
        # Phone numbers (after SSNs and cards, so their digits are already gone)
        text = _PHONE_RE.sub(INFO_TYPE_TOKENS["PHONE_NUMBER"], text)
        # Email addresses (e.g. user@domain.com)
        text = _EMAIL_RE.sub(INFO_TYPE_TOKENS["EMAIL_ADDRESS"], text)
        return text


# Global singleton instance
model_armor = ModelArmorRedactor()
