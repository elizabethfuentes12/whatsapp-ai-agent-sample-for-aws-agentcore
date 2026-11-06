"""Processor Lambda: DDB Stream with tumbling window -> aggregate -> AgentCore -> reply.

Receives batched DynamoDB Stream records accumulated during the tumbling window.
Groups messages by sender, concatenates text, processes media, invokes AgentCore once
per sender, and sends the reply via AWS End User Messaging Social.

Reliability model:
* Every message is claimed with a conditional update before work starts and marked
  `done` after the reply, so stream replays and retried batches never answer twice.
* A failing sender is reported through `batchItemFailures`; the other senders in the
  same batch are not retried more than once (their records are already `done`).

Aggregation pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import base64
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer

import idempotency
from agentcore_service import AgentCoreService, AgentCoreUnavailable
from message_utils import (
    aggregate_messages,
    build_tagged_prompt,
    detect_image_format,
    group_by_sender,
    split_text,
)
from safe_logging import configure_logging, mask_phone

configure_logging()
logger = logging.getLogger()

TABLE_NAME = os.environ.get("TABLE_NAME", "")
AGENT_ARN = os.environ.get("AGENT_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
META_API_VERSION = os.environ.get("META_API_VERSION", "v25.0")
MAX_INLINE_MEDIA_BYTES = int(os.environ.get("MAX_INLINE_MEDIA_BYTES", "1500000"))
TRANSCRIBE_MAX_WAIT_SECONDS = 120

TRY_AGAIN_REPLY = "I could not process your message right now. Please try again in a minute."
TOO_LARGE_REPLY = "That file is too large for me to read here. Please send a smaller one (up to about 1.5 MB)."
UNREADABLE_REPLY = "I could not read that file. Please send it again."

s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")
social_client = boto3.client("socialmessaging")
table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None

_deserializer = TypeDeserializer()
_agentcore = None


def _agent() -> AgentCoreService:
    global _agentcore
    if _agentcore is None:
        _agentcore = AgentCoreService(AGENT_ARN)
    return _agentcore


def _deserialize_dynamodb(item):
    """Convert DynamoDB typed format to plain Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


def lambda_handler(event, context):
    state = event.get("state") or {}
    seconds_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None

    records = []
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        item = _deserialize_dynamodb(record.get("dynamodb", {}).get("NewImage", {}))
        item["_sequence_number"] = record.get("dynamodb", {}).get("SequenceNumber", "")
        records.append(item)

    failures = []
    if records:
        logger.info("batch_received", extra={"fields": {"records": len(records)}})
        for phone, messages in group_by_sender(records).items():
            try:
                _process_sender(phone, messages, seconds_left)
            except Exception as error:
                logger.error(
                    "sender_failed",
                    extra={"fields": {"user": mask_phone(phone), "messages": len(messages),
                                      "error_type": type(error).__name__}},
                )
                failures.extend(
                    {"itemIdentifier": m["_sequence_number"]} for m in messages if m.get("_sequence_number")
                )

    return {"state": state, "batchItemFailures": failures}


def _process_sender(phone, messages, seconds_left=None):
    claimed = [m for m in messages if idempotency.claim(table, m)]
    if not claimed:
        logger.info("batch_already_handled", extra={"fields": {"user": mask_phone(phone)}})
        return

    try:
        _handle_claimed(phone, claimed, seconds_left)
    except Exception:
        for message in claimed:
            idempotency.release(table, message)
        raise
    for message in claimed:
        idempotency.complete(table, message)


