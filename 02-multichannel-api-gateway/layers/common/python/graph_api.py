"""One place for the Meta Graph API version and for outbound URL safety checks.

Meta keeps each Graph API version for about two years. v25.0 is supported until
2028-07-29 (https://developers.facebook.com/docs/graph-api/changelog/versions).
Override per deployment with the GRAPH_API_VERSION environment variable (the CDK
context key `graph_api_version` sets it on every Lambda).
"""

import ipaddress
import os
from typing import Iterable, Optional
from urllib.parse import urlparse

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v25.0")

# Hosts that serve WhatsApp and Instagram media. The webhook body is signed by Meta,
# but the token and the download still go only to Meta owned domains.
DEFAULT_MEDIA_HOST_SUFFIXES = ("fbcdn.net", "fbsbx.com", "cdninstagram.com", "facebook.com", "instagram.com")


def facebook_url(path: str) -> str:
    """URL on graph.facebook.com (WhatsApp Cloud API)."""
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{path.lstrip('/')}"


def instagram_url(path: str) -> str:
    """URL on graph.instagram.com (Instagram messaging)."""
    return f"https://graph.instagram.com/{GRAPH_API_VERSION}/{path.lstrip('/')}"


def media_host_suffixes() -> Iterable[str]:
    configured = os.environ.get("MEDIA_HOST_SUFFIXES", "")
    parsed = tuple(s.strip().lower() for s in configured.split(",") if s.strip())
    return parsed or DEFAULT_MEDIA_HOST_SUFFIXES


def is_allowed_media_url(url: str, suffixes: Optional[Iterable[str]] = None) -> bool:
    """HTTPS only, no credentials or custom port, no IP literal, host on the allowlist."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        return any(host == s or host.endswith("." + s) for s in (suffixes or media_host_suffixes()))
    except ValueError:
        return False
