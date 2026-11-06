"""CDK Stack for standalone AgentCore Runtime deployment.

Deploys the multimodal agent with Memory and RuntimeEndpoint,
and exports ARNs via SSM Parameter Store so that WhatsApp
integration stacks can consume them.

Optional settings (CDK context, `cdk deploy -c key=value`, or the env var in brackets):

* ``model_id`` [MODEL_ID]: Bedrock model or inference profile ID (default ``DEFAULT_MODEL_ID``).
* ``memory_expiry_days``: short-term event retention, 3 to 365 (default 30).
* ``enable_summary_memory`` / ``enable_episodic_memory``: ``false`` to drop a strategy.
* ``guardrail_id`` / ``guardrail_version``: apply an existing Bedrock Guardrail.
* ``unified_traces_destination``: ``true``/``false`` to force the span log group.
* ``media_retention_days``: expiry of media under images/, voice/, audio/, video/, documents/ (default 30).
* ``package_path``: prebuilt deployment zip (skips the uv build, used by tests).
"""

import os
from typing import Optional

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_bedrockagentcore as bedrockagentcore,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

from agentcore.agentcore_role import AgentCoreRole
from agentcore.agentcore_deployment import AgentCoreDeployment
from agentcore.agentcore_memory import AgentCoreMemory
from agentcore.config import (
    DEFAULT_MEMORY_EXPIRY_DAYS,
    DEFAULT_MODEL_ID,
    guardrail_arn,
)

MEDIA_PREFIXES = ("images/", "voice/", "audio/", "video/", "documents/")
TL_MODEL_NAME = os.environ.get("TL_MODEL_NAME", "pegasus1.2")


def _flag(value: Optional[object], default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no")


class AgentAgentCoreStack(Stack):
    """Standalone AgentCore Runtime + Memory stack."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        model_id = ctx("model_id") or os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID
        expiry_days = int(ctx("memory_expiry_days") or DEFAULT_MEMORY_EXPIRY_DAYS)
        if not 3 <= expiry_days <= 365:
            raise ValueError("memory_expiry_days must be between 3 and 365")
        guardrail_id = ctx("guardrail_id") or os.environ.get("BEDROCK_GUARDRAIL_ID") or ""
        guardrail_version = ctx("guardrail_version") or os.environ.get("BEDROCK_GUARDRAIL_VERSION") or "DRAFT"
        unified_traces = ctx("unified_traces_destination")
        media_retention_days = int(ctx("media_retention_days") or 30)
        if media_retention_days < 1:
            raise ValueError("media_retention_days must be at least 1")

        # --- Secrets Manager for TwelveLabs API key ---
        # The template carries only a generated placeholder. Store the real key with:
        #   aws secretsmanager put-secret-value --secret-id <TwelveLabsSecretArn output> \
        #     --secret-string '{"TL_API_KEY":"<your key>"}'
        tl_secret = secretsmanager.Secret(
            self,
            "TwelveLabsApiKey",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="TL_API_KEY",
                exclude_punctuation=True,
            ),
            description="TwelveLabs API key for video analysis. Replace the generated placeholder with your key.",
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
            # Media is ephemeral by design: expire it, and drop old versions quickly.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id=f"Expire{prefix.rstrip('/').capitalize()}",
                    prefix=prefix,
                    expiration=Duration.days(media_retention_days),
                    noncurrent_version_expiration=Duration.days(7),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
                for prefix in MEDIA_PREFIXES
            ],
        )

        # --- AgentCore Memory ---
        memory = AgentCoreMemory(
            self,
            "AgentCoreMemory",
            expiry_days=expiry_days,
            enable_summary=_flag(ctx("enable_summary_memory"), True),
            enable_episodic=_flag(ctx("enable_episodic_memory"), True),
            model_id=model_id,
        )

        # --- AgentCore Runtime ---
        agentcore_role = AgentCoreRole(
            self,
            "AgentCoreRole",
            memory_arn=memory.memory_arn,
            model_id=model_id,
            guardrail_arn=guardrail_arn(guardrail_id, self.region, self.account, self.partition),
        )
        bucket.grant_read_write(agentcore_role.role)
        tl_secret.grant_read(agentcore_role.role)

        environment_variables = {
            "AWS_REGION": self.region,
            "MODEL_ID": model_id,
            "TL_SECRET_ARN": tl_secret.secret_arn,
            "TL_MODEL_NAME": TL_MODEL_NAME,
        }
        if guardrail_id:
            environment_variables["BEDROCK_GUARDRAIL_ID"] = guardrail_id
            environment_variables["BEDROCK_GUARDRAIL_VERSION"] = guardrail_version
        if unified_traces is not None:
            environment_variables["UNIFIED_TRACES_DESTINATION_ENABLED"] = str(
                _flag(unified_traces, True)
            ).lower()

        agentcore = AgentCoreDeployment(
            self,
            "AgentCore",
            bucket=bucket,
            role=agentcore_role.role,
            memory_id=memory.memory_id,
            environment_variables=environment_variables,
            package_path=ctx("package_path"),
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

        # --- Export via SSM Parameter Store (consumed by stacks 01 and 02) ---
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
