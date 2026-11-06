"""Secrets Manager reader with a short TTL cache that fails closed.

A short TTL means a rotated secret is picked up without a redeploy. Any error returns
None so callers can reject the request instead of falling back to a default.
"""

import json
import logging
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TTL_SECONDS = 300

_cache: Dict[str, Tuple[float, dict]] = {}


def load_secret(client, secret_arn: str, now: Callable[[], float] = time.monotonic) -> Optional[dict]:
    """Return the secret JSON object, or None when it is missing or unreadable."""
    if not secret_arn:
        return None
    cached = _cache.get(secret_arn)
    if cached and now() - cached[0] < TTL_SECONDS:
        return cached[1]
    try:
        raw = client.get_secret_value(SecretId=secret_arn)["SecretString"]
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
    except Exception as error:
        logger.error("secret_unreadable", extra={"fields": {"error_type": type(error).__name__}})
        return None
    _cache[secret_arn] = (now(), value)
    return value


def clear_cache() -> None:
    _cache.clear()
