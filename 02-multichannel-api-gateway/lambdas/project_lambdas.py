"""Lambda constructs: webhook receiver (API GW) + processor (DDB Stream with tumbling window).

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
    aws_iam as iam,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_logs as logs,
)

from whatsapp_api_gateway.config import MAX_INLINE_MEDIA_BYTES, MESSAGE_TTL_SECONDS

RUNTIME = _lambda.Runtime.PYTHON_3_13
ARCHITECTURE = _lambda.Architecture.ARM_64


class ProjectLambdas(Construct):
    """Webhook receiver (API GW) + Message processor (DDB Stream tumbling window)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        table: ddb.Table,
        users_table: ddb.Table,
        bucket: s3.Bucket,
        common_layer: _lambda.LayerVersion,
        secret_arn: str,
        ig_secret_arn: str,
        agent_runtime_arn: str,
        graph_api_version: str,
        buffer_seconds: int = 10,
        receiver_reserved_concurrency: Optional[int] = None,
        processor_reserved_concurrency: Optional[int] = None,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stack = Stack.of(self)

        default_env = {
            "TABLE_NAME": table.table_name,
            "USERS_TABLE_NAME": users_table.table_name,
            "S3_BUCKET": bucket.bucket_name,
            "GRAPH_API_VERSION": graph_api_version,
            "MESSAGE_TTL_SECONDS": str(MESSAGE_TTL_SECONDS),
        }

        self.stream_dlq = sqs.Queue(
            self,
            "ProcessorFailureQueue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )

        # --- Webhook receiver: API GW -> save to DDB ---
        # Must answer within the API Gateway integration timeout (29 s).
        self.webhook_receiver = _lambda.Function(
            self,
            "WebhookReceiver",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/webhook_receiver"),
            timeout=Duration.seconds(28),
            memory_size=512,
            layers=[common_layer],
            tracing=_lambda.Tracing.ACTIVE,
            reserved_concurrent_executions=receiver_reserved_concurrency,
            log_group=logs.LogGroup(
                self,
                "WebhookReceiverLogs",
                retention=log_retention,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **default_env,
                "SECRET_ARN": secret_arn,
                "IG_SECRET_ARN": ig_secret_arn,
            },
        )

        # The receiver only writes new items (conditional put) and stores media.
        self.webhook_receiver.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[table.table_arn])
        )
        bucket.grant_put(self.webhook_receiver)
        self.webhook_receiver.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret_arn, ig_secret_arn],
            )
        )

        # --- Message processor: DDB Stream with tumbling window -> AgentCore ---
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
            layers=[common_layer],
            tracing=_lambda.Tracing.ACTIVE,
            reserved_concurrent_executions=processor_reserved_concurrency,
            log_group=logs.LogGroup(
                self,
                "MessageProcessorLogs",
                retention=log_retention,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **default_env,
                "AGENT_ARN": agent_runtime_arn,
                "SECRET_ARN": secret_arn,
                "IG_SECRET_ARN": ig_secret_arn,
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

        # Claims and completion markers are single-item updates.
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:GetItem"], resources=[table.table_arn]
            )
        )
        # Unified identity: lookups through the GSIs, creates and updates of user items.
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[users_table.table_arn, f"{users_table.table_arn}/index/*"],
            )
        )
        bucket.grant_read(self.message_processor)
        bucket.grant_put(self.message_processor, "transcriptions/*")

        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret_arn, ig_secret_arn],
            )
        )
        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[agent_runtime_arn, f"{agent_runtime_arn}/runtime-endpoint/*"],
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
