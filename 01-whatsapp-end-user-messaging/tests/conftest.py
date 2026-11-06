"""Shared fixtures: load Lambda modules by folder (both Lambdas have a lambda_function.py)."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "lambdas" / "code"
LOCAL_MODULES = (
    "lambda_function", "whatsapp_service", "safe_logging", "message_utils",
    "idempotency", "agentcore_service",
)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")


def load_lambda(folder: str, module: str = "lambda_function"):
    """Import `module` from lambdas/code/<folder> with that folder first on sys.path."""
    for name in LOCAL_MODULES:
        sys.modules.pop(name, None)
    path = str(CODE / folder)
    sys.path.insert(0, path)
    try:
        return importlib.import_module(module)
    finally:
        sys.path.remove(path)


@pytest.fixture()
def handler_module():
    return load_lambda("whatsapp_handler")


@pytest.fixture()
def processor_module():
    return load_lambda("message_processor")


def conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")


class FakeTable:
    """Tiny stand-in for a DynamoDB Table that enforces the conditions the code relies on."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(item):
        return (item["from_phone"], item["id"])

    def put_item(self, Item, ConditionExpression=None):
        key = self._key(Item)
        if ConditionExpression and key in self.items:
            raise conditional_failure()
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kwargs):
        key = (Key["from_phone"], Key["id"])
        values = ExpressionAttributeValues or {}
        item = self.items.get(key)
        condition = kwargs.get("ConditionExpression", "")
        if condition.startswith("attribute_exists(id)"):
            state = item.get("proc_state") if item else None
            claimed_at = item.get("claimed_at", 0) if item else 0
            stale = values[":stale"]
            if item is None or not (state is None or (state == "processing" and claimed_at < stale)):
                raise conditional_failure()
        elif condition == "#s = :processing":
            if not item or item.get("proc_state") != "processing":
                raise conditional_failure()
        if UpdateExpression.startswith("SET #s = :processing"):
            item.update(proc_state="processing", claimed_at=values[":now"])
        elif UpdateExpression.startswith("SET #s = :done"):
            item.update(proc_state="done", done_at=values[":now"])
            item.pop("claimed_at", None)
        elif UpdateExpression.startswith("REMOVE"):
            item.pop("proc_state", None)
            item.pop("claimed_at", None)
