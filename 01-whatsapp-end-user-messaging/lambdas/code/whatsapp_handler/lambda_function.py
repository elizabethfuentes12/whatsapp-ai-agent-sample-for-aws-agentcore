"""Receiver Lambda: save messages to DynamoDB for tumbling window aggregation.

Processes incoming WhatsApp messages from SNS. For each message:
1. Mark as read + reaction (immediate user feedback)
2. Download media to S3 (WhatsApp media URLs expire)
3. Save to DynamoDB (DDB Stream + tumbling window triggers the processor)

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import re
import time

import boto3

from whatsapp_service import WhatsAppService

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None

SUPPORTED_TYPES = {"text", "image", "audio", "video", "document"}


def process_message(message):
    """Save message to DynamoDB for tumbling window processing."""
    phone = message.phone_number
    msg_type = message.get_message_type()

    message.mark_as_read()
    message.reaction("👍")

    if msg_type not in SUPPORTED_TYPES:
        message.text_reply(
            "Message type not supported. Send text, image, audio, video, or document."
        )
        return

    item = {
        "from_phone": phone,
        "id": message.message_id,
        "timestamp": message.message.get("timestamp", ""),
        "ttl": int(time.time()) + 86400,
        "type": msg_type,
        "phone_number_id": message.phone_number_id,
        "phone_number_arn": message.phone_number_arn,
        "meta_api_version": message.meta_api_version,
    }

    if message.contact_name:
        item["contact_name"] = message.contact_name

    if msg_type == "text":
        text = message.get_text()
        if text:
            item["text"] = text

    elif msg_type == "image":
        image = message.get_image(download=True)
        caption = image.get("caption", "")
        if caption:
            item["caption"] = caption
        if image.get("s3_url"):
            item["media_ref"] = json.dumps({"type": "image", "s3_url": image["s3_url"]})

    elif msg_type == "audio":
        audio = message.get_audio(download=True)
        if audio.get("s3_url"):
            item["media_ref"] = json.dumps({
                "type": "audio", "s3_url": audio["s3_url"],
                "media_id": audio.get("media_id", ""),
            })

    elif msg_type == "video":
        video = message.get_video(download=True)
        caption = video.get("caption", "")
        if caption:
            item["caption"] = caption
        if video.get("s3_url"):
            item["media_ref"] = json.dumps({"type": "video", "s3_url": video["s3_url"]})

    elif msg_type == "document":
        doc = message.get_document(download=True)
        caption = doc.get("caption", "")
        if caption:
            item["caption"] = caption
        if doc.get("s3_url"):
            raw_name = doc.get("filename", "document")
            name_no_ext = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
            filename = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
            filename = re.sub(r"\s+", " ", filename) or "document"
            item["media_ref"] = json.dumps({
                "type": "document", "s3_url": doc["s3_url"], "filename": filename,
            })

    table.put_item(Item=item)
    logger.info("Saved message %s from %s (type=%s)", message.message_id, phone, msg_type)


def lambda_handler(event, context):
    """Entry point: receives SNS events from AWS End User Messaging Social."""
    for record in event.get("Records", []):
        try:
            sns_message_str = record.get("Sns", {}).get("Message", "{}")
            sns_message = json.loads(sns_message_str)
            whatsapp = WhatsAppService(sns_message)

            for message in whatsapp.messages:
                process_message(message)
        except Exception as e:
            logger.error("Error processing record: %s", str(e), exc_info=True)

    return {"statusCode": 200}
