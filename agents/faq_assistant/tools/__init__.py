"""Tools package for agents.faq_assistant."""
from agents.faq_assistant.tools.orders import (  # noqa: F401
    MOCK_ORDERS,
    VALID_STATUSES,
    lookup_order,
    normalize_order_id,
)
from agents.faq_assistant.tools_impl import (  # noqa: F401
    search_support_records,
    get_order_status,
    _get_search_client,
)

# Re-export all symbols for backwards compatibility
from agents.faq_assistant.tools_impl import *  # noqa: F401,F403
