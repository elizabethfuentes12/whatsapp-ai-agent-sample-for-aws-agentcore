"""Webhook receiver: authenticates Meta, downloads media, saves to DDB for tumbling window.

Handles GET (verification) and POST (event notifications) from the Meta Cloud API.
Supports dual-channel: WhatsApp Business ("whatsapp_business_account") and
Instagram DMs ("instagram"). Normalizes both into a common DynamoDB item format
so the processor Lambda can handle them uniformly.

Security model
* The URL is public because the caller is Meta. The authentication gate is the
  `X-Hub-Signature-256` HMAC-SHA256 of the RAW body with the app secret stored in
  Secrets Manager. A missing or unreadable secret, a missing header or a mismatch
  rejects the request with 403 before the body is parsed. Comparison is constant time.
* Bodies, headers, tokens and phone numbers are never logged.
* Meta retries failed deliveries for up to 36 hours, so every item is written with a
  conditional put on the message id.

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import re
import time
import uuid

import boto3
import requests
from botocore.exceptions import ClientError

from graph_api import facebook_url, instagram_url
from media_utils import MediaTooLarge, download_and_store_media, download_from_url_and_store
from message_utils import message_age_seconds
from safe_logging import configure_logging, mask_phone
from secrets_cache import load_secret
from security import SIGNATURE_HEADER, get_header, get_raw_body, is_placeholder, verified_channels
from utils import build_response, validate_webhook

configure_logging()
logger = logging.getLogger()

TABLE_NAME = os.environ.get("TABLE_NAME", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
SECRET_ARN = os.environ.get("SECRET_ARN", "")
IG_SECRET_ARN = os.environ.get("IG_SECRET_ARN", "")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1024 * 1024)))
MAX_MESSAGE_AGE_SECONDS = int(os.environ.get("MAX_MESSAGE_AGE_SECONDS", "900"))
MESSAGE_TTL_SECONDS = int(os.environ.get("MESSAGE_TTL_SECONDS", str(48 * 3600)))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
secrets_client = boto3.client("secretsmanager")

SUPPORTED_TYPES = {"text", "image", "audio", "video", "document"}
UNSUPPORTED_REPLY = "Message type not supported. Send text, image, audio, video, or document."
TOO_LARGE_REPLY = "That file is too large for me. Please send a smaller one."


def get_secrets() -> dict:
    """WhatsApp secret object, or {} when unreadable (callers fail closed)."""
    return load_secret(secrets_client, SECRET_ARN) or {}


def get_ig_secrets() -> dict:
    """Instagram secret object, or {} when unreadable (callers fail closed)."""
    return load_secret(secrets_client, IG_SECRET_ARN) or {}


def lambda_handler(event, context):
    method = (event.get("httpMethod") or "").upper()
    if method == "GET":
        return _handle_verification(event)
    if method == "POST":
        return _handle_notification(event)
    return build_response(405, "Method not allowed")


def _handle_verification(event):
    """GET: webhook verification for WhatsApp and Instagram."""
    tokens = [
        get_secrets().get("WHATS_VERIFICATION_TOKEN"),
        get_ig_secrets().get("IG_VERIFICATION_TOKEN"),
    ]
    challenge = validate_webhook(event, tokens)
    if challenge:
        return build_response(200, challenge)
    return build_response(403, "Verification failed")


def _handle_notification(event):
    """POST: authenticate with the HMAC signature, then process."""
    raw_body = get_raw_body(event)
    if len(raw_body) > MAX_BODY_BYTES:
        logger.warning("payload_too_large")
        return build_response(413, "Payload too large")

    wa_secrets, ig_secrets = get_secrets(), get_ig_secrets()
    verified = verified_channels(
        raw_body,
        get_header(event, SIGNATURE_HEADER),
        {"whatsapp": wa_secrets.get("WHATS_APP_SECRET"), "instagram": ig_secrets.get("IG_APP_SECRET")},
    )
    if not verified:
        logger.warning("signature_rejected")
        return build_response(403, "Forbidden")

    try:
        body = json.loads(raw_body)
    except ValueError:
        return build_response(400, "Invalid JSON")
    if not isinstance(body, dict):
        return build_response(400, "Invalid JSON")

    channel = body.get("object", "")
    required = {"whatsapp_business_account": "whatsapp", "instagram": "instagram"}.get(channel)
    if required is None:
        logger.info("unknown_object_ignored")
        return build_response(200, "OK")
    if required not in verified:
        logger.warning("signature_rejected_for_object")
        return build_response(403, "Forbidden")

    try:
        if required == "whatsapp":
            _process_whatsapp_entries(body, wa_secrets)
        else:
            _process_instagram_entries(body, ig_secrets)
    except Exception as error:
        # 500 makes Meta retry; the conditional put makes the retry safe.
        logger.error("processing_failed", extra={"fields": {"error_type": type(error).__name__}})
        return build_response(500, "Error")
    return build_response(200, "OK")


def save_once(item: dict, tbl=None) -> bool:
    """Conditional put: False when this message id was already stored."""
    tbl = tbl or table
    item.setdefault("ttl", int(time.time()) + MESSAGE_TTL_SECONDS)
    try:
        tbl.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(from_phone) AND attribute_not_exists(id)",
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def clean_filename(raw_name: str) -> str:
    """Document names go into the agent prompt, so keep letters, digits and a few separators."""
    name_no_ext = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    filename = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
    return re.sub(r"\s+", " ", filename) or "document"


# ---------------------------------------------------------------------------
# WhatsApp processing
# ---------------------------------------------------------------------------

def _process_whatsapp_entries(body, secrets):
    token = secrets.get("WHATS_TOKEN")
    if is_placeholder(token):
        raise RuntimeError("WHATS_TOKEN is not configured")
    whats_token = "Bearer " + token
    valid_phone = secrets.get("DISPLAY_PHONE_NUMBER", "")
    if is_placeholder(valid_phone):
        valid_phone = ""

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            metadata = value.get("metadata", {})
            display_phone = metadata.get("display_phone_number", "")
            phone_id = metadata.get("phone_number_id", "")

            if valid_phone and display_phone != valid_phone:
                logger.info("message_for_other_number_ignored")
                continue

            contacts = value.get("contacts", [])
            contact_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

            for msg in messages:
                age = message_age_seconds(msg.get("timestamp", 0), time.time())
                if age > MAX_MESSAGE_AGE_SECONDS:
                    logger.info("old_message_skipped", extra={"fields": {"age_seconds": int(age)}})
                    continue
                _save_whatsapp_message(msg, phone_id, whats_token, contact_name)


def _media_info(msg, msg_type):
    """(media object, S3 prefix, default extension) for one WhatsApp media message."""
    prefixes = {"image": ("images/", "jpeg"), "audio": ("audio/", "ogg"),
                "video": ("video/", "mp4"), "document": ("documents/", "pdf")}
    prefix, default_ext = prefixes[msg_type]
    return msg.get(msg_type, {}), prefix, default_ext


def _save_whatsapp_message(msg, phone_id, whats_token, contact_name=""):
    """Save WhatsApp message to DynamoDB for tumbling window processing."""
    message_id = msg.get("id", "")
    phone = msg.get("from", "")
    msg_type = msg.get("type", "")
    if not message_id or not phone:
        logger.warning("message_without_identity_skipped")
        return

    if msg_type not in SUPPORTED_TYPES:
        _send_whatsapp_reply_direct(phone, whats_token, phone_id, UNSUPPORTED_REPLY, message_id)
        return

    item = {
        "from_phone": phone,
        "id": message_id,
        "timestamp": msg.get("timestamp", ""),
        "type": msg_type,
        "channel": "whatsapp",
        "phone_id": phone_id,
    }
    if contact_name:
        item["contact_name"] = contact_name

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "")
        if text:
            item["text"] = text
    else:
        media, prefix, default_ext = _media_info(msg, msg_type)
        if media.get("caption"):
            item["caption"] = media["caption"]
        media_id = media.get("id", "")
        if media_id:
            mime_type = media.get("mime_type", "")
            ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else default_ext
            try:
                stored = download_and_store_media(media_id, whats_token, S3_BUCKET, prefix, ext)
            except MediaTooLarge:
                _send_whatsapp_reply_direct(phone, whats_token, phone_id, TOO_LARGE_REPLY, message_id)
                return
            if stored:
                ref = {"type": msg_type, "s3_url": stored["s3_url"]}
                if msg_type == "audio":
                    ref["media_id"] = media_id
                if msg_type == "document":
                    ref["filename"] = clean_filename(media.get("filename", "document"))
                item["media_ref"] = json.dumps(ref)

    stored_now = save_once(item)
    logger.info(
        "wa_message_saved" if stored_now else "duplicate_message_dropped",
        extra={"fields": {"message_id": message_id, "type": msg_type, "from": mask_phone(phone)}},
    )


def _send_whatsapp_reply_direct(phone, whats_token, phone_id, text, in_reply_to=""):
    """Send a reply directly via WhatsApp Cloud API (for unsupported types)."""
    if not str(phone_id).isdigit():
        return
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    if in_reply_to:
        data["context"] = {"message_id": in_reply_to}
    try:
        requests.post(
            facebook_url(f"{phone_id}/messages"),
            headers={"Authorization": whats_token, "Content-Type": "application/json"},
            json=data,
            timeout=(5, 15),
        )
    except Exception as error:
        logger.warning("wa_direct_reply_failed", extra={"fields": {"error_type": type(error).__name__}})


# ---------------------------------------------------------------------------
# Instagram processing
# ---------------------------------------------------------------------------

# Map Instagram attachment types to internal media types
IG_ATTACHMENT_TYPE_MAP = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "file": "document",
}

# Map Instagram attachment types to file extensions
IG_ATTACHMENT_EXT_MAP = {
    "image": "jpeg",
    "video": "mp4",
    "audio": "m4a",
    "file": "pdf",
}

_ig_profile_cache = {}


def _fetch_ig_profile(sender_id, ig_token):
    """Fetch Instagram user profile (name, username) via the Graph API.

    The token travels in the Authorization header, never in the URL, so it cannot
    leak through logged URLs. Cached per sender for the life of the container.
    """
    if sender_id in _ig_profile_cache:
        return _ig_profile_cache[sender_id]

    # Validate sender_id is numeric to prevent URL injection
    if not sender_id or not sender_id.isdigit():
        logger.error("ig_sender_id_rejected")
        return {}

    try:
        resp = requests.get(
            instagram_url(sender_id),
            params={"fields": "name,username"},
            headers={"Authorization": f"Bearer {ig_token}"},
            timeout=(5, 10),
        )
        if resp.status_code == 200:
            profile = resp.json()
            _ig_profile_cache[sender_id] = profile
            return profile
        logger.warning("ig_profile_fetch_failed", extra={"fields": {"status": resp.status_code}})
    except Exception as error:
        logger.warning("ig_profile_fetch_error", extra={"fields": {"error_type": type(error).__name__}})
    return {}


def _process_instagram_entries(body, ig_secrets):
    ig_account_id = ig_secrets.get("IG_ACCOUNT_ID", "")
    ig_token = ig_secrets.get("IG_TOKEN", "")
    if is_placeholder(ig_token):
        raise RuntimeError("IG_TOKEN is not configured")

    for entry in body.get("entry", []):
        for msg_event in entry.get("messaging", []):
            sender_id = msg_event.get("sender", {}).get("id", "")

            # Skip messages sent by our own account (echo)
            if ig_account_id and sender_id == ig_account_id:
                continue

            message = msg_event.get("message", {})
            if not message or message.get("is_echo"):
                continue  # delivery or read receipt, or echo

            age = message_age_seconds(msg_event.get("timestamp", 0), time.time())
            if age > MAX_MESSAGE_AGE_SECONDS:
                logger.info("old_message_skipped", extra={"fields": {"age_seconds": int(age)}})
                continue

            profile = _fetch_ig_profile(sender_id, ig_token)
            _save_instagram_message(message, sender_id, ig_secrets, profile)


def _save_instagram_message(message, sender_id, ig_secrets, profile=None):
    """Save Instagram message to DynamoDB for tumbling window processing."""
    message_id = message.get("mid", "")
    text = message.get("text", "")
    attachments = message.get("attachments", [])

    # Use ig- prefix to avoid collision with WhatsApp phone numbers
    from_key = f"ig-{sender_id}"

    contact_name = ""
    ig_username = ""
    if profile:
        contact_name = profile.get("name", "") or (f"@{profile['username']}" if profile.get("username") else "")
        ig_username = profile.get("username", "")

    item = {
        "from_phone": from_key,
        "id": message_id or f"ig-{uuid.uuid4().hex[:12]}",
        "timestamp": str(int(time.time())),
        "channel": "instagram",
        "ig_sender_id": sender_id,
        "ig_account_id": ig_secrets.get("IG_ACCOUNT_ID", ""),
    }
    if contact_name:
        item["contact_name"] = contact_name
    if ig_username:
        item["ig_username"] = ig_username

    if text and not attachments:
        item["type"] = "text"
        item["text"] = text
    elif attachments:
        attachment = attachments[0]
        att_type = attachment.get("type", "image")
        att_url = attachment.get("payload", {}).get("url", "")

        internal_type = IG_ATTACHMENT_TYPE_MAP.get(att_type, "image")
        item["type"] = internal_type
        if text:
            item["caption"] = text

        if att_url:
            ext = IG_ATTACHMENT_EXT_MAP.get(att_type, "bin")
            file_id = message_id or uuid.uuid4().hex[:16]
            try:
                stored = download_from_url_and_store(att_url, S3_BUCKET, f"ig-{internal_type}s/", file_id, ext)
            except MediaTooLarge:
                logger.info("ig_media_too_large")
                return
            if stored:
                media_ref = {"type": internal_type, "s3_url": stored["s3_url"]}
                if internal_type == "audio":
                    media_ref["media_id"] = file_id
                item["media_ref"] = json.dumps(media_ref)
    else:
        logger.info("ig_message_without_content_skipped")
        return

    stored_now = save_once(item)
    logger.info(
        "ig_message_saved" if stored_now else "duplicate_message_dropped",
        extra={"fields": {"message_id": item["id"], "type": item.get("type"), "from": mask_phone(from_key)}},
    )
