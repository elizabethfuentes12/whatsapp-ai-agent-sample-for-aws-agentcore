"""Pure helpers for the processor (no AWS calls, unit tested)."""

import hashlib
import json
import re
from collections import defaultdict

MAX_ID_LENGTH = 128  # agent side limit for idempotency_key
WHATSAPP_TEXT_LIMIT = 4096


def pad_id(value: str, min_length: int = 33) -> str:
    """AgentCore actor and session ids need at least 33 characters."""
    return value.ljust(min_length, "0")


def actor_id_for(sender_key: str, canonical_user_id: str = None) -> str:
    """Identifies the USER; long-term memory is keyed on it and shared across channels.

    The canonical id from the unified users table wins; without it the id is derived
    from the verified sender (`ig-<id>` or the WhatsApp phone number).
    """
    if canonical_user_id:
        return pad_id(canonical_user_id)
    if sender_key.startswith("ig-"):
        return pad_id(f"ig-user-{sender_key[3:]}")
    return pad_id(f"wa-user-{sender_key.replace('+', '')}")


def session_id_for(sender_key: str) -> str:
    """One continuous conversation thread per user and channel."""
    if sender_key.startswith("ig-"):
        return pad_id(f"ig-chat-{sender_key[3:]}")
    return pad_id(f"wa-chat-{sender_key.replace('+', '')}")


def channel_for(sender_key: str) -> str:
    return "instagram" if sender_key.startswith("ig-") else "whatsapp"


def transcription_job_name(media_id: str) -> str:
    """Transcribe job names allow [0-9a-zA-Z._-] only."""
    return "whatsapp-audio-" + re.sub(r"[^0-9A-Za-z._-]", "-", str(media_id))[:150]


def message_age_seconds(timestamp, now: float) -> float:
    """Age of a Meta timestamp in seconds. Instagram sends milliseconds, WhatsApp seconds."""
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return float("inf")
    if value > 1e12:
        value /= 1000.0
    return now - value


def idempotency_key_for(message_id: str) -> str:
    """The message id, or its SHA-256 when it exceeds the length the agent accepts."""
    if 0 < len(message_id) <= MAX_ID_LENGTH:
        return message_id
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def group_by_sender(records):
    """Group records by from_phone, sorted by timestamp (then id for a stable order)."""
    grouped = defaultdict(list)
    for record in records:
        grouped[record.get("from_phone", "")].append(record)
    for sender in grouped:
        grouped[sender].sort(key=lambda m: (str(m.get("timestamp", "")), str(m.get("id", ""))))
    return grouped


def aggregate_messages(messages):
    """Concatenate texts and captions, keep the last media reference."""
    texts = []
    media = None
    for msg in messages:
        if msg.get("text"):
            texts.append(str(msg["text"]))
        if msg.get("caption"):
            texts.append(str(msg["caption"]))
        if msg.get("media_ref"):
            media = json.loads(msg["media_ref"])
    return "\n".join(texts), media


def build_tagged_prompt(text: str, contact_name: str = "", channel: str = "whatsapp", user_id: str = "") -> str:
    """Prefix the tags the agent reads (`[Channel: ...]`, `[UserID: ...]`, `[User: ...]`)."""
    tags = f"[Channel: {channel}]"
    if user_id:
        tags += f" [UserID: {user_id}]"
    if contact_name:
        tags += f" [User: {contact_name}]"
    return f"{tags} {text}" if text else tags


def split_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT):
    """Split a reply into chunks that fit a WhatsApp message, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def detect_image_format(data: bytes, content_type: str = "") -> str:
    """Format name the agent expects, from magic bytes with a content type fallback."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "xlsx" if "spreadsheet" in content_type else "docx"
    mime_to_format = {
        "msword": "doc", "plain": "txt",
        "vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "vnd.ms-excel": "xls",
    }
    suffix = content_type.split("/")[-1] if "/" in content_type else ""
    return mime_to_format.get(suffix, suffix or "jpeg")


def split_text_bytes(text: str, max_bytes: int):
    """Split text into chunks of at most `max_bytes` UTF-8 bytes (Instagram: 1000 bytes)."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks, current, size = [], [], 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > max_bytes:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += char_size
    if current:
        chunks.append("".join(current))
    return chunks
