"""DynamoDB table for WhatsApp messages with stream and GSI."""

from constructs import Construct
from aws_cdk import (
    RemovalPolicy,
    aws_dynamodb as ddb,
)


class MessageDatabase(Construct):
    """DynamoDB table for message storage with stream for processing pipeline."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = ddb.Table(
            self,
            "MessagesTable",
            partition_key=ddb.Attribute(name="messages_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
        )

        self.table.add_global_secondary_index(
            index_name="jobnameindex",
            partition_key=ddb.Attribute(name="jobName", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )
