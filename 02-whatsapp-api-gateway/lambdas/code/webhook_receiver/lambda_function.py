"""Webhook receiver: validates, downloads media, saves to DDB for tumbling window.

Handles GET (verification) and POST (message reception) from Meta Cloud API.
For each message: download media to S3, save to DynamoDB.
The DDB Stream with tumbling window triggers the processor Lambda.

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import re
import time

import boto3

from utils import validate_webhook, build_response
from media_utils import download_and_store_media

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
SECRET_ARN = os.environ.get("SECRET_ARN", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
secrets_client = boto3.client("secretsmanager")

SUPPORTED_TYPES = {"text", "image", "audio", "video", "document"}

_cached_secrets = None


def get_secrets() -> dict:
    global _cached_secrets
    if _cached_secrets:
        return _cached_secrets
    response = secrets_client.get_secret_value(SecretId=SECRET_ARN)
    _cached_secrets = json.loads(response["SecretString"])
    return _cached_secrets


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps(event))
    secrets = get_secrets()

    # GET: Webhook verification
    if event.get("httpMethod") == "GET":
        challenge = validate_webhook(event, secrets["WHATS_VERIFICATION_TOKEN"])
        if challenge:
            return build_response(200, challenge)
        return build_response(403, "Verification failed")

    # POST: Message reception
    body = json.loads(event.get("body", "{}"))
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

            _save_message(msg, phone_id, whats_token, contact_name)

    return build_response(200, "OK")


def _save_message(msg, phone_id, whats_token, contact_name=""):
    """Save message to DynamoDB for tumbling window processing."""
    message_id = msg.get("id", "")
    phone = msg.get("from", "")
    msg_type = msg.get("type", "")
    timestamp = msg.get("timestamp", "")

    if msg_type not in SUPPORTED_TYPES:
        _send_reply_direct(phone, whats_token, phone_id,
                           "Message type not supported. Send text, image, audio, video, or document.",
                           message_id)
        return

    item = {
        "from_phone": phone,
        "id": message_id,
        "timestamp": timestamp,
        "ttl": int(time.time()) + 86400,
        "type": msg_type,
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
    logger.info("Saved message %s from %s (type=%s)", message_id, phone, msg_type)


def _send_reply_direct(phone, whats_token, phone_id, text, in_reply_to=""):
    """Send a reply directly via Meta Cloud API (for unsupported types)."""
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
        logger.error("Failed to send reply: %s", str(e))
