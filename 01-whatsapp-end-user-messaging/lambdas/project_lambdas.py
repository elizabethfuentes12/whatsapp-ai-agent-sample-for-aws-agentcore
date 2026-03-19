"""Lambda function construct for WhatsApp handler."""

from constructs import Construct
from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_sns_subscriptions as sns_subs,
    aws_sns as sns,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_iam as iam,
)


class ProjectLambdas(Construct):
    """Lambda function that processes WhatsApp messages from SNS."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        topic: sns.Topic,
        table: ddb.Table,
        bucket: s3.Bucket,
        agent_runtime_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.whatsapp_handler = _lambda.Function(
            self,
            "WhatsAppHandler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/whatsapp_handler"),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "TABLE_NAME": table.table_name,
                "AGENT_ARN": agent_runtime_arn,
                "S3_BUCKET": bucket.bucket_name,
                "ATTACHMENT_PREFIX": "images/",
                "VOICE_PREFIX": "voice/",
                "VIDEO_PREFIX": "video/",
                "DOCUMENT_PREFIX": "documents/",
            },
        )

        topic.add_subscription(sns_subs.LambdaSubscription(self.whatsapp_handler))

        table.grant_read_write_data(self.whatsapp_handler)
        bucket.grant_read_write(self.whatsapp_handler)

        self.whatsapp_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=["*"],
            )
        )

        self.whatsapp_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "social-messaging:SendWhatsAppMessage",
                    "social-messaging:GetWhatsAppMessageMedia",
                ],
                resources=["*"],
            )
        )

        self.whatsapp_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                ],
                resources=["*"],
            )
        )
