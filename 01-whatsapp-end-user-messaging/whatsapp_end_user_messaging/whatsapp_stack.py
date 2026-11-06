"""CDK Stack for WhatsApp integration via AWS End User Messaging Social.

Reads the AgentCore Runtime ARN from SSM Parameter Store (written by
00-agent-agentcore). The parameters are resolved by CloudFormation at deploy
time, so `cdk synth` works without AWS credentials.
"""

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    aws_s3 as s3,
    aws_iam as iam,
    aws_ssm as ssm,
    RemovalPolicy,
)
from constructs import Construct

from databases.databases import MessageDatabase
from topic.topic import WhatsAppTopic
from lambdas.project_lambdas import ProjectLambdas
from whatsapp_end_user_messaging.config import (
    DEFAULT_MEDIA_RETENTION_DAYS,
    GRAPH_API_VERSION,
    is_true,
    optional_int,
)

MEDIA_PREFIXES = ("images/", "voice/", "video/", "documents/", "transcriptions/")


class WhatsAppEndUserMessagingStack(Stack):
    """WhatsApp via End User Messaging Social -> SNS -> Lambda -> AgentCore."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        retain_data = is_true(self.node.try_get_context("retain_data"))
        retention_days = optional_int(self.node.try_get_context("media_retention_days")) or DEFAULT_MEDIA_RETENTION_DAYS
        meta_api_version = self.node.try_get_context("meta_api_version") or GRAPH_API_VERSION
        reserved = optional_int(self.node.try_get_context("processor_reserved_concurrency"))
        source_arn = self.node.try_get_context("topic_source_arn_pattern") or None

        # --- AgentCore config from 00-agent-agentcore (resolved by CloudFormation at deploy time) ---
        agent_runtime_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/agentcore/agent_runtime_arn"
        )
        runtime_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/agentcore/runtime_role_arn"
        )

        # --- S3 Bucket for media ---
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

        # --- Grant AgentCore runtime role read access to media bucket ---
        agentcore_role = iam.Role.from_role_arn(self, "AgentCoreRole", runtime_role_arn)
        bucket.grant_read(agentcore_role)

        # --- DynamoDB ---
        db = MessageDatabase(self, "Database", retain_data=retain_data)

        # --- SNS Topic for WhatsApp events ---
        sns_topic = WhatsAppTopic(self, "Topic", source_arn_pattern=source_arn)

        # --- Lambda ---
        lambdas = ProjectLambdas(
            self,
            "Lambdas",
            topic=sns_topic.topic,
            table=db.table,
            bucket=bucket,
            agent_runtime_arn=agent_runtime_arn,
            meta_api_version=meta_api_version,
            processor_reserved_concurrency=reserved,
        )

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=agent_runtime_arn)
        CfnOutput(self, "MessagesTableName", value=db.table.table_name)
        CfnOutput(self, "WhatsAppTopicArn", value=sns_topic.topic.topic_arn)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "LambdaFunctionName", value=lambdas.whatsapp_handler.function_name)
        CfnOutput(self, "ProcessorFunctionName", value=lambdas.message_processor.function_name)
        CfnOutput(self, "HandlerDeadLetterQueueUrl", value=lambdas.handler_dlq.queue_url)
        CfnOutput(self, "ProcessorFailureQueueUrl", value=lambdas.stream_dlq.queue_url)
