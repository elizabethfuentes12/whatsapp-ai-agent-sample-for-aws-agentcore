import types

import pytest

import multimodal_agent as ma
from agent_helpers import AgentRequest, pad_id
from agent_hooks import ToolSafetyPlugin


def settings(**over):
    base = dict(
        memory_id="mem-123",
        region="us-east-1",
        model_id=ma.DEFAULT_MODEL_ID,
        guardrail_id="",
        guardrail_version="DRAFT",
        prompt_caching=True,
        max_turns=5,
        max_total_tokens=1000,
        window_size=10,
        log_level="INFO",
    )
    base.update(over)
    return ma.Settings(**base)


def req(session="s1", actor="a1", **over):
    base = dict(prompt="hello", actor_id=pad_id(actor), session_id=pad_id(session), channel="whatsapp")
    base.update(over)
    return AgentRequest(**base)


class FakeResult:
    def __init__(self, text="ok", stop_reason="end_turn"):
        self.message = {"role": "assistant", "content": [{"text": text}]}
        self.stop_reason = stop_reason


class FakeSession:
    closed = 0

    def __init__(self, config, region):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeSession.closed += 1


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FakeSession.closed = 0
    ma._recent_results = ma.RecentResultCache()
    monkeypatch.setattr(ma, "AgentCoreMemorySessionManager", FakeSession)


def test_memory_config_uses_declared_namespaces():
    cfg = ma.build_memory_config(settings(), pad_id("a1"), pad_id("s1"))
    keys = set(cfg.retrieval_config)
    assert f"/facts/{pad_id('a1')}/" in keys
    assert f"/summaries/{pad_id('a1')}/{pad_id('s1')}/" in keys
    assert cfg.batch_size == 1


def test_memory_config_none_without_memory_id():
    assert ma.build_memory_config(settings(memory_id=""), "a", "s") is None


def test_agent_is_built_per_request_not_shared(monkeypatch):
    built, calls = [], []

    class FakeAgent:
        def __init__(self, request, manager):
            built.append(self)

        def __call__(self, prompt, **kwargs):
            calls.append(kwargs)
            return FakeResult()

    monkeypatch.setattr(ma, "build_agent", lambda s, r, m: FakeAgent(r, m))
    ma.run_agent(settings(), req(session="s1"))
    ma.run_agent(settings(), req(session="s2"))
    ma.run_agent(settings(), req(session="s1"))
    assert len({id(a) for a in built}) == 3
    assert FakeSession.closed == 3  # session manager closed every time
    assert calls[0]["limits"] == {"turns": 5, "total_tokens": 1000}


def test_limit_stop_reason_returns_fallback(monkeypatch):
    class LimitAgent:
        def __call__(self, prompt, **kw):
            r = FakeResult("", stop_reason="limit_turns")
            return r

    monkeypatch.setattr(ma, "build_agent", lambda s, r, m: LimitAgent())
    assert ma.run_agent(settings(), req()) == ma.FALLBACK_LIMIT_REPLY


def test_runs_without_memory(monkeypatch):
    seen = []

    class A:
        def __call__(self, prompt, **kw):
            return FakeResult("hi")

    monkeypatch.setattr(ma, "build_agent", lambda s, r, m: seen.append(m) or A())
    assert ma.run_agent(settings(memory_id=""), req()) == "hi"
    assert seen == [None]


def test_invoke_rejects_bad_payload_with_user_message():
    out = ma.invoke({"prompt": "x" * 50_000}, None)
    assert "too long" in out["result"]


def test_invoke_hides_internal_errors(monkeypatch):
    def boom(s, r):
        raise RuntimeError("bucket secret-bucket exploded")

    monkeypatch.setattr(ma, "run_agent", boom)
    out = ma.invoke({"prompt": "hi"}, types.SimpleNamespace(session_id="sess", request_headers={}))
    assert out == {"result": ma.FALLBACK_ERROR_REPLY}


def test_invoke_dedupes_by_idempotency_key(monkeypatch):
    count = []
    monkeypatch.setattr(ma, "run_agent", lambda s, r: count.append(1) or "answer")
    ctx = types.SimpleNamespace(session_id="sess", request_headers={})
    payload = {"prompt": "hi", "idempotency_key": "wamid.1"}
    assert ma.invoke(payload, ctx) == {"result": "answer"}
    assert ma.invoke(payload, ctx) == {"result": "answer"}
    assert len(count) == 1


