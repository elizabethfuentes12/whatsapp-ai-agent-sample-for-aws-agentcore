import base64
import hashlib
import hmac

SECRET = "super-secret-app-value"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def _sig(body=BODY, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted(layer_modules):
    import security

    assert security.verify_signature(BODY, _sig(), SECRET)
    assert security.verify_signature(BODY, _sig().upper().replace("SHA256=", "sha256="), SECRET)


def test_invalid_missing_or_malformed_signatures_are_rejected(layer_modules):
    import security

    assert not security.verify_signature(BODY, _sig(secret="other"), SECRET)
    assert not security.verify_signature(BODY, _sig(b"tampered"), SECRET)
    assert not security.verify_signature(BODY, None, SECRET)
    assert not security.verify_signature(BODY, "", SECRET)
    assert not security.verify_signature(BODY, _sig()[7:], SECRET)  # no sha256= prefix
    assert not security.verify_signature(BODY, "sha1=" + _sig()[7:], SECRET)


def test_missing_or_placeholder_secret_never_verifies(layer_modules):
    import security

    # An attacker who knows the template value must not be able to forge signatures.
    assert not security.verify_signature(BODY, _sig(secret="CHANGE_ME_APP_SECRET"), "CHANGE_ME_APP_SECRET")
    assert not security.verify_signature(BODY, _sig(secret=""), "")
    assert not security.verify_signature(BODY, _sig(), None)


def test_uses_constant_time_compare(layer_modules, monkeypatch):
    import security

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(security.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    security.verify_signature(BODY, _sig(), SECRET)
    assert calls


def test_raw_body_handles_base64(layer_modules):
    import security

    encoded = base64.b64encode(BODY).decode()
    assert security.get_raw_body({"body": encoded, "isBase64Encoded": True}) == BODY
    assert security.get_raw_body({"body": BODY.decode()}) == BODY
    assert security.get_raw_body({}) == b""


def test_signature_covers_non_ascii_bodies(layer_modules):
    import security

    body = '{"text":"hola \u00f1 \U0001F44D"}'.encode("utf-8")
    event = {"body": body.decode("utf-8")}
    assert security.verify_signature(security.get_raw_body(event), _sig(body), SECRET)


def test_header_lookup_is_case_insensitive(layer_modules):
    import security

    assert security.get_header({"headers": {"x-hub-signature-256": "a"}}, security.SIGNATURE_HEADER) == "a"
    assert security.get_header({"headers": {"X-Hub-Signature-256": "b"}}, security.SIGNATURE_HEADER) == "b"
    assert security.get_header({"multiValueHeaders": {"X-HUB-SIGNATURE-256": ["c"]}}, security.SIGNATURE_HEADER) == "c"
    assert security.get_header({}, security.SIGNATURE_HEADER) is None


def test_verified_channels_checks_each_app_secret(layer_modules):
    import security

    secrets = {"whatsapp": SECRET, "instagram": "ig-secret"}
    assert security.verified_channels(BODY, _sig(), secrets) == {"whatsapp"}
    assert security.verified_channels(BODY, _sig(secret="ig-secret"), secrets) == {"instagram"}
    assert security.verified_channels(BODY, _sig(secret="x"), secrets) == set()


def test_verify_token_matching(layer_modules):
    import security

    assert security.token_matches("abc", ["zzz", "abc"])
    assert not security.token_matches("abc", ["zzz", None])
    assert not security.token_matches("CHANGE_ME_X", ["CHANGE_ME_X"])  # placeholder never matches
    assert not security.token_matches(None, ["abc"])
    assert not security.token_matches("", [""])
