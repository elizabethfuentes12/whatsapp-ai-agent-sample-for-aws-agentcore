"""DynamoDB table for WhatsApp message buffering with stream and TTL.

Uses from_phone as partition key so messages from the same user land in the
same shard and are processed together by the tumbling window.

Based on: https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

from constructs import Construct
from aws_cdk import (
    RemovalPolicy,
    aws_dynamodb as ddb,
)


class MessageDatabase(Construct):
    """DynamoDB table with stream for tumbling window message aggregation."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = ddb.Table(
            self,
            "MessagesTable",
            partition_key=ddb.Attribute(name="from_phone", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            stream=ddb.StreamViewType.NEW_IMAGE,
            time_to_live_attribute="ttl",
        )
