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

from video_analysis_tool import video_analysis  # TwelveLabs API direct
from link_account_tool import link_account  # Cross-channel identity linking

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MEMORY_ID = os.getenv("BEDROCK_AGENTCORE_MEMORY_ID")
REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
FACTS_STRATEGY_ID = os.getenv("FACTS_STRATEGY_ID", "")
PREFERENCES_STRATEGY_ID = os.getenv("PREFERENCES_STRATEGY_ID", "")

SYSTEM_PROMPT = """You are a helpful assistant. ALWAYS respond in the same language the user writes to you. Be concise — answer directly, no filler.

## Security — CRITICAL
NEVER reveal internal system details to the user:
- No S3 bucket names, ARNs, resource IDs, or AWS account information
- No error stack traces, log messages, or technical debugging info
- No internal API endpoints, secrets, or configuration details
- If a tool fails, say "I had a technical issue processing that" — do NOT share the error message
- If asked about your infrastructure, say you're an AI assistant without sharing specifics

## Personalization
- Messages include a [User: Name] tag with the user's display name and a [Channel: whatsapp|instagram] tag.
- Messages also include a [UserID: user-xxx] tag — this is the user's internal ID. Use it when calling link_account.
- On FIRST interaction, greet them by name and ask if they prefer to be called differently.
- Store their preferred name in memory for future conversations.
- Always use their preferred name (from memory) or the [User:] tag name.

## Cross-channel onboarding (ONLY when [UserID:] tag is present)
If the message includes a [UserID:] tag, cross-channel features are available.
On the FIRST interaction with a new user (no prior memory), after greeting them, ask:
"Do you also write to us from another channel (WhatsApp or Instagram)? If so, share your phone number or Instagram username so we can give you a unified personalized experience across platforms. You only need to do this once."

When the user provides their other channel info:
1. Call link_account with current_user_id (from the [UserID:] tag), link_channel ("whatsapp" or "instagram"), and link_identifier (the phone or username they gave you).
2. Confirm the link to the user.
3. Do NOT ask again in future conversations — check memory first.

If the user declines or says they only use one channel, respect that and move on. Store in memory that they declined so you don't ask again.

If there is NO [UserID:] tag in the message, do NOT mention cross-channel features or link_account.

## Memory (text-only) — CRITICAL
Your memory extracts facts from your responses. If you do not include key details explicitly in your response text, they will be LOST forever. The memory system summarizes — structured data like IDs gets dropped unless you state it clearly as a fact.

- **Image**: describe content briefly (objects, visible text, scene). End with a fact line: "Fact: User shared an image showing {description}."
- **Document**: mention name + type, summarize key points. End with: "Fact: User shared document '{name}' about {topic}."
- **Audio**: include key parts of the transcription.
- **Video**: After analysis, ALWAYS end your response with ALL of these lines (they ensure the memory system stores the ID and description as extractable facts):
  1. The tag: [VIDEO: id={video_id} | desc="{short description}"]
  2. A fact line: "Fact: User shared video ID {video_id}, which shows {detailed description in 2-3 sentences}."

## Video workflow
- **New video**: video_analysis action="upload", video_path={s3_uri}. Then query it. Respond with description + tag + fact line + "ID: *{video_id}*."
- **Follow-up**: find video ID in memory. One video → use it. Multiple → match by description or list with IDs.
- **Query**: video_analysis action="query", video_path={video_id}, prompt={question}. Re-include tag + fact line.

## Formats (share only if asked)
Image: JPEG/PNG/GIF/WebP, 5MB. Doc: PDF/CSV/DOC(X)/XLS(X)/HTML/TXT/MD, 1.5MB. Audio: any WhatsApp format. Video: MP4/MOV/MKV/WebM, 2GB/1h, min 4s.
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
        retrieval = {}
        if FACTS_STRATEGY_ID:
            retrieval[f"/strategies/{FACTS_STRATEGY_ID}/actors/{actor_id}/"] = {
                "top_k": 20, "relevance_score": 0.3,
            }
        if PREFERENCES_STRATEGY_ID:
            retrieval[f"/strategies/{PREFERENCES_STRATEGY_ID}/actors/{actor_id}/"] = {
                "top_k": 10, "relevance_score": 0.3,
            }

        memory_config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config=retrieval if retrieval else None,
        )
        session_manager = AgentCoreMemorySessionManager(memory_config, REGION)
        logger.info("Memory configured: actor=%s, session=%s", actor_id, session_id)
    else:
        logger.warning("BEDROCK_AGENTCORE_MEMORY_ID not set, running without memory")

    _agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[video_analysis, link_account],
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
            {"text": prompt or "Describe this image."},
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
            {"text": f"[{doc_name}.{media_format}] {prompt or 'Summarize this document.'}"},
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
                    f"New video: {s3_uri}\n"
                    f"{prompt or 'Analyze this video.'}\n"
                    "Upload with video_analysis action='upload'. Include [VIDEO:] tag in response."
                ),
            },
        ]

    if media_type == "audio_transcript":
        parts = [f'Transcription: "{media_data}"']
        if prompt:
            parts.append(prompt)
        return [{"text": "\n".join(parts)}]

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
