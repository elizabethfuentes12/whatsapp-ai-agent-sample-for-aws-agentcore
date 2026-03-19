"""DynamoDB table for WhatsApp message storage."""

from constructs import Construct
from aws_cdk import (
    RemovalPolicy,
    aws_dynamodb as ddb,
)


class MessageDatabase(Construct):
    """DynamoDB table to store WhatsApp messages."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = ddb.Table(
            self,
            "MessagesTable",
            partition_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
