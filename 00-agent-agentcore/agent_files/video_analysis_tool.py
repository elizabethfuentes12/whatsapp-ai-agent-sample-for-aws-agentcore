"""
Video analysis tool using TwelveLabs API.

Uses TwelveLabs SDK for uploads (handles multipart/form-data) and
requests for queries and listing. Video IDs and metadata are returned
so the agent can store them in memory for follow-up questions.
"""

import os
import json
import logging
from typing import Dict

import boto3
import requests
from strands import tool

logger = logging.getLogger(__name__)

TL_SECRET_ARN = os.environ.get("TL_SECRET_ARN", "")
TL_BASE_URL = "https://api.twelvelabs.io/v1.3"
REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_INDEX_NAME = os.environ.get("TL_INDEX_NAME", "whatsapp-video-index")
TL_MODEL_NAME = os.environ.get("TL_MODEL_NAME", "pegasus1.2")
S3_PRESIGNED_EXPIRY = 3600  # 1 hour

_cached_api_key = None


def _get_api_key() -> str:
    """Retrieve TwelveLabs API key from Secrets Manager (cached after first call)."""
    global _cached_api_key
    if _cached_api_key:
        return _cached_api_key

    if not TL_SECRET_ARN:
        raise ValueError("TL_SECRET_ARN environment variable not set.")

    sm = boto3.client("secretsmanager", region_name=REGION)
    resp = sm.get_secret_value(SecretId=TL_SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    _cached_api_key = secret["TL_API_KEY"]
    return _cached_api_key


def _get_tl_headers() -> dict:
    return {"x-api-key": _get_api_key()}


def _generate_presigned_url(s3_uri: str) -> str:
    """Convert s3://bucket/key to a pre-signed URL for TwelveLabs."""
    parts = s3_uri.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1]
    s3_client = boto3.client("s3", region_name=REGION)
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=S3_PRESIGNED_EXPIRY,
    )


def _get_or_create_index(client, index_name: str):
    """Get existing index or create new one using TwelveLabs SDK."""
    from twelvelabs.indexes import IndexesCreateRequestModelsItem

    try:
        for index in client.indexes.list():
            if index.index_name == index_name:
                return index
    except Exception:
        pass

    return client.indexes.create(
        index_name=index_name,
        models=[
            IndexesCreateRequestModelsItem(
                model_name=TL_MODEL_NAME,
                model_options=["visual", "audio"],
            )
        ],
    )


@tool
def video_analysis(
    action: str,
    video_path: str = None,
    video_name: str = None,
    prompt: str = None,
    index_name: str = DEFAULT_INDEX_NAME,
    temperature: float = 0.2,
) -> Dict:
    """Analyze videos using TwelveLabs API.

    Actions:
    - upload: Upload a video for indexing. Use video_path for the S3 URI (s3://bucket/key)
      or a public URL. Returns video_id and metadata (title, topics, hashtags).
      IMPORTANT: After uploading, tell the user the video_id and metadata so it is
      stored in memory for future queries.
    - query: Ask a question about an already-uploaded video. Requires video_path
      set to the video_id and prompt with the question.
    - list_videos: List all indexed videos with their IDs.

    Args:
        action: Operation to perform (upload, query, list_videos).
        video_path: S3 URI or URL for upload; video_id for query.
        video_name: Human-readable name for the video (upload only).
        prompt: Question about the video content (query only).
        index_name: TwelveLabs index name.
        temperature: Model temperature for query responses.

    Returns:
        Dict with operation results.
    """
    if not TL_SECRET_ARN:
        return {
            "status": "error",
            "content": [{"text": "TL_SECRET_ARN environment variable not set."}],
        }

    try:
        if action == "upload":
            return _handle_upload(video_path, video_name, index_name)
        elif action == "query":
            return _handle_query(video_path, prompt, temperature)
        elif action == "list_videos":
            return _handle_list_videos()
        else:
            return {
                "status": "error",
                "content": [{"text": f"Invalid action: {action}. Use upload, query, or list_videos."}],
            }
    except Exception as e:
        logger.error("video_analysis failed: action=%s, error=%s", action, str(e), exc_info=True)
        return {
            "status": "error",
            "content": [{"text": f"Video analysis failed: {str(e)}"}],
        }


