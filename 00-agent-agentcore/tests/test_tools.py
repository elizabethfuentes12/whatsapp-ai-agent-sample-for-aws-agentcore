import json

import pytest

import link_account_tool as link
import video_analysis_tool as video


# --- video analysis --------------------------------------------------------------
def test_video_location_validation(monkeypatch):
    monkeypatch.setattr(video, "S3_BUCKET", "agent-bucket")
    assert video.validate_video_location("s3://agent-bucket/videos/a.mp4") is None
    assert video.validate_video_location("s3://other-bucket/a.mp4") == "That video location is not allowed."
    assert video.validate_video_location("https://example.com/a.mp4") is None
    assert video.validate_video_location("http://example.com/a.mp4") is not None
    assert video.validate_video_location("file:///etc/passwd") is not None
    assert video.validate_video_location(None) is not None


def test_video_id_validation():
    assert video.validate_video_id("65f1a2b3c4d5e6f7a8b9c0d1")
    for bad in (None, "", "a b", "../x", "x" * 100, 5):
        assert not video.validate_video_id(bad)


def test_invalid_action_and_unconfigured(monkeypatch):
    out = video.video_analysis(action="delete")
    assert out["status"] == "error" and "Invalid action" in out["content"][0]["text"]
    monkeypatch.setattr(video, "TL_SECRET_ARN", "")
    assert video.video_analysis(action="list_videos")["status"] == "error"


def test_query_requires_valid_id_and_prompt():
    assert video._handle_query("bad id", "q", 0.2)["status"] == "error"
    assert video._handle_query("65f1a2b3c4d5", "", 0.2)["status"] == "error"


