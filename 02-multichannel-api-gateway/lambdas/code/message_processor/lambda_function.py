"""Processor Lambda: DDB Stream with tumbling window -> aggregate -> AgentCore -> reply.

Receives batched DynamoDB Stream records accumulated during the tumbling window.
Groups messages by sender, concatenates text, processes media, invokes AgentCore once
per sender, and sends the reply via the correct channel (WhatsApp or Instagram).

Reliability model: every message is claimed with a conditional update before work starts
and marked `done` after the reply, so stream replays and retried batches never answer
twice. A failing sender is reported through `batchItemFailures`.

Access tokens are read from Secrets Manager at send time. They are never stored in the
messages table or in the stream.

Aggregation pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import base64
import json
import logging
import os
import time

import boto3
import requests
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer

import idempotency
from agentcore_service import AgentCoreService, AgentCoreUnavailable
from graph_api import facebook_url, instagram_url
from message_utils import (
    actor_id_for,
    aggregate_messages,
    build_tagged_prompt,
    channel_for,
    detect_image_format,
    group_by_sender,
    session_id_for,
    split_text,
    split_text_bytes,
    transcription_job_name,
)
from safe_logging import configure_logging, mask_phone
from secrets_cache import load_secret
from security import is_placeholder

configure_logging()
logger = logging.getLogger()

AGENT_ARN = os.environ.get("AGENT_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
SECRET_ARN = os.environ.get("SECRET_ARN", "")
IG_SECRET_ARN = os.environ.get("IG_SECRET_ARN", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "")
MAX_INLINE_MEDIA_BYTES = int(os.environ.get("MAX_INLINE_MEDIA_BYTES", "1500000"))
TRANSCRIBE_MAX_WAIT_SECONDS = 120
MAX_USER_SCAN_PAGES = 20

TRY_AGAIN_REPLY = "I could not process your message right now. Please try again in a minute."
TOO_LARGE_REPLY = "That file is too large for me to read here. Please send a smaller one (up to about 1.5 MB)."
UNREADABLE_REPLY = "I could not read that file. Please send it again."

s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")
secrets_client = boto3.client("secretsmanager")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
users_table = dynamodb.Table(USERS_TABLE_NAME) if USERS_TABLE_NAME else None

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
        for sender_key, messages in group_by_sender(records).items():
            try:
                _process_sender(sender_key, messages, seconds_left)
            except Exception as error:
                logger.error(
                    "sender_failed",
                    extra={"fields": {"user": mask_phone(sender_key), "messages": len(messages),
                                      "error_type": type(error).__name__}},
                )
                failures.extend(
                    {"itemIdentifier": m["_sequence_number"]} for m in messages if m.get("_sequence_number")
                )

    return {"state": state, "batchItemFailures": failures}


# ---------------------------------------------------------------------------
# Cross-channel identity
# ---------------------------------------------------------------------------

def _find_by_ig_username(ig_username):
    """Bounded paginated scan (a scan `Limit` would apply before the filter)."""
    kwargs = {"FilterExpression": "ig_username = :u", "ExpressionAttributeValues": {":u": ig_username}}
    for _ in range(MAX_USER_SCAN_PAGES):
        resp = users_table.scan(**kwargs)
        if resp.get("Items"):
            return resp["Items"][0]
        if "LastEvaluatedKey" not in resp:
            return None
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return None


def _resolve_canonical_user(sender_key, channel, messages):
    """Resolve or create a canonical user_id for cross-channel identity.

    user_id is DETERMINISTIC: wa-user-{phone} or ig-user-{id}, and equals the actor_id
    in AgentCore Memory. When accounts are linked, both channels resolve to the SAME
    user_id so memory is shared.
    """
    if not users_table:
        return None

    contact_name = next((str(m["contact_name"]) for m in messages if m.get("contact_name")), "")
    ig_username = next((str(m["ig_username"]) for m in messages if m.get("ig_username")), "")
    now = int(time.time())

    if channel == "instagram":
        ig_id = sender_key[3:]  # strip "ig-" prefix
        items = users_table.query(
            IndexName="ig-id-index", KeyConditionExpression=Key("ig_id").eq(ig_id), Limit=1
        ).get("Items", [])

        if not items and ig_username:
            found = _find_by_ig_username(ig_username)
            if found:
                items = [found]
                users_table.update_item(
                    Key={"user_id": found["user_id"]},
                    UpdateExpression="SET ig_id = :id, updated_at = :ua",
                    ExpressionAttributeValues={":id": ig_id, ":ua": now},
                )

        if items:
            user = items[0]
            updates = {}
            if ig_username and user.get("ig_username") != ig_username:
                updates["ig_username"] = ig_username
            if contact_name and user.get("display_name") != contact_name:
                updates["display_name"] = contact_name
            if updates:
                updates["updated_at"] = now
                users_table.update_item(
                    Key={"user_id": user["user_id"]},
                    UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in updates),
                    ExpressionAttributeNames={f"#{k}": k for k in updates},
                    ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
                )
            return user["user_id"]

        user_id = f"ig-user-{ig_id}"
        item = {"user_id": user_id, "ig_id": ig_id, "created_at": now, "updated_at": now}
        if ig_username:
            item["ig_username"] = ig_username
        if contact_name:
            item["display_name"] = contact_name
        users_table.put_item(Item=item)
        return user_id

    wa_phone = sender_key
    items = users_table.query(
        IndexName="wa-phone-index", KeyConditionExpression=Key("wa_phone").eq(wa_phone), Limit=1
    ).get("Items", [])
    if items:
        user = items[0]
        if contact_name and user.get("display_name") != contact_name:
            users_table.update_item(
                Key={"user_id": user["user_id"]},
                UpdateExpression="SET #dn = :dn, #ua = :ua",
                ExpressionAttributeNames={"#dn": "display_name", "#ua": "updated_at"},
                ExpressionAttributeValues={":dn": contact_name, ":ua": now},
            )
        return user["user_id"]

    user_id = f"wa-user-{wa_phone}"
    item = {"user_id": user_id, "wa_phone": wa_phone, "created_at": now, "updated_at": now}
    if contact_name:
        item["display_name"] = contact_name
    users_table.put_item(Item=item)
    return user_id


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def _process_sender(sender_key, messages, seconds_left=None):
    claimed = [m for m in messages if idempotency.claim(table, m)]
    if not claimed:
        logger.info("batch_already_handled", extra={"fields": {"user": mask_phone(sender_key)}})
        return
    try:
        _handle_claimed(sender_key, claimed, seconds_left)
    except Exception:
        for message in claimed:
            idempotency.release(table, message)
        raise
    for message in claimed:
        idempotency.complete(table, message)


def _reply_target(sender_key, last_msg):
    """Routing info for the reply (no tokens: they are read from Secrets Manager when sending)."""
    if channel_for(sender_key) == "instagram":
        return {
            "channel": "instagram",
            "ig_sender_id": last_msg.get("ig_sender_id", sender_key[3:]),
            "ig_account_id": last_msg.get("ig_account_id", ""),
        }
    return {
        "channel": "whatsapp",
        "to_phone": sender_key,
        "phone_id": last_msg.get("phone_id", ""),
        "last_message_id": last_msg.get("id", ""),
    }


def _handle_claimed(sender_key, messages, seconds_left):
    combined_text, media = aggregate_messages(messages)
    last_msg = messages[-1]
    channel = channel_for(sender_key)
    target = _reply_target(sender_key, last_msg)
    last_message_id = last_msg.get("id", "")
    contact_name = next((str(m["contact_name"]) for m in messages if m.get("contact_name")), "")

    canonical_user_id = _resolve_canonical_user(sender_key, channel, messages)
    actor_id = actor_id_for(sender_key, canonical_user_id)
    session_id = session_id_for(sender_key)
    prompt_text = build_tagged_prompt(combined_text, contact_name, channel, canonical_user_id or "")

    def reply(text):
        _send_reply(target, text)

    def ask(prompt, agent_media=None):
        return _agent().invoke(
            actor_id, session_id, prompt, media=agent_media,
            message_id=last_message_id, seconds_left=seconds_left,
        )

    transcript_text = None
    response_text = None
    try:
        if media:
            media_type = media["type"]
            if media_type in ("image", "document"):
                data, error_reply = _read_inline_media(media["s3_url"])
                if error_reply:
                    reply(error_reply)
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
                    reply(UNREADABLE_REPLY)
                    return
                transcript_text = f"_Transcription: {transcript}_"
                response_text = ask(f'{prompt_text} Audio transcription: "{transcript}"')
            elif media_type == "video":
                response_text = ask(
                    prompt_text if combined_text else f"{prompt_text} Analyze this video in detail.",
                    {"type": "video", "s3_uri": media["s3_url"]},
                )
        elif combined_text:
            response_text = ask(prompt_text)
    except AgentCoreUnavailable:
        reply(TRY_AGAIN_REPLY)
        return

    if transcript_text:
        reply(transcript_text)
    if response_text:
        reply(response_text)
    elif not transcript_text:
        reply(TRY_AGAIN_REPLY)


# ---------------------------------------------------------------------------
# Channel-aware reply dispatch
# ---------------------------------------------------------------------------

def _send_reply(target, text):
    """Route reply to the correct channel."""
    if target.get("channel") == "instagram":
        _send_instagram_reply(target, text)
    else:
        _send_whatsapp_reply(target, text)


def _send_whatsapp_reply(target, text):
    token = (load_secret(secrets_client, SECRET_ARN) or {}).get("WHATS_TOKEN")
    phone_id = target.get("phone_id", "")
    if is_placeholder(token) or not str(phone_id).isdigit():
        raise RuntimeError("WhatsApp reply is not configured")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for index, chunk in enumerate(split_text(text)):
        data = {
            "messaging_product": "whatsapp",
            "to": target.get("to_phone", ""),
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        if index == 0 and target.get("last_message_id"):
            data["context"] = {"message_id": target["last_message_id"]}
        response = requests.post(facebook_url(f"{phone_id}/messages"), headers=headers, json=data, timeout=(5, 20))
        if response.status_code >= 500 or response.status_code == 429:
            raise RuntimeError(f"WhatsApp send failed with {response.status_code}")
        if response.status_code >= 400:
            logger.error("wa_reply_rejected", extra={"fields": {"status": response.status_code}})
            return  # a 4xx will not succeed on retry


def _send_instagram_reply(target, text):
    ig_secrets = load_secret(secrets_client, IG_SECRET_ARN) or {}
    ig_token = ig_secrets.get("IG_TOKEN")
    ig_account_id = target.get("ig_account_id") or ig_secrets.get("IG_ACCOUNT_ID", "")
    recipient_id = target.get("ig_sender_id", "")
    if is_placeholder(ig_token) or not ig_account_id or not recipient_id:
        raise RuntimeError("Instagram reply is not configured")

    url = instagram_url(f"{ig_account_id}/messages")
    for chunk in split_text_bytes(text, 1000):  # Instagram text limit is 1000 bytes
        response = requests.post(
            url,
            json={"recipient": {"id": recipient_id}, "message": {"text": chunk}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ig_token}"},
            timeout=(5, 20),
        )
        if response.status_code >= 500 or response.status_code == 429:
            raise RuntimeError(f"Instagram send failed with {response.status_code}")
        if response.status_code != 200:
            logger.error("ig_reply_rejected", extra={"fields": {"status": response.status_code}})
            return


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def _read_inline_media(s3_url):
    """Return ((base64, format), None) or (None, user_message). Enforces the agent size limit."""
    try:
        bucket, key = s3_url.replace("s3://", "", 1).split("/", 1)
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
        job_name = transcription_job_name(media_id)
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
