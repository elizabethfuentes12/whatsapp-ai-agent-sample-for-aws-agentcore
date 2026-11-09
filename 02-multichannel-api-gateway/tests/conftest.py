"""Shared fixtures: load Lambda modules by folder (both Lambdas have a lambda_function.py)."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "lambdas" / "code"
LAYER = ROOT / "layers" / "common" / "python"
LOCAL_MODULES = (
    "lambda_function", "agentcore_service", "graph_api", "idempotency", "media_utils",
    "message_utils", "safe_logging", "secrets_cache", "security", "utils",
)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_BUCKET", "media-bucket")


def load_lambda(folder: str, module: str = "lambda_function"):
    """Import `module` from lambdas/code/<folder> with the shared layer modules on sys.path."""
    for name in LOCAL_MODULES:
        sys.modules.pop(name, None)
    paths = [str(CODE / folder), str(LAYER)]
    for path in reversed(paths):
        sys.path.insert(0, path)
    try:
        return importlib.import_module(module)
    finally:
        for path in paths:
            sys.path.remove(path)


@pytest.fixture()
def layer_modules():
    """Layer modules (security, utils, ...) importable on their own."""
    sys.path.insert(0, str(LAYER))
    for name in LOCAL_MODULES:
        sys.modules.pop(name, None)
    yield
    sys.path.remove(str(LAYER))


@pytest.fixture()
def receiver():
    return load_lambda("webhook_receiver")


@pytest.fixture()
def processor():
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
            if item is None or not (state is None or (state == "processing" and claimed_at < values[":stale"])):
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


class FakeSecrets:
    """Secrets Manager client double returning JSON secrets by ARN."""

    def __init__(self, secrets):
        self.secrets = secrets

    def get_secret_value(self, SecretId):
        import json

        if SecretId not in self.secrets:
            raise RuntimeError("AccessDenied")
        return {"SecretString": json.dumps(self.secrets[SecretId])}
