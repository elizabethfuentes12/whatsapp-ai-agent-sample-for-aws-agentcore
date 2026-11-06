"""The webhook is public, so the signature check is the gate: unsigned or invalid POSTs get 403."""

import hashlib
import hmac
import json

import pytest

from conftest import FakeSecrets, FakeTable

WA_SECRET = "wa-app-secret-value"
IG_SECRET = "ig-app-secret-value"
WA_ARN, IG_ARN = "arn:wa", "arn:ig"


def sign(body: str, secret=WA_SECRET):
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def wa_body(message_id="wamid.1", text="hello", ts=None, phone="573001234567"):
    import time

    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"display_phone_number": "15550001111", "phone_number_id": "123456"},
            "contacts": [{"profile": {"name": "Ana"}}],
            "messages": [{"id": message_id, "from": phone, "type": "text",
                          "timestamp": str(ts or int(time.time())), "text": {"body": text}}],
        }}]}],
    })


@pytest.fixture()
def configured(receiver, monkeypatch):
    import secrets_cache

    secrets_cache.clear_cache()
    table = FakeTable()
    monkeypatch.setattr(receiver, "table", table)
    monkeypatch.setattr(receiver, "SECRET_ARN", WA_ARN)
    monkeypatch.setattr(receiver, "IG_SECRET_ARN", IG_ARN)
    monkeypatch.setattr(receiver, "secrets_client", FakeSecrets({
        WA_ARN: {"WHATS_APP_SECRET": WA_SECRET, "WHATS_TOKEN": "tok", "WHATS_VERIFICATION_TOKEN": "verify-me",
                 "DISPLAY_PHONE_NUMBER": "CHANGE_ME_PHONE_NUMBER"},
        IG_ARN: {"IG_APP_SECRET": IG_SECRET, "IG_TOKEN": "igtok", "IG_ACCOUNT_ID": "999",
                 "IG_VERIFICATION_TOKEN": "ig-verify"},
    }))
    return receiver, table


def post(body, signature=None, **extra):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return {"httpMethod": "POST", "headers": headers, "body": body, **extra}


def test_unsigned_post_is_rejected_with_403_and_nothing_is_stored(configured):
    module, table = configured
    response = module.lambda_handler(post(wa_body()), None)
    assert response["statusCode"] == 403
    assert table.items == {}


def test_invalid_signature_is_rejected_with_403(configured):
    module, table = configured
    body = wa_body()
    assert module.lambda_handler(post(body, sign(body, "wrong")), None)["statusCode"] == 403
    assert module.lambda_handler(post(body, "sha256=deadbeef"), None)["statusCode"] == 403
    assert module.lambda_handler(post(wa_body(text="other"), sign(body)), None)["statusCode"] == 403
    assert table.items == {}


def test_signature_check_happens_before_parsing(configured):
    module, _ = configured
    # Not JSON and unsigned: must be 403, not 400, so the body is never interpreted.
    assert module.lambda_handler(post("not json"), None)["statusCode"] == 403


def test_secret_missing_or_unreadable_fails_closed(receiver, monkeypatch):
    import secrets_cache

    secrets_cache.clear_cache()
    monkeypatch.setattr(receiver, "table", FakeTable())
    monkeypatch.setattr(receiver, "SECRET_ARN", WA_ARN)
    monkeypatch.setattr(receiver, "IG_SECRET_ARN", IG_ARN)
    monkeypatch.setattr(receiver, "secrets_client", FakeSecrets({}))  # every read fails
    body = wa_body()
    assert receiver.lambda_handler(post(body, sign(body)), None)["statusCode"] == 403


def test_placeholder_app_secret_cannot_be_used_to_sign(receiver, monkeypatch):
    import secrets_cache

    secrets_cache.clear_cache()
    table = FakeTable()
    monkeypatch.setattr(receiver, "table", table)
    monkeypatch.setattr(receiver, "SECRET_ARN", WA_ARN)
    monkeypatch.setattr(receiver, "IG_SECRET_ARN", IG_ARN)
    monkeypatch.setattr(receiver, "secrets_client", FakeSecrets({
        WA_ARN: {"WHATS_APP_SECRET": "CHANGE_ME_APP_SECRET", "WHATS_TOKEN": "tok"},
        IG_ARN: {},
    }))
    body = wa_body()
    assert receiver.lambda_handler(post(body, sign(body, "CHANGE_ME_APP_SECRET")), None)["statusCode"] == 403
    assert table.items == {}


def test_valid_signed_post_is_stored_once_even_when_meta_retries(configured):
    module, table = configured
    body = wa_body()
    event = post(body, sign(body))
    assert module.lambda_handler(event, None)["statusCode"] == 200
    assert module.lambda_handler(event, None)["statusCode"] == 200  # Meta retry
    assert len(table.items) == 1
    item = next(iter(table.items.values()))
    assert item["text"] == "hello" and item["channel"] == "whatsapp"
    assert "whats_token" not in item and "ig_token" not in item  # tokens never reach the table
    assert item["ttl"] > 0


