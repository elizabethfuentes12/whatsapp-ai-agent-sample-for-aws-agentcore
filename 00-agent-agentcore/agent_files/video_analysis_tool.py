"""
Video analysis tool using TwelveLabs Pegasus model via Amazon Bedrock.

Provides visual + audio understanding of videos, far richer than
audio-only transcription. The text output is suitable for storage
in AgentCore Memory (text-only).
"""

import os
import json
import logging

import boto3
from strands import tool

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PEGASUS_MODEL_ID = "twelvelabs.pegasus-1-2-v1:0"


@tool
def video_analysis(
    s3_uri: str,
    prompt: str = "Describe this video in detail including visual content, actions, text on screen, and any spoken words.",
    temperature: float = 0.2,
) -> dict:
    """Analyze a video using TwelveLabs Pegasus model via Amazon Bedrock.

    This tool provides rich visual + audio understanding of videos stored in S3.
    Use it when the user sends a video and you need to understand its content.

    Args:
        s3_uri: S3 URI of the video (e.g., s3://bucket/key.mp4).
        prompt: Question or instruction about the video content.
        temperature: Model temperature for response generation.

    Returns:
        Dict with video analysis results including detailed text description.
    """
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        sts = boto3.client("sts", region_name=AWS_REGION)

        account_id = sts.get_caller_identity()["Account"]

        body = {
            "inputPrompt": prompt,
            "mediaSource": {
                "s3Location": {
                    "uri": s3_uri,
                    "bucketOwner": account_id,
                }
            },
            "temperature": temperature,
        }

        response = bedrock.invoke_model(
            modelId=PEGASUS_MODEL_ID,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )

        response_body = json.loads(response["body"].read())
        analysis = response_body.get("message", "No analysis available")

        logger.info("Video analysis completed for %s: %d chars", s3_uri, len(analysis))

        return {
            "status": "success",
            "content": [
                {
                    "json": {
                        "s3_uri": s3_uri,
                        "prompt": prompt,
                        "analysis": analysis,
                        "finish_reason": response_body.get("finishReason", "unknown"),
                    }
                }
            ],
        }

    except Exception as e:
        logger.error("Video analysis failed for %s: %s", s3_uri, str(e))
        return {
            "status": "error",
            "content": [{"text": f"Video analysis failed: {str(e)}"}],
        }
