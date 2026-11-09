"""Webhook authenticity: X-Hub-Signature-256 verification and request helpers.

Meta signs every event notification with HMAC-SHA256 of the raw request body using
the app secret and sends it as `X-Hub-Signature-256: sha256=<hex>`
(https://developers.facebook.com/docs/graph-api/webhooks/getting-started).
The webhook URL is public on purpose (the caller is Meta), so this check is the
authentication gate and it fails closed: a missing secret, a placeholder secret, a
missing header or a mismatch all mean "reject".
"""

import base64
import hashlib
import hmac
from typing import Dict, Iterable, Optional, Set

SIGNATURE_HEADER = "x-hub-signature-256"
PLACEHOLDER_PREFIX = "CHANGE_ME"


def is_placeholder(value: Optional[str]) -> bool:
    """Unset or still the template value written by the stack (never usable as a key)."""
    return not value or not str(value).strip() or str(value).strip().upper().startswith(PLACEHOLDER_PREFIX)


def get_header(event: dict, name: str) -> Optional[str]:
    """Case-insensitive header lookup for REST (v1) proxy events."""
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return value
    for key, values in (event.get("multiValueHeaders") or {}).items():
        if key.lower() == wanted and values:
            return values[0]
    return None


def get_raw_body(event: dict) -> bytes:
    """The exact bytes Meta signed (decode base64 when API Gateway encoded the body)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def compute_signature(raw_body: bytes, app_secret: str) -> str:
    return "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header_value: Optional[str], app_secret: Optional[str]) -> bool:
    """Constant-time check of one signature against one app secret."""
    if is_placeholder(app_secret) or not header_value:
        return False
    header_value = header_value.strip()
    if not header_value.lower().startswith("sha256="):
        return False
    expected = compute_signature(raw_body, app_secret).encode("ascii")
    received = ("sha256=" + header_value[7:].strip().lower()).encode("utf-8", errors="replace")
    return hmac.compare_digest(expected, received)


def verified_channels(raw_body: bytes, header_value: Optional[str], secrets_by_channel: Dict[str, Optional[str]]) -> Set[str]:
    """Channels whose app secret produced the signature (every secret is checked)."""
    return {
        channel
        for channel, secret in secrets_by_channel.items()
        if verify_signature(raw_body, header_value, secret)
    }


def token_matches(candidate: Optional[str], expected_tokens: Iterable[Optional[str]]) -> bool:
    """Constant-time comparison of the webhook verify token against the configured tokens."""
    if not candidate:
        return False
    matched = False
    for expected in expected_tokens:
        if is_placeholder(expected):
            continue
        if hmac.compare_digest(str(candidate).encode("utf-8"), str(expected).encode("utf-8")):
            matched = True
    return matched
