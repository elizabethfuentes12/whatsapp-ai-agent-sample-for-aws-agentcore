import json

import pytest

from conftest import FakeTable

TOPIC = "arn:aws:sns:us-east-1:123456789012:events"


def test_only_records_from_the_expected_topic_are_trusted(handler_module):
    ok = {"EventSource": "aws:sns", "Sns": {"TopicArn": TOPIC}}
    assert handler_module.is_trusted_record(ok, TOPIC)
    assert not handler_module.is_trusted_record({"EventSource": "aws:sns", "Sns": {"TopicArn": TOPIC + "x"}}, TOPIC)
    assert not handler_module.is_trusted_record({"EventSource": "aws:sqs", "Sns": {"TopicArn": TOPIC}}, TOPIC)
    assert not handler_module.is_trusted_record({}, TOPIC)


def test_untrusted_when_no_topic_is_configured(handler_module):
    ok = {"EventSource": "aws:sns", "Sns": {"TopicArn": TOPIC}}
    handler_module.EXPECTED_TOPIC_ARN = ""
    assert not handler_module.is_trusted_record(ok)  # fails closed


def test_untrusted_record_is_not_processed(handler_module, monkeypatch):
    handler_module.EXPECTED_TOPIC_ARN = TOPIC
    called = []
    monkeypatch.setattr(handler_module, "WhatsAppService", lambda *_: called.append(1))
    record = {"EventSource": "aws:sns", "Sns": {"TopicArn": "arn:aws:sns:us-east-1:999999999999:evil", "Message": "{}"}}
    assert handler_module.lambda_handler({"Records": [record]}, None) == {"statusCode": 200}
    assert called == []


class FakeMessage:
    def __init__(self, text="hi", msg_type="text", message_id="wamid.1"):
        self.phone_number = "573001234567"
        self.message_id = message_id
        self.phone_number_id = "phone-number-id-abc"
        self.phone_number_arn = "arn:aws:social-messaging:us-east-1:1:phone-number-id/abc"
        self.contact_name = "Ana"
        self.message = {"timestamp": "1700000000"}
        self._type = msg_type
        self._text = text
        self.sent = []

    def get_message_type(self):
        return self._type

    def get_text(self):
        return self._text

    def mark_as_read(self):
        self.sent.append("read")

    def reaction(self, emoji):
        self.sent.append("reaction")

    def text_reply(self, text):
        self.sent.append(("reply", text))


def test_duplicate_message_id_is_stored_once(handler_module):
    table = FakeTable()
    handler_module.process_message(FakeMessage(), table)
    handler_module.process_message(FakeMessage(text="changed"), table)
    assert len(table.items) == 1
    assert next(iter(table.items.values()))["text"] == "hi"


def test_item_ttl_covers_meta_retry_window(handler_module):
    item = handler_module.build_item(FakeMessage(), "text", now=1000)
    assert item["ttl"] - 1000 >= 36 * 3600


def test_unsupported_type_gets_a_reply_and_is_not_stored(handler_module):
    table = FakeTable()
    message = FakeMessage(msg_type="sticker")
    handler_module.process_message(message, table)
    assert table.items == {}
    assert ("reply", handler_module.UNSUPPORTED_REPLY) in message.sent


def test_failed_record_raises_so_the_async_retry_and_dlq_apply(handler_module, monkeypatch):
    handler_module.EXPECTED_TOPIC_ARN = TOPIC

    def boom(_):
        raise ValueError("bad")

    monkeypatch.setattr(handler_module, "WhatsAppService", boom)
    record = {"EventSource": "aws:sns", "Sns": {"TopicArn": TOPIC, "Message": json.dumps({})}}
    with pytest.raises(RuntimeError):
        handler_module.lambda_handler({"Records": [record]}, None)


def test_clean_filename(handler_module):
    assert handler_module.clean_filename("Report (v2)!!.pdf") == "Report (v2)"
    assert handler_module.clean_filename("***.pdf") == "document"


def test_mask_phone_keeps_only_last_digits(handler_module):
    from safe_logging import mask_phone

    assert mask_phone("573001234567") == "***4567"
    assert mask_phone("") == ""
