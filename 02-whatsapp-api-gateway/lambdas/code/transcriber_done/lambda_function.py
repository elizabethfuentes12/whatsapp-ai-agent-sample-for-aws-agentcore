"""Lambda triggered when AWS Transcribe completes (S3 event).

Reads the transcription result, retrieves original message metadata,
and forwards to the agent_processor Lambda.
"""

import json
import logging
import os

import boto3

from db_utils import query_by_index

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "")
AGENT_PROCESSOR_LAMBDA = os.environ.get("AGENT_PROCESSOR_LAMBDA", "")

s3_client = boto3.client("s3")
lambda_client = boto3.client("lambda")


def lambda_handler(event, context):
    for record in event.get("Records", []):
        try:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]

            if not key.startswith("transcriptions/"):
                continue

            # Download transcription result
            response = s3_client.get_object(Bucket=bucket, Key=key)
            result = json.loads(response["Body"].read())
            transcript = result["results"]["transcripts"][0]["transcript"]

            # Extract job name from S3 key
            filename = key.split("/")[-1]
            job_name = filename.replace(".json", "")

            logger.info("Transcription done for job: %s (%d chars)", job_name, len(transcript))

            # Get original message metadata from DynamoDB
            job_data = query_by_index(TABLE_NAME, "jobnameindex", "jobName", job_name)
            if not job_data:
                logger.error("No metadata found for job: %s", job_name)
                continue

            message_type = job_data.get("message_type", "audio")
            media_type = "audio_transcript" if message_type == "audio" else "video_transcript"

            # Forward to agent_processor
            lambda_client.invoke(
                FunctionName=AGENT_PROCESSOR_LAMBDA,
                InvocationType="Event",
                Payload=json.dumps({
                    "phone": job_data["phone"],
                    "whats_token": job_data["whats_token"],
                    "phone_id": job_data["phone_id"],
                    "messages_id": job_data["original_messages_id"],
                    "message_type": media_type,
                    "transcript": transcript,
                    "caption": job_data.get("caption", ""),
                }).encode(),
            )

        except Exception as e:
            logger.error("Error processing transcription: %s", str(e), exc_info=True)

    return {"statusCode": 200}
