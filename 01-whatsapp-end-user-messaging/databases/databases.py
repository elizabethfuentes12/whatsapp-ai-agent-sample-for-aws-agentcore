"""DynamoDB table for WhatsApp message buffering with stream and TTL.

Uses from_phone as partition key so messages from the same user land in the
same shard and are processed together by the tumbling window.

The item doubles as the idempotency record: the receiver writes it with a
conditional put (a repeated message id is dropped) and the processor marks it
`done` once the reply was sent.

Based on: https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

from constructs import Construct
from aws_cdk import (
    RemovalPolicy,
    aws_dynamodb as ddb,
)


class MessageDatabase(Construct):
    """DynamoDB table with stream for tumbling window message aggregation."""

    def __init__(self, scope: Construct, construct_id: str, retain_data: bool = False, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = ddb.Table(
            self,
            "MessagesTable",
            partition_key=ddb.Attribute(name="from_phone", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY,
            deletion_protection=retain_data,
            stream=ddb.StreamViewType.NEW_IMAGE,
            time_to_live_attribute="ttl",
            encryption=ddb.TableEncryption.AWS_MANAGED,
            # The table only buffers messages for a few hours, so point-in-time
            # recovery is turned on only together with `retain_data`.
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=retain_data
            ),
        )
