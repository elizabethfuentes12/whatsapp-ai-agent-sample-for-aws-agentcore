"""CDK Stack for WhatsApp + Instagram integration via API Gateway + Meta Cloud API.

Reads the AgentCore Runtime ARN from SSM Parameter Store (deployed by 00-agent-agentcore).
The parameters are resolved by CloudFormation at deploy time, so `cdk synth` works without
AWS credentials. Supports dual-channel: WhatsApp Business and Instagram Direct Messages.
"""

import json

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_s3 as s3,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

from databases.databases import MessageDatabase, UserIdentityDatabase
from layers.project_layers import ProjectLayers
from lambdas.project_lambdas import ProjectLambdas
from apis.webhooks import WebhookApi
from whatsapp_api_gateway.config import (
    DEFAULT_MEDIA_RETENTION_DAYS,
    DEFAULT_THROTTLE_BURST,
    DEFAULT_THROTTLE_RATE,
    GRAPH_API_VERSION,
    is_true,
    optional_int,
)

MEDIA_PREFIXES = (
    "images/", "audio/", "video/", "documents/",
    "ig-images/", "ig-audios/", "ig-videos/", "ig-documents/",
    "transcriptions/",
)


class WhatsAppApiGatewayStack(Stack):
    """WhatsApp + Instagram via Meta Cloud API -> API Gateway -> Lambda pipeline -> AgentCore."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        retain_data = is_true(ctx("retain_data"))
        retention_days = optional_int(ctx("media_retention_days")) or DEFAULT_MEDIA_RETENTION_DAYS
        graph_api_version = ctx("graph_api_version") or GRAPH_API_VERSION
        enable_waf = is_true(ctx("enable_waf"))

        # --- AgentCore config from 00-agent-agentcore (resolved by CloudFormation at deploy time) ---
        agent_runtime_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/agentcore/agent_runtime_arn"
        )
        runtime_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/agentcore/runtime_role_arn"
        )

        # --- Secrets Manager: Meta credentials ---
        # The *_APP_SECRET keys are generated randomly, so an unconfigured deployment can
        # never verify a signature. Replace them with the real app secrets from the Meta
        # App Dashboard (App settings > Basic > App secret).
        secrets = secretsmanager.Secret(
            self,
            "WhatsAppSecrets",
            description="WhatsApp Cloud API credentials. Replace the CHANGE_ME values.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({
                    "WHATS_VERIFICATION_TOKEN": "CHANGE_ME_VERIFICATION_TOKEN",
                    "WHATS_TOKEN": "CHANGE_ME_TOKEN",
                    "DISPLAY_PHONE_NUMBER": "CHANGE_ME_PHONE_NUMBER",
                }),
                generate_string_key="WHATS_APP_SECRET",
                exclude_punctuation=True,
                password_length=40,
            ),
        )
        ig_secrets = secretsmanager.Secret(
            self,
            "InstagramSecrets",
            description="Instagram messaging credentials. Replace the CHANGE_ME values.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({
                    "IG_TOKEN": "CHANGE_ME_IG_TOKEN",
                    "IG_ACCOUNT_ID": "CHANGE_ME_IG_ACCOUNT_ID",
                    "IG_VERIFICATION_TOKEN": "CHANGE_ME_IG_VERIFICATION_TOKEN",
                }),
                generate_string_key="IG_APP_SECRET",
                exclude_punctuation=True,
                password_length=40,
            ),
        )

        # --- S3 Bucket for media and transcriptions ---
        bucket = s3.Bucket(
            self,
            "MediaBucket",
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY,
            auto_delete_objects=not retain_data,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id=f"expire-{prefix.strip('/')}",
                    prefix=prefix,
                    expiration=Duration.days(retention_days),
                    noncurrent_version_expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
                for prefix in MEDIA_PREFIXES
            ],
        )

        # --- DynamoDB ---
        db = MessageDatabase(self, "Database", retain_data=retain_data)
        users_db = UserIdentityDatabase(self, "UsersDatabase", retain_data=retain_data)

        # --- Grant AgentCore runtime role access to media bucket + users table ---
        agentcore_role = iam.Role.from_role_arn(self, "AgentCoreRuntimeRole", runtime_role_arn)
        bucket.grant_read(agentcore_role)
        users_db.table.grant_read_write_data(agentcore_role)

        # --- Lambda Layers ---
        layers = ProjectLayers(self, "Layers", layer_path=ctx("layer_path"))

        # --- Lambda Functions ---
        lambdas = ProjectLambdas(
            self,
            "Lambdas",
            table=db.table,
            users_table=users_db.table,
            bucket=bucket,
            common_layer=layers.common_layer,
            secret_arn=secrets.secret_arn,
            ig_secret_arn=ig_secrets.secret_arn,
            agent_runtime_arn=agent_runtime_arn,
            graph_api_version=graph_api_version,
            receiver_reserved_concurrency=optional_int(ctx("receiver_reserved_concurrency")),
            processor_reserved_concurrency=optional_int(ctx("processor_reserved_concurrency")),
        )

        # --- API Gateway ---
        WebhookApi(
            self,
            "API",
            whatsapp_in_fn=lambdas.webhook_receiver,
            throttle_rate=optional_int(ctx("throttle_rate_limit")) or DEFAULT_THROTTLE_RATE,
            throttle_burst=optional_int(ctx("throttle_burst_limit")) or DEFAULT_THROTTLE_BURST,
            enable_waf=enable_waf,
        )

        # --- Export unified_users table name to SSM for AgentCore tool ---
        ssm.StringParameter(
            self,
            "UnifiedUsersTableParam",
            parameter_name="/agentcore/unified_users_table_name",
            string_value=users_db.table.table_name,
        )

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=agent_runtime_arn)
        CfnOutput(self, "MessagesTableName", value=db.table.table_name)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "UnifiedUsersTableName", value=users_db.table.table_name)
        CfnOutput(self, "SecretArn", value=secrets.secret_arn)
        CfnOutput(self, "IGSecretArn", value=ig_secrets.secret_arn)
        CfnOutput(self, "WebhookReceiverName", value=lambdas.webhook_receiver.function_name)
        CfnOutput(self, "ProcessorName", value=lambdas.message_processor.function_name)
        CfnOutput(self, "ProcessorFailureQueueUrl", value=lambdas.stream_dlq.queue_url)
