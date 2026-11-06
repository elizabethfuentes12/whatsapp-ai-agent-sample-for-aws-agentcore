"""Single source of truth for stack settings that more than one construct needs."""

from typing import Any, Optional

# Meta Graph API version used for every AWS End User Messaging Social call.
# v25.0 is supported until 2028-07-29 (Meta versions page). Override per
# deployment with `-c meta_api_version=v26.0`; the value reaches both Lambdas as
# the META_API_VERSION environment variable.
GRAPH_API_VERSION = "v25.0"

# Media is ephemeral by design: the agent reads it once and the user keeps the original.
DEFAULT_MEDIA_RETENTION_DAYS = 30

# Meta retries failed webhook deliveries for up to 36 hours, so the dedupe record
# (the message item itself) lives for 48 hours.
MESSAGE_TTL_SECONDS = 48 * 3600

# Largest media object sent inline to the agent (decoded bytes). Keep equal to
# MAX_MEDIA_BYTES in 00-agent-agentcore/agent_files/agent_helpers.py.
MAX_INLINE_MEDIA_BYTES = 1_500_000


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
