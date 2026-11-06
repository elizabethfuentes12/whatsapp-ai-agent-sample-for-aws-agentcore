import hashlib


def test_actor_and_session_ids_are_padded_to_33_chars(processor_module):
    from message_utils import actor_id_for, session_id_for, pad_id

    assert actor_id_for("5730012345") == "wa-user-5730012345".ljust(33, "0")
    assert session_id_for("5730012345") == "wa-chat-5730012345".ljust(33, "0")
    assert len(actor_id_for("1")) == 33
    long_phone = "5" * 40
    assert actor_id_for(long_phone) == f"wa-user-{long_phone}"  # never truncated
    assert pad_id("abc", 5) == "abc00"


def test_idempotency_key_keeps_short_ids_and_hashes_long_ones(processor_module):
    from message_utils import idempotency_key_for

    assert idempotency_key_for("wamid.ABC") == "wamid.ABC"
    long_id = "wamid." + "x" * 200
    assert idempotency_key_for(long_id) == hashlib.sha256(long_id.encode()).hexdigest()
    assert len(idempotency_key_for(long_id)) <= 128


def test_group_by_sender_orders_by_timestamp(processor_module):
    from message_utils import group_by_sender

    records = [
        {"from_phone": "a", "id": "2", "timestamp": "20"},
        {"from_phone": "b", "id": "9", "timestamp": "5"},
        {"from_phone": "a", "id": "1", "timestamp": "10"},
    ]
    grouped = group_by_sender(records)
    assert [m["id"] for m in grouped["a"]] == ["1", "2"]
    assert list(grouped) == ["a", "b"]


def test_aggregate_joins_text_and_caption_and_keeps_last_media(processor_module):
    import json
    from message_utils import aggregate_messages

    messages = [
        {"text": "hello"},
        {"caption": "look", "media_ref": json.dumps({"type": "image", "s3_url": "s3://b/1"})},
        {"media_ref": json.dumps({"type": "image", "s3_url": "s3://b/2"})},
    ]
    text, media = aggregate_messages(messages)
    assert text == "hello\nlook"
    assert media["s3_url"] == "s3://b/2"


def test_tagged_prompt_has_channel_tag_for_the_agent(processor_module):
    from message_utils import build_tagged_prompt

    assert build_tagged_prompt("hi", "Ana") == "[Channel: whatsapp] [User: Ana] hi"
    assert build_tagged_prompt("", "") == "[Channel: whatsapp]"


def test_split_text_respects_limit(processor_module):
    from message_utils import split_text

    assert split_text("short") == ["short"]
    text = "\n".join(["line " * 30] * 80)
    chunks = split_text(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert all(len(c) <= 10 for c in split_text("x" * 35, 10))


def test_detect_image_format(processor_module):
    from message_utils import detect_image_format

    assert detect_image_format(b"\xff\xd8\xff\xe0") == "jpeg"
    assert detect_image_format(b"\x89PNG\r\n\x1a\n....") == "png"
    assert detect_image_format(b"%PDF-1.7") == "pdf"
    assert detect_image_format(b"RIFF....WEBP") == "webp"
    assert detect_image_format(b"zzzz", "text/plain") == "txt"
