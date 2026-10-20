"""Compatibility wrapper forwarding to agents.faq_assistant.tools.orders."""
from agents.faq_assistant.tools.orders import *  # noqa: F401,F403
from agents.faq_assistant.tools.orders import (  # noqa: F401
    MOCK_ORDERS,
    VALID_STATUSES,
    lookup_order,
    normalize_order_id,
)
