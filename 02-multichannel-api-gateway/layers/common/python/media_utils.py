"""Media download and storage utilities for WhatsApp and Instagram Cloud APIs.

Downloads are streamed with a size cap and a total time budget, and credentials are only
sent to Meta owned hosts (see graph_api.is_allowed_media_url).
"""

import logging
import os
import re
import time

import boto3
import requests

from graph_api import facebook_url, is_allowed_media_url

logger = logging.getLogger(__name__)

s3_client = boto3.client("s3")

MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
DOWNLOAD_DEADLINE_SECONDS = 20
REQUEST_TIMEOUT = (5, 15)  # connect, read

_SAFE_EXT = re.compile(r"[^a-z0-9]")
_SAFE_KEY_PART = re.compile(r"[^0-9A-Za-z._-]")


class MediaTooLarge(Exception):
    """The media is larger than MAX_DOWNLOAD_BYTES."""


def safe_extension(extension: str, default: str = "bin") -> str:
    """Lowercase alphanumeric extension of at most 10 characters."""
    cleaned = _SAFE_EXT.sub("", (extension or "").lower())[:10]
    return cleaned or default


def safe_key_part(value: str, max_length: int = 120) -> str:
    """Make an id safe for an S3 key and a Transcribe job name."""
    return _SAFE_KEY_PART.sub("-", str(value or ""))[:max_length] or "unknown"


def get_media_url(media_id: str, whats_token: str) -> str:
    """Get the download URL for a WhatsApp media file ('' if unavailable or untrusted)."""
    if not str(media_id).isdigit():
        logger.error("media_id_rejected")
        return ""
    response = requests.get(
        facebook_url(media_id), headers={"Authorization": whats_token}, timeout=REQUEST_TIMEOUT
    )
    if response.status_code != 200:
        logger.error("media_url_lookup_failed", extra={"fields": {"status": response.status_code}})
        return ""
    url = response.json().get("url", "")
    return url if is_allowed_media_url(url) else ""


def _stream(url: str, headers=None, max_bytes: int = None, deadline_seconds: int = DOWNLOAD_DEADLINE_SECONDS) -> bytes:
    """Download with a size cap and a total deadline. Returns b'' on any failure."""
    max_bytes = MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes
    deadline = time.monotonic() + deadline_seconds
    try:
        with requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                logger.error("media_download_failed", extra={"fields": {"status": response.status_code}})
                return b""
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > max_bytes:
                raise MediaTooLarge()
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLarge()
                if time.monotonic() > deadline:
                    logger.error("media_download_deadline")
                    return b""
                chunks.append(chunk)
            return b"".join(chunks)
    except MediaTooLarge:
        raise
    except Exception as error:
        logger.error("media_download_error", extra={"fields": {"error_type": type(error).__name__}})
        return b""


def download_media(url: str, whats_token: str) -> bytes:
    """Download media content from WhatsApp (token is sent only to allowlisted hosts)."""
    if not is_allowed_media_url(url):
        logger.error("media_host_rejected")
        return b""
    return _stream(url, headers={"Authorization": whats_token})


def upload_to_s3(data: bytes, bucket: str, key: str):
    """Upload bytes to S3."""
    s3_client.put_object(Bucket=bucket, Key=key, Body=data)
    logger.info("media_stored", extra={"fields": {"prefix": key.split("/")[0]}})


def download_from_s3(bucket: str, key: str) -> bytes:
    """Download bytes from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def download_and_store_media(media_id: str, whats_token: str, bucket: str, prefix: str, extension: str = "") -> dict:
    """Download media from WhatsApp and store it in S3.

    Returns:
        Dict with s3_bucket, s3_key and s3_url, or {} when the media could not be fetched.

    Raises:
        MediaTooLarge: when the file exceeds MAX_DOWNLOAD_BYTES.
    """
    media_url = get_media_url(media_id, whats_token)
    if not media_url:
        return {}
    content = download_media(media_url, whats_token)
    if not content:
        return {}
    return _store(content, bucket, f"{prefix}{safe_key_part(media_id)}.{safe_extension(extension)}")


def download_from_url_and_store(url: str, bucket: str, prefix: str, file_id: str, extension: str = "") -> dict:
    """Download media from a direct URL (Instagram) and store it in S3.

    Returns:
        Dict with s3_bucket, s3_key and s3_url, or {} on failure.

    Raises:
        MediaTooLarge: when the file exceeds MAX_DOWNLOAD_BYTES.
    """
    if not is_allowed_media_url(url):
        logger.error("media_host_rejected")
        return {}
    content = _stream(url)
    if not content:
        return {}
    return _store(content, bucket, f"{prefix}{safe_key_part(file_id)}.{safe_extension(extension)}")


def _store(content: bytes, bucket: str, key: str) -> dict:
    upload_to_s3(content, bucket, key)
    return {"s3_bucket": bucket, "s3_key": key, "s3_url": f"s3://{bucket}/{key}"}
