"""CDK Stack for standalone AgentCore Runtime deployment.

Deploys the multimodal agent with Memory and RuntimeEndpoint,
and exports ARNs via SSM Parameter Store so that WhatsApp
integration stacks can consume them.
"""

import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    SecretValue,
    aws_bedrockagentcore as bedrockagentcore,
    aws_iam as iam,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

from agentcore.agentcore_role import AgentCoreRole
from agentcore.agentcore_deployment import AgentCoreDeployment
from agentcore.agentcore_memory import AgentCoreMemory


REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")


class AgentAgentCoreStack(Stack):
    """Standalone AgentCore Runtime + Memory stack."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Secrets Manager for TwelveLabs API key ---
        tl_secret = secretsmanager.Secret(
            self,
            "TwelveLabsApiKey",
            secret_object_value={
                "TL_API_KEY": SecretValue.unsafe_plain_text("CHANGE_ME"),
            },
            description="TwelveLabs API key for video analysis",
        )

        # --- S3 Bucket for agent code and media ---
        bucket = s3.Bucket(
            self,
            "AgentBucket",
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        # --- AgentCore Memory ---
        memory = AgentCoreMemory(self, "AgentCoreMemory")

        # --- AgentCore Runtime ---
        agentcore_role = AgentCoreRole(self, "AgentCoreRole")
        bucket.grant_read_write(agentcore_role.role)
        tl_secret.grant_read(agentcore_role.role)

        agentcore = AgentCoreDeployment(
            self,
            "AgentCore",
            bucket=bucket,
            role=agentcore_role.role,
            memory_id=memory.memory_id,
            environment_variables={
                "AWS_REGION": REGION,
                "MODEL_ID": MODEL_ID,
                "TL_SECRET_ARN": tl_secret.secret_arn,
            },
        )

        # --- AgentCore Runtime Endpoint ---
        endpoint = bedrockagentcore.CfnRuntimeEndpoint(
            self,
            "AgentCoreEndpoint",
            agent_runtime_id=agentcore.agent_runtime_id,
            name="WhatsAppAgentEndpoint",
            description="Endpoint to invoke the WhatsApp multimodal agent",
        )
        endpoint.node.add_dependency(agentcore.runtime)

        # --- Export via SSM Parameter Store ---
        ssm.StringParameter(
            self,
            "AgentRuntimeArnParam",
            parameter_name="/agentcore/agent_runtime_arn",
            string_value=agentcore.agent_runtime_arn,
        )

        ssm.StringParameter(
            self,
            "S3BucketParam",
            parameter_name="/agentcore/s3_bucket_name",
            string_value=bucket.bucket_name,
        )

        ssm.StringParameter(
            self,
            "MemoryIdParam",
            parameter_name="/agentcore/memory_id",
            string_value=memory.memory_id,
        )

        ssm.StringParameter(
            self,
            "RuntimeRoleArnParam",
            parameter_name="/agentcore/runtime_role_arn",
            string_value=agentcore_role.role.role_arn,
        )

        # --- Outputs ---
        CfnOutput(self, "AgentRuntimeArn", value=agentcore.agent_runtime_arn)
        CfnOutput(self, "AgentRuntimeId", value=agentcore.agent_runtime_id)
        CfnOutput(self, "EndpointArn", value=endpoint.attr_agent_runtime_endpoint_arn)
        CfnOutput(self, "MemoryId", value=memory.memory_id)
        CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        CfnOutput(self, "TwelveLabsSecretArn", value=tl_secret.secret_arn)
