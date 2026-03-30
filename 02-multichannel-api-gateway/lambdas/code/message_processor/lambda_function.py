"""Processor Lambda: DDB Stream with tumbling window -> aggregate -> AgentCore -> reply.

Receives batched DynamoDB Stream records accumulated during the tumbling window.
Groups messages by sender, concatenates text, processes media, invokes AgentCore once
per sender, and sends the reply via the correct channel (WhatsApp or Instagram).

Aggregation pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

import json
import logging
import os
import time
import uuid
from collections import defaultdict

import boto3
from boto3.dynamodb.types import TypeDeserializer
import requests

from media_utils import get_s3_as_base64

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ARN = os.environ.get("AGENT_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
IG_SECRET_ARN = os.environ.get("IG_SECRET_ARN", "")
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "")

s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")
agentcore_client = boto3.client("bedrock-agentcore")
secrets_client = boto3.client("secretsmanager")

dynamodb = boto3.resource("dynamodb")
users_table = dynamodb.Table(USERS_TABLE_NAME) if USERS_TABLE_NAME else None

GRAPH_API_VERSION = "v21.0"
IG_GRAPH_API_VERSION = "v24.0"

_cached_ig_secrets = None


def _get_ig_secrets() -> dict:
    global _cached_ig_secrets
    if _cached_ig_secrets:
        return _cached_ig_secrets
    response = secrets_client.get_secret_value(SecretId=IG_SECRET_ARN)
    _cached_ig_secrets = json.loads(response["SecretString"])
    return _cached_ig_secrets


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
    for sender_key, messages in grouped.items():
        try:
            _process_sender(sender_key, messages)
        except Exception as e:
            logger.error("Error processing sender %s: %s", sender_key, str(e), exc_info=True)

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


def _get_channel_info(sender_key, messages):
    """Extract channel-specific routing info from the last message."""
    last_msg = messages[-1]
    channel = last_msg.get("channel", "whatsapp")

    if channel == "instagram":
        # Read IG secrets from Secrets Manager to ensure we use the latest token
        ig_secrets = _get_ig_secrets()
        return {
            "channel": "instagram",
            "ig_sender_id": last_msg.get("ig_sender_id", ""),
            "ig_account_id": ig_secrets.get("IG_ACCOUNT_ID", last_msg.get("ig_account_id", "")),
            "ig_token": ig_secrets.get("IG_TOKEN", last_msg.get("ig_token", "")),
            "last_message_id": last_msg.get("id", ""),
        }

    return {
        "channel": "whatsapp",
        "to_phone": sender_key,  # from_phone in DDB = recipient phone for reply
        "phone_id": last_msg.get("phone_id", ""),
        "whats_token": last_msg.get("whats_token", ""),
        "last_message_id": last_msg.get("id", ""),
    }


def _resolve_canonical_user(sender_key, channel, messages):
    """Resolve or create a canonical user_id for cross-channel identity.

    user_id is DETERMINISTIC: wa-user-{phone} or ig-user-{id}.
    user_id = actor_id = PK of unified_users table.
    When accounts are linked, both channels resolve to the SAME user_id
    so AgentCore Memory is shared.
    """
    if not users_table:
        return None

    from boto3.dynamodb.conditions import Key

    contact_name = ""
    ig_username = ""
    for msg in messages:
        if msg.get("contact_name"):
            contact_name = str(msg["contact_name"])
        if msg.get("ig_username"):
            ig_username = str(msg["ig_username"])
        if contact_name:
            break

    if channel == "instagram":
        ig_id = sender_key[3:]  # strip "ig-" prefix

        # 1. Try exact lookup by ig_id (GSI)
        resp = users_table.query(
            IndexName="ig-id-index",
            KeyConditionExpression=Key("ig_id").eq(ig_id),
            Limit=1,
        )
        items = resp.get("Items", [])

        # 2. Fallback: find by ig_username (linked from WA but missing ig_id)
        if not items and ig_username:
            scan_resp = users_table.scan(
                FilterExpression="ig_username = :u",
                ExpressionAttributeValues={":u": ig_username},
                Limit=1,
            )
            items = scan_resp.get("Items", [])
            if items:
                users_table.update_item(
                    Key={"user_id": items[0]["user_id"]},
                    UpdateExpression="SET ig_id = :id, updated_at = :ua",
                    ExpressionAttributeValues={":id": ig_id, ":ua": int(time.time())},
                )
                logger.info("Linked ig_id %s to existing user %s (matched by @%s)",
                            ig_id, items[0]["user_id"], ig_username)

        if items:
            user = items[0]
            updates = {}
            if ig_username and user.get("ig_username") != ig_username:
                updates["ig_username"] = ig_username
            if contact_name and user.get("display_name") != contact_name:
                updates["display_name"] = contact_name
            if updates:
                updates["updated_at"] = int(time.time())
                expr = "SET " + ", ".join(f"#{k} = :{k}" for k in updates)
                users_table.update_item(
                    Key={"user_id": user["user_id"]},
                    UpdateExpression=expr,
                    ExpressionAttributeNames={f"#{k}": k for k in updates},
                    ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
                )
            logger.info("Resolved IG %s -> %s", ig_id, user["user_id"])
            return user["user_id"]

        # 3. New IG user — deterministic user_id
        user_id = f"ig-user-{ig_id}"
        item = {"user_id": user_id, "ig_id": ig_id,
                "created_at": int(time.time()), "updated_at": int(time.time())}
        if ig_username:
            item["ig_username"] = ig_username
        if contact_name:
            item["display_name"] = contact_name
        users_table.put_item(Item=item)
        logger.info("Created user %s for IG %s (@%s)", user_id, ig_id, ig_username)
        return user_id

    else:  # whatsapp
        wa_phone = sender_key

        # 1. Lookup by wa_phone (GSI)
        resp = users_table.query(
            IndexName="wa-phone-index",
            KeyConditionExpression=Key("wa_phone").eq(wa_phone),
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            user = items[0]
            if contact_name and user.get("display_name") != contact_name:
                users_table.update_item(
                    Key={"user_id": user["user_id"]},
                    UpdateExpression="SET #dn = :dn, #ua = :ua",
                    ExpressionAttributeNames={"#dn": "display_name", "#ua": "updated_at"},
                    ExpressionAttributeValues={":dn": contact_name, ":ua": int(time.time())},
                )
            logger.info("Resolved WA %s -> %s", wa_phone, user["user_id"])
            return user["user_id"]

        # 2. New WA user — deterministic user_id
        user_id = f"wa-user-{wa_phone}"
        item = {"user_id": user_id, "wa_phone": wa_phone,
                "created_at": int(time.time()), "updated_at": int(time.time())}
        if contact_name:
            item["display_name"] = contact_name
        users_table.put_item(Item=item)
        logger.info("Created user %s for WA %s", user_id, wa_phone)
        return user_id


def _process_sender(sender_key, messages):
    combined_text, media = _aggregate_messages(messages)
    channel_info = _get_channel_info(sender_key, messages)
    channel = channel_info["channel"]

    # Resolve canonical user for cross-channel memory
    canonical_user_id = _resolve_canonical_user(sender_key, channel, messages)

    # Get contact name from any message in the batch
    contact_name = ""
    for msg in messages:
        if msg.get("contact_name"):
            contact_name = str(msg["contact_name"])
            break

    # Prepend context tags so the agent knows who, where, and user_id
    tags = f"[Channel: {channel}]"
    if canonical_user_id:
        tags += f" [UserID: {canonical_user_id}]"
    if contact_name:
        tags += f" [User: {contact_name}]"

    if combined_text:
        combined_text = f"{tags} {combined_text}"
    elif contact_name:
        combined_text = tags

    response_text = None
    transcript_text = None

    if media:
        media_type = media["type"]

        if media_type == "image":
            b64_data, img_format = _get_s3_as_base64_from_url(media["s3_url"])
            if b64_data:
                response_text = _invoke_agentcore(
                    sender_key, combined_text or "Analyze this image in detail.",
                    canonical_user_id=canonical_user_id,
                    media={"type": "image", "format": img_format, "data": b64_data},
                )

        elif media_type == "audio":
            transcript = _transcribe_audio(media["s3_url"], media.get("media_id", ""))
            if transcript:
                transcript_text = f"_Transcription: {transcript}_"
                full_prompt = f'Audio transcription: "{transcript}"'
                if combined_text:
                    full_prompt += f"\n{combined_text}"
                response_text = _invoke_agentcore(sender_key, full_prompt,
                                                  canonical_user_id=canonical_user_id)

        elif media_type == "video":
            prompt = combined_text or "Analyze this video in detail."
            response_text = _invoke_agentcore(
                sender_key, prompt,
                canonical_user_id=canonical_user_id,
                media={"type": "video", "s3_uri": media["s3_url"]},
            )

        elif media_type == "document":
            b64_data, doc_format = _get_s3_as_base64_from_url(media["s3_url"])
            if b64_data:
                response_text = _invoke_agentcore(
                    sender_key, combined_text or "Analyze this document.",
                    canonical_user_id=canonical_user_id,
                    media={
                        "type": "document", "format": doc_format,
                        "data": b64_data, "name": media.get("filename", "document"),
                    },
                )
    else:
        if combined_text:
            response_text = _invoke_agentcore(sender_key, combined_text,
                                              canonical_user_id=canonical_user_id)

    if transcript_text:
        _send_reply(channel_info, transcript_text)
    if response_text:
        _send_reply(channel_info, response_text)
    elif not transcript_text:
        _send_reply(channel_info, "Could not process the message. Please try again.")


MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds


def _invoke_agentcore(sender_key, prompt, canonical_user_id=None, media=None):
    """Invoke AgentCore Runtime with exponential backoff retry.

    Retries on InternalServerException (500) and RuntimeClientError (424)
    which can occur during cold starts after session resume or when the
    runtime microVM is temporarily unavailable.

    References:
    - InvokeAgentRuntime errors: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html
    - Session resume cold starts: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html
    """
    # actor_id = user_id from unified_users table (deterministic, same across channels)
    if canonical_user_id:
        actor_id = canonical_user_id.ljust(33, "0")
    elif sender_key.startswith("ig-"):
        actor_id = f"ig-user-{sender_key[3:]}".ljust(33, "0")
    else:
        actor_id = f"wa-user-{sender_key.replace('+', '')}".ljust(33, "0")

    # session_id: channel-specific (separate conversation threads)
    if sender_key.startswith("ig-"):
        session_id = f"ig-chat-{sender_key[3:]}".ljust(33, "0")
    else:
        session_id = f"wa-chat-{sender_key.replace('+', '')}".ljust(33, "0")

    payload_data = {"prompt": prompt.strip(), "actor_id": actor_id}
    if media:
        payload_data["media"] = media

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
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

        except agentcore_client.exceptions.RuntimeClientError as e:
            last_error = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("RuntimeClientError (attempt %d/%d), retrying in %ds: %s",
                           attempt + 1, MAX_RETRIES, delay, str(e))
            time.sleep(delay)

        except agentcore_client.exceptions.InternalServerException as e:
            last_error = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("InternalServerException (attempt %d/%d), retrying in %ds: %s",
                           attempt + 1, MAX_RETRIES, delay, str(e))
            time.sleep(delay)

        except agentcore_client.exceptions.ThrottlingException as e:
            last_error = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("ThrottlingException (attempt %d/%d), retrying in %ds: %s",
                           attempt + 1, MAX_RETRIES, delay, str(e))
            time.sleep(delay)

    logger.error("AgentCore invocation failed after %d attempts: %s", MAX_RETRIES, str(last_error))
    return None


# ---------------------------------------------------------------------------
# Channel-aware reply dispatch
# ---------------------------------------------------------------------------

def _send_reply(channel_info, text):
    """Route reply to the correct channel."""
    channel = channel_info.get("channel", "whatsapp")

    if channel == "instagram":
        _send_instagram_reply(channel_info, text)
    else:
        _send_whatsapp_reply(channel_info, text)


def _send_whatsapp_reply(channel_info, text):
    phone_id = channel_info["phone_id"]
    whats_token = channel_info["whats_token"]
    # sender_key is from_phone (the WhatsApp phone number)
    # We need to extract the phone from the channel info or the sender_key
    # The phone is the from_phone which is the DDB partition key
    # It's passed through the grouped messages
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_id}/messages"
    headers = {"Authorization": whats_token, "Content-Type": "application/json"}

    # Split long messages into chunks of 4096 chars (WhatsApp limit)
    chunks = _split_text(text, 4096)
    for chunk in chunks:
        data = {
            "messaging_product": "whatsapp",
            "to": channel_info.get("to_phone", ""),
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        last_msg_id = channel_info.get("last_message_id", "")
        if last_msg_id:
            data["context"] = {"message_id": last_msg_id}
        try:
            requests.post(url, headers=headers, json=data, timeout=30)
        except Exception as e:
            logger.error("Failed to send WA reply: %s", str(e))


def _send_instagram_reply(channel_info, text):
    ig_token = channel_info["ig_token"]
    ig_account_id = channel_info["ig_account_id"]
    recipient_id = channel_info["ig_sender_id"]

    if not ig_account_id or not recipient_id:
        logger.error("Missing IG account_id or sender_id for reply")
        return

    url = f"https://graph.instagram.com/{IG_GRAPH_API_VERSION}/{ig_account_id}/messages"

    # Instagram text limit is 1000 bytes, split if needed
    chunks = _split_text(text, 1000)
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
            "access_token": ig_token,
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error("IG reply failed: %s %s", resp.status_code, resp.text)
            else:
                logger.info("IG reply sent to %s", recipient_id)
        except Exception as e:
            logger.error("Failed to send IG reply: %s", str(e))


def _split_text(text, max_bytes):
    """Split text into chunks that fit within max_bytes (UTF-8)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]

    chunks = []
    current = ""
    for char in text:
        candidate = current + char
        if len(candidate.encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = char
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


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
