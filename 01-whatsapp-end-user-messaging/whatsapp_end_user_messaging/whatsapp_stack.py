"""CDK Stack for WhatsApp integration via AWS End User Messaging Social.

Reads AgentCore Runtime ARN from SSM Parameter Store (deployed by 00-agent-agentcore).
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    aws_s3 as s3,
    aws_iam as iam,
    RemovalPolicy,
)
from constructs import Construct

from get_param import get_string_param
from databases.databases import MessageDatabase
from topic.topic import WhatsAppTopic
from lambdas.project_lambdas import ProjectLambdas

# Read AgentCore config from SSM (set by 00-agent-agentcore stack)
AGENT_RUNTIME_ARN = get_string_param("/agentcore/agent_runtime_arn")
RUNTIME_ROLE_ARN = get_string_param("/agentcore/runtime_role_arn")


class WhatsAppEndUserMessagingStack(Stack):
    """WhatsApp via End User Messaging Social -> SNS -> Lambda -> AgentCore."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- S3 Bucket for media ---
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
            self, "AgentCoreRole", RUNTIME_ROLE_ARN
        )
        bucket.grant_read(agentcore_role)

        # --- DynamoDB ---
        db = MessageDatabase(self, "Database")

        # --- SNS Topic for WhatsApp events ---
        sns_topic = WhatsAppTopic(self, "Topic")

        # --- Lambda ---
        lambdas = ProjectLambdas(
            self,
            "Lambdas",
            topic=sns_topic.topic,
            table=db.table,
            bucket=bucket,
            agent_runtime_arn=AGENT_RUNTIME_ARN,
        )

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=AGENT_RUNTIME_ARN)
        CfnOutput(self, "MessagesTableName", value=db.table.table_name)
        CfnOutput(self, "WhatsAppTopicArn", value=sns_topic.topic.topic_arn)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "LambdaFunctionName", value=lambdas.whatsapp_handler.function_name)
