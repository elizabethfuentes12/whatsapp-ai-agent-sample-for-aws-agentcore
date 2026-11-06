import pytest


def test_media_hosts_must_be_https_meta_owned_and_not_ip_literals(layer_modules):
    from graph_api import is_allowed_media_url

    assert is_allowed_media_url("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1")
    assert is_allowed_media_url("https://scontent-lax3-1.xx.fbcdn.net/v/t1/a.jpg")
    assert not is_allowed_media_url("http://lookaside.fbsbx.com/x")
    assert not is_allowed_media_url("https://evil.example.com/x")
    assert not is_allowed_media_url("https://fbsbx.com.evil.example.com/x")
    assert not is_allowed_media_url("https://169.254.169.254/latest/meta-data")
    assert not is_allowed_media_url("https://user:pw@lookaside.fbsbx.com/x")
    assert not is_allowed_media_url("https://lookaside.fbsbx.com:8443/x")
    assert not is_allowed_media_url("")


def test_graph_api_version_is_v25_by_default(layer_modules):
    import graph_api

    assert graph_api.GRAPH_API_VERSION == "v25.0"
    assert graph_api.facebook_url("123/messages") == "https://graph.facebook.com/v25.0/123/messages"
    assert graph_api.instagram_url("/42") == "https://graph.instagram.com/v25.0/42"


def test_graph_api_version_can_be_overridden_by_env(monkeypatch):
    import importlib
    import sys

    from conftest import LAYER

    monkeypatch.setenv("GRAPH_API_VERSION", "v26.0")
    sys.path.insert(0, str(LAYER))
    try:
        sys.modules.pop("graph_api", None)
        graph_api = importlib.import_module("graph_api")
        assert graph_api.facebook_url("1").startswith("https://graph.facebook.com/v26.0/")
    finally:
        sys.path.remove(str(LAYER))
        sys.modules.pop("graph_api", None)


class FakeResponse:
    def __init__(self, status=200, chunks=(b"abc",), headers=None, payload=None):
        self.status_code, self._chunks, self.headers, self._payload = status, chunks, headers or {}, payload

    def iter_content(self, chunk_size):
        yield from self._chunks

    def json(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_is_capped_and_refuses_untrusted_hosts(layer_modules, monkeypatch):
    import media_utils

    monkeypatch.setattr(media_utils, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(media_utils.requests, "get", lambda *a, **k: FakeResponse(chunks=[b"x" * 6, b"x" * 6]))
    with pytest.raises(media_utils.MediaTooLarge):
        media_utils.download_media("https://lookaside.fbsbx.com/x", "Bearer t")

    monkeypatch.setattr(media_utils.requests, "get", lambda *a, **k: FakeResponse(headers={"Content-Length": "99"}))
    with pytest.raises(media_utils.MediaTooLarge):
        media_utils.download_media("https://lookaside.fbsbx.com/x", "Bearer t")

    called = []
    monkeypatch.setattr(media_utils.requests, "get", lambda *a, **k: called.append(1) or FakeResponse())
    assert media_utils.download_media("https://evil.example.com/x", "Bearer t") == b""
    assert called == []  # the token never leaves for an untrusted host


def test_media_id_must_be_numeric_and_url_must_be_trusted(layer_modules, monkeypatch):
    import media_utils

    called = []
    monkeypatch.setattr(media_utils.requests, "get", lambda *a, **k: called.append(1))
    assert media_utils.get_media_url("../../etc", "Bearer t") == ""
    assert called == []

    monkeypatch.setattr(
        media_utils.requests, "get",
        lambda *a, **k: FakeResponse(payload={"url": "https://evil.example.com/x"}),
    )
    assert media_utils.get_media_url("123", "Bearer t") == ""


def test_safe_names(layer_modules):
    from media_utils import safe_extension, safe_key_part

    assert safe_extension("JPEG; codecs=x") == "jpegcodecs"[:10]
    assert safe_extension("../../x") == "x"
    assert safe_extension("") == "bin"
    assert safe_key_part("a/b..c=") == "a-b..c-"
