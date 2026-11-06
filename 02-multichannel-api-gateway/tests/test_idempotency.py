from conftest import FakeTable


def test_claim_complete_release_cycle(layer_modules):
    import idempotency

    table = FakeTable()
    record = {"from_phone": "1", "id": "m1"}
    table.items[("1", "m1")] = dict(record)
    assert idempotency.claim(table, record, now=1000)
    assert not idempotency.claim(table, record, now=1001)
    idempotency.release(table, record)
    assert idempotency.claim(table, record, now=1002)
    idempotency.complete(table, record, now=1003)
    assert not idempotency.claim(table, record, now=10**9)
    assert idempotency.claim(table, {"from_phone": "1", "id": "gone"}, now=1) is False
