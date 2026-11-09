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


def actor_id_for(from_phone: str) -> str:
    """Identifies the USER; long-term memory (facts, preferences) is keyed on it."""
    return pad_id(f"wa-user-{from_phone}")


def session_id_for(from_phone: str) -> str:
    """One continuous conversation thread per WhatsApp user."""
    return pad_id(f"wa-chat-{from_phone}")


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


_TAG_PATTERN = re.compile(r"\[\s*(?:channel|user|userid|user_id|videoid|video)\s*[:=][^\]]*\]?", re.IGNORECASE)


def neutralize_tags(text: str) -> str:
    """Strip look-alike `[Channel: ..]`, `[UserID: ..]`, `[User: ..]` tags from untrusted text.

    The Lambda prefixes trusted tags to the user's message. Without this, a user
    could type their own tag and impersonate another identity or channel.
    """
    previous = None
    while previous != text:  # repeat so nested fragments cannot rebuild a tag
        previous = text
        text = _TAG_PATTERN.sub("", text)
    return text


def build_tagged_prompt(text: str, contact_name: str = "") -> str:
    """Prefix the tags the agent reads (`[Channel: ...]`, `[User: ...]`)."""
    tags = "[Channel: whatsapp]"
    contact_name = neutralize_tags(contact_name or "").replace("\n", " ").strip()[:80]
    if contact_name:
        tags += f" [User: {contact_name}]"
    text = neutralize_tags(text or "")
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
