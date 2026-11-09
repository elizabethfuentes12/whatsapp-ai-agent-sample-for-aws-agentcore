"""SNS Topic for WhatsApp End User Messaging events."""

from typing import Optional

from constructs import Construct
from aws_cdk import (
    Stack,
    aws_sns as sns,
    aws_iam as iam,
)


class WhatsAppTopic(Construct):
    """SNS topic that receives WhatsApp events from AWS End User Messaging Social.

    The only principal allowed to publish is the End User Messaging Social service
    acting for this account (confused deputy protection through `aws:SourceAccount`).
    Pass `source_arn_pattern` (for example the ARN of your linked WhatsApp business
    account) to narrow it further once you have verified the service sends it.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        source_arn_pattern: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "WhatsAppEventsTopic",
            display_name="WhatsApp Events",
            enforce_ssl=True,
        )

        conditions = {"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
        if source_arn_pattern:
            conditions["ArnLike"] = {"aws:SourceArn": source_arn_pattern}

        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowEndUserMessagingSocialPublish",
                actions=["sns:Publish"],
                principals=[iam.ServicePrincipal("social-messaging.amazonaws.com")],
                resources=[self.topic.topic_arn],
                conditions=conditions,
            )
        )