def _handle_upload(video_path: str, video_name: str, index_name: str) -> Dict:
    """Upload video to TwelveLabs for indexing using the SDK."""
    if not video_path:
        return {"status": "error", "content": [{"text": "video_path required for upload."}]}

    from twelvelabs import TwelveLabs

    api_key = _get_api_key()
    client = TwelveLabs(api_key=api_key)
    index = _get_or_create_index(client, index_name)

    # Convert S3 URI to pre-signed URL
    video_url = video_path
    if video_path.startswith("s3://"):
        video_url = _generate_presigned_url(video_path)
        logger.info("Generated pre-signed URL for %s", video_path)

    # Upload via SDK (handles multipart/form-data)
    logger.info("TwelveLabs upload: index=%s, url_length=%d", index.id, len(video_url))
    task = client.tasks.create(index_id=index.id, video_url=video_url)
    task = client.tasks.wait_for_done(task_id=task.id)

    if task.status != "ready":
        return {"status": "error", "content": [{"text": f"Video indexing failed: status={task.status}"}]}

    video_id = task.video_id
    logger.info("Video indexed: video_id=%s", video_id)

    # Get metadata/insights via REST API (SDK gist() not available in all versions)
    headers = _get_tl_headers()
    gist_resp = requests.post(
        f"{TL_BASE_URL}/gist",
        headers=headers,
        json={"video_id": video_id, "types": ["title", "topic", "hashtag"]},
        timeout=30,
    )
    gist_data = gist_resp.json() if gist_resp.status_code == 200 else {}

    return {
        "status": "success",
        "content": [{
            "json": {
                "video_id": video_id,
                "s3_uri": video_path if video_path.startswith("s3://") else None,
                "title": gist_data.get("title", video_name or "Untitled"),
                "topics": gist_data.get("topics", []),
                "hashtags": gist_data.get("hashtags", []),
            }
        }],
    }


def _handle_query(video_id: str, prompt: str, temperature: float) -> Dict:
    """Query an indexed video using TwelveLabs analyze API."""
    if not video_id or not prompt:
        return {"status": "error", "content": [{"text": "video_id and prompt required for query."}]}

    headers = _get_tl_headers()

    response = requests.post(
        f"{TL_BASE_URL}/analyze",
        headers=headers,
        json={
            "video_id": video_id,
            "prompt": prompt,
            "temperature": temperature,
        },
        timeout=60,
    )

    if response.status_code != 200:
        return {"status": "error", "content": [{"text": f"Query failed: {response.text}"}]}

    text_parts = []
    for line in response.text.strip().split("\n"):
        if line.strip():
            try:
                event = json.loads(line)
                if event.get("event_type") == "text_generation":
                    text_parts.append(event.get("text", ""))
            except json.JSONDecodeError:
                continue

    return {
        "status": "success",
        "content": [{
            "json": {
                "video_id": video_id,
                "prompt": prompt,
                "response": "".join(text_parts),
            }
        }],
    }


def _handle_list_videos() -> Dict:
    """List all indexed videos across all indexes."""
    headers = _get_tl_headers()

    indexes_resp = requests.get(
        f"{TL_BASE_URL}/indexes",
        headers=headers,
        params={"model_family": "pegasus"},
        timeout=30,
    )
    if indexes_resp.status_code != 200:
        return {"status": "error", "content": [{"text": f"Failed to list indexes: {indexes_resp.text}"}]}

    all_videos = []
    for index in indexes_resp.json().get("data", []):
        if index.get("video_count", 0) > 0:
            videos_resp = requests.get(
                f"{TL_BASE_URL}/indexes/{index['_id']}/videos",
                headers=headers,
                timeout=30,
            )
            if videos_resp.status_code == 200:
                for video in videos_resp.json().get("data", []):
                    all_videos.append({
                        "video_id": video["_id"],
                        "created_at": video.get("created_at"),
                        "index_name": index["index_name"],
                    })

    return {
        "status": "success",
        "content": [{
            "json": {
                "videos": all_videos,
                "total_count": len(all_videos),
            }
        }],
    }
