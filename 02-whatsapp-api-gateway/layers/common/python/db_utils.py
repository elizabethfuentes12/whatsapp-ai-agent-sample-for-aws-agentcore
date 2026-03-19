"""DynamoDB utility functions."""

import json
import logging
from decimal import Decimal

import boto3

logger = logging.getLogger(__name__)

dynamodb_resource = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def put_item(table_name: str, item: dict):
    """Put an item into DynamoDB."""
    table = dynamodb_resource.Table(table_name)
    table.put_item(Item=item)


def get_item(table_name: str, key: dict) -> dict:
    """Get an item from DynamoDB."""
    table = dynamodb_resource.Table(table_name)
    response = table.get_item(Key=key)
    return response.get("Item", {})


def query_by_index(table_name: str, index_name: str, key_name: str, key_value: str) -> dict:
    """Query DynamoDB using a GSI."""
    table = dynamodb_resource.Table(table_name)
    from boto3.dynamodb.conditions import Key

    response = table.query(
        IndexName=index_name,
        KeyConditionExpression=Key(key_name).eq(key_value),
    )
    items = response.get("Items", [])
    return items[0] if items else {}