def test_errors_are_generic_and_secret_free(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-api-key-123 s3://private-bucket/x")

    monkeypatch.setattr(video, "_handle_list_videos", boom)
    out = video.video_analysis(action="list_videos")
    text = out["content"][0]["text"]
    assert "secret" not in text and "private-bucket" not in text


def test_query_parses_ndjson_stream(monkeypatch):
    class Resp:
        status_code = 200
        text = "\n".join(
            json.dumps(e)
            for e in [
                {"event_type": "stream_start"},
                {"event_type": "text_generation", "text": "Hello "},
                {"event_type": "text_generation", "text": "world"},
            ]
        )

    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return Resp()

    monkeypatch.setattr(video, "_get_api_key", lambda: "k")
    monkeypatch.setattr(video.requests, "post", fake_post)
    out = video._handle_query("65f1a2b3c4d5", "what happens?", 0.2)
    assert out["content"][0]["json"]["response"] == "Hello world"
    assert seen["timeout"] == video.REQUEST_TIMEOUT  # every call has a timeout


def test_failed_query_does_not_echo_provider_body(monkeypatch):
    class Resp:
        status_code = 401
        text = "invalid key sk-abc"

    monkeypatch.setattr(video, "_get_api_key", lambda: "k")
    monkeypatch.setattr(video.requests, "post", lambda *a, **k: Resp())
    out = video._handle_query("65f1a2b3c4d5", "q", 0.2)
    assert "sk-abc" not in json.dumps(out)


def test_api_key_cached_with_ttl_and_placeholder_rejected(monkeypatch):
    calls = []

    class SM:
        def __init__(self, value):
            self.value = value

        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": json.dumps({"TL_API_KEY": self.value})}

    monkeypatch.setattr(video, "_cached_api_key", None)
    monkeypatch.setattr(video.boto3, "client", lambda *a, **k: SM("CHANGE_ME"))
    with pytest.raises(ValueError):
        video._get_api_key()

    monkeypatch.setattr(video.boto3, "client", lambda *a, **k: SM("real-key"))
    assert video._get_api_key() == "real-key"
    assert video._get_api_key() == "real-key"
    assert len(calls) == 2  # one failed fetch + one cached success
    monkeypatch.setattr(video, "_cached_api_key", None)


def test_wait_for_indexing_times_out(monkeypatch):
    class Task:
        status = "indexing"

    class Tasks:
        def retrieve(self, task_id):
            return Task()

    client = type("C", (), {"tasks": Tasks()})()
    monkeypatch.setattr(video, "INDEXING_TIMEOUT", 0)
    assert video._wait_for_indexing(client, "t").status == "indexing"


# --- link account ----------------------------------------------------------------
def test_normalize_identifier():
    assert link.normalize_identifier("whatsapp", "+57 300-123-4567") == ("573001234567", None)
    assert link.normalize_identifier("whatsapp", "abc")[1] is not None
    assert link.normalize_identifier("instagram", "@maria_dev") == ("maria_dev", None)
    assert link.normalize_identifier("instagram", "bad name!")[1] is not None
    assert link.normalize_identifier("telegram", "x")[1] is not None


class FakeTable:
    def __init__(self, users):
        self.users = {u["user_id"]: dict(u) for u in users}
        self.scan_kwargs = []

    def get_item(self, Key):
        item = self.users.get(Key["user_id"])
        return {"Item": item} if item else {}

    def query(self, **kw):
        phone = kw["KeyConditionExpression"]._values[1]
        return {"Items": [u for u in self.users.values() if u.get("wa_phone") == phone]}

    def scan(self, **kw):
        self.scan_kwargs.append(kw)
        # emulate two pages: the match is only on the second page
        if "ExclusiveStartKey" not in kw:
            return {"Items": [], "LastEvaluatedKey": {"user_id": "x"}}
        return {"Items": [u for u in self.users.values() if u.get("ig_username") == "maria_dev"]}

    def update_item(self, Key, ExpressionAttributeValues, **kw):
        user = self.users[Key["user_id"]]
        for k, v in ExpressionAttributeValues.items():
            user[k.lstrip(":") if k not in (":p", ":u", ":v") else {":p": "wa_phone", ":u": "ig_username", ":v": "wa_phone"}[k]] = v

    def delete_item(self, Key):
        self.users.pop(Key["user_id"], None)


def run_link(table, channel, ident, user="user-1"):
    link._table = table
    try:
        return link.link_account(current_user_id=user, link_channel=channel, link_identifier=ident)
    finally:
        link._table = None


def test_link_validates_inputs_before_touching_dynamodb():
    assert run_link(FakeTable([]), "whatsapp", "nope")["status"] == "error"
    assert run_link(FakeTable([]), "whatsapp", "573001234567", user="../bad")["status"] == "error"


def test_link_scan_paginates_and_merges(monkeypatch):
    monkeypatch.setattr(link, "ALLOW_MERGE", True)
    table = FakeTable([{"user_id": "user-1", "wa_phone": "111111111"}, {"user_id": "user-2", "ig_username": "maria_dev"}])
    out = run_link(table, "instagram", "@maria_dev")
    assert out["status"] == "linked"
    assert len(table.scan_kwargs) == 2
    assert all("Limit" not in kw for kw in table.scan_kwargs)
    assert "user-2" not in table.users


def test_link_merge_can_be_disabled(monkeypatch):
    monkeypatch.setattr(link, "ALLOW_MERGE", False)
    table = FakeTable([{"user_id": "user-1"}, {"user_id": "user-2", "wa_phone": "573001234567"}])
    out = run_link(table, "whatsapp", "+573001234567")
    assert out["status"] == "error"
    assert "user-2" in table.users


def test_link_registers_new_identifier(monkeypatch):
    table = FakeTable([{"user_id": "user-1"}])
    out = run_link(table, "whatsapp", "573001234567")
    assert out["status"] == "linked"
    assert table.users["user-1"]["wa_phone"] == "573001234567"


def test_link_unknown_current_user():
    assert run_link(FakeTable([]), "whatsapp", "573001234567")["status"] == "error"
