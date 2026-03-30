"""DynamoDB tables for message buffering and unified user identity.

Messages table: from_phone as PK so messages from the same user land in the
same shard and are processed together by the tumbling window.

Users table: canonical user_id as PK with GSIs on wa_phone and ig_id for
cross-channel identity resolution. Enables shared AgentCore Memory across
WhatsApp and Instagram for the same person.

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
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


class UserIdentityDatabase(Construct):
    """Unified user identity table for cross-channel memory.

    Maps WhatsApp phone numbers and Instagram IDs to a single canonical
    user_id used as actor_id in AgentCore Memory. When the same person
    messages from both WhatsApp and Instagram, their conversations share
    the same long-term memory.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = ddb.Table(
            self,
            "UnifiedUsersTable",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI to look up by WhatsApp phone number
        self.table.add_global_secondary_index(
            index_name="wa-phone-index",
            partition_key=ddb.Attribute(name="wa_phone", type=ddb.AttributeType.STRING),
        )

        # GSI to look up by Instagram scoped ID
        self.table.add_global_secondary_index(
            index_name="ig-id-index",
            partition_key=ddb.Attribute(name="ig_id", type=ddb.AttributeType.STRING),
        )
