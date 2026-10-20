"""Compatibility wrapper forwarding to agents.faq_assistant.tools.orders."""
try:
    from .tools.orders import *  # noqa: F401,F403
    from .tools.orders import (  # noqa: F401
        MOCK_ORDERS,
        VALID_STATUSES,
        lookup_order,
        normalize_order_id,
    )
except ImportError:  # Loaded as a top-level module (deployed agent directory).
    from tools.orders import *  # noqa: F401,F403
    from tools.orders import (  # noqa: F401
        MOCK_ORDERS,
        VALID_STATUSES,
        lookup_order,
        normalize_order_id,
    )
