"""
Video analysis tool using TwelveLabs API.

Uses TwelveLabs SDK for uploads (handles multipart/form-data) and
requests for queries and listing. Video IDs and metadata are returned
so the agent can store them in memory for follow-up questions.

Hardening: inputs are validated, every network call has a timeout, the API key
is cached with a TTL and never logged, and callers only ever see generic error
messages (details go to the logs as exception class names).
"""

import json
import logging
import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

import boto3
import requests
from botocore.config import Config
from strands import tool

logger = logging.getLogger(__name__)

TL_SECRET_ARN = os.environ.get("TL_SECRET_ARN", "")
TL_BASE_URL = "https://api.twelvelabs.io/v1.3"
REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_INDEX_NAME = os.environ.get("TL_INDEX_NAME", "whatsapp-video-index")
TL_MODEL_NAME = os.environ.get("TL_MODEL_NAME", "pegasus1.2")
S3_BUCKET = os.environ.get("S3_BUCKET", "")  # when set, uploads are limited to this bucket
S3_PRESIGNED_EXPIRY = 3600  # 1 hour

# Network and polling limits (seconds)
REQUEST_TIMEOUT = (5, 60)  # connect, read
GIST_TIMEOUT = (5, 30)
INDEXING_TIMEOUT = int(os.environ.get("TL_INDEXING_TIMEOUT_SECONDS", "540"))
INDEXING_POLL_INTERVAL = 5
SECRET_TTL_SECONDS = 3600
MAX_LISTED_VIDEOS = 100

# Input limits
MAX_PROMPT_CHARS = 2000
MAX_NAME_CHARS = 200
VALID_ACTIONS = ("upload", "query", "list_videos")

_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
_INDEX_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\- ]{0,63}$")
_S3_URI_PATTERN = re.compile(r"^s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/([^\s]+)$")
_HTTPS_URL_PATTERN = re.compile(r"^https://[^\s]+$")

_secret_lock = threading.Lock()
_cached_api_key: Optional[Tuple[str, float]] = None


def _error(message: str) -> Dict:
    return {"status": "error", "content": [{"text": message}]}


GENERIC_ERROR = "Video analysis had a technical issue. Tell the user you could not process the video right now."


def _get_api_key() -> str:
    """Retrieve the TwelveLabs API key from Secrets Manager (cached with a TTL)."""
    global _cached_api_key
    with _secret_lock:
        now = time.monotonic()
        if _cached_api_key and now - _cached_api_key[1] < SECRET_TTL_SECONDS:
            return _cached_api_key[0]

        if not TL_SECRET_ARN:
            raise ValueError("TL_SECRET_ARN is not set")

        sm = boto3.client(
            "secretsmanager",
            region_name=REGION,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}),
        )
        resp = sm.get_secret_value(SecretId=TL_SECRET_ARN)
        api_key = json.loads(resp["SecretString"]).get("TL_API_KEY", "")
        if not api_key or api_key == "CHANGE_ME":
            raise ValueError("TL_API_KEY has not been configured")
        _cached_api_key = (api_key, now)
        return api_key


def _get_tl_headers() -> dict:
    return {"x-api-key": _get_api_key()}


def validate_video_location(video_path: Optional[str]) -> Optional[str]:
    """Return an error message when video_path is not an allowed S3 URI or HTTPS URL."""
    if not video_path or not isinstance(video_path, str) or len(video_path) > 2048:
        return "video_path is required for upload."
    match = _S3_URI_PATTERN.match(video_path)
    if match:
        if S3_BUCKET and match.group(1) != S3_BUCKET:
            return "That video location is not allowed."
        return None
    if _HTTPS_URL_PATTERN.match(video_path):
        return None
    return "video_path must be an s3:// URI or an https:// URL."


def validate_video_id(video_id: Optional[str]) -> bool:
    return isinstance(video_id, str) and bool(_VIDEO_ID_PATTERN.match(video_id))


def _generate_presigned_url(s3_uri: str) -> str:
    """Convert s3://bucket/key to a pre-signed URL for TwelveLabs."""
    match = _S3_URI_PATTERN.match(s3_uri)
    if not match:
        raise ValueError("invalid S3 URI")
    bucket, key = match.group(1), match.group(2)
    s3_client = boto3.client(
        "s3", region_name=REGION, config=Config(connect_timeout=5, read_timeout=10, signature_version="s3v4")
    )
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
    except Exception as exc:  # noqa: BLE001 - fall through and create the index
        logger.warning("index_list_failed", extra={"fields": {"error": type(exc).__name__}})

    return client.indexes.create(
        index_name=index_name,
        models=[
            IndexesCreateRequestModelsItem(
                model_name=TL_MODEL_NAME,
                model_options=["visual", "audio"],
            )
        ],
    )


