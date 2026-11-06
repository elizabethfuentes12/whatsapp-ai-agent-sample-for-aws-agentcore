"""Tool to link WhatsApp and Instagram accounts for cross-channel memory.

Security notes:

* The acting user is NEVER taken from model output. It comes from the verified
  ``user_id`` the message-processor Lambda sends in the invocation payload and
  that the agent entrypoint passes through ``invocation_state``. The tool fails
  closed when it is missing.
* The identifier to link comes from what the user types in chat and is not
  verified. Merging with an existing record is refused by default
  (``LINK_ALLOW_MERGE`` must be set to ``true`` explicitly). Add an out-of-band
  verification step before using this in production (see AGENTS.md).
"""

import logging
import os
import re
import threading
from typing import Dict, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from strands import ToolContext, tool

logger = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "us-east-1")
TABLE_NAME_PARAMETER = "/agentcore/unified_users_table_name"
ALLOW_MERGE = os.getenv("LINK_ALLOW_MERGE", "false").lower() == "true"
MAX_SCAN_PAGES = 20  # bounds the ig_username scan (no GSI on that attribute)

_BOTO_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3, "mode": "standard"})
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
_PHONE_PATTERN = re.compile(r"^\d{7,15}$")
_IG_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")

_table = None
_table_lock = threading.Lock()


def _get_users_table():
    """Get the unified_users DynamoDB table. Reads the table name from SSM once."""
    global _table
    with _table_lock:
        if _table is not None:
            return _table
        ssm = boto3.client("ssm", region_name=REGION, config=_BOTO_CONFIG)
        table_name = ssm.get_parameter(Name=TABLE_NAME_PARAMETER)["Parameter"]["Value"]
        dynamodb = boto3.resource("dynamodb", region_name=REGION, config=_BOTO_CONFIG)
        _table = dynamodb.Table(table_name)
        return _table


def normalize_identifier(channel: str, identifier: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate and normalize a channel identifier.

    Returns ``(normalized, error)``. Phone numbers keep digits only; Instagram
    usernames lose a leading ``@``.
    """
    if channel not in ("whatsapp", "instagram"):
        return None, "Channel must be 'whatsapp' or 'instagram'."
    if not isinstance(identifier, str):
        return None, "The identifier is not valid."
    if channel == "whatsapp":
        phone = re.sub(r"[\s+\-()]", "", identifier)
        if not _PHONE_PATTERN.match(phone):
            return None, "That phone number does not look valid. Include the country code."
        return phone, None
    username = identifier.strip().lstrip("@")
    if not _IG_USERNAME_PATTERN.match(username):
        return None, "That Instagram username does not look valid."
    return username, None


def _find_other_user(table, channel: str, identifier: str) -> Optional[dict]:
    if channel == "whatsapp":
        resp = table.query(
            IndexName="wa-phone-index",
            KeyConditionExpression=Key("wa_phone").eq(identifier),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    # ig_username is not a GSI key. The table is small, so scan page by page.
    # (A scan Limit would apply before the filter, so it must not be used here.)
    start_key = None
    for _ in range(MAX_SCAN_PAGES):
        kwargs = {"FilterExpression": Attr("ig_username").eq(identifier)}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**kwargs)
        if resp.get("Items"):
            return resp["Items"][0]
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            return None
    return None


VERIFIED_USER_KEY = "verified_user_id"


@tool(context=True)
def link_account(
    link_channel: str,
    link_identifier: str,
    tool_context: ToolContext,
) -> Dict:
    """Link a WhatsApp phone number or Instagram username to the current user
    for cross-channel personalized experience.

    Args:
        link_channel: The OTHER channel to link. Must be "whatsapp" or "instagram".
        link_identifier: The identifier on the other channel:
            - For whatsapp: phone number with country code (e.g. "5491155001234")
            - For instagram: Instagram username without @ (e.g. "maria_dev")
    """
    # The acting user comes from the verified request, never from the model.
    current_user_id = (tool_context.invocation_state or {}).get(VERIFIED_USER_KEY)
    if not isinstance(current_user_id, str) or not _USER_ID_PATTERN.match(current_user_id):
        return {"status": "error", "message": "Current user not found."}

    identifier, problem = normalize_identifier(link_channel, link_identifier)
    if problem:
        return {"status": "error", "message": problem}

    try:
        table = _get_users_table()
    except Exception as exc:  # noqa: BLE001
        logger.error("users_table_unavailable", extra={"fields": {"error": type(exc).__name__}})
        return {"status": "error", "message": "Cross-channel linking is not available."}

    try:
        return _link(table, current_user_id, link_channel, identifier)
    except Exception as exc:  # noqa: BLE001
        logger.error("link_account_failed", extra={"fields": {"error": type(exc).__name__}})
        return {"status": "error", "message": "Cross-channel linking is not available right now."}


def _link(table, current_user_id: str, link_channel: str, identifier: str) -> Dict:
    current_user = table.get_item(Key={"user_id": current_user_id}).get("Item")
    if not current_user:
        return {"status": "error", "message": "Current user not found."}

    other_user = _find_other_user(table, link_channel, identifier)

    if other_user:
        if other_user["user_id"] == current_user_id:
            return {"status": "already_linked", "message": "Accounts are already linked."}

        if not ALLOW_MERGE:
            logger.warning("link_merge_refused")
            return {
                "status": "error",
                "message": "That account already exists and needs to be linked by support.",
            }

        # Merge: copy the other user's channel fields into the current user
        updates = {}
        for field in ("wa_phone", "ig_id", "ig_username"):
            if other_user.get(field) and not current_user.get(field):
                updates[field] = other_user[field]

        if not updates:
            return {"status": "already_linked", "message": "Accounts are already linked."}

        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in updates)
        table.update_item(
            Key={"user_id": current_user_id},
            UpdateExpression=expr,
            ExpressionAttributeNames={f"#{k}": k for k in updates},
            ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
        )
        # Delete the other user (now merged)
        table.delete_item(Key={"user_id": other_user["user_id"]})

        logger.info("accounts_linked", extra={"fields": {"channel": link_channel, "merged": True}})
        return {"status": "linked", "message": "Accounts linked. Cross-channel memory is now active."}

    # No existing user for that channel: just add the field to the current user
    attribute = "wa_phone" if link_channel == "whatsapp" else "ig_username"
    table.update_item(
        Key={"user_id": current_user_id},
        UpdateExpression=f"SET {attribute} = :v",
        ExpressionAttributeValues={":v": identifier},
    )
    logger.info("accounts_linked", extra={"fields": {"channel": link_channel, "merged": False}})
    return {
        "status": "linked",
        "message": f"Account registered. When you write from {link_channel}, your memory will be shared.",
    }
