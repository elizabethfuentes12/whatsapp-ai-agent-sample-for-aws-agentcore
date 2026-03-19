"""Lambda handler for WhatsApp messages via AWS End User Messaging (SNS).

Processes text, image, audio, video, and document messages.
Multimedia is processed by the agent, which stores text understanding in memory.
"""

import base64
import json
import logging
import os

import boto3

from whatsapp_service import WhatsAppService
from agentcore_service import AgentCoreService

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ARN = os.environ.get("AGENT_ARN", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
s3_client = boto3.client("s3")
transcribe_client = boto3.client("transcribe")


def get_s3_object_as_base64(s3_url: str) -> tuple:
    """Download an S3 object and return it as base64 with its detected format.

    Returns:
        Tuple of (base64_string, format_string) or (None, None) on error.
    """
    try:
        parts = s3_url.replace("s3://", "").split("/", 1)
        bucket = parts[0]
        key = parts[1]

        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()

        image_base64 = base64.b64encode(data).decode("ascii")

        # Detect format from magic bytes
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
            # ZIP-based formats (docx, xlsx, pptx)
            content_type = response.get("ContentType", "")
            if "wordprocessing" in content_type:
                fmt = "docx"
            elif "spreadsheet" in content_type:
                fmt = "xlsx"
            elif "presentation" in content_type:
                fmt = "pptx"
            else:
                fmt = "docx"
        else:
            content_type = response.get("ContentType", "")
            # Map common mime types to Claude-supported formats
            mime_to_format = {
                "msword": "doc",
                "vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
                "vnd.ms-excel": "xls",
                "plain": "txt",
            }
            mime_suffix = content_type.split("/")[-1] if "/" in content_type else ""
            fmt = mime_to_format.get(mime_suffix, mime_suffix or "jpeg")

        return image_base64, fmt
    except Exception as e:
        logger.error("Failed to read S3 object %s: %s", s3_url, str(e))
        return None, None


def transcribe_audio(s3_url: str, media_id: str) -> str:
    """Transcribe audio from S3 using Amazon Transcribe.

    Returns:
        Transcribed text or empty string on error.
    """
    import time

    try:
        job_name = f"whatsapp-audio-{media_id}"
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            IdentifyLanguage=True,
            Media={"MediaFileUri": s3_url},
            OutputBucketName=S3_BUCKET,
            OutputKey=f"transcriptions/{job_name}.json",
        )

        # Poll for job completion
        for _ in range(60):
            job = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
            status = job["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED":
                logger.error("Transcription job failed for %s", media_id)
                return ""
            time.sleep(5)
        else:
            logger.error("Transcription job timed out for %s", media_id)
            return ""

        # Get transcription result
        result = s3_client.get_object(
            Bucket=S3_BUCKET,
            Key=f"transcriptions/{job_name}.json",
        )
        transcription = json.loads(result["Body"].read())
        text = transcription["results"]["transcripts"][0]["transcript"]

        logger.info("Transcription completed for %s: %s chars", media_id, len(text))
        return text
    except Exception as e:
        logger.error("Transcription failed for %s: %s", media_id, str(e))
        return ""


def process_message(message, agentcore: AgentCoreService):
    """Process a single WhatsApp message."""
    phone = message.phone_number
    phone_id = message.phone_number_id
    msg_type = message.get_message_type()

    message.mark_as_read()
    message.reaction("👍")

    if table:
        message.save(table)

    # --- IMAGE ---
    if msg_type == "image":
        image = message.get_image(download=True)
        if image.get("s3_url"):
            image_data, image_format = get_s3_object_as_base64(image["s3_url"])
            if image_data:
                prompt = image.get("caption", "").strip() or "Analyze this image in detail."
                response = agentcore.invoke_agent(
                    phone, phone_id, prompt,
                    media={
                        "type": "image",
                        "format": image_format,
                        "data": image_data,
                    },
                )
                message.text_reply(response)
                return
        message.text_reply("Could not process the image. Please try again.")
        return

    # --- AUDIO ---
    if msg_type == "audio":
        audio = message.get_audio(download=True)
        if audio.get("s3_url"):
            transcript = transcribe_audio(audio["s3_url"], audio.get("media_id", ""))
            if transcript:
                message.text_reply(f"_Transcription: {transcript}_")
                response = agentcore.invoke_agent(
                    phone, phone_id,
                    f'Audio transcription: "{transcript}"',
                )
                message.text_reply(response)
                return
        message.text_reply("Could not process the audio. Please try again.")
        return

    # --- VIDEO (analyzed with TwelveLabs Pegasus via Bedrock) ---
    if msg_type == "video":
        video = message.get_video(download=True)
        if video.get("s3_url"):
            caption = video.get("caption", "").strip()
            prompt = caption or "Analyze this video in detail."
            response = agentcore.invoke_agent(
                phone, phone_id, prompt,
                media={
                    "type": "video",
                    "s3_uri": video["s3_url"],
                },
            )
            message.text_reply(response)
            return
        message.text_reply(
            "Could not process the video. Please try again."
        )
        return

    # --- DOCUMENT ---
    if msg_type == "document":
        document = message.get_document(download=True)
        if document.get("s3_url"):
            doc_data, doc_format = get_s3_object_as_base64(document["s3_url"])
            if doc_data:
                prompt = document.get("caption", "").strip() or "Analyze this document."
                import re
                raw_name = document.get("filename", "document")
                # Remove extension and sanitize: only alphanumeric, spaces, hyphens, parens, brackets
                name_no_ext = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
                filename = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
                filename = re.sub(r"\s+", " ", filename) or "document"
                response = agentcore.invoke_agent(
                    phone, phone_id, prompt,
                    media={
                        "type": "document",
                        "format": doc_format,
                        "data": doc_data,
                        "name": filename,
                    },
                )
                message.text_reply(response)
                return
        message.text_reply("Could not process the document. Please try again.")
        return

    # --- TEXT ---
    text = message.get_text()
    if text:
        response = agentcore.invoke_agent(phone, phone_id, text)
        message.text_reply(response)
        return

    message.text_reply("Message type not supported. Send text, image, audio, video, or document.")


def lambda_handler(event, context):
    """Entry point: receives SNS events from AWS End User Messaging Social."""
    agentcore = AgentCoreService(AGENT_ARN)

    for record in event.get("Records", []):
        try:
            sns_message_str = record.get("Sns", {}).get("Message", "{}")
            sns_message = json.loads(sns_message_str)
            whatsapp = WhatsAppService(sns_message)

            for message in whatsapp.messages:
                process_message(message, agentcore)
        except Exception as e:
            logger.error("Error processing record: %s", str(e), exc_info=True)

    return {"statusCode": 200}
