"""Lambda constructs: receiver (SNS) + processor (DDB Stream with tumbling window).

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

from constructs import Construct
from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_lambda_event_sources as event_sources,
    aws_sns_subscriptions as sns_subs,
    aws_sns as sns,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_iam as iam,
)


class ProjectLambdas(Construct):
    """Receiver Lambda (SNS) + Processor Lambda (DDB Stream tumbling window)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        topic: sns.Topic,
        table: ddb.Table,
        bucket: s3.Bucket,
        agent_runtime_arn: str,
        buffer_seconds: int = 20,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Receiver Lambda (SNS -> save to DDB) ---
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
                    "social-messaging:SendWhatsAppMessage",
                    "social-messaging:GetWhatsAppMessageMedia",
                ],
                resources=["*"],
            )
        )

        # --- Processor Lambda (DDB Stream with tumbling window -> AgentCore) ---
        self.message_processor = _lambda.Function(
            self,
            "MessageProcessor",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/message_processor"),
            timeout=Duration.minutes(5),
            memory_size=512,
            environment={
                "TABLE_NAME": table.table_name,
                "AGENT_ARN": agent_runtime_arn,
                "S3_BUCKET": bucket.bucket_name,
            },
        )

        buffer_duration = Duration.seconds(buffer_seconds)
        self.message_processor.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=_lambda.StartingPosition.TRIM_HORIZON,
                tumbling_window=buffer_duration,
                batch_size=1000,
                max_batching_window=buffer_duration,
            )
        )

        table.grant_read_data(self.message_processor)
        bucket.grant_read(self.message_processor)

        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=["*"],
            )
        )

        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "social-messaging:SendWhatsAppMessage",
                ],
                resources=["*"],
            )
        )

        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                ],
                resources=["*"],
            )
        )
