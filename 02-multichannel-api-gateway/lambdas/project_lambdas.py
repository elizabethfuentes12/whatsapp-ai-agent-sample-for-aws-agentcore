"""Lambda constructs: webhook receiver (API GW) + processor (DDB Stream with tumbling window).

Buffering pattern based on:
https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat
"""

from constructs import Construct
from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_lambda_event_sources as event_sources,
    aws_iam as iam,
    aws_dynamodb as ddb,
    aws_s3 as s3,
)


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
        buffer_seconds: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        default_env = {
            "TABLE_NAME": table.table_name,
            "USERS_TABLE_NAME": users_table.table_name,
            "S3_BUCKET": bucket.bucket_name,
        }

        # --- Webhook receiver: API GW -> save to DDB ---
        self.webhook_receiver = _lambda.Function(
            self,
            "WebhookReceiver",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/webhook_receiver"),
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[common_layer],
            environment={
                **default_env,
                "SECRET_ARN": secret_arn,
                "IG_SECRET_ARN": ig_secret_arn,
            },
        )

        table.grant_read_write_data(self.webhook_receiver)
        users_table.grant_read_write_data(self.webhook_receiver)
        bucket.grant_read_write(self.webhook_receiver)

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
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/message_processor"),
            timeout=Duration.minutes(5),
            memory_size=512,
            layers=[common_layer],
            environment={
                **default_env,
                "AGENT_ARN": agent_runtime_arn,
                "IG_SECRET_ARN": ig_secret_arn,
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
        users_table.grant_read_write_data(self.message_processor)
        bucket.grant_read_write(self.message_processor)

        self.message_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[ig_secret_arn],
            )
        )

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
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                ],
                resources=["*"],
            )
        )
