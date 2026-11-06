"""Durable de-duplication for stream replays and retried batches.

Each message item moves through `proc_state`: absent -> processing -> done.
`claim` is a conditional update, so two overlapping invocations can never both
process the same message. A claim older than `LOCK_SECONDS` is treated as
abandoned (the Lambda timeout is shorter), so a crashed run is retried.
"""

import time

from botocore.exceptions import ClientError

STATE_ATTR = "proc_state"
PROCESSING = "processing"
DONE = "done"
LOCK_SECONDS = 11 * 60  # longer than the 10 minute Lambda timeout


def _key(record: dict) -> dict:
    return {"from_phone": record["from_phone"], "id": record["id"]}


def _is_conditional_failure(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def claim(table, record: dict, now: int = None, lock_seconds: int = LOCK_SECONDS) -> bool:
    """Try to take the message. False means it is done or being processed elsewhere."""
    now = int(time.time()) if now is None else now
    try:
        table.update_item(
            Key=_key(record),
            UpdateExpression="SET #s = :processing, claimed_at = :now",
            ConditionExpression=(
                "attribute_exists(id) AND (attribute_not_exists(#s) OR "
                "(#s = :processing AND claimed_at < :stale))"
            ),
            ExpressionAttributeNames={"#s": STATE_ATTR},
            ExpressionAttributeValues={
                ":processing": PROCESSING,
                ":now": now,
                ":stale": now - lock_seconds,
            },
        )
        return True
    except ClientError as error:
        if _is_conditional_failure(error):
            return False
        raise


def complete(table, record: dict, now: int = None) -> None:
    """Mark the message as fully handled (reply sent)."""
    now = int(time.time()) if now is None else now
    table.update_item(
        Key=_key(record),
        UpdateExpression="SET #s = :done, done_at = :now REMOVE claimed_at",
        ExpressionAttributeNames={"#s": STATE_ATTR},
        ExpressionAttributeValues={":done": DONE, ":now": now},
    )


def release(table, record: dict) -> None:
    """Give the claim back after a failure so the retry can process the message."""
    try:
        table.update_item(
            Key=_key(record),
            UpdateExpression="REMOVE #s, claimed_at",
            ConditionExpression="#s = :processing",
            ExpressionAttributeNames={"#s": STATE_ATTR},
            ExpressionAttributeValues={":processing": PROCESSING},
        )
    except ClientError as error:
        if not _is_conditional_failure(error):
            raise
