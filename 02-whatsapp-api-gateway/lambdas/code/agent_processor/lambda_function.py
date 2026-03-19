"""Agent processor Lambda - invokes AgentCore Runtime for text, image, and document messages.

Handles multimedia by:
1. Downloading from WhatsApp Cloud API to S3
2. Converting to base64 for the agent
3. The agent processes and creates a text understanding
4. Text understanding is stored in AgentCore Memory (text-only)
"""

import json
import logging
import os
import re

import boto3

from media_utils import download_and_store_media, get_s3_as_base64

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ARN = os.environ.get("AGENT_ARN", "")
WHATSAPP_OUT_LAMBDA = os.environ.get("WHATSAPP_OUT_LAMBDA", "")

lambda_client = boto3.client("lambda")
agentcore_client = boto3.client("bedrock-agentcore")


def send_reply(phone, whats_token, phone_id, text, messages_id):
    """Send reply via whatsapp_out Lambda."""
    lambda_client.invoke(
        FunctionName=WHATSAPP_OUT_LAMBDA,
        InvocationType="Event",
        Payload=json.dumps({
            "phone": phone,
            "whats_token": whats_token,
            "phone_id": phone_id,
            "message": text,
            "in_reply_to": messages_id,
        }).encode(),
    )


def invoke_agentcore(from_phone, phone_id, prompt, media=None):
    """Invoke AgentCore Runtime with text and optional media."""
    phone_clean = from_phone.replace("+", "")

    # actor_id: identifies the USER — for long-term memory (facts, preferences)
    actor_id = f"wa-user-{phone_clean}".ljust(33, "0")

    # session_id: identifies the CONVERSATION — for short-term memory (turns, expires per TTL)
    session_id = f"wa-chat-{phone_clean}".ljust(33, "0")

    payload_data = {
        "prompt": prompt.strip(),
        "actor_id": actor_id,
    }
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


def lambda_handler(event, context):
    phone = event["phone"]
    whats_token = event["whats_token"]
    phone_id = event["phone_id"]
    messages_id = event["messages_id"]
    message_type = event.get("message_type", "text")
    s3_bucket = event.get("s3_bucket", "")

    try:
        # --- TEXT ---
        if message_type == "text":
            text = event.get("message_text", "")
            response = invoke_agentcore(phone, phone_id, text)
            send_reply(phone, whats_token, phone_id, response, messages_id)
            return {"statusCode": 200}

        # --- IMAGE ---
        if message_type == "image":
            media_id = event.get("media_id", "")
            mime_type = event.get("mime_type", "")
            caption = event.get("caption", "")
            ext = mime_type.split("/")[-1] if "/" in mime_type else "jpeg"

            stored = download_and_store_media(
                media_id, whats_token, s3_bucket, "images/", ext
            )
            if stored:
                b64_data, img_format = get_s3_as_base64(stored["s3_bucket"], stored["s3_key"])
                if b64_data:
                    prompt = caption.strip() or "Analyze this image in detail."
                    response = invoke_agentcore(
                        phone, phone_id, prompt,
                        media={"type": "image", "format": img_format, "data": b64_data},
                    )
                    send_reply(phone, whats_token, phone_id, response, messages_id)
                    return {"statusCode": 200}

            send_reply(phone, whats_token, phone_id,
                       "Could not process the image. Please try again.", messages_id)
            return {"statusCode": 200}

        # --- DOCUMENT ---
        if message_type == "document":
            media_id = event.get("media_id", "")
            mime_type = event.get("mime_type", "")
            filename = event.get("filename", "document")
            caption = event.get("caption", "")
            ext = mime_type.split("/")[-1] if "/" in mime_type else "pdf"

            stored = download_and_store_media(
                media_id, whats_token, s3_bucket, "documents/", ext
            )
            if stored:
                b64_data, doc_format = get_s3_as_base64(stored["s3_bucket"], stored["s3_key"])
                if b64_data:
                    prompt = caption.strip() or "Analyze this document."
                    # Sanitize filename: only alphanumeric, spaces, hyphens, parens, brackets
                    name_no_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                    safe_name = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
                    safe_name = re.sub(r"\s+", " ", safe_name) or "document"
                    response = invoke_agentcore(
                        phone, phone_id, prompt,
                        media={
                            "type": "document",
                            "format": doc_format,
                            "data": b64_data,
                            "name": safe_name,
                        },
                    )
                    send_reply(phone, whats_token, phone_id, response, messages_id)
                    return {"statusCode": 200}

            send_reply(phone, whats_token, phone_id,
                       "Could not process the document. Please try again.", messages_id)
            return {"statusCode": 200}

        # --- VIDEO (analyzed with TwelveLabs Pegasus via Bedrock) ---
        if message_type == "video":
            media_id = event.get("media_id", "")
            mime_type = event.get("mime_type", "")
            caption = event.get("caption", "")
            ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "mp4"

            stored = download_and_store_media(
                media_id, whats_token, s3_bucket, "video/", ext
            )
            if stored:
                prompt = caption.strip() or "Analyze this video in detail."
                response = invoke_agentcore(
                    phone, phone_id, prompt,
                    media={
                        "type": "video",
                        "s3_uri": f"s3://{stored['s3_bucket']}/{stored['s3_key']}",
                    },
                )
                send_reply(phone, whats_token, phone_id, response, messages_id)
                return {"statusCode": 200}

            send_reply(phone, whats_token, phone_id,
                       "Could not process the video. Please try again.", messages_id)
            return {"statusCode": 200}

        # --- AUDIO TRANSCRIPT (from transcriber_done) ---
        if message_type == "audio_transcript":
            transcript = event.get("transcript", "")
            caption = event.get("caption", "")
            prompt = f'Audio transcription: "{transcript}"'
            if caption:
                prompt = f'{caption}\n\nAudio transcription: "{transcript}"'
            response = invoke_agentcore(phone, phone_id, prompt)
            send_reply(phone, whats_token, phone_id, response, messages_id)
            return {"statusCode": 200}

    except Exception as e:
        logger.error("Error processing message: %s", str(e), exc_info=True)
        send_reply(phone, whats_token, phone_id,
                   "An error occurred. Please try again.", messages_id)

    return {"statusCode": 200}
