"""SNS Topic for WhatsApp End User Messaging events."""

from constructs import Construct
from aws_cdk import (
    aws_sns as sns,
    aws_iam as iam,
)


class WhatsAppTopic(Construct):
    """SNS topic that receives WhatsApp webhook events from AWS Social Messaging."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "WhatsAppEventsTopic",
            display_name="WhatsApp Events",
        )

        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                principals=[iam.ServicePrincipal("social-messaging.amazonaws.com")],
                resources=[self.topic.topic_arn],
            )
        )
