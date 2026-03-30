"""CDK Stack for WhatsApp + Instagram integration via API Gateway + Meta Cloud API.

Reads AgentCore Runtime ARN from SSM Parameter Store (deployed by 00-agent-agentcore).
Supports dual-channel: WhatsApp Business and Instagram Direct Messages.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_s3 as s3,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
    SecretValue,
)
from constructs import Construct

from get_param import get_string_param
from databases.databases import MessageDatabase, UserIdentityDatabase
from layers.project_layers import ProjectLayers
from lambdas.project_lambdas import ProjectLambdas
from apis.webhooks import WebhookApi

# Read AgentCore config from SSM (set by 00-agent-agentcore stack)
AGENT_RUNTIME_ARN = get_string_param("/agentcore/agent_runtime_arn")
RUNTIME_ROLE_ARN = get_string_param("/agentcore/runtime_role_arn")


class WhatsAppApiGatewayStack(Stack):
    """WhatsApp + Instagram via Meta Cloud API -> API Gateway -> Lambda pipeline -> AgentCore."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Secrets Manager for WhatsApp credentials ---
        secrets = secretsmanager.Secret(
            self,
            "WhatsAppSecrets",
            secret_object_value={
                "WHATS_VERIFICATION_TOKEN": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_VERIFICATION_TOKEN"
                ),
                "WHATS_TOKEN": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_TOKEN"
                ),
                "DISPLAY_PHONE_NUMBER": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_PHONE_NUMBER"
                ),
            },
        )

        # --- Secrets Manager for Instagram credentials ---
        ig_secrets = secretsmanager.Secret(
            self,
            "InstagramSecrets",
            secret_object_value={
                "IG_TOKEN": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_IG_TOKEN"
                ),
                "IG_ACCOUNT_ID": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_IG_ACCOUNT_ID"
                ),
                "IG_VERIFICATION_TOKEN": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_IG_VERIFICATION_TOKEN"
                ),
            },
        )

        # --- S3 Bucket for media and transcriptions ---
        bucket = s3.Bucket(
            self,
            "MediaBucket",
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        # --- DynamoDB ---
        db = MessageDatabase(self, "Database")
        users_db = UserIdentityDatabase(self, "UsersDatabase")

        # --- Grant AgentCore runtime role access to media bucket + users table ---
        agentcore_role = iam.Role.from_role_arn(
            self, "AgentCoreRuntimeRole", RUNTIME_ROLE_ARN
        )
        bucket.grant_read(agentcore_role)
        users_db.table.grant_read_write_data(agentcore_role)

        # --- Lambda Layers ---
        layers = ProjectLayers(self, "Layers")

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
            agent_runtime_arn=AGENT_RUNTIME_ARN,
        )

        # --- API Gateway ---
        api = WebhookApi(self, "API", whatsapp_in_fn=lambdas.webhook_receiver)

        # --- Export unified_users table name to SSM for AgentCore tool ---
        ssm.StringParameter(
            self,
            "UnifiedUsersTableParam",
            parameter_name="/agentcore/unified_users_table_name",
            string_value=users_db.table.table_name,
        )

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=AGENT_RUNTIME_ARN)
        CfnOutput(self, "MessagesTableName", value=db.table.table_name)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "UnifiedUsersTableName", value=users_db.table.table_name)
        CfnOutput(self, "SecretArn", value=secrets.secret_arn)
        CfnOutput(self, "IGSecretArn", value=ig_secrets.secret_arn)
        CfnOutput(self, "WebhookReceiverName", value=lambdas.webhook_receiver.function_name)
        CfnOutput(self, "ProcessorName", value=lambdas.message_processor.function_name)
