"""Webhook receiver: validates, downloads media, saves to DDB for tumbling window.

Handles GET (verification) and POST (message reception) from Meta Cloud API.
Supports dual-channel: WhatsApp Business ("whatsapp_business_account") and
Instagram DMs ("instagram"). Normalizes both into a common DynamoDB item format
so the processor Lambda can handle them uniformly.

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

from utils import validate_webhook, build_response
from media_utils import download_and_store_media, download_from_url_and_store

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
SECRET_ARN = os.environ.get("SECRET_ARN", "")
IG_SECRET_ARN = os.environ.get("IG_SECRET_ARN", "")

IG_GRAPH_API_VERSION = "v24.0"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
secrets_client = boto3.client("secretsmanager")

SUPPORTED_TYPES = {"text", "image", "audio", "video", "document"}

_cached_secrets = None
_cached_ig_secrets = None


def get_secrets() -> dict:
    global _cached_secrets
    if _cached_secrets:
        return _cached_secrets
    response = secrets_client.get_secret_value(SecretId=SECRET_ARN)
    _cached_secrets = json.loads(response["SecretString"])
    return _cached_secrets


def get_ig_secrets() -> dict:
    global _cached_ig_secrets
    if _cached_ig_secrets:
        return _cached_ig_secrets
    response = secrets_client.get_secret_value(SecretId=IG_SECRET_ARN)
    _cached_ig_secrets = json.loads(response["SecretString"])
    return _cached_ig_secrets


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    # GET: Webhook verification (works for both WhatsApp and Instagram)
    if event.get("httpMethod") == "GET":
        # Try WhatsApp token first, then Instagram token
        wa_secrets = get_secrets()
        challenge = validate_webhook(event, wa_secrets["WHATS_VERIFICATION_TOKEN"])
        if challenge:
            return build_response(200, challenge)
        ig_secrets = get_ig_secrets()
        challenge = validate_webhook(event, ig_secrets.get("IG_VERIFICATION_TOKEN", ""))
        if challenge:
            return build_response(200, challenge)
        return build_response(403, "Verification failed")

    # POST: Message reception
    body = json.loads(event.get("body", "{}"))
    channel = body.get("object", "")

    if channel == "whatsapp_business_account":
        _process_whatsapp_entries(body)
    elif channel == "instagram":
        _process_instagram_entries(body)
    else:
        logger.warning("Unknown channel object: %s", channel)

    return build_response(200, "OK")


# ---------------------------------------------------------------------------
# WhatsApp processing (existing logic)
# ---------------------------------------------------------------------------

def _process_whatsapp_entries(body):
    secrets = get_secrets()
    whats_token = "Bearer " + secrets["WHATS_TOKEN"]
    valid_phone = secrets.get("DISPLAY_PHONE_NUMBER", "")

    for entry in body.get("entry", []):
        changes = entry.get("changes", [])
        if not changes:
            continue

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            continue

        metadata = value.get("metadata", {})
        display_phone = metadata.get("display_phone_number", "")
        phone_id = metadata.get("phone_number_id", "")

        if valid_phone and display_phone != valid_phone:
            logger.info("Ignoring message for phone: %s", display_phone)
            continue

        contacts = value.get("contacts", [])
        contact_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

        for msg in messages:
            timestamp = int(msg.get("timestamp", 0))
            now = int(time.time())
            if now - timestamp > 300:
                logger.info("Skipping old message: %d seconds old", now - timestamp)
                continue

            _save_whatsapp_message(msg, phone_id, whats_token, contact_name)


def _save_whatsapp_message(msg, phone_id, whats_token, contact_name=""):
    """Save WhatsApp message to DynamoDB for tumbling window processing."""
    message_id = msg.get("id", "")
    phone = msg.get("from", "")
    msg_type = msg.get("type", "")
    timestamp = msg.get("timestamp", "")

    if msg_type not in SUPPORTED_TYPES:
        _send_whatsapp_reply_direct(phone, whats_token, phone_id,
                                    "Message type not supported. Send text, image, audio, video, or document.",
                                    message_id)
        return

    item = {
        "from_phone": phone,
        "id": message_id,
        "timestamp": timestamp,
        "ttl": int(time.time()) + 86400,
        "type": msg_type,
        "channel": "whatsapp",
        "phone_id": phone_id,
        "whats_token": whats_token,
    }

    if contact_name:
        item["contact_name"] = contact_name

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "")
        if text:
            item["text"] = text

    elif msg_type == "image":
        image = msg.get("image", {})
        media_id = image.get("id", "")
        caption = image.get("caption", "")
        if caption:
            item["caption"] = caption
        if media_id:
            mime_type = image.get("mime_type", "")
            ext = mime_type.split("/")[-1] if "/" in mime_type else "jpeg"
            stored = download_and_store_media(media_id, whats_token, S3_BUCKET, "images/", ext)
            if stored:
                item["media_ref"] = json.dumps({"type": "image", "s3_url": stored["s3_url"]})

    elif msg_type == "audio":
        audio = msg.get("audio", {})
        media_id = audio.get("id", "")
        if media_id:
            mime_type = audio.get("mime_type", "")
            ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "ogg"
            stored = download_and_store_media(media_id, whats_token, S3_BUCKET, "audio/", ext)
            if stored:
                item["media_ref"] = json.dumps({
                    "type": "audio", "s3_url": stored["s3_url"], "media_id": media_id,
                })

    elif msg_type == "video":
        video = msg.get("video", {})
        media_id = video.get("id", "")
        caption = video.get("caption", "")
        if caption:
            item["caption"] = caption
        if media_id:
            mime_type = video.get("mime_type", "")
            ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "mp4"
            stored = download_and_store_media(media_id, whats_token, S3_BUCKET, "video/", ext)
            if stored:
                item["media_ref"] = json.dumps({"type": "video", "s3_url": stored["s3_url"]})

    elif msg_type == "document":
        doc = msg.get("document", {})
        media_id = doc.get("id", "")
        caption = doc.get("caption", "")
        if caption:
            item["caption"] = caption
        if media_id:
            mime_type = doc.get("mime_type", "")
            ext = mime_type.split("/")[-1] if "/" in mime_type else "pdf"
            stored = download_and_store_media(media_id, whats_token, S3_BUCKET, "documents/", ext)
            if stored:
                raw_name = doc.get("filename", "document")
                name_no_ext = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
                filename = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
                filename = re.sub(r"\s+", " ", filename) or "document"
                item["media_ref"] = json.dumps({
                    "type": "document", "s3_url": stored["s3_url"], "filename": filename,
                })

    table.put_item(Item=item)
    logger.info("Saved WA message %s from %s (type=%s)", message_id, phone, msg_type)


def _send_whatsapp_reply_direct(phone, whats_token, phone_id, text, in_reply_to=""):
    """Send a reply directly via WhatsApp Cloud API (for unsupported types)."""
    import requests
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    headers = {"Authorization": whats_token, "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    if in_reply_to:
        data["context"] = {"message_id": in_reply_to}
    try:
        requests.post(url, headers=headers, json=data, timeout=30)
    except Exception as e:
        logger.error("Failed to send WA reply: %s", str(e))


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
    """Fetch Instagram user profile (name, username) via Graph API.

    Uses in-memory cache to avoid repeated calls for the same sender
    within the same Lambda invocation.
    """
    if sender_id in _ig_profile_cache:
        return _ig_profile_cache[sender_id]

    # Validate sender_id is numeric to prevent URL injection
    if not sender_id or not sender_id.isdigit():
        logger.error("Invalid IG sender_id: must be numeric. Got: %s", sender_id)
        return {}

    url = (
        f"https://graph.instagram.com/{IG_GRAPH_API_VERSION}/{sender_id}"
        f"?fields=name,username&access_token={ig_token}"
    )
    try:
        import requests
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            profile = resp.json()
            _ig_profile_cache[sender_id] = profile
            logger.info("Fetched IG profile for %s: @%s (%s)",
                        sender_id, profile.get("username", "?"), profile.get("name", "?"))
            return profile
        logger.warning("IG profile fetch failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to fetch IG profile for %s: %s", sender_id, str(e))
    return {}


def _process_instagram_entries(body):
    ig_secrets = get_ig_secrets()
    ig_account_id = ig_secrets.get("IG_ACCOUNT_ID", "")
    ig_token = ig_secrets.get("IG_TOKEN", "")

    for entry in body.get("entry", []):
        messaging_list = entry.get("messaging", [])

        for msg_event in messaging_list:
            sender_id = msg_event.get("sender", {}).get("id", "")

            # Skip messages sent by our own account (echo)
            if ig_account_id and sender_id == ig_account_id:
                logger.info("Skipping own IG message from: %s", sender_id)
                continue

            message = msg_event.get("message", {})
            if not message:
                logger.info("Skipping non-message IG event (delivery/read receipt)")
                continue

            timestamp = msg_event.get("timestamp", 0)
            now_ms = int(time.time() * 1000)
            # Instagram timestamps are in milliseconds
            if isinstance(timestamp, int) and timestamp > 1e12:
                age_seconds = (now_ms - timestamp) / 1000
            else:
                age_seconds = int(time.time()) - int(timestamp)

            if age_seconds > 300:
                logger.info("Skipping old IG message: %.0f seconds old", age_seconds)
                continue

            # Fetch sender profile for contact_name and username
            profile = _fetch_ig_profile(sender_id, ig_token)

            _save_instagram_message(message, sender_id, ig_secrets, profile)


def _save_instagram_message(message, sender_id, ig_secrets, profile=None):
    """Save Instagram message to DynamoDB for tumbling window processing."""
    message_id = message.get("mid", "")
    text = message.get("text", "")
    attachments = message.get("attachments", [])

    # Use ig- prefix to avoid collision with WhatsApp phone numbers
    from_key = f"ig-{sender_id}"
    timestamp_str = str(int(time.time()))

    # Build contact name from profile: prefer "name", fallback to "@username"
    contact_name = ""
    ig_username = ""
    if profile:
        contact_name = profile.get("name", "") or f"@{profile.get('username', '')}"
        ig_username = profile.get("username", "")

    item = {
        "from_phone": from_key,
        "id": message_id or f"ig-{uuid.uuid4().hex[:12]}",
        "timestamp": timestamp_str,
        "ttl": int(time.time()) + 86400,
        "channel": "instagram",
        "ig_sender_id": sender_id,
        "ig_account_id": ig_secrets.get("IG_ACCOUNT_ID", ""),
        "ig_token": ig_secrets.get("IG_TOKEN", ""),
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
            prefix = f"ig-{internal_type}s/"
            stored = download_from_url_and_store(att_url, S3_BUCKET, prefix, file_id, ext)
            if stored:
                media_ref = {"type": internal_type, "s3_url": stored["s3_url"]}
                if internal_type == "audio":
                    media_ref["media_id"] = file_id
                item["media_ref"] = json.dumps(media_ref)
    else:
        logger.info("IG message with no text and no attachments, skipping")
        return

    table.put_item(Item=item)
    logger.info("Saved IG message %s from %s (type=%s)", item["id"], from_key, item.get("type"))
