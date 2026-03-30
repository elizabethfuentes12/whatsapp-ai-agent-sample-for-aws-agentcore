"""Tool to link WhatsApp and Instagram accounts for cross-channel memory."""

import os
import logging
from typing import Dict

import boto3
from boto3.dynamodb.conditions import Key
from strands import tool

logger = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "us-east-1")

_table = None


def _get_users_table():
    """Get the unified_users DynamoDB table. Reads table name from SSM once."""
    global _table
    if _table:
        return _table
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name="/agentcore/unified_users_table_name")
    table_name = resp["Parameter"]["Value"]
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    _table = dynamodb.Table(table_name)
    logger.info("Unified users table: %s", table_name)
    return _table


@tool
def link_account(
    current_user_id: str,
    link_channel: str,
    link_identifier: str,
) -> Dict:
    """Link a WhatsApp phone number or Instagram username to the current user
    for cross-channel personalized experience.

    Args:
        current_user_id: The current user's canonical ID (from the [UserID:] tag in the prompt).
        link_channel: The OTHER channel to link. Must be "whatsapp" or "instagram".
        link_identifier: The identifier on the other channel:
            - For whatsapp: phone number with country code (e.g. "5491155001234")
            - For instagram: Instagram username without @ (e.g. "maria_dev")
    """
    try:
        table = _get_users_table()
    except Exception as e:
        logger.error("Cannot access users table: %s", str(e))
        return {"status": "error", "message": "Cross-channel linking is not available."}

    if link_channel not in ("whatsapp", "instagram"):
        return {"status": "error", "message": "Channel must be 'whatsapp' or 'instagram'."}

    # Get current user
    resp = table.get_item(Key={"user_id": current_user_id})
    current_user = resp.get("Item")
    if not current_user:
        return {"status": "error", "message": "Current user not found."}

    # Find the other user by their channel identifier
    if link_channel == "whatsapp":
        phone = link_identifier.replace("+", "").replace(" ", "").replace("-", "")
        other_resp = table.query(
            IndexName="wa-phone-index",
            KeyConditionExpression=Key("wa_phone").eq(phone),
            Limit=1,
        )
    else:
        # Search by ig_id if numeric, otherwise we can't link by username alone
        # (ig_username is not a GSI key, ig_id is)
        identifier = link_identifier.replace("@", "").strip()
        # Scan for ig_username match (small table, acceptable)
        other_resp = table.scan(
            FilterExpression="ig_username = :u",
            ExpressionAttributeValues={":u": identifier},
            Limit=1,
        )

    other_items = other_resp.get("Items", [])

    if other_items:
        other_user = other_items[0]
        if other_user["user_id"] == current_user_id:
            return {"status": "already_linked", "message": "Accounts are already linked."}

        # Merge: copy the other user's channel fields into the current user
        updates = {}
        if other_user.get("wa_phone") and not current_user.get("wa_phone"):
            updates["wa_phone"] = other_user["wa_phone"]
        if other_user.get("ig_id") and not current_user.get("ig_id"):
            updates["ig_id"] = other_user["ig_id"]
        if other_user.get("ig_username") and not current_user.get("ig_username"):
            updates["ig_username"] = other_user["ig_username"]

        if not updates:
            return {"status": "already_linked", "message": "Accounts are already linked."}

        # Update current user with the other channel's fields
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in updates)
        table.update_item(
            Key={"user_id": current_user_id},
            UpdateExpression=expr,
            ExpressionAttributeNames={f"#{k}": k for k in updates},
            ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
        )

        # Delete the other user (now merged)
        table.delete_item(Key={"user_id": other_user["user_id"]})

        logger.info("Linked %s -> %s (merged %s)", current_user_id, link_channel, other_user["user_id"])
        return {"status": "linked", "message": f"Accounts linked. Cross-channel memory is now active."}

    else:
        # No existing user for that channel — just add the field to current user
        if link_channel == "whatsapp":
            phone = link_identifier.replace("+", "").replace(" ", "").replace("-", "")
            table.update_item(
                Key={"user_id": current_user_id},
                UpdateExpression="SET wa_phone = :p",
                ExpressionAttributeValues={":p": phone},
            )
        else:
            identifier = link_identifier.replace("@", "").strip()
            table.update_item(
                Key={"user_id": current_user_id},
                UpdateExpression="SET ig_username = :u",
                ExpressionAttributeValues={":u": identifier},
            )

        logger.info("Added %s (%s) to user %s", link_channel, link_identifier, current_user_id)
        return {"status": "linked", "message": f"Account registered. When you write from {link_channel}, your memory will be shared."}