def _handle_claimed(phone, messages, seconds_left):
    combined_text, media = aggregate_messages(messages)
    last_msg = messages[-1]
    phone_number_id = last_msg.get("phone_number_id", "")
    last_message_id = last_msg.get("id", "")
    contact_name = next((str(m["contact_name"]) for m in messages if m.get("contact_name")), "")

    prompt_text = build_tagged_prompt(combined_text, contact_name)
    reply_to = lambda text: _send_reply(phone_number_id, phone, last_message_id, text)  # noqa: E731

    def ask(prompt, agent_media=None):
        return _agent().invoke_agent(
            phone, prompt, media=agent_media, message_id=last_message_id, seconds_left=seconds_left
        )

    transcript_text = None
    try:
        response_text = None
        if media:
            media_type = media["type"]
            if media_type in ("image", "document"):
                data, error_reply = _read_inline_media(media["s3_url"])
                if error_reply:
                    reply_to(error_reply)
                    return
                b64_data, fmt = data
                if media_type == "image":
                    response_text = ask(
                        prompt_text if combined_text else f"{prompt_text} Analyze this image in detail.",
                        {"type": "image", "format": fmt, "data": b64_data},
                    )
                else:
                    response_text = ask(
                        prompt_text if combined_text else f"{prompt_text} Analyze this document.",
                        {"type": "document", "format": fmt, "data": b64_data,
                         "name": media.get("filename", "document")},
                    )
            elif media_type == "audio":
                transcript = _transcribe_audio(media["s3_url"], media.get("media_id", ""))
                if not transcript:
                    reply_to(UNREADABLE_REPLY)
                    return
                transcript_text = f"_Transcription: {transcript}_"
                full_prompt = f'{prompt_text} Audio transcription: "{transcript}"'
                response_text = ask(full_prompt)
            elif media_type == "video":
                response_text = ask(
                    prompt_text if combined_text else f"{prompt_text} Analyze this video in detail.",
                    {"type": "video", "s3_uri": media["s3_url"]},
                )
        elif combined_text:
            response_text = ask(prompt_text)
    except AgentCoreUnavailable:
        response_text = None
        reply_to(TRY_AGAIN_REPLY)
        return

    if transcript_text:
        reply_to(transcript_text)
    if response_text:
        reply_to(response_text)
    elif not transcript_text:
        reply_to(TRY_AGAIN_REPLY)


def _send_reply(phone_number_id, to_phone, reply_to_message_id, text):
    """Send the reply in WhatsApp sized chunks; the first chunk quotes the user message."""
    for index, chunk in enumerate(split_text(text)):
        message_object = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": f"+{to_phone}",
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        if index == 0 and reply_to_message_id:
            message_object["context"] = {"message_id": reply_to_message_id}
        try:
            social_client.send_whatsapp_message(
                originationPhoneNumberId=phone_number_id,
                metaApiVersion=META_API_VERSION,
                message=bytes(json.dumps(message_object), "utf-8"),
            )
        except Exception as error:
            logger.error("reply_failed", extra={"fields": {"error_type": type(error).__name__,
                                                          "to": mask_phone(to_phone)}})
            raise


def _split_s3_url(s3_url):
    bucket, key = s3_url.replace("s3://", "", 1).split("/", 1)
    return bucket, key


def _read_inline_media(s3_url):
    """Return ((base64, format), None) or (None, user_message). Enforces the agent size limit."""
    try:
        bucket, key = _split_s3_url(s3_url)
        if bucket != S3_BUCKET:
            logger.error("media_bucket_mismatch")
            return None, UNREADABLE_REPLY
        head = s3_client.head_object(Bucket=bucket, Key=key)
        if head.get("ContentLength", 0) > MAX_INLINE_MEDIA_BYTES:
            return None, TOO_LARGE_REPLY
        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()
        fmt = detect_image_format(data, response.get("ContentType", ""))
        return (base64.b64encode(data).decode("ascii"), fmt), None
    except Exception as error:
        logger.error("media_read_failed", extra={"fields": {"error_type": type(error).__name__}})
        return None, UNREADABLE_REPLY


def _transcribe_audio(s3_url, media_id):
    try:
        job_name = f"whatsapp-audio-{media_id}"
        try:
            transcribe_client.start_transcription_job(
                TranscriptionJobName=job_name,
                IdentifyLanguage=True,
                Media={"MediaFileUri": s3_url},
                OutputBucketName=S3_BUCKET,
                OutputKey=f"transcriptions/{job_name}.json",
            )
        except transcribe_client.exceptions.ConflictException:
            logger.info("transcription_job_reused")  # a retried batch re-reads the finished job
        deadline = time.monotonic() + TRANSCRIBE_MAX_WAIT_SECONDS
        while True:
            job = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
            status = job["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED" or time.monotonic() > deadline:
                return ""
            time.sleep(5)
        result = s3_client.get_object(Bucket=S3_BUCKET, Key=f"transcriptions/{job_name}.json")
        transcription = json.loads(result["Body"].read())
        return transcription["results"]["transcripts"][0]["transcript"]
    except Exception as error:
        logger.error("transcription_failed", extra={"fields": {"error_type": type(error).__name__}})
        return ""
