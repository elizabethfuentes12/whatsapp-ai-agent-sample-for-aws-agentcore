from conftest import FakeTable


def _record(i="m1"):
    return {"from_phone": "573001", "id": i}


def test_claim_complete_release_cycle(processor_module):
    import idempotency

    table = FakeTable()
    table.items[("573001", "m1")] = dict(_record())

    assert idempotency.claim(table, _record(), now=1000) is True
    assert idempotency.claim(table, _record(), now=1001) is False  # in progress elsewhere

    idempotency.release(table, _record())
    assert idempotency.claim(table, _record(), now=1002) is True

    idempotency.complete(table, _record(), now=1003)
    assert idempotency.claim(table, _record(), now=999999) is False  # done stays done
    assert table.items[("573001", "m1")]["proc_state"] == "done"


def test_stale_claim_can_be_taken_over(processor_module):
    import idempotency

    table = FakeTable()
    table.items[("573001", "m1")] = dict(_record())
    assert idempotency.claim(table, _record(), now=1000) is True
    assert idempotency.claim(table, _record(), now=1000 + idempotency.LOCK_SECONDS + 5) is True


def test_claim_of_expired_item_is_refused(processor_module):
    import idempotency

    assert idempotency.claim(FakeTable(), _record(), now=1) is False
