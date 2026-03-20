"""Processor Lambda: DDB Stream with tumbling window -> aggregate -> AgentCore -> reply.

Receives batched DynamoDB Stream records accumulated during the tumbling window.
Groups messages by sender, concatenates text, processes media, invokes AgentCore once
per sender, and sends the reply via AWS End User Messaging Social.

Aggregation pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import base64
import json
import logging
import os
import time
from collections import defaultdict

import boto3
from boto3.dynamodb.types import TypeDeserializer

from agentcore_service import AgentCoreService

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
AGENT_ARN = os.environ.get("AGENT_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")
social_client = boto3.client("socialmessaging")


def lambda_handler(event, context):
    state = event.get("state", {})
    raw_records = event.get("Records", [])

    records = []
    for record in raw_records:
        if record.get("eventName") != "INSERT":
            continue
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        deserialized = _deserialize_dynamodb(new_image)
        records.append(deserialized)

    if not records:
        return {"state": state}

    logger.info("Processing %d records from tumbling window", len(records))

    grouped = _group_by_sender(records)
    for phone, messages in grouped.items():
        try:
            _process_sender(phone, messages)
        except Exception as e:
            logger.error("Error processing phone %s: %s", phone, str(e), exc_info=True)

    return {"state": state}


_deserializer = TypeDeserializer()


def _deserialize_dynamodb(item):
    """Convert DynamoDB typed format to plain Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


def _group_by_sender(records):
    """Group records by from_phone, sorted by timestamp."""
    grouped = defaultdict(list)
    for record in records:
        sender = record.get("from_phone", "")
        grouped[sender].append(record)
    for sender in grouped:
        grouped[sender].sort(key=lambda m: m.get("timestamp", ""))
    return grouped


def _aggregate_messages(messages):
    """Concatenate consecutive texts, keep last media."""
    texts = []
    media = None

    for msg in messages:
        if msg.get("text"):
            texts.append(str(msg["text"]))
        if msg.get("caption"):
            texts.append(str(msg["caption"]))
        if msg.get("media_ref"):
            media = json.loads(msg["media_ref"])

    combined_text = "\n".join(texts) if texts else ""
    return combined_text, media


def _process_sender(phone, messages):
    combined_text, media = _aggregate_messages(messages)
    last_msg = messages[-1]

    phone_number_id = last_msg.get("phone_number_id", "")
    meta_api_version = last_msg.get("meta_api_version", "v20.0")
    last_message_id = last_msg.get("id", "")

    # Get contact name from any message in the batch
    contact_name = ""
    for msg in messages:
        if msg.get("contact_name"):
            contact_name = str(msg["contact_name"])
            break

    # Prepend contact name to the prompt so the agent knows who's writing
    if contact_name and combined_text:
        combined_text = f"[User: {contact_name}] {combined_text}"
    elif contact_name and not combined_text:
        combined_text = f"[User: {contact_name}]"

    agentcore = AgentCoreService(AGENT_ARN)
    response_text = None
    transcript_text = None

    if media:
        media_type = media["type"]

        if media_type == "image":
            b64_data, img_format = _get_s3_as_base64(media["s3_url"])
            if b64_data:
                response_text = agentcore.invoke_agent(
                    phone, phone_number_id,
                    combined_text or "Analyze this image in detail.",
                    media={"type": "image", "format": img_format, "data": b64_data},
                )

        elif media_type == "audio":
            transcript = _transcribe_audio(media["s3_url"], media.get("media_id", ""))
            if transcript:
                transcript_text = f"_Transcription: {transcript}_"
                full_prompt = f'Audio transcription: "{transcript}"'
                if combined_text:
                    full_prompt += f"\n{combined_text}"
                response_text = agentcore.invoke_agent(phone, phone_number_id, full_prompt)

        elif media_type == "video":
            prompt = combined_text or "Analyze this video in detail."
            response_text = agentcore.invoke_agent(
                phone, phone_number_id, prompt,
                media={"type": "video", "s3_uri": media["s3_url"]},
            )

        elif media_type == "document":
            b64_data, doc_format = _get_s3_as_base64(media["s3_url"])
            if b64_data:
                response_text = agentcore.invoke_agent(
                    phone, phone_number_id,
                    combined_text or "Analyze this document.",
                    media={
                        "type": "document", "format": doc_format,
                        "data": b64_data, "name": media.get("filename", "document"),
                    },
                )
    else:
        if combined_text:
            response_text = agentcore.invoke_agent(phone, phone_number_id, combined_text)

    if transcript_text:
        _send_reply(phone_number_id, meta_api_version, phone, last_message_id, transcript_text)
    if response_text:
        _send_reply(phone_number_id, meta_api_version, phone, last_message_id, response_text)
    elif not transcript_text:
        _send_reply(phone_number_id, meta_api_version, phone, last_message_id,
                    "Could not process the message. Please try again.")


def _send_reply(phone_number_id, meta_api_version, to_phone, reply_to_message_id, text):
    message_object = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "context": {"message_id": reply_to_message_id},
        "to": f"+{to_phone}",
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    try:
        social_client.send_whatsapp_message(
            originationPhoneNumberId=phone_number_id,
            metaApiVersion=meta_api_version,
            message=bytes(json.dumps(message_object), "utf-8"),
        )
    except Exception as e:
        logger.error("Failed to send reply: %s", str(e))


def _get_s3_as_base64(s3_url):
    try:
        parts = s3_url.replace("s3://", "").split("/", 1)
        bucket, key = parts[0], parts[1]
        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()
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
        elif data[:4] == b"PK\x03\x04":
            content_type = response.get("ContentType", "")
            fmt = "docx" if "wordprocessing" in content_type else "xlsx" if "spreadsheet" in content_type else "docx"
        else:
            content_type = response.get("ContentType", "")
            mime_to_format = {
                "msword": "doc", "plain": "txt",
                "vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
                "vnd.ms-excel": "xls",
            }
            mime_suffix = content_type.split("/")[-1] if "/" in content_type else ""
            fmt = mime_to_format.get(mime_suffix, mime_suffix or "jpeg")
        return b64, fmt
    except Exception as e:
        logger.error("Failed to read %s: %s", s3_url, str(e))
        return None, None


def _transcribe_audio(s3_url, media_id):
    try:
        job_name = f"whatsapp-audio-{media_id}"
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            IdentifyLanguage=True,
            Media={"MediaFileUri": s3_url},
            OutputBucketName=S3_BUCKET,
            OutputKey=f"transcriptions/{job_name}.json",
        )
        for _ in range(60):
            job = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
            status = job["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED":
                return ""
            time.sleep(5)
        else:
            return ""
        result = s3_client.get_object(Bucket=S3_BUCKET, Key=f"transcriptions/{job_name}.json")
        transcription = json.loads(result["Body"].read())
        return transcription["results"]["transcripts"][0]["transcript"]
    except Exception as e:
        logger.error("Transcription failed for %s: %s", media_id, str(e))
        return ""
