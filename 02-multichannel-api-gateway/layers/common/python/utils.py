"""Common utilities for WhatsApp and Instagram webhook handling."""

import json
import logging
import re
from typing import Iterable, Optional

from security import token_matches

logger = logging.getLogger(__name__)

_CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def validate_webhook(event: dict, verification_tokens: Iterable[Optional[str]]) -> str:
    """Validate the Meta webhook verification request (GET).

    Args:
        event: API Gateway event with queryStringParameters.
        verification_tokens: Configured verify tokens (WhatsApp and Instagram).

    Returns:
        The challenge to echo when mode is `subscribe` and the token matches, else "".
    """
    params = event.get("queryStringParameters") or {}
    challenge = params.get("hub.challenge", "")
    if params.get("hub.mode") != "subscribe" or not _CHALLENGE_PATTERN.match(challenge or ""):
        return ""
    if token_matches(params.get("hub.verify_token"), verification_tokens):
        logger.info("webhook_verification_ok")
        return challenge
    logger.warning("webhook_verification_failed")
    return ""


def build_response(status_code: int, body) -> dict:
    """Build an API Gateway proxy response. A webhook called by Meta needs no CORS headers."""
    is_json = isinstance(body, dict)
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json" if is_json else "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        "body": json.dumps(body) if is_json else str(body),
    }
