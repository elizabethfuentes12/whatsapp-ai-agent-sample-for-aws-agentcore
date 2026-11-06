import base64
import json
import logging

import pytest

from agent_helpers import (
    DEFAULT_ACTOR_ID,
    DEFAULT_SESSION_ID,
    MAX_MEDIA_BYTES,
    JsonFormatter,
    PayloadError,
    RecentResultCache,
    decoded_size,
    detect_channel,
    extract_text,
    hash_id,
    memory_retrieval_namespaces,
    pad_id,
    parse_request,
    redact_sensitive,
    sanitize_document_name,
    sanitize_id,
    validate_media,
)


def b64(n: int) -> str:
    return base64.b64encode(b"a" * n).decode()


def test_pad_id_matches_lambda_rule():
    assert pad_id("wa-user-573001234567") == "wa-user-5730012345670000000000000"
    assert len(pad_id("a")) == 33
    assert pad_id("x" * 40) == "x" * 40


def test_default_ids_are_33_chars():
    assert len(DEFAULT_ACTOR_ID) == 33
    assert len(DEFAULT_SESSION_ID) == 33


@pytest.mark.parametrize("value", [None, "", "../etc", "a b", "x" * 200, 12, "-leading"])
def test_sanitize_id_rejects_bad_values(value):
    assert sanitize_id(value, "fallback") == "fallback"


def test_sanitize_id_pads_valid_value():
    assert sanitize_id("wa-user-123", "fallback") == pad_id("wa-user-123")


def test_decoded_size_is_decoded_not_encoded():
    assert decoded_size(b64(300)) == 300
    assert decoded_size(b64(1)) == 1


def test_media_limit_uses_decoded_bytes():
    ok = {"type": "image", "format": "png", "data": b64(MAX_MEDIA_BYTES)}
    too_big = {"type": "image", "format": "png", "data": b64(MAX_MEDIA_BYTES + 10)}
    assert validate_media(ok) is None
    assert "too large" in validate_media(too_big)


def test_media_rejects_bad_format_type_and_base64():
    assert "Unsupported image format" in validate_media({"type": "image", "format": "bmp", "data": b64(3)})
    assert "Unsupported document format" in validate_media({"type": "document", "format": "exe", "data": b64(3)})
    assert validate_media({"type": "rocket"}) == "Unsupported media type."
    assert "could not be read" in validate_media({"type": "image", "format": "png", "data": "***not-base64***"})
    assert validate_media("nope") == "The attached media is not valid."
    assert "empty" in validate_media({"type": "image", "format": "png", "data": ""})


def test_video_requires_valid_s3_uri():
    assert validate_media({"type": "video"}) == "Video S3 URI is missing."
    assert validate_media({"type": "video", "s3_uri": "http://x"}) == "The video location is not valid."
    assert validate_media({"type": "video", "s3_uri": "s3://bucket-name/videos/a.mp4"}) is None


def test_sanitize_document_name():
    assert sanitize_document_name("report_final.v2.pdf") == "report final v2"
    assert sanitize_document_name("  a    b  .txt") == "a b"
    assert sanitize_document_name("???.pdf") == "document"
    assert sanitize_document_name(None) == "document"
    assert sanitize_document_name("Plan (draft) [1]") == "Plan (draft) [1]"


def test_detect_channel():
    assert detect_channel("[User: Ana] [Channel: WhatsApp] hi") == "whatsapp"
    assert detect_channel("hello") == "unknown"


def test_parse_request_actor_precedence():
    payload = {"prompt": "hi", "actor_id": "wa-user-1"}
    req = parse_request(payload, "wa-chat-1", "header-actor")
    assert req.actor_id == pad_id("wa-user-1")
    assert req.session_id == pad_id("wa-chat-1")

    req = parse_request({"prompt": "hi"}, "wa-chat-1", "header-actor")
    assert req.actor_id == pad_id("header-actor")

    req = parse_request({"prompt": "hi"}, None, None)
    assert (req.actor_id, req.session_id) == (DEFAULT_ACTOR_ID, DEFAULT_SESSION_ID)


def test_parse_request_rejects_bad_payloads():
    with pytest.raises(PayloadError):
        parse_request("string", None, None)
    with pytest.raises(PayloadError):
        parse_request({"prompt": 5}, None, None)
    with pytest.raises(PayloadError):
        parse_request({"prompt": "x" * 20_000}, None, None)
    with pytest.raises(PayloadError):
        parse_request({"prompt": "   "}, None, None)
    with pytest.raises(PayloadError):
        parse_request({"prompt": "hi", "media": {"type": "image", "format": "bmp", "data": "AA=="}}, None, None)


def test_parse_request_media_without_prompt_is_ok_and_key_validated():
    req = parse_request(
        {"media": {"type": "audio_transcript", "data": "hello"}, "idempotency_key": "wamid.1"}, None, None
    )
    assert req.media["type"] == "audio_transcript"
    assert req.idempotency_key == "wamid.1"
    assert parse_request({"prompt": "x", "idempotency_key": "k" * 500}, None, None).idempotency_key is None


def test_retrieval_namespaces_are_scoped_to_actor_and_session():
    ns = memory_retrieval_namespaces("actor-1", "sess-1")
    assert set(ns) == {
        "/facts/actor-1/",
        "/preferences/actor-1/",
        "/summaries/actor-1/sess-1/",
        "/episodes/actor-1/",
    }
    assert ns["/facts/actor-1/"]["top_k"] == 20


def test_redact_sensitive():
    text = "failed s3://my-bucket/key.mp4 arn:aws:iam::123456789012:role/x acct 123456789012 api_key=abc123"
    out = redact_sensitive(text)
    for leaked in ("my-bucket", "arn:aws", "123456789012", "abc123"):
        assert leaked not in out


def test_extract_text_joins_text_blocks_only():
    msg = {"role": "assistant", "content": [{"text": "Hello"}, {"toolUse": {}}, {"text": "World"}]}
    assert extract_text(msg) == "Hello\nWorld"
    assert extract_text(None) == ""


def test_recent_result_cache_ttl_and_bound():
    now = [0.0]
    cache = RecentResultCache(max_items=2, ttl_seconds=10, clock=lambda: now[0])
    cache.put(("a",), "1")
    cache.put(("b",), "2")
    cache.put(("c",), "3")
    assert cache.get(("a",)) is None  # evicted
    assert cache.get(("c",)) == "3"
    now[0] = 11
    assert cache.get(("c",)) is None  # expired
    assert len(cache) == 1 or len(cache) == 0


def test_hash_id_is_stable_and_short():
    assert hash_id("x") == hash_id("x")
    assert len(hash_id("x")) == 12
    assert "573" not in hash_id("wa-user-573001234567")


def test_json_formatter_includes_fields_and_redacts_exceptions():
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)
    record.fields = {"tool": "video_analysis"}
    try:
        raise RuntimeError("bad s3://secret-bucket/key")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert data["tool"] == "video_analysis"
    assert "secret-bucket" not in data["exception"]
