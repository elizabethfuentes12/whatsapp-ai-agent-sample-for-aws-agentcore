"""Common utilities for WhatsApp webhook handling."""

import json
import logging

logger = logging.getLogger(__name__)


def validate_webhook(event: dict, verification_token: str) -> str:
    """Validate WhatsApp webhook challenge-response.

    Args:
        event: API Gateway event with queryStringParameters.
        verification_token: Expected verification token.

    Returns:
        Challenge string if valid, empty string otherwise.
    """
    params = event.get("queryStringParameters", {})
    if params and "hub.challenge" in params:
        if params.get("hub.verify_token") == verification_token:
            logger.info("Webhook verification successful")
            return params["hub.challenge"]
        logger.warning("Webhook verification failed: token mismatch")
    return ""


def build_response(status_code: int, body) -> dict:
    """Build API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body) if isinstance(body, dict) else str(body),
    }