def test_invoke_reads_actor_from_header_case_insensitive(monkeypatch):
    seen = []
    monkeypatch.setattr(ma, "run_agent", lambda s, r: seen.append(r) or "x")
    ctx = types.SimpleNamespace(
        session_id="sess", request_headers={"x-amzn-bedrock-agentcore-runtime-custom-actor-id": "hdr-actor"}
    )
    ma.invoke({"prompt": "hi"}, ctx)
    assert seen[0].actor_id == pad_id("hdr-actor")


def test_multimodal_prompt_puts_text_first():
    import base64

    data = base64.b64encode(b"img").decode()
    blocks = ma.build_multimodal_prompt("what is this", {"type": "image", "format": "png", "data": data})
    assert "text" in blocks[0] and blocks[1]["image"]["source"]["bytes"] == b"img"

    doc = ma.build_multimodal_prompt("", {"type": "document", "format": "pdf", "name": "a_b.pdf", "data": data})
    assert doc[0]["text"].startswith("[a b.pdf]")
    assert doc[1]["document"]["name"] == "a b"

    video = ma.build_multimodal_prompt("", {"type": "video", "s3_uri": "s3://b/k.mp4"})
    assert "s3://b/k.mp4" in video[0]["text"]

    audio = ma.build_multimodal_prompt("", {"type": "audio_transcript", "data": "hello"})
    assert audio == [{"text": 'Transcription: "hello"'}]


def test_model_config_flags(monkeypatch):
    captured = {}

    class FakeModel:
        def __init__(self, **cfg):
            captured.update(cfg)

    monkeypatch.setattr(ma, "BedrockModel", FakeModel)
    monkeypatch.setattr(ma, "_model", None)
    ma.get_model(settings(guardrail_id="gr-1", guardrail_version="2"))
    assert captured["model_id"] == ma.DEFAULT_MODEL_ID
    assert captured["guardrail_id"] == "gr-1" and captured["guardrail_version"] == "2"
    assert captured["cache_config"].strategy == "auto"
    monkeypatch.setattr(ma, "_model", None)


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.setenv("AGENT_MAX_TURNS", "bad")
    s = ma.Settings.from_env()
    assert s.model_id == "global.anthropic.claude-sonnet-4-6"
    assert s.max_turns == 12


def make_event(result, name="video_analysis", exception=None):
    return types.SimpleNamespace(result=result, tool_use={"name": name}, duration=0.5, exception=exception)


def test_plugin_replaces_error_results():
    event = make_event({"status": "error", "content": [{"text": "boom s3://secret/key arn:aws:s3:::b"}]})
    ToolSafetyPlugin().after_tool_call(event)
    text = event.result["content"][0]["text"]
    assert "secret" not in text and "arn:" not in text


def test_plugin_scrubs_text_but_keeps_json():
    payload = {"video_id": "abc", "s3_uri": "s3://bucket/key"}
    event = make_event(
        {"status": "success", "content": [{"text": "see arn:aws:iam::123456789012:role/x"}, {"json": payload}]}
    )
    ToolSafetyPlugin().after_tool_call(event)
    assert "arn:" not in event.result["content"][0]["text"]
    assert event.result["content"][1]["json"] == payload


def test_plugin_registers_hook_on_real_agent():
    from strands.hooks import AfterToolCallEvent

    plugin = ToolSafetyPlugin()
    assert plugin.name == "tool-safety"
    assert any(AfterToolCallEvent in getattr(h, "_hook_event_types", [AfterToolCallEvent]) for h in plugin.hooks)


def test_build_agent_with_real_strands_objects(monkeypatch):
    """Constructing a real Agent catches API drift (plugins, trace attributes, tools)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setattr(ma, "_model", None)
    agent = ma.build_agent(settings(), req(), None)
    assert sorted(agent.tool_names) == ["link_account", "video_analysis"]
    assert agent.trace_attributes["app.channel"] == "whatsapp"
    assert pad_id("a1") not in str(agent.trace_attributes)  # only hashed IDs in traces
    other = ma.build_agent(settings(), req(session="s2"), None)
    assert other is not agent
    monkeypatch.setattr(ma, "_model", None)
