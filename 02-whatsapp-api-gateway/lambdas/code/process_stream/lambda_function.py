"""DynamoDB Stream processor for incoming WhatsApp messages.

Routes messages by type to the appropriate processing Lambda:
- text -> agent_processor (AgentCore Runtime)
- image -> agent_processor (with media processing)
- audio -> audio_transcriptor
- video -> audio_transcriptor (extracts audio track)
- document -> agent_processor (with document processing)
"""

import json
import logging
import os
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client("lambda")

AGENT_PROCESSOR_LAMBDA = os.environ.get("AGENT_PROCESSOR_LAMBDA", "")
WHATSAPP_OUT_LAMBDA = os.environ.get("WHATSAPP_OUT_LAMBDA", "")
AUDIO_TRANSCRIPTOR_LAMBDA = os.environ.get("AUDIO_TRANSCRIPTOR_LAMBDA", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def ddb_deserialize(item):
    """Deserialize DynamoDB stream record."""
    deserializer = boto3.dynamodb.types.TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in item.items()}


def invoke_lambda(function_name: str, payload: dict):
    """Invoke a Lambda function asynchronously."""
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload, cls=DecimalEncoder).encode(),
    )


def send_reply(phone, whats_token, phone_id, text, messages_id):
    """Send a reply via the whatsapp_out Lambda."""
    invoke_lambda(WHATSAPP_OUT_LAMBDA, {
        "phone": phone,
        "whats_token": whats_token,
        "phone_id": phone_id,
        "message": text,
        "in_reply_to": messages_id,
    })


def lambda_handler(event, context):
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue

        try:
            entry = ddb_deserialize(record["dynamodb"]["NewImage"])
            entry = json.loads(json.dumps(entry, cls=DecimalEncoder))

            messages_id = entry.get("messages_id", "")
            whats_token = entry.get("whats_token", "")

            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "contacts" not in value or "messages" not in value:
                    continue

                whats_message = value["messages"][0]
                phone_id = value["metadata"]["phone_number_id"]
                phone = "+" + str(whats_message["from"])
                message_type = whats_message.get("type", "")

                base_payload = {
                    "phone": phone,
                    "whats_token": whats_token,
                    "phone_id": phone_id,
                    "messages_id": messages_id,
                    "s3_bucket": S3_BUCKET,
                }

                if message_type == "text":
                    invoke_lambda(AGENT_PROCESSOR_LAMBDA, {
                        **base_payload,
                        "message_type": "text",
                        "message_text": whats_message["text"]["body"],
                    })

                elif message_type == "image":
                    invoke_lambda(AGENT_PROCESSOR_LAMBDA, {
                        **base_payload,
                        "message_type": "image",
                        "media_id": whats_message["image"]["id"],
                        "mime_type": whats_message["image"].get("mime_type", ""),
                        "caption": whats_message["image"].get("caption", ""),
                    })

                elif message_type == "audio":
                    invoke_lambda(AUDIO_TRANSCRIPTOR_LAMBDA, {
                        **base_payload,
                        "message_type": "audio",
                        "whats_message": whats_message,
                    })

                elif message_type == "video":
                    invoke_lambda(AGENT_PROCESSOR_LAMBDA, {
                        **base_payload,
                        "message_type": "video",
                        "media_id": whats_message["video"]["id"],
                        "mime_type": whats_message["video"].get("mime_type", ""),
                        "caption": whats_message["video"].get("caption", ""),
                    })

                elif message_type == "document":
                    invoke_lambda(AGENT_PROCESSOR_LAMBDA, {
                        **base_payload,
                        "message_type": "document",
                        "media_id": whats_message["document"]["id"],
                        "mime_type": whats_message["document"].get("mime_type", ""),
                        "filename": whats_message["document"].get("filename", ""),
                        "caption": whats_message["document"].get("caption", ""),
                    })

                else:
                    send_reply(
                        phone, whats_token, phone_id,
                        "Message type not supported. Send text, image, audio, video, or document.",
                        messages_id,
                    )

        except Exception as e:
            logger.error("Error processing stream record: %s", str(e), exc_info=True)

    return {"statusCode": 200}
