"""
Multimodal WhatsApp Agent for AWS AgentCore Runtime.

Handles text, images, videos, and documents sent via WhatsApp.
Since AgentCore Memory only accepts text, multimedia content is first processed
and understood by the agent, then the text understanding is stored in memory.
"""

import os
import re
import json
import base64
import logging

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

from video_analysis_tool import video_analysis

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MEMORY_ID = os.getenv("BEDROCK_AGENTCORE_MEMORY_ID")
REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

SYSTEM_PROMPT = """You are a helpful assistant on WhatsApp. You can process text, images, documents, audio, and videos.

Always respond in the same language the user writes to you.

When you receive multimedia, describe or summarize the content in detail so it is preserved in your memory for future questions.

For videos, use the video_analysis tool with the provided S3 URI.

Keep responses under 4000 characters and use WhatsApp-friendly formatting.

## Supported media formats and limits

When the user asks what you can receive or if a file fails to process, share these details:

*Images*: JPEG, PNG, GIF, WebP. Max 5 MB per image. Max resolution 8000x8000 px (optimal under 1568 px on the longest edge).

*Documents*: PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT, MD. Max ~1.5 MB when sent via WhatsApp. PDFs up to 600 pages.

*Audio/Voice notes*: Any format WhatsApp supports (OGG, MP3, AAC, M4A, WAV, AMR). Automatically transcribed to text before reaching you.

*Video*: MP4, MOV, MKV, WebM, FLV, MPEG, 3GP. Max 2 GB / 1 hour. The video must have a standard codec (H.264/H.265) and last at least 4 seconds. Very short clips or unusual codecs may fail.
"""

VALID_IMAGE_FORMATS = {"jpeg", "png", "gif", "webp"}
VALID_DOCUMENT_FORMATS = {"pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "txt", "md"}
MAX_MEDIA_BYTES = 1_500_000  # ~1.5 MB base64 limit for AgentCore payload

app = BedrockAgentCoreApp()

_agent = None
_current_session = None


def get_or_create_agent(actor_id: str, session_id: str) -> Agent:
    """Get or create a Strands agent with AgentCore Memory.

    Args:
        actor_id: Identifies the USER — for long-term memory (facts, preferences).
                  Persists across sessions. Format: wa-user-{phone}.
        session_id: Identifies the CONVERSATION — for short-term memory (turns).
                    Events expire per configured TTL. Format: wa-chat-{phone}.
    """
    global _agent, _current_session

    if _agent is not None and _current_session == session_id:
        return _agent

    session_manager = None
    if MEMORY_ID:
        memory_config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config={
                f"/users/{actor_id}/facts": {
                    "top_k": 5,
                    "relevance_score": 0.4,
                },
                f"/users/{actor_id}/preferences": {
                    "top_k": 3,
                    "relevance_score": 0.5,
                },
            },
        )
        session_manager = AgentCoreMemorySessionManager(memory_config, REGION)
        logger.info("Memory configured: actor=%s, session=%s", actor_id, session_id)
    else:
        logger.warning("BEDROCK_AGENTCORE_MEMORY_ID not set, running without memory")

    _agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[video_analysis],
        session_manager=session_manager,
    )
    _current_session = session_id
    logger.info("Agent created: model=%s, actor=%s, session=%s", MODEL_ID, actor_id, session_id)

    return _agent


def _sanitize_document_name(name: str) -> str:
    """Sanitize document name for Claude ConverseStream API.

    Only alphanumeric, whitespace, hyphens, parentheses, and square brackets allowed.
    No consecutive whitespace. No extension.
    """
    name_no_ext = name.rsplit(".", 1)[0] if "." in name else name
    sanitized = re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", " ", name_no_ext).strip()
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized or "document"