def _wait_for_indexing(client, task_id: str):
    """Poll the indexing task until it finishes or INDEXING_TIMEOUT elapses."""
    deadline = time.monotonic() + INDEXING_TIMEOUT
    while True:
        task = client.tasks.retrieve(task_id)
        if task.status in ("ready", "failed"):
            return task
        if time.monotonic() >= deadline:
            return task
        time.sleep(INDEXING_POLL_INTERVAL)


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
        temperature: Model temperature for query responses (0.0 to 1.0).

    Returns:
        Dict with operation results.
    """
    if not TL_SECRET_ARN:
        logger.error("video_analysis_not_configured")
        return _error("Video analysis is not configured.")

    if action not in VALID_ACTIONS:
        return _error(f"Invalid action. Use one of: {', '.join(VALID_ACTIONS)}.")

    if not isinstance(index_name, str) or not _INDEX_NAME_PATTERN.match(index_name):
        index_name = DEFAULT_INDEX_NAME
    try:
        temperature = min(1.0, max(0.0, float(temperature)))
    except (TypeError, ValueError):
        temperature = 0.2
    if video_name is not None:
        video_name = str(video_name)[:MAX_NAME_CHARS]

    try:
        if action == "upload":
            return _handle_upload(video_path, video_name, index_name)
        if action == "query":
            return _handle_query(video_path, prompt, temperature)
        return _handle_list_videos()
    except Exception as exc:  # noqa: BLE001 - details stay in the logs, the model gets a generic message
        logger.error("video_analysis_failed", extra={"fields": {"action": action, "error": type(exc).__name__}})
        return _error(GENERIC_ERROR)


def _handle_upload(video_path: str, video_name: Optional[str], index_name: str) -> Dict:
    """Upload video to TwelveLabs for indexing using the SDK."""
    problem = validate_video_location(video_path)
    if problem:
        return _error(problem)

    from twelvelabs import TwelveLabs

    client = TwelveLabs(api_key=_get_api_key(), timeout=REQUEST_TIMEOUT[1])
    index = _get_or_create_index(client, index_name)

    # Convert S3 URI to pre-signed URL (never logged: it carries a signature)
    video_url = video_path
    if video_path.startswith("s3://"):
        video_url = _generate_presigned_url(video_path)

    logger.info("tl_upload_start", extra={"fields": {"index_id": index.id}})
    task = client.tasks.create(index_id=index.id, video_url=video_url)
    task = _wait_for_indexing(client, task.id)

    if task.status != "ready":
        logger.error("tl_indexing_incomplete", extra={"fields": {"status": task.status}})
        return _error("The video could not be indexed in time. Tell the user to try again later.")

    video_id = task.video_id
    logger.info("tl_video_indexed", extra={"fields": {"video_id": video_id}})

    # Get metadata/insights via REST API (SDK gist() not available in all versions)
    gist_data: dict = {}
    try:
        gist_resp = requests.post(
            f"{TL_BASE_URL}/gist",
            headers=_get_tl_headers(),
            json={"video_id": video_id, "types": ["title", "topic", "hashtag"]},
            timeout=GIST_TIMEOUT,
        )
        if gist_resp.status_code == 200:
            gist_data = gist_resp.json()
    except requests.RequestException as exc:
        logger.warning("tl_gist_failed", extra={"fields": {"error": type(exc).__name__}})

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


def _handle_query(video_id: Optional[str], prompt: Optional[str], temperature: float) -> Dict:
    """Query an indexed video using TwelveLabs analyze API."""
    if not validate_video_id(video_id):
        return _error("A valid video_id is required for query.")
    if not prompt or not isinstance(prompt, str):
        return _error("A prompt is required for query.")
    prompt = prompt[:MAX_PROMPT_CHARS]

    response = requests.post(
        f"{TL_BASE_URL}/analyze",
        headers=_get_tl_headers(),
        json={
            "video_id": video_id,
            "prompt": prompt,
            "temperature": temperature,
        },
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code != 200:
        logger.error("tl_query_failed", extra={"fields": {"http_status": response.status_code}})
        return _error(GENERIC_ERROR)

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
    """List indexed videos across all indexes (capped at MAX_LISTED_VIDEOS)."""
    headers = _get_tl_headers()

    indexes_resp = requests.get(
        f"{TL_BASE_URL}/indexes",
        headers=headers,
        params={"model_family": "pegasus"},
        timeout=GIST_TIMEOUT,
    )
    if indexes_resp.status_code != 200:
        logger.error("tl_list_indexes_failed", extra={"fields": {"http_status": indexes_resp.status_code}})
        return _error(GENERIC_ERROR)

    all_videos = []
    for index in indexes_resp.json().get("data", []):
        if len(all_videos) >= MAX_LISTED_VIDEOS:
            break
        if index.get("video_count", 0) > 0:
            videos_resp = requests.get(
                f"{TL_BASE_URL}/indexes/{index['_id']}/videos",
                headers=headers,
                timeout=GIST_TIMEOUT,
            )
            if videos_resp.status_code == 200:
                for video in videos_resp.json().get("data", []):
                    all_videos.append({
                        "video_id": video["_id"],
                        "created_at": video.get("created_at"),
                        "index_name": index["index_name"],
                    })

    all_videos = all_videos[:MAX_LISTED_VIDEOS]
    return {
        "status": "success",
        "content": [{
            "json": {
                "videos": all_videos,
                "total_count": len(all_videos),
            }
        }],
    }
