"""Tools package for agents.faq_assistant.

Imports must resolve both from the repo root (package ``agents.faq_assistant.tools``)
and inside the Agent Runtime container, where the agent directory is the import root
and this is the top-level package ``tools``. Never import via ``agents.faq_assistant``.
"""
from .orders import (  # noqa: F401
    MOCK_ORDERS,
    VALID_STATUSES,
    lookup_order,
    normalize_order_id,
)

try:
    from ..tools_impl import (  # noqa: F401
        search_support_records,
        get_order_status,
        order_id_guard,
        _get_search_client,
    )

    # Re-export all symbols for backwards compatibility
    from ..tools_impl import *  # noqa: F401,F403
except ImportError:  # Loaded as a top-level package (deployed agent directory).
    from tools_impl import (  # noqa: F401
        search_support_records,
        get_order_status,
        order_id_guard,
        _get_search_client,
    )

    from tools_impl import *  # noqa: F401,F403