def _validate_media(media: dict) -> str | None:
    """Validate media before sending to the agent.

    Returns an error message if invalid, None if valid.
    """
    media_type = media.get("type", "")
    media_format = media.get("format", "")
    media_data = media.get("data", "")

    if media_type == "image":
        if media_format not in VALID_IMAGE_FORMATS:
            return f"Unsupported image format: {media_format}. Supported: {', '.join(VALID_IMAGE_FORMATS)}"
        if len(media_data) > MAX_MEDIA_BYTES:
            return "Image is too large to process. Please send a smaller image (under 1.5 MB)."

    if media_type == "document":
        if media_format not in VALID_DOCUMENT_FORMATS:
            return f"Unsupported document format: {media_format}. Supported: {', '.join(VALID_DOCUMENT_FORMATS)}"
        if len(media_data) > MAX_MEDIA_BYTES:
            return "Document is too large to process. Please send a smaller document (under 1.5 MB)."

    if media_type == "video":
        if not media.get("s3_uri"):
            return "Video S3 URI is missing."

    return None


def build_multimodal_prompt(prompt: str, media: dict) -> list:
    """Build a multimodal message with text and media content for the agent.

    Text is always the FIRST content block — required by AgentCoreMemorySessionManager
    which reads content[0]["text"] for memory retrieval queries.
    """
    media_type = media.get("type", "image")
    media_format = media.get("format", "jpeg")
    media_data = media.get("data", "")

    if media_type == "image":
        return [
            {"text": prompt or "Describe this image in detail."},
            {
                "image": {
                    "format": media_format,
                    "source": {"bytes": base64.b64decode(media_data)},
                }
            },
        ]

    if media_type == "document":
        doc_name = _sanitize_document_name(media.get("name", "document"))
        return [
            {"text": prompt or "Summarize this document."},
            {
                "document": {
                    "format": media_format,
                    "name": doc_name,
                    "source": {"bytes": base64.b64decode(media_data)},
                }
            },
        ]

    if media_type == "video":
        s3_uri = media.get("s3_uri", "")
        return [
            {
                "text": (
                    f"The user sent a video at: {s3_uri}\n"
                    f"User message: {prompt or 'Analyze this video.'}\n"
                    "Use the video_analysis tool with that S3 URI."
                ),
            },
        ]

    if media_type == "audio_transcript":
        return [
            {
                "text": (
                    f"Audio transcription: \"{media_data}\"\n\n"
                    f"{prompt}" if prompt else f"Audio transcription: \"{media_data}\""
                ),
            },
        ]

    return [{"text": prompt}]


@app.entrypoint
def invoke(payload, context=None):
    """Handle incoming requests from the WhatsApp Lambda handler.

    Expected payload:
    {
        "prompt": "user text",
        "actor_id": "wa-user-5730012345670000000000",  # identifies the USER
        "media": { "type": "...", ... }               # optional
    }

    session_id comes from context.session_id (set by runtimeSessionId in the API call).
    actor_id comes from the payload (most reliable) with fallback to context.
    """
    user_message = payload.get("prompt", "")

    # session_id: from runtimeSessionId -> identifies the CONVERSATION
    session_id = getattr(context, "session_id", None) or "default-session-000000000000"

    # actor_id: from payload -> identifies the USER (persists across sessions)
    # Fallback chain: payload > context header > context user_id > default
    actor_id = payload.get("actor_id")
    if not actor_id and context:
        headers = getattr(context, "request_headers", None) or {}
        actor_id = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id")
    if not actor_id:
        actor_id = getattr(context, "user_id", None)
    if not actor_id:
        actor_id = "default-actor-000000000000000"

    logger.info("invoke: session=%s, actor=%s", session_id, actor_id)

    media = payload.get("media")

    # Validate media BEFORE creating the agent — prevents invalid content from entering memory
    if media:
        validation_error = _validate_media(media)
        if validation_error:
            logger.warning("Media validation failed: %s", validation_error)
            return {"result": validation_error}

    agent = get_or_create_agent(actor_id, session_id)

    if media:
        content_blocks = build_multimodal_prompt(user_message, media)
        logger.info("Multimodal request: type=%s", media.get("type"))
        result = agent(content_blocks)
    else:
        result = agent(user_message)

    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