def test_base64_encoded_body_is_verified_against_the_decoded_bytes(configured):
    import base64

    module, table = configured
    body = wa_body(text="hola \u00f1")
    event = post(base64.b64encode(body.encode()).decode(), sign(body), isBase64Encoded=True)
    assert module.lambda_handler(event, None)["statusCode"] == 200
    assert len(table.items) == 1


def test_oversized_body_is_rejected(configured, monkeypatch):
    module, _ = configured
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 100)
    body = wa_body()
    assert module.lambda_handler(post(body, sign(body)), None)["statusCode"] == 413


def test_wa_signature_cannot_authorize_an_instagram_object(configured):
    module, table = configured
    body = json.dumps({"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "42"}, "timestamp": 9999999999999, "message": {"mid": "m1", "text": "x"}}]}]})
    assert module.lambda_handler(post(body, sign(body, WA_SECRET)), None)["statusCode"] == 403
    assert table.items == {}


def test_instagram_message_with_its_own_secret_is_stored(configured, monkeypatch):
    import time

    module, table = configured
    monkeypatch.setattr(module, "_fetch_ig_profile", lambda *_: {"name": "Bo", "username": "bo"})
    body = json.dumps({"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "42"}, "timestamp": int(time.time() * 1000),
         "message": {"mid": "m1", "text": "hey"}}]}]})
    assert module.lambda_handler(post(body, sign(body, IG_SECRET)), None)["statusCode"] == 200
    item = table.items[("ig-42", "m1")]
    assert item["text"] == "hey" and item["ig_username"] == "bo"
    assert "ig_token" not in item


def test_unknown_object_with_valid_signature_is_acknowledged_not_processed(configured):
    module, table = configured
    body = json.dumps({"object": "page", "entry": []})
    assert module.lambda_handler(post(body, sign(body)), None)["statusCode"] == 200
    assert table.items == {}


def test_other_methods_are_not_allowed(configured):
    module, _ = configured
    assert module.lambda_handler({"httpMethod": "PUT", "body": "x"}, None)["statusCode"] == 405


def test_old_messages_are_skipped(configured):
    module, table = configured
    body = wa_body(ts=1_600_000_000)
    assert module.lambda_handler(post(body, sign(body)), None)["statusCode"] == 200
    assert table.items == {}


def test_transient_failure_returns_500_so_meta_retries(configured, monkeypatch):
    module, _ = configured

    def boom(*_args, **_kwargs):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(module, "save_once", boom)
    body = wa_body()
    assert module.lambda_handler(post(body, sign(body)), None)["statusCode"] == 500


def test_verification_handshake(configured):
    module, _ = configured

    def get(token, challenge="12345", mode="subscribe"):
        return {"httpMethod": "GET", "queryStringParameters": {
            "hub.mode": mode, "hub.challenge": challenge, "hub.verify_token": token}}

    ok = module.lambda_handler(get("verify-me"), None)
    assert ok["statusCode"] == 200 and ok["body"] == "12345"
    assert module.lambda_handler(get("ig-verify"), None)["statusCode"] == 200
    assert module.lambda_handler(get("wrong"), None)["statusCode"] == 403
    assert module.lambda_handler(get("verify-me", mode="other"), None)["statusCode"] == 403
    assert module.lambda_handler(get("verify-me", challenge="<script>"), None)["statusCode"] == 403
    assert module.lambda_handler({"httpMethod": "GET"}, None)["statusCode"] == 403


def test_placeholder_verify_token_does_not_pass(receiver, monkeypatch):
    import secrets_cache

    secrets_cache.clear_cache()
    monkeypatch.setattr(receiver, "SECRET_ARN", WA_ARN)
    monkeypatch.setattr(receiver, "IG_SECRET_ARN", IG_ARN)
    monkeypatch.setattr(receiver, "secrets_client", FakeSecrets({
        WA_ARN: {"WHATS_VERIFICATION_TOKEN": "CHANGE_ME_VERIFICATION_TOKEN"}, IG_ARN: {}}))
    event = {"httpMethod": "GET", "queryStringParameters": {
        "hub.mode": "subscribe", "hub.challenge": "1", "hub.verify_token": "CHANGE_ME_VERIFICATION_TOKEN"}}
    assert receiver.lambda_handler(event, None)["statusCode"] == 403


def test_responses_have_no_cors_wildcard(configured):
    module, _ = configured
    response = module.lambda_handler(post("x"), None)
    assert not any(k.lower().startswith("access-control-") for k in response["headers"])


def test_logs_do_not_contain_bodies_or_phone_numbers(configured, caplog):
    import logging

    module, _ = configured
    body = wa_body(text="my secret message")
    with caplog.at_level(logging.DEBUG):
        module.lambda_handler(post(body, sign(body)), None)
        module.lambda_handler(post(body), None)
    output = " ".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "my secret message" not in output
    assert "573001234567" not in output
    assert WA_SECRET not in output
