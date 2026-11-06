"""Lambda constructs: receiver (SNS) + processor (DDB Stream with tumbling window).

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

from typing import Optional

from constructs import Construct
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_lambda as _lambda,
    aws_lambda_event_sources as event_sources,
    aws_sns_subscriptions as sns_subs,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_iam as iam,
    aws_logs as logs,
)

from whatsapp_end_user_messaging.config import MAX_INLINE_MEDIA_BYTES, MESSAGE_TTL_SECONDS

RUNTIME = _lambda.Runtime.PYTHON_3_13
ARCHITECTURE = _lambda.Architecture.ARM_64


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
        meta_api_version: str,
        buffer_seconds: int = 20,
        processor_reserved_concurrency: Optional[int] = None,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stack = Stack.of(self)

        phone_number_arn = stack.format_arn(
            service="social-messaging", resource="phone-number-id", resource_name="*"
        )

        # --- Queues that keep failed work instead of dropping it ---
        self.handler_dlq = sqs.Queue(
            self,
            "HandlerDeadLetterQueue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.stream_dlq = sqs.Queue(
            self,
            "ProcessorFailureQueue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )

        # --- Receiver Lambda (SNS -> save to DDB) ---
        self.whatsapp_handler = _lambda.Function(
            self,
            "WhatsAppHandler",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/whatsapp_handler"),
            timeout=Duration.minutes(2),
            memory_size=512,
            tracing=_lambda.Tracing.ACTIVE,
            dead_letter_queue=self.handler_dlq,
            retry_attempts=2,
            max_event_age=Duration.hours(1),
            log_group=logs.LogGroup(
                self,
                "WhatsAppHandlerLogs",
                retention=log_retention,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "TABLE_NAME": table.table_name,
                "S3_BUCKET": bucket.bucket_name,
                "ATTACHMENT_PREFIX": "images/",
                "VOICE_PREFIX": "voice/",
                "VIDEO_PREFIX": "video/",
                "DOCUMENT_PREFIX": "documents/",
                "META_API_VERSION": meta_api_version,
                "EXPECTED_TOPIC_ARN": topic.topic_arn,
                "MESSAGE_TTL_SECONDS": str(MESSAGE_TTL_SECONDS),
            },
        )

        topic.add_subscription(
            sns_subs.LambdaSubscription(self.whatsapp_handler, dead_letter_queue=self.handler_dlq)
        )

        # The receiver only writes new items (conditional put) and stores media.
        self.whatsapp_handler.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[table.table_arn])
        )
        bucket.grant_put(self.whatsapp_handler)
        self.whatsapp_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "social-messaging:SendWhatsAppMessage",
                    "social-messaging:GetWhatsAppMessageMedia",
                ],
                resources=[phone_number_arn],
            )
        )

        # --- Processor Lambda (DDB Stream with tumbling window -> AgentCore) ---
        self.message_processor = _lambda.Function(
            self,
            "MessageProcessor",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/message_processor"),
            # Budget: transcription (<= 2 min) + AgentCore call (<= 4 min, 2 attempts guarded by the deadline).
            timeout=Duration.minutes(10),
            memory_size=512,
            tracing=_lambda.Tracing.ACTIVE,
            reserved_concurrent_executions=processor_reserved_concurrency,
            log_group=logs.LogGroup(
                self,
                "MessageProcessorLogs",
                retention=log_retention,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "TABLE_NAME": table.table_name,
                "AGENT_ARN": agent_runtime_arn,
                "S3_BUCKET": bucket.bucket_name,
                "META_API_VERSION": meta_api_version,
                "MAX_INLINE_MEDIA_BYTES": str(MAX_INLINE_MEDIA_BYTES),
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
                # A poison record must not block the shard for the whole 24 h stream retention.
                retry_attempts=3,
                max_record_age=Duration.hours(1),
                bisect_batch_on_error=True,
                report_batch_item_failures=True,
                on_failure=event_sources.SqsDlq(self.stream_dlq),
            )
        )

        # Claims and completion markers are single-item updates; reads are keyed lookups.
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:GetItem"],
                resources=[table.table_arn],
            )
        )
        bucket.grant_read(self.message_processor)
        bucket.grant_put(self.message_processor, "transcriptions/*")

        runtime_endpoints_arn = f"{agent_runtime_arn}/runtime-endpoint/*"
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[agent_runtime_arn, runtime_endpoints_arn],
            )
        )
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["social-messaging:SendWhatsAppMessage"],
                resources=[phone_number_arn],
            )
        )
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"],
                resources=[
                    stack.format_arn(
                        service="transcribe",
                        resource="transcription-job",
                        resource_name="whatsapp-audio-*",
                    )
                ],
            )
        )
