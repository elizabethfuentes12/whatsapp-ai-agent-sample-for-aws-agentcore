"""Webhook receiver Lambda for WhatsApp Cloud API via API Gateway.

Handles GET (verification) and POST (message reception) requests.
Stores incoming messages in DynamoDB, which triggers the processing pipeline.
"""

import json
import logging
import os
import time

import boto3

from utils import validate_webhook, build_response
from db_utils import put_item

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
DISPLAY_PHONE_NUMBER = os.environ.get("DISPLAY_PHONE_NUMBER", "")

secrets_client = boto3.client("secretsmanager")
SECRET_ARN = os.environ.get("SECRET_ARN", "")


def get_secrets() -> dict:
    """Retrieve WhatsApp secrets from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=SECRET_ARN)
    return json.loads(response["SecretString"])


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

        # Validate phone number
        if DISPLAY_PHONE_NUMBER and display_phone != DISPLAY_PHONE_NUMBER:
            logger.info("Ignoring message for phone: %s", display_phone)
            continue

        # Skip old messages (>5 minutes)
        timestamp = int(messages[0].get("timestamp", 0))
        now = int(time.time())
        if now - timestamp > 300:
            logger.info("Skipping old message: %d seconds old", now - timestamp)
            continue

        # Store in DynamoDB to trigger stream processing
        messages_id = messages[0].get("id", "")
        item = {
            "messages_id": messages_id,
            "whats_token": whats_token,
            "changes": changes,
            "timestamp": str(timestamp),
        }
        put_item(TABLE_NAME, item)
        logger.info("Stored message: %s", messages_id)

    return build_response(200, "OK")
