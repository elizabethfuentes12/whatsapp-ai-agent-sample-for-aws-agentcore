"""
YouTube download tool for Strands Agents.

Downloads YouTube videos to S3 for subsequent analysis with TwelveLabs.
Validates duration (max 1 hour) before downloading.
"""

import os
import logging
import pathlib
from typing import Dict

import boto3
from strands import tool

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_DURATION_SECONDS = 3600  # 1 hour


@tool
def youtube_download(url: str) -> Dict:
    """Download a YouTube video to S3 for analysis.

    Downloads the video, uploads to S3, and returns the S3 URI.
    Use the returned S3 URI with video_analysis action="upload" to index it.

    Validates duration before downloading (max 1 hour / 60 minutes).

    Args:
        url: YouTube video URL (e.g. https://www.youtube.com/watch?v=...)

    Returns:
        Dict with S3 URI, title, and duration. Use the s3_uri with
        video_analysis(action="upload", video_path=s3_uri) to index it.
    """
    if not url:
        return {"status": "error", "content": [{"text": "YouTube URL is required."}]}

    if not S3_BUCKET:
        return {"status": "error", "content": [{"text": "S3_BUCKET not configured."}]}

    try:
        import yt_dlp
    except ImportError:
        return {"status": "error", "content": [{"text": "yt-dlp package not available."}]}

    # Step 1: Get metadata and check duration (no download)
    logger.info("YouTube: fetching metadata for %s", url)
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error("YouTube metadata fetch failed: %s", str(e))
        return {"status": "error", "content": [{"text": f"Could not access YouTube video: {str(e)}"}]}

    duration = info.get("duration", 0) or 0
    title = info.get("title", "Untitled")
    video_id = info.get("id", "unknown")
    uploader = info.get("uploader", "")

    if duration > MAX_DURATION_SECONDS:
        minutes = duration // 60
        return {
            "status": "error",
            "content": [{"text": f"Video is {minutes} minutes long. Maximum allowed is 60 minutes for analysis."}],
        }

    duration_str = f"{duration // 60}m{duration % 60}s"
    logger.info("YouTube: title=%s, duration=%s, id=%s — downloading...", title, duration_str, video_id)

    # Step 2: Download to /tmp
    output_path = f"/tmp/yt_{video_id}.mp4"
    ydl_opts = {
        **base_opts,
        "format": "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "outtmpl": output_path,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.error("YouTube download failed: %s", str(e))
        return {"status": "error", "content": [{"text": f"Failed to download video: {str(e)}"}]}

    if not pathlib.Path(output_path).exists():
        return {"status": "error", "content": [{"text": "Download completed but file not found."}]}

    file_size_mb = pathlib.Path(output_path).stat().st_size / (1024 * 1024)
    logger.info("YouTube: downloaded %.1f MB to %s", file_size_mb, output_path)

    # Step 3: Upload to S3
    s3_key = f"youtube/{video_id}.mp4"
    s3_uri = f"s3://{S3_BUCKET}/{s3_key}"
    logger.info("YouTube: uploading to %s", s3_uri)

    try:
        s3_client = boto3.client("s3", region_name=REGION)
        s3_client.upload_file(output_path, S3_BUCKET, s3_key)
    except Exception as e:
        logger.error("YouTube S3 upload failed: %s", str(e))
        return {"status": "error", "content": [{"text": f"Failed to upload to S3: {str(e)}"}]}
    finally:
        pathlib.Path(output_path).unlink(missing_ok=True)

    logger.info("YouTube: uploaded successfully to %s", s3_uri)

    return {
        "status": "success",
        "content": [{
            "json": {
                "s3_uri": s3_uri,
                "title": title,
                "duration": duration_str,
                "uploader": uploader,
                "youtube_id": video_id,
                "file_size_mb": round(file_size_mb, 1),
            }
        }],
    }
