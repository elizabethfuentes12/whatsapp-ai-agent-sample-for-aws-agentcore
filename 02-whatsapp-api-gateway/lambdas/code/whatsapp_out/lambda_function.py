"""Lambda handler for sending WhatsApp messages via Cloud API."""

import json
import logging

import requests

from utils import normalize_phone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GRAPH_API_VERSION = "v21.0"


def send_whatsapp_message(phone, whats_token, phone_id, message, in_reply_to=""):
    """Send a text message via WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_id}/messages"
    headers = {"Authorization": whats_token, "Content-Type": "application/json"}

    data = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(phone),
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }

    if in_reply_to:
        data["context"] = {"message_id": in_reply_to}

    response = requests.post(url, headers=headers, json=data, timeout=30)
    logger.info("WhatsApp API response: %s", response.json())
    return response.json()


def lambda_handler(event, context):
    phone = event["phone"]
    whats_token = event["whats_token"]
    phone_id = event["phone_id"]
    message = event["message"]
    in_reply_to = event.get("in_reply_to", "")

    try:
        send_whatsapp_message(phone, whats_token, phone_id, message, in_reply_to)
        return {"statusCode": 200}
    except Exception as e:
        logger.error("Failed to send message: %s", str(e))
        return {"statusCode": 500}
