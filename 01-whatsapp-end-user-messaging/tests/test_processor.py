import json

from conftest import FakeTable


class Ctx:
    def get_remaining_time_in_millis(self):
        return 600_000


def _stream_record(seq, message_id, text="hello", phone="573001", ts="1700000000"):
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {
                "from_phone": {"S": phone}, "id": {"S": message_id}, "timestamp": {"S": ts},
                "text": {"S": text}, "phone_number_id": {"S": "phone-number-id-abc"},
            },
        },
    }


class FakeAgent:
    def __init__(self, reply="agent says hi", error=None):
        self.reply, self.error, self.calls = reply, error, []

    def invoke_agent(self, phone, prompt, media=None, message_id="", seconds_left=None):
        self.calls.append({"phone": phone, "prompt": prompt, "message_id": message_id})
        if self.error:
            raise self.error
        return self.reply


def _setup(processor_module, monkeypatch, agent, ids=("m1",)):
    table = FakeTable()
    for i in ids:
        table.items[("573001", i)] = {"from_phone": "573001", "id": i}
    sent = []
    monkeypatch.setattr(processor_module, "table", table)
    monkeypatch.setattr(processor_module, "_agentcore", agent)
    monkeypatch.setattr(processor_module, "_send_reply", lambda *args: sent.append(args[-1]))
    return table, sent


def test_replies_once_and_marks_done(processor_module, monkeypatch):
    agent = FakeAgent()
    table, sent = _setup(processor_module, monkeypatch, agent)
    event = {"Records": [_stream_record("1", "m1")], "state": {"x": 1}}

    result = processor_module.lambda_handler(event, Ctx())

    assert result == {"state": {"x": 1}, "batchItemFailures": []}
    assert sent == ["agent says hi"]
    assert agent.calls[0]["message_id"] == "m1"  # becomes idempotency_key in the payload
    assert agent.calls[0]["prompt"].startswith("[Channel: whatsapp]")
    assert table.items[("573001", "m1")]["proc_state"] == "done"


def test_replayed_batch_does_not_answer_twice(processor_module, monkeypatch):
    agent = FakeAgent()
    _, sent = _setup(processor_module, monkeypatch, agent)
    event = {"Records": [_stream_record("1", "m1")]}

    processor_module.lambda_handler(event, Ctx())
    processor_module.lambda_handler(event, Ctx())

    assert len(agent.calls) == 1
    assert len(sent) == 1


def test_unexpected_error_is_reported_per_item_and_claim_released(processor_module, monkeypatch):
    agent = FakeAgent(error=RuntimeError("boom"))
    table, sent = _setup(processor_module, monkeypatch, agent, ids=("m1", "m2"))
    event = {"Records": [_stream_record("11", "m1"), _stream_record("12", "m2", ts="1700000001")]}

    result = processor_module.lambda_handler(event, Ctx())

    assert {f["itemIdentifier"] for f in result["batchItemFailures"]} == {"11", "12"}
    assert "proc_state" not in table.items[("573001", "m1")]  # retry can claim again
    assert sent == []


def test_agent_unavailable_sends_friendly_message_without_retry_storm(processor_module, monkeypatch):
    from agentcore_service import AgentCoreUnavailable

    agent = FakeAgent(error=AgentCoreUnavailable("down"))
    table, sent = _setup(processor_module, monkeypatch, agent)

    result = processor_module.lambda_handler({"Records": [_stream_record("1", "m1")]}, Ctx())

    assert result["batchItemFailures"] == []
    assert sent == [processor_module.TRY_AGAIN_REPLY]
    assert table.items[("573001", "m1")]["proc_state"] == "done"


def test_non_insert_events_are_ignored(processor_module, monkeypatch):
    agent = FakeAgent()
    _setup(processor_module, monkeypatch, agent)
    record = _stream_record("1", "m1")
    record["eventName"] = "MODIFY"
    assert processor_module.lambda_handler({"Records": [record]}, Ctx())["batchItemFailures"] == []
    assert agent.calls == []


def test_response_always_contains_state(processor_module, monkeypatch):
    # Tumbling windows treat a response without `state` as a failed invocation.
    _setup(processor_module, monkeypatch, FakeAgent())
    assert "state" in processor_module.lambda_handler({"Records": []}, Ctx())


class FakeBoto:
    class exceptions:
        class RuntimeClientError(Exception):
            pass

        class InternalServerException(Exception):
            pass

        class ThrottlingException(Exception):
            pass

    def __init__(self, fail_times=0):
        self.fail_times, self.payloads = fail_times, []

    def invoke_agent_runtime(self, **kwargs):
        self.payloads.append(kwargs)
        if len(self.payloads) <= self.fail_times:
            raise self.exceptions.InternalServerException("x")
        return {"response": [json.dumps({"result": "ok"}).encode()]}


def test_agentcore_payload_carries_idempotency_key_and_ids(processor_module, monkeypatch):
    import agentcore_service

    monkeypatch.setattr(agentcore_service.time, "sleep", lambda s: None)
    client = FakeBoto()
    service = agentcore_service.AgentCoreService("arn:agent", client=client)

    assert service.invoke_agent("573001234567", "hi", message_id="wamid.X") == "ok"

    call = client.payloads[0]
    payload = json.loads(call["payload"])
    assert payload["idempotency_key"] == "wamid.X"
    assert "runtimeUserId" not in call  # not available on every managed-runtime boto3
    assert payload["actor_id"].startswith("wa-user-")
    assert len(call["runtimeSessionId"]) >= 33


def test_agentcore_retries_then_raises(processor_module, monkeypatch):
    import agentcore_service

    monkeypatch.setattr(agentcore_service.time, "sleep", lambda s: None)
    client = FakeBoto(fail_times=5)
    service = agentcore_service.AgentCoreService("arn:agent", client=client)
    try:
        service.invoke_agent("1", "hi")
        raise AssertionError("expected AgentCoreUnavailable")
    except agentcore_service.AgentCoreUnavailable:
        pass
    assert len(client.payloads) == agentcore_service.MAX_ATTEMPTS


def test_agentcore_gives_up_when_time_budget_is_gone(processor_module):
    import agentcore_service

    client = FakeBoto()
    service = agentcore_service.AgentCoreService("arn:agent", client=client)
    try:
        service.invoke_agent("1", "hi", seconds_left=lambda: 5)
        raise AssertionError("expected AgentCoreUnavailable")
    except agentcore_service.AgentCoreUnavailable:
        pass
    assert client.payloads == []


def test_complete_failure_after_reply_does_not_raise(processor_module, monkeypatch):
    agent = FakeAgent()
    _, sent = _setup(processor_module, monkeypatch, agent)

    def boom(*args, **kwargs):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(processor_module.idempotency, "complete", boom)
    result = processor_module.lambda_handler({"Records": [_stream_record("1", "m1")]}, Ctx())
    assert result["batchItemFailures"] == [] and len(sent) == 1


def test_param_validation_error_is_logged_distinctly(processor_module, monkeypatch, caplog):
    import agentcore_service
    from botocore.exceptions import ParamValidationError

    class Rejecting(FakeBoto):
        def invoke_agent_runtime(self, **kwargs):
            raise ParamValidationError(report="Unknown parameter")

    service = agentcore_service.AgentCoreService("arn:agent", client=Rejecting())
    with caplog.at_level("ERROR"):
        try:
            service.invoke_agent("573001234567", "hi")
            raise AssertionError("expected AgentCoreUnavailable")
        except agentcore_service.AgentCoreUnavailable:
            pass
    assert any(r.getMessage() == "agent_invoke_invalid_params" for r in caplog.records)
