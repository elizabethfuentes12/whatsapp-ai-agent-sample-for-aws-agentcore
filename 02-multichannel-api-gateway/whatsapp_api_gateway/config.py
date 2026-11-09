"""Single source of truth for stack settings that more than one construct needs."""

from typing import Any, Optional

# Meta Graph API version for WhatsApp (graph.facebook.com) and Instagram
# (graph.instagram.com). v25.0 is supported until 2028-07-29. Override with
# `-c graph_api_version=v26.0`; the value reaches every Lambda as GRAPH_API_VERSION.
GRAPH_API_VERSION = "v25.0"

DEFAULT_MEDIA_RETENTION_DAYS = 30

# Meta retries failed deliveries for up to 36 hours; the message item is the dedupe record.
MESSAGE_TTL_SECONDS = 48 * 3600

# Largest media object sent inline to the agent (decoded bytes). Keep equal to
# MAX_MEDIA_BYTES in 00-agent-agentcore/agent_files/agent_helpers.py.
MAX_INLINE_MEDIA_BYTES = 1_500_000

# Stage throttling for the public webhook (requests per second / burst).
DEFAULT_THROTTLE_RATE = 50
DEFAULT_THROTTLE_BURST = 100


def is_true(value: Any) -> bool:
    """CDK context values arrive as bool or as the string given with `-c key=value`."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def optional_int(value: Any) -> Optional[int]:
    """Parse an optional integer context value (None when unset or blank)."""
    if value is None or str(value).strip() == "":
        return None
    return int(value)
