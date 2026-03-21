"""Processor Lambda: DDB Stream with tumbling window -> aggregate -> AgentCore -> reply.

Receives batched DynamoDB Stream records accumulated during the tumbling window.
Groups messages by sender, concatenates text, processes media, invokes AgentCore once
per sender, and sends the reply via Meta Cloud API.

Aggregation pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import time
from collections import defaultdict

import boto3
from boto3.dynamodb.types import TypeDeserializer
import requests

from media_utils import get_s3_as_base64

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ARN = os.environ.get("AGENT_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")
agentcore_client = boto3.client("bedrock-agentcore")

GRAPH_API_VERSION = "v21.0"


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

    phone_id = last_msg.get("phone_id", "")
    whats_token = last_msg.get("whats_token", "")
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

    response_text = None
    transcript_text = None

    if media:
        media_type = media["type"]

        if media_type == "image":
            b64_data, img_format = _get_s3_as_base64_from_url(media["s3_url"])
            if b64_data:
                response_text = _invoke_agentcore(
                    phone, combined_text or "Analyze this image in detail.",
                    media={"type": "image", "format": img_format, "data": b64_data},
                )

        elif media_type == "audio":
            transcript = _transcribe_audio(media["s3_url"], media.get("media_id", ""))
            if transcript:
                transcript_text = f"_Transcription: {transcript}_"
                full_prompt = f'Audio transcription: "{transcript}"'
                if combined_text:
                    full_prompt += f"\n{combined_text}"
                response_text = _invoke_agentcore(phone, full_prompt)

        elif media_type == "video":
            prompt = combined_text or "Analyze this video in detail."
            response_text = _invoke_agentcore(
                phone, prompt,
                media={"type": "video", "s3_uri": media["s3_url"]},
            )

        elif media_type == "document":
            b64_data, doc_format = _get_s3_as_base64_from_url(media["s3_url"])
            if b64_data:
                response_text = _invoke_agentcore(
                    phone, combined_text or "Analyze this document.",
                    media={
                        "type": "document", "format": doc_format,
                        "data": b64_data, "name": media.get("filename", "document"),
                    },
                )
    else:
        if combined_text:
            response_text = _invoke_agentcore(phone, combined_text)

    if transcript_text:
        _send_reply(phone, whats_token, phone_id, transcript_text, last_message_id)
    if response_text:
        _send_reply(phone, whats_token, phone_id, response_text, last_message_id)
    elif not transcript_text:
        _send_reply(phone, whats_token, phone_id,
                    "Could not process the message. Please try again.", last_message_id)


def _invoke_agentcore(from_phone, prompt, media=None):
    phone_clean = from_phone.replace("+", "")
    actor_id = f"wa-user-{phone_clean}".ljust(33, "0")
    session_id = f"wa-chat-{phone_clean}".ljust(33, "0")

    payload_data = {"prompt": prompt.strip(), "actor_id": actor_id}
    if media:
        payload_data["media"] = media

    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_ARN,
        runtimeSessionId=session_id,
        runtimeUserId=actor_id,
        payload=json.dumps(payload_data).encode(),
    )

    content = []
    for chunk in response.get("response", []):
        if isinstance(chunk, bytes):
            content.append(chunk.decode("utf-8"))
        elif isinstance(chunk, dict) and "bytes" in chunk:
            content.append(chunk["bytes"].decode("utf-8"))

    response_text = "".join(content)
    try:
        return json.loads(response_text).get("result", response_text)
    except json.JSONDecodeError:
        return response_text


def _send_reply(phone, whats_token, phone_id, text, in_reply_to=""):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_id}/messages"
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


def _get_s3_as_base64_from_url(s3_url):
    try:
        parts = s3_url.replace("s3://", "").split("/", 1)
        bucket, key = parts[0], parts[1]
        return get_s3_as_base64(bucket, key)
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
