"""Audio/Video transcription initiator Lambda.

Downloads audio/video from WhatsApp, uploads to S3,
and starts an AWS Transcribe job.
"""

import json
import logging
import os

import boto3

from media_utils import download_and_store_media
from db_utils import put_item

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
WHATSAPP_OUT_LAMBDA = os.environ.get("WHATSAPP_OUT_LAMBDA", "")

transcribe_client = boto3.client("transcribe")
lambda_client = boto3.client("lambda")


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


def lambda_handler(event, context):
    phone = event["phone"]
    whats_token = event["whats_token"]
    phone_id = event["phone_id"]
    messages_id = event["messages_id"]
    message_type = event.get("message_type", "audio")
    s3_bucket = event.get("s3_bucket", S3_BUCKET)

    whats_message = event.get("whats_message", {})
    media_info = whats_message.get(message_type, {})
    media_id = media_info.get("id", "")
    mime_type = media_info.get("mime_type", "")
    caption = media_info.get("caption", "")

    if not media_id:
        send_reply(phone, whats_token, phone_id,
                   "Could not process the media file.", messages_id)
        return {"statusCode": 400}

    # Determine file extension from mime type
    ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "ogg"
    prefix = "audio/" if message_type == "audio" else "video/"

    # Download from WhatsApp to S3
    stored = download_and_store_media(media_id, whats_token, s3_bucket, prefix, ext)
    if not stored:
        send_reply(phone, whats_token, phone_id,
                   "Could not download the media file.", messages_id)
        return {"statusCode": 400}

    # Start transcription job
    job_name = f"whatsapp-{message_type}-{media_id}"
    s3_uri = f"s3://{stored['s3_bucket']}/{stored['s3_key']}"
    output_key = f"transcriptions/{job_name}.json"

    try:
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            IdentifyLanguage=True,
            Media={"MediaFileUri": s3_uri},
            OutputBucketName=s3_bucket,
            OutputKey=output_key,
        )
        logger.info("Started transcription job: %s", job_name)
    except Exception as e:
        logger.error("Failed to start transcription: %s", str(e))
        send_reply(phone, whats_token, phone_id,
                   "Could not transcribe the media.", messages_id)
        return {"statusCode": 500}

    # Store job metadata for the transcriber_done Lambda
    put_item(TABLE_NAME, {
        "messages_id": f"job-{job_name}",
        "jobName": job_name,
        "original_messages_id": messages_id,
        "phone": phone,
        "whats_token": whats_token,
        "phone_id": phone_id,
        "message_type": message_type,
        "caption": caption,
    })

    return {"statusCode": 200}
