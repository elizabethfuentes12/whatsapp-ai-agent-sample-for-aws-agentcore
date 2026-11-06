"""Receiver Lambda: save messages to DynamoDB for tumbling window aggregation.

Processes incoming WhatsApp messages from SNS. For each message:
1. Mark as read + reaction (immediate user feedback)
2. Download media to S3 (WhatsApp media URLs expire)
3. Save to DynamoDB with a conditional put (a repeated message id is dropped);
   the DDB Stream + tumbling window then triggers the processor.

Source of the event: AWS End User Messaging Social publishes to the SNS topic and SNS
invokes this function. Both hops are authenticated by AWS (topic policy for the
publisher, Lambda resource policy for SNS). As defense in depth the handler also
checks that the record comes from the expected topic and fails closed otherwise.

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

from safe_logging import configure_logging, mask_phone
from whatsapp_service import WhatsAppService

configure_logging()
logger = logging.getLogger()

TABLE_NAME = os.environ.get("TABLE_NAME", "")
EXPECTED_TOPIC_ARN = os.environ.get("EXPECTED_TOPIC_ARN", "")
MESSAGE_TTL_SECONDS = int(os.environ.get("MESSAGE_TTL_SECONDS", str(48 * 3600)))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None

SUPPORTED_TYPES = {"text", "image", "audio", "video", "document"}
UNSUPPORTED_REPLY = "Message type not supported. Send text, image, audio, video, or document."


def is_trusted_record(record: dict, expected_topic_arn: str = None) -> bool:
    """True only for an SNS record from the configured topic (fails closed)."""
    expected = EXPECTED_TOPIC_ARN if expected_topic_arn is None else expected_topic_arn
    if not expected:
        return False
    sns_part = record.get("Sns") or {}
    return record.get("EventSource") == "aws:sns" and sns_part.get("TopicArn") == expected


def clean_filename(raw_name: str) -> str:
    """Document names go into the agent prompt, so keep letters, digits and a few separators."""
    name_no_ext = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    filename = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
    return re.sub(r"\s+", " ", filename) or "document"


def build_item(message, msg_type: str, now: int = None) -> dict:
    """Build the DynamoDB item for one supported message (downloads media)."""
    now = int(time.time()) if now is None else now
    item = {
        "from_phone": message.phone_number,
        "id": message.message_id,
        "timestamp": message.message.get("timestamp", ""),
        "ttl": now + MESSAGE_TTL_SECONDS,
        "type": msg_type,
        "phone_number_id": message.phone_number_id,
        "phone_number_arn": message.phone_number_arn,
    }
    if message.contact_name:
        item["contact_name"] = message.contact_name

    if msg_type == "text":
        text = message.get_text()
        if text:
            item["text"] = text
        return item

    getter = {
        "image": message.get_image,
        "audio": message.get_audio,
        "video": message.get_video,
        "document": message.get_document,
    }[msg_type]
    media = getter(download=True)
    if media.get("caption"):
        item["caption"] = media["caption"]
    if media.get("s3_url"):
        ref = {"type": msg_type, "s3_url": media["s3_url"]}
        if msg_type == "audio":
            ref["media_id"] = media.get("media_id", "")
        if msg_type == "document":
            ref["filename"] = clean_filename(media.get("filename", "document"))
        item["media_ref"] = json.dumps(ref)
    return item


def save_once(item: dict, tbl=None) -> bool:
    """Conditional put: returns False when this message id was already stored."""
    tbl = tbl or table
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


def process_message(message, tbl=None):
    """Save message to DynamoDB for tumbling window processing."""
    msg_type = message.get_message_type()

    if not message.message_id or not message.phone_number:
        logger.warning("message_without_identity_skipped")
        return

    message.mark_as_read()
    message.reaction("\U0001F44D")

    if msg_type not in SUPPORTED_TYPES:
        message.text_reply(UNSUPPORTED_REPLY)
        return

    item = build_item(message, msg_type)
    stored = save_once(item, tbl)
    logger.info(
        "message_saved" if stored else "duplicate_message_dropped",
        extra={"fields": {"message_id": message.message_id, "type": msg_type,
                          "from": mask_phone(message.phone_number)}},
    )


def lambda_handler(event, context):
    """Entry point: receives SNS events from AWS End User Messaging Social."""
    failures = 0
    for record in event.get("Records", []):
        if not is_trusted_record(record):
            logger.error("untrusted_record_rejected")
            continue
        try:
            sns_message = json.loads(record.get("Sns", {}).get("Message", "{}"))
            whatsapp = WhatsAppService(sns_message)
            for message in whatsapp.messages:
                process_message(message)
        except Exception as error:
            failures += 1
            logger.error("record_failed", extra={"fields": {"error_type": type(error).__name__}})

    if failures:
        # Raise so the asynchronous invoke retries and then lands in the dead letter queue.
        # Already stored messages are skipped on retry by the conditional put.
        raise RuntimeError(f"{failures} record(s) failed")
    return {"statusCode": 200}
