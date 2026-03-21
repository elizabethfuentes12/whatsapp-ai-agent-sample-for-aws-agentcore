"""Media download and processing utilities for WhatsApp Cloud API."""

import base64
import logging

import boto3
import requests

logger = logging.getLogger(__name__)

s3_client = boto3.client("s3")

GRAPH_API_VERSION = "v21.0"


def get_media_url(media_id: str, whats_token: str) -> str:
    """Get download URL for a WhatsApp media file.

    Args:
        media_id: WhatsApp media ID.
        whats_token: Bearer token for authentication.

    Returns:
        Download URL or empty string.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"
    headers = {"Authorization": whats_token}
    response = requests.get(url, headers=headers, timeout=30)
    data = response.json()
    return data.get("url", "")


def download_media(url: str, whats_token: str) -> bytes:
    """Download media content from WhatsApp.

    Args:
        url: Media download URL.
        whats_token: Bearer token for authentication.

    Returns:
        File bytes or empty bytes.
    """
    headers = {"Authorization": whats_token}
    response = requests.get(url, headers=headers, timeout=60)
    if response.status_code == 200:
        return response.content
    logger.error("Media download failed: %s", response.status_code)
    return b""


def upload_to_s3(data: bytes, bucket: str, key: str):
    """Upload bytes to S3."""
    s3_client.put_object(Bucket=bucket, Key=key, Body=data)
    logger.info("Uploaded to s3://%s/%s", bucket, key)


def download_from_s3(bucket: str, key: str) -> bytes:
    """Download bytes from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def get_s3_as_base64(bucket: str, key: str) -> tuple:
    """Download S3 object and return as base64 with format detection.

    Returns:
        Tuple of (base64_string, format_string) or (None, None).
    """
    try:
        data = download_from_s3(bucket, key)
        b64 = base64.b64encode(data).decode("ascii")

        if data[:3] == b"\xff\xd8\xff":
            fmt = "jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
        elif data[:4] == b"GIF8":
            fmt = "gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = "webp"
        elif data[:5] == b"%PDF-":
            fmt = "pdf"
        else:
            fmt = "jpeg"

        return b64, fmt
    except Exception as e:
        logger.error("Failed to read s3://%s/%s: %s", bucket, key, str(e))
        return None, None


def download_and_store_media(
    media_id: str,
    whats_token: str,
    bucket: str,
    prefix: str,
    extension: str = "",
) -> dict:
    """Download media from WhatsApp and store in S3.

    Returns:
        Dict with s3_bucket, s3_key, and s3_url.
    """
    media_url = get_media_url(media_id, whats_token)
    if not media_url:
        return {}

    content = download_media(media_url, whats_token)
    if not content:
        return {}

    ext = extension or "bin"
    s3_key = f"{prefix}{media_id}.{ext}"
    upload_to_s3(content, bucket, s3_key)

    return {
        "s3_bucket": bucket,
        "s3_key": s3_key,
        "s3_url": f"s3://{bucket}/{s3_key}",
    }
