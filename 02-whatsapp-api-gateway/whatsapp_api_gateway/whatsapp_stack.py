"""CDK Stack for WhatsApp integration via API Gateway + Meta Cloud API.

Reads AgentCore Runtime ARN from SSM Parameter Store (deployed by 00-agent-agentcore).
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_s3 as s3,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    SecretValue,
)
from constructs import Construct

from get_param import get_string_param
from databases.databases import MessageDatabase
from layers.project_layers import ProjectLayers
from lambdas.project_lambdas import ProjectLambdas
from apis.webhooks import WebhookApi

# Read AgentCore config from SSM (set by 00-agent-agentcore stack)
AGENT_RUNTIME_ARN = get_string_param("/agentcore/agent_runtime_arn")
RUNTIME_ROLE_ARN = get_string_param("/agentcore/runtime_role_arn")


class WhatsAppApiGatewayStack(Stack):
    """WhatsApp via Meta Cloud API -> API Gateway -> Lambda pipeline -> AgentCore."""

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
                "WHATS_PHONE_ID": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_PHONE_ID"
                ),
                "WHATS_TOKEN": SecretValue.unsafe_plain_text(
                    "CHANGE_ME_TOKEN"
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

        # --- Grant AgentCore runtime role read access to media bucket ---
        agentcore_role = iam.Role.from_role_arn(
            self, "AgentCoreRuntimeRole", RUNTIME_ROLE_ARN
        )
        bucket.grant_read(agentcore_role)

        # --- DynamoDB ---
        db = MessageDatabase(self, "Database")

        # --- Lambda Layers ---
        layers = ProjectLayers(self, "Layers")

        # --- Lambda Functions ---
        lambdas = ProjectLambdas(
            self,
            "Lambdas",
            table=db.table,
            bucket=bucket,
            common_layer=layers.common_layer,
            secret_arn=secrets.secret_arn,
            agent_runtime_arn=AGENT_RUNTIME_ARN,
        )

        # --- API Gateway ---
        api = WebhookApi(self, "API", whatsapp_in_fn=lambdas.webhook_receiver)

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=AGENT_RUNTIME_ARN)
        CfnOutput(self, "MessagesTableName", value=db.table.table_name)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "SecretArn", value=secrets.secret_arn)
        CfnOutput(self, "WebhookReceiverName", value=lambdas.webhook_receiver.function_name)
        CfnOutput(self, "ProcessorName", value=lambdas.message_processor.function_name)
